# Powertools の `@idempotent` を使わずに書いた冪等性

worker の冪等性には AWS Lambda Powertools の `@idempotent_function` を使っている。
これは、Powertools が中でやっていることを理解するために、同じことを boto3 で直接書いた版。
**実際の worker では使っていない**（2026-10-05、ステップ6で比較してから Powertools を採用）。

## 何を防ぐか

SQS は同じ通知を2回渡すことがある（at-least-once）。
Aurora の UPDATE は `status = 'analyzing'` を条件にしているので DB の中身は壊れないが、
UPDATE より前の作業（S3 の読み込み・Bedrock の呼び出し）は2回走る。
処理を始める**前に**「この通知はもう処理したか」を確かめて、2回目は処理しない。

## 仕組み

DynamoDB の1行 ＝ 写真1枚。行の状態は2つだけ。

| status | 意味 |
|---|---|
| `IN_PROGRESS` | いま誰かが処理している。`in_progress_until` を過ぎたら、その実行はもう死んでいる |
| `DONE` | 処理が終わった（成功、または恒久的な失敗を DB に書いた） |

関数は3つ。

| 関数 | いつ | DynamoDB への操作 |
|---|---|---|
| `claim` | 処理を始める前 | 条件付き `PutItem`（行が無いか、処理中のまま期限切れのときだけ書く） |
| `mark_done` | 成功、または恒久的な失敗を DB に書いた後 | `PutItem`（`DONE` で上書き） |
| `release` | 一時的な失敗のとき | `DeleteItem`（SQS が渡し直したときに `claim` できるように） |

## コード

```python
# 冪等性の記録テーブル。
# resource は client より書きやすい作りのもの。client だと値を {"S": "文字列"} のように型付きで書く必要があるが、
# resource なら Python の str / int をそのまま渡せる。
idempotency_table = boto3.resource("dynamodb").Table(os.environ["IDEMPOTENCY_TABLE"])

# 「済み」の記録を何秒残すか。過ぎたら DynamoDB の TTL が消す。
# SQS の重複配信はたいてい数分以内、やり直しも 6分 × 3回 で終わるので、1時間あれば足りる。
# 1時間を過ぎて重複が来ても、Aurora の UPDATE（status = 'analyzing' の条件）が上書きを止める。
DONE_TTL_SECONDS = 3600


def claim(key: str, context) -> bool:
    """この写真の処理を始めてよいか。DynamoDB に「処理中」を書けたら True。

    書けるのは次のどれかのときだけ:
      ・行が無い（初めての通知）
      ・行はあるが expiration を過ぎている（TTL の削除は数日遅れることがあるので、残っている場合がある）
      ・「処理中」のまま期限が過ぎている（前の実行がタイムアウトで死んだ）
    書けなかったら「済み」か「別の実行が処理中」なので False を返す（呼び出し元は処理しない）。

    「行が無ければ書く」を1回の命令でやるのが大事。
    「読む → 無ければ書く」の2回に分けると、同時に来た2つの実行が両方「無い」と読んで、両方書いてしまう。
    """
    now = int(time.time())
    # この Lambda があと何秒で強制終了されるか。それを過ぎても IN_PROGRESS なら、その実行はもう死んでいる
    remaining_seconds = context.get_remaining_time_in_millis() // 1000

    try:
        idempotency_table.put_item(
            Item={
                "id": key,
                "status": "IN_PROGRESS",
                "in_progress_until": now + remaining_seconds,
                "expiration": now + DONE_TTL_SECONDS,  # TTL。万一 DONE にならなくても、いずれ消える
            },
            # 条件付き書き込み：この条件が成り立つときだけ書く。成り立たなければ例外になる
            ConditionExpression=(
                "attribute_not_exists(id)"
                " OR expiration < :now"
                " OR (#status = :in_progress AND in_progress_until < :now)"
            ),
            # status は DynamoDB の予約語なので、式の中に直接は書けない。#status という仮の名前を使う
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":in_progress": "IN_PROGRESS", ":now": now},
        )
        return True
    except idempotency_table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def mark_done(key: str) -> None:
    """「済み」に書き換える。以後1時間、同じ通知は claim が False になり、処理されない。"""
    idempotency_table.put_item(
        Item={
            "id": key,
            "status": "DONE",
            "expiration": int(time.time()) + DONE_TTL_SECONDS,
        }
    )


def release(key: str) -> None:
    """「処理中」の行を消す。一時的な失敗のとき、SQS の渡し直しで claim できるようにする。"""
    idempotency_table.delete_item(Key={"id": key})
```

