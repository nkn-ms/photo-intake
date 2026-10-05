"""SQS から S3 のイベントを受け取る worker。

写真を Bedrock のモデルに渡し、返ってきた答えを items に保存する。

失敗は2種類に分けて扱う:
  ・恒久的な失敗（何度やっても同じ）→ その場で analysis_failed にする。例外は投げない
  ・一時的な失敗（時間をおけば通るかも）→ 例外を投げて SQS に渡し直させる

SQS は同じ通知を2回渡すことがある（at-least-once）。
写真1枚の処理（analyze_and_record）に Powertools の @idempotent_function を付けて、2回目は処理しない。
Powertools が中でやっていることは docs/idempotency-by-hand.md に、同じことを手で書いた版がある。
"""

# 型注釈を実行時に評価しない（文字列のまま持っておく）。
# 下の TYPE_CHECKING の中でしか import しない型を、注釈に書けるようにするため。
from __future__ import annotations

import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote_plus

import boto3
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
)
from aws_lambda_powertools.utilities.typing import LambdaContext

# boto3 の型（types-boto3）は開発用のパッケージで、Lambda には入っていない。
# TYPE_CHECKING は mypy が検査するときだけ True になり、実行時は False。
# だから Lambda ではこの import は実行されず、「そんなパッケージは無い」で落ちることもない。
# （TS の import type に当たる）
if TYPE_CHECKING:
    from types_boto3_bedrock_runtime.literals import ImageFormatType
    from types_boto3_rds_data.type_defs import (
        ExecuteStatementResponseTypeDef,
        SqlParameterTypeDef,
    )

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# クライアントは関数の外で1回だけ作る。
# Lambda は同じ実行環境を使い回すので、2回目以降の呼び出しでは作り直さずに済む。
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")
rds_data = boto3.client("rds-data")  # Data API（HTTPS で SQL を送る）

# 冪等性の記録先。Terraform が作った DynamoDB のテーブル（列の名前 id / expiration は Powertools の既定）
idempotency_store = DynamoDBPersistenceLayer(table_name=os.environ["IDEMPOTENCY_TABLE"])

idempotency_config = IdempotencyConfig(
    # 何で「同じ通知」を見分けるか。analyze_and_record に渡す photo のうち key（S3 のキー）だけを使う。
    # API が登録ごとに新しいキーを発行するので、1つのキー ＝ 1回の登録。
    # "key" は JMESPath（AWS CLI の --query と同じ書き方）で「photo の中の key」という意味。
    event_key_jmespath="key",
    # key が取り出せなかったら、冪等性を効かせずに素通りするのではなく、エラーにする
    raise_on_no_idempotency_key=True,
    # 「済み」の記録を何秒残すか（Powertools の既定と同じ。分かるように明示した）。過ぎたら TTL が消す。
    # SQS の重複配信はたいてい数分以内、やり直しも 6分 × 3回 で終わるので、1時間あれば足りる。
    # 1時間を過ぎて重複が来ても、Aurora の UPDATE（status = 'analyzing' の条件）が上書きを止める。
    expires_after_seconds=3600,
)

# Data API に渡す3点セット。make sql が --resource-arn / --secret-arn / --database で渡しているのと同じもの。
DB_CLUSTER_ARN = os.environ["DB_CLUSTER_ARN"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
DB_NAME = os.environ["DB_NAME"]

# 推論プロファイルのID（jp. 付き）。Terraform が環境変数で渡す。
# jp. の付かないモデルIDを渡すと、Nova 2 Lite も Claude も「直接は呼べない」と失敗する。
VISION_MODEL_ID = os.environ["VISION_MODEL_ID"]

# 本体キューの maxReceiveCount と同じ数。Terraform が同じ locals から渡す。
# この回数目の配信で失敗したら、次は DLQ に落ちる＝最終試行。
MAX_RECEIVE_COUNT = int(os.environ["MAX_RECEIVE_COUNT"])

# "bedrock" なら本物のモデルを呼ぶ。"dummy" なら呼ばずに決め打ちの答えを返す。
# アカウントで Bedrock が止められている間（サポート待ち）も、保存の流れを先に作るため。
ANALYZER = os.environ.get("ANALYZER", "bedrock")

# items.ai_model に記録する名前。ダミーの行を後から見分けて、精度の集計から外せるようにする。
AI_MODEL = "dummy" if ANALYZER == "dummy" else VISION_MODEL_ID

# ダミーの答え。本物のモデルに頼んでいるのと同じ形にしておく。
DUMMY_ANSWER = json.dumps(
    {
        "category": "財布",
        "color": "黒",
        "brand": None,
        "features": ["二つ折り", "革製", "角に擦れ"],
    },
    ensure_ascii=False,  # 日本語を 財 のような形にせず、そのまま出す
)

# 拡張子 → Converse API の format。
# format に "jpg" という値は無い（"jpeg" だけ）。Playground で JPG を選べなかったのと同じ種類の落とし穴。
# ImageFormatType は "jpeg" | "png" | "gif" | "webp" のような決まった文字列だけを許す型。
# 右側に "jpg" と書き間違えると、mypy がその場で指摘してくれる。
IMAGE_FORMATS: dict[str, ImageFormatType] = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".gif": "gif",
    ".webp": "webp",
}