handler の中ではこう使う。

```python
key = unquote_plus(s3_event["s3"]["object"]["key"])

# 同じ通知の2回目なら、ここで止めて処理しない（S3 も Bedrock も DB も触らない）
if not claim(key, context):
    logger.info("skipped: 処理済みか、別の実行が処理中: %s", key)
    continue

try:
    analyze(bucket, key)
except PermanentError as e:
    # 恒久的な失敗。下の mark_done まで進むので「済み」になる＝重複が来ても、もう一度は試さない
    mark_failed(key, str(e))
except Exception as e:
    # 「処理中」の記録を消す。消さないと、SQS が渡し直したときに
    # 自分の記録に阻まれて claim できず、処理されないまま終わってしまう
    release(key)
    if receive_count >= MAX_RECEIVE_COUNT:
        mark_failed(key, f"{receive_count}回試して失敗: {type(e).__name__}: {e}")
    raise

# 成功、または恒久的な失敗を DB に書き終えた
mark_done(key)
```

## 「処理中」の重複を処理しなくてよい理由

| 先に始めた実行がどうなったか | どうなるか |
|---|---|
| 成功 | 「済み」になる。処理しなかった重複は、もともと不要だった |
| 一時的な失敗 | `release` して raise → 先の実行のメッセージは SQS に残っていて、6分後に渡し直される → `claim` できる |
| タイムアウトで死んだ | 「処理中」が残るが、6分後の渡し直しのときには `in_progress_until` を過ぎている → `claim` できる |

どの場合も、先に始めた実行のメッセージが最後まで責任を持つ。重複は処理しなくてよい。

## Powertools との対応

| この版 | Powertools（`@idempotent_function`） |
|---|---|
| `claim` の条件付き `PutItem` | 関数の中身を実行する前に、同じく条件付き `PutItem` で「処理中」を書く（条件も同じ3つ：行が無い／`expiration` 切れ／処理中のまま期限切れ）。`persistence/dynamodb.py` の `_put_record` |
| `claim` が False | 「済み」なら記録してあった戻り値を返し、中身は実行しない。「処理中」なら `IdempotencyAlreadyInProgressError` を投げる |
| `in_progress_until`（`context.get_remaining_time_in_millis()` から計算） | `config.register_lambda_context(context)` を呼ぶと同じことをする |
| `mark_done` | 関数が return したら `UpdateItem` で「済み」と戻り値を書く（`_update_record`） |
| `release` | 関数が例外を投げたら `DeleteItem` で記録を消す（`_delete_record`） |
| `id` = S3 のキーそのまま | `id` = 「関数名#（冪等性キーのハッシュ）」 |
| `DONE_TTL_SECONDS = 3600` | `expires_after_seconds`（既定 3600） |
| なし | 戻り値の保存、ペイロードの検証、ローカルキャッシュなど |

Powertools のソースは GitHub の `aws-powertools/powertools-lambda-python`、
`aws_lambda_powertools/utilities/idempotency/` の下にある（`base.py` が流れ、`persistence/dynamodb.py` が DynamoDB への読み書き）。

## 面談での言い方

「`@idempotent` が中でやっているのは、条件付き書き込みで『処理中』を取り、成功したら『済み』、失敗したら消す、の3つです。
『読んでから書く』に分けると同時実行で両方通るので、1回の条件付き書き込みにしているのが要点です。
自分で書いて確かめたうえで、実装は Powertools に任せました」