PROMPT = """この写真は、施設で拾われた落とし物です。
写っている物について、次の形の JSON だけを出力してください。説明文は付けないでください。

{"category": "種別（例: 財布、傘、スマートフォン）",
 "color": "主な色",
 "brand": "ブランド名。読み取れなければ null",
 "features": ["目立つ特徴を短く。最大5つ"]}"""


class PermanentError(Exception):
    """何度やり直しても同じ結果になる失敗。

    handler がこれを捕まえたら、やり直さずに処理を止めて analysis_failed を書く。
    SQS には「成功した」と返すので、渡し直し（3分×3回の無駄）が起きない。
    """


def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, bool]:
    """Lambda のエントリポイント。

    event の形は SQS 経由の場合こうなる:
        {"Records": [{"body": "<S3イベントのJSON文字列>", ...}, ...]}

    S3 のイベント自体も {"Records": [...]} という形なので、
    二重に入れ子になっている点に注意。
    """
    logger.info("received %d record(s)", len(event.get("Records", [])))

    # この Lambda があと何秒で強制終了されるかを Powertools に教える。
    #
    # Lambda がタイムアウトすると、プログラムはその場で止められる（except も動かない）。
    # すると Powertools は記録を消せず、「処理中」のまま DynamoDB に残る。
    #   ・これを呼ばないと：「処理中」は expires_after_seconds（1時間）まで有効のまま。
    #     6分後に SQS が渡し直しても「別の実行が処理中」と判断され、handler は処理しない（continue）。
    #     正常に終わるので SQS はメッセージを消し、写真は解析されないまま analyzing で残る。
    #   ・これを呼ぶと：「処理中」の期限が、この実行の残り時間（最大60秒）になる。
    #     6分後の渡し直しのときには期限が過ぎているので、もう一度処理される。
    idempotency_config.register_lambda_context(context)

    for record in event.get("Records", []):
        body = json.loads(record["body"])

        # このメッセージが何回目の配信か。SQS は数字を文字列で入れてくる
        receive_count = int(record["attributes"]["ApproximateReceiveCount"])

        # S3 のテスト通知（バケット設定時に送られる）には Records が無い
        if "Records" not in body:
            logger.info("skipped non-s3 event: %s", body.get("Event", "unknown"))
            continue

        for s3_event in body["Records"]:
            bucket = s3_event["s3"]["bucket"]["name"]
            # S3 の通知に入るキーは URL エンコードされている（空白は +、日本語は %E3%81... になる）。
            # そのまま get_object に渡すと「そんなファイルは無い」と言われるので、元に戻す。
            key = unquote_plus(s3_event["s3"]["object"]["key"])
            try:
                # photo= を付けて呼ぶこと。Powertools は data_keyword_argument="photo" の名前で引数を探す
                result = analyze_and_record(photo={"bucket": bucket, "key": key})
                # 2回目の通知では analyze_and_record の中身は動かず、1回目の戻り値がそのまま返ってくる
                # （downloaded や model answer のログが出ずに、これだけが出る）
                logger.info("result: %s", result)
            except IdempotencyAlreadyInProgressError:
                # 同じ通知を、別の実行がいま処理している。待たずに、この実行では処理しない。
                # 先の実行が失敗しても、そのメッセージは SQS に残っていて、SQS が渡し直すから。
                # 失敗ではないので analysis_failed は書かない（書くと、先の実行の成功が保存されなくなる）。
                logger.info("skipped: 別の実行が処理中: %s", key)
                continue
            except Exception as e:
                # 一時的なエラー（DB の起動待ち、Bedrock の混雑など）か、想定していないエラー（バグなど）。
                # どちらか見分けられないので、やり直す側に倒す。本当にバグなら3回とも失敗し、
                # DLQ → アラームで開発者が気づく。
                # 例外を投げ直す（raise）と、SQS はメッセージを消さずに、少し後でもう一度渡してくれる。
                #
                # SQS は同じメッセージを MAX_RECEIVE_COUNT 回（3回）まで渡してくれる:
                #    0分  1回目 失敗 → raise → SQS「3分後にもう一度」
                #    3分  2回目 失敗 → raise → SQS「3分後にもう一度」
                #    6分  3回目 失敗 → raise → SQS「もう諦める。DLQ へ」
                # 3回目が「最終試行」（DLQ に移る前の最後の配信）。これより後に worker が呼ばれることはない。
                #
                # ■ なぜ最終試行で analysis_failed を書くのか
                #   DLQ と DB は見る人が違う。
                #     ・DLQ … 開発者が見る（アラームのメールで気づく）
                #     ・DB  … スタッフが管理画面で見る。画面は DLQ を読まない
                #   書かずに DLQ へ落ちると、画面には analyzing（解析中）が出続け、
                #   「待てば終わるのか、もう終わらないのか」がスタッフに分からない。
                #   手入力に回すボタンも analysis_failed の行にしか出ない。
                #
                # ■ なぜ1回目・2回目では書かないのか
                #   次の配信で成功するかもしれないから。先に analysis_failed を書くと、
                #   次の配信で成功しても save_result の条件（status = 'analyzing'）に合わず、
                #   成功が保存されないまま失敗として残ってしまう。
                if receive_count >= MAX_RECEIVE_COUNT:
                    logger.error("giving up after %d attempts: %s", receive_count, key)
                    mark_failed(
                        key,
                        f"{receive_count}回試して失敗: {type(e).__name__}: {e}",
                    )

                # ■ なぜ最終試行でも raise するのか（恒久的なエラーのように、ここで処理を止めない）
                #   raise すると DLQ に入り、アラームが開発者にメールする。
                #   一時的な失敗が3回続いた＝写真1枚ではなく「仕組み」が壊れている疑いがあるので、
                #   スタッフの画面だけでなく開発者にも知らせたい。
                #   （恒久的なエラーは写真1枚の問題なので、上の except で処理を止めて画面だけにしている）
                #
                # 引数なしの raise は「いま捕まえた例外をそのまま投げ直す」。TS の throw e と同じ。
                raise

    # 正常終了すると、Lambda が SQS のメッセージを自動で削除する。
    # 例外を投げると削除されず、可視性タイムアウト後に再配信される。
    return {"ok": True}


# Powertools のデコレーターは戻り値の型を Any と宣言しているので、mypy は「型の無いデコレーター」と言う。
# 中身（analyze_and_record）の型は自分で書いてあるので、この指摘だけを黙らせる。
@idempotent_function(  # type: ignore[untyped-decorator]
    data_keyword_argument="photo",
    persistence_store=idempotency_store,
    config=idempotency_config,
)
def analyze_and_record(photo: dict[str, str]) -> dict[str, str]:
    """写真1枚を解析し、結果を items に記録する（成功なら pending_review、恒久的な失敗なら analysis_failed）。

    手順はこの関数に全部並んでいる。下の部品（check_format など）はどれも1つのことだけをして返し、
    部品同士は呼び合わない。

    Powertools が中身の前後で次のことをする（手で書いた版は docs/idempotency-by-hand.md）:
      前：DynamoDB に「処理中」を書く。もう「済み」なら中身を動かさず、前回の戻り値を返す。
          「処理中」なら IdempotencyAlreadyInProgressError を投げる
      後：return したら「済み」と戻り値を書く。例外が出たら記録を消す（次の配信でやり直せるように）

    だから恒久的な失敗はここで受け止めて return する。外に例外として出すと記録が消され、
    重複が来るたびにやり直す（JSON が読めない失敗なら Bedrock をもう一度呼ぶ）ことになる。
    一時的な失敗は受け止めずに外に出す。記録が消え、handler が raise して SQS がやり直す。

    戻り値は DynamoDB に保存されるので、JSON にできる形（dict / str / 数値）にする。
    """
    key = photo["key"]
    try:
        image_format = check_format(key)  # 1. 対応している形式か
        image_bytes = download(photo["bucket"], key)  # 2. S3 から写真を読む
        answer = ask_model(image_bytes, image_format)  # 3. モデルに聞く
        ai_result = parse_answer(answer)  # 4. 答えを JSON として読む
    except PermanentError as e:
        # 恒久的なエラー（1 か 4）。やり直しても同じなので、ここで処理を止める。
        # 失敗を DB に書いて正常に終わるので、SQS はメッセージを消す（やり直しは起きない）。
        logger.warning("analysis failed: %s: %s", key, e)
        mark_failed(key, str(e))
        return {"key": key, "outcome": "analysis_failed"}

    save_result(key, ai_result)  # 5. 保存して pending_review に進める
    return {"key": key, "outcome": "analyzed"}


def check_format(key: str) -> ImageFormatType:
    """拡張子から Converse API の format を返す。対応していない形式なら PermanentError。"""
    ext = os.path.splitext(key)[1].lower()
    image_format = IMAGE_FORMATS.get(ext)
    if image_format is None:
        raise PermanentError(f"未対応の形式: {ext or '(拡張子なし)'}")
    return image_format


def download(bucket: str, key: str) -> bytes:
    """S3 から写真を読む。通知には「どのファイルか」しか入っていないので、写真そのものを取りに行く。"""
    image_bytes = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    logger.info("downloaded s3://%s/%s (%d bytes)", bucket, key, len(image_bytes))
    return image_bytes


def parse_answer(answer: str) -> dict[str, Any]:
    """モデルの答えを JSON として読む。読めなければ PermanentError。

    同じ写真・同じ temperature=0 で渡し直しても、同じ答えが返ってくるだけなので恒久的な失敗。
    （本物のモデルは ```json で囲んで返すことがある。その扱いは Bedrock が使えるようになってから決める）
    """
    logger.info("model answer: %s", answer)
    try:
        ai_result: dict[str, Any] = json.loads(answer)
    except json.JSONDecodeError:
        # 答えの頭だけを残す。人が画面で見て「なぜ失敗したか」が分かるように
        raise PermanentError(f"モデルの答えが JSON として読めない: {answer[:200]}")
    return ai_result


# Aurora は5分使われないと一時停止し、次の SQL で起き始める。起きるまで十数秒かかり、
# その間に SQL を送ると DatabaseResumingException が返る。
#
# これは「待てば必ず直る」と分かっている一時的なエラーなので、SQS に6分預けてやり直すより、
# その場で数秒待って送り直すほうが早い。最終試行の analysis_failed も書けるようになる。
DB_RESUME_WAIT_SECONDS = 5  # 何秒おきに送り直すか
DB_RESUME_MAX_ATTEMPTS = 6  # 何回まで送るか（待つのは最大 5秒 × 5回 = 25秒）


def execute_sql(sql: str, parameters: list[SqlParameterTypeDef]) -> ExecuteStatementResponseTypeDef:
    """Data API で SQL を1本送る。DB が起きる途中なら、起きるまで待って送り直す。"""
    for attempt in range(1, DB_RESUME_MAX_ATTEMPTS + 1):
        try:
            return rds_data.execute_statement(
                resourceArn=DB_CLUSTER_ARN,
                secretArn=DB_SECRET_ARN,
                database=DB_NAME,
                sql=sql,
                parameters=parameters,
            )
        except rds_data.exceptions.DatabaseResumingException:
            if attempt == DB_RESUME_MAX_ATTEMPTS:
                # 25秒待っても起きなかった。ここで諦めて呼び出し元に任せる
                # （一時的なエラーとして SQS がやり直す／最終試行なら DLQ とアラーム）
                raise
            logger.info(
                "DB is resuming, retry in %ds (%d/%d)",
                DB_RESUME_WAIT_SECONDS,
                attempt,
                DB_RESUME_MAX_ATTEMPTS,
            )
            time.sleep(DB_RESUME_WAIT_SECONDS)
    # ここには来ない（ループは必ず return か raise で抜ける）。
    # ただ、関数の最後に何も無いと「dict を返すはずなのに None を返しうる」と型チェック（mypy）に言われるので置いている。
    raise AssertionError("unreachable")


# 行は API（ステップ8）が署名付きURLを発行するときに作る。worker は UPDATE だけを担当する。
# API ができるまでは make sql で手で INSERT しておく。
#
# status = 'analyzing' を条件に入れているので、次の2つの場合は1行も更新されない:
#   ・行が無い（登録されていない写真）
#   ・もう解析済み（SQS の2回目の配信）→ 人が確認した後の結果を上書きしないで済む
#
# :名前 はパラメータ。値は SQL の文字列に埋め込まず、parameters で別に渡す（SQL インジェクション対策）。
UPDATE_SQL = """
update items
   set status         = 'pending_review',
       ai_result      = :ai_result,
       ai_model       = :ai_model,
       ai_analyzed_at = now(),
       updated_at     = now()
 where s3_key = :s3_key
   and status = 'analyzing'
"""


def save_result(key: str, ai_result: dict[str, Any]) -> None:
    """AI の答えを items に保存して、status を pending_review に進める。"""
    response = execute_sql(
        UPDATE_SQL,
        [
            # typeHint="JSON"：文字列として渡すが、DB 側では jsonb として扱ってもらう
            {
                "name": "ai_result",
                "value": {"stringValue": json.dumps(ai_result, ensure_ascii=False)},
                "typeHint": "JSON",
            },
            {"name": "ai_model", "value": {"stringValue": AI_MODEL}},
            {"name": "s3_key", "value": {"stringValue": key}},
        ],
    )

    if response["numberOfRecordsUpdated"] == 0:
        logger.warning("no row updated (未登録の写真か、解析済み): %s", key)
    else:
        logger.info("saved: %s -> pending_review", key)


# 条件は UPDATE_SQL と同じ（status = 'analyzing' のときだけ）。
# ai_model も残すのは、「どのモデルが JSON を返し損ねたか」を後で数えられるようにするため。
FAIL_SQL = """
update items
   set status     = 'analysis_failed',
       ai_error   = :ai_error,
       ai_model   = :ai_model,
       updated_at = now()
 where s3_key = :s3_key
   and status = 'analyzing'
"""


def mark_failed(key: str, reason: str) -> None:
    """解析をあきらめたことを items に記録する。画面から手入力に回せるようにするため。"""
    response = execute_sql(
        FAIL_SQL,
        [
            {"name": "ai_error", "value": {"stringValue": reason}},
            {"name": "ai_model", "value": {"stringValue": AI_MODEL}},
            {"name": "s3_key", "value": {"stringValue": key}},
        ],
    )

    if response["numberOfRecordsUpdated"] == 0:
        logger.warning("no row updated (未登録の写真か、解析済み): %s", key)
    else:
        logger.info("saved: %s -> analysis_failed", key)


def ask_model(image_bytes: bytes, image_format: ImageFormatType) -> str:
    """写真をモデルに渡して、答え（文字列）を返す。"""
    if ANALYZER == "dummy":
        logger.info("ANALYZER=dummy: Bedrock は呼ばずに決め打ちの答えを返す")
        return DUMMY_ANSWER

    # Converse API：モデルが変わっても同じ形で呼べる
    try:
        response = bedrock.converse(
            modelId=VISION_MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"image": {"format": image_format, "source": {"bytes": image_bytes}}},
                        {"text": PROMPT},
                    ],
                }
            ],
            # temperature=0：毎回なるべく同じ答えにする（抽出の作業なので、創造性は要らない）
            inferenceConfig={"maxTokens": 512, "temperature": 0},
        )
    except bedrock.exceptions.ValidationException as e:
        # ValidationException は「送った中身が受け付けられない」という意味（画像が大きすぎる、など）。
        # 同じ中身を送り直しても通らないので恒久的な失敗。
        # ThrottlingException（混雑）などはここでは捕まえない。一時的なエラーとして SQS にやり直してもらう。
        raise PermanentError(f"Bedrock が受け付けなかった: {e}")

    usage = response["usage"]  # 入出力のトークン数。料金はこれで決まる
    logger.info("tokens: input=%d output=%d", usage["inputTokens"], usage["outputTokens"])
    return response["output"]["message"]["content"][0]["text"]
