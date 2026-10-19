"""LINE の webhook を受け取る Lambda: line-webhook（ステップ11）。

ボット（LINE 公式アカウント「photo-intake 落とし物」）は2つの場所で使う:
  ・スタッフとボットの1対1のトーク … スタッフが拾得物の写真を送る（他の人には見えない）
  ・落とし物専用のグループ           … 解析が終わったら、worker がここに知らせる
どちらでも、何か起きるたびに（写真が送られた・ボットがグループに招待された、など）、
LINE のサーバーが API Gateway の POST /line/webhook にリクエストを送ってくる。ここはその受け口。
こちらから LINE に聞きに行くのではなく、LINE のほうから知らせてくる（これが webhook）。

この関数がすることは、入口の確かめだけ:
  1. 署名を確かめる（本当に LINE から来たリクエストか）。違えば 401 で断る
  2. 届いたイベントを1つずつ line-intake（intake.py）に渡す。終わるのは待たない
  3. すぐ 200 を返す
どのイベントをどう扱うか（写真を登録する・招待されたグループの ID をログに出す、など）は、line-intake が決める。
この関数は DB にも S3 にも触れない（line.tf で権限を分けている）。

ファイル名を handler.py にしないのは、テストのときに worker の handler.py と名前がぶつかるため
（pyproject.toml の pythonpath で、worker/ と line/ の両方を import できるようにしている）。
"""

import base64
import functools
import hashlib
import hmac
import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

secrets = boto3.client("secretsmanager")  # LINE の鍵を読む
lambda_client = boto3.client("lambda")  # line-intake を呼ぶ

# 鍵そのものではなく、鍵が入ったシークレットの場所（line.tf が環境変数で渡す）
LINE_CHANNEL_SECRET_ARN = os.environ["LINE_CHANNEL_SECRET_ARN"]

# 写真を渡す先（line-intake の関数名）
LINE_INTAKE_FUNCTION_NAME = os.environ["LINE_INTAKE_FUNCTION_NAME"]


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """API Gateway（HTTP API・payload 2.0）から呼ばれる入口。"""
    body = raw_body(event)

    # API Gateway の HTTP API は、ヘッダーの名前をすべて小文字にして渡してくる
    signature = event.get("headers", {}).get("x-line-signature", "")
    if not is_valid_signature(body, signature, channel_secret()):
        # 署名が無い・合わない ＝ LINE 以外の誰かが送ってきた（か、途中で書き換えられた）。中身は読まずに断る
        logger.warning("invalid signature")
        return {"statusCode": 401, "body": ""}

    # LINE が送ってくる本文は {"destination": "...", "events": [...]} の形の JSON。
    # 設定画面の「検証」ボタンは、events が空の本文を送ってくる。
    events: list[dict[str, Any]] = json.loads(body).get("events", [])

    # 何が届いたか（イベントの種類と、どこから来たか）をログに残す。例：[('join', 'group'), ('message', 'user')]
    logger.info(
        "received %d events: %s",
        len(events),
        [(e.get("type"), e.get("source", {}).get("type")) for e in events],
    )

    for e in events:
        hand_over(e)

    # LINE は 200 が返らないと「届かなかった」とみなす。中身の処理は line-intake が後で行うので、ここではすぐ返す。
    return {"statusCode": 200, "body": ""}


def raw_body(event: dict[str, Any]) -> bytes:
    """LINE が送ってきた本文を、送られてきたとおりのバイト列で取り出す。

    署名は「本文のバイト列」から計算されているので、JSON として読んで書き直したものでは合わない
    （空白や並び順が1文字でも変わると、別の署名になる）。必ず受け取ったままの本文で確かめる。
    API Gateway は本文を文字列で渡すが、中身によっては base64 にして渡す（isBase64Encoded が true）。
    """
    body: str = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    # LINE は UTF-8 で送ってくるので、UTF-8 に戻せば送られてきたバイト列と同じになる
    return body.encode("utf-8")


def is_valid_signature(body: bytes, signature: str, secret: str) -> bool:
    """x-line-signature ヘッダーの署名が、本文とチャネルシークレットから計算したものと一致するか。

    LINE は「チャネルシークレットを鍵にして、本文の HMAC-SHA256 を計算し、base64 にしたもの」を送ってくる。
    チャネルシークレットを知っているのは LINE とこちらだけなので、同じ値を作れるのは LINE だけ。
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    # == で比べると、先頭から何文字合っていたかで返るまでの時間がわずかに変わり、
    # 時間を測って1文字ずつ当てる攻撃に使われうる。compare_digest は合っていてもいなくても同じ時間で比べる
    return hmac.compare_digest(expected, signature)


def hand_over(e: dict[str, Any]) -> None:
    """イベント1つの処理を line-intake に頼む。終わるのは待たない。

    InvocationType="Event" が「非同期で呼ぶ」の指定。Lambda が頼みを受け付けた時点で返ってくる（数十ミリ秒）。
    line-intake が失敗したら、Lambda が自動でやり直す（line.tf の aws_lambda_function_event_invoke_config）。
    イベントは1つずつ頼む。まとめて頼むと、1つの失敗で全部がやり直しになるため。
    写真のイベントでも、中に入っているのは写真の中身ではなく、LINE のメッセージ ID だけ
    （line-intake が、それを使って LINE から取ってくる）。
    """
    lambda_client.invoke(
        FunctionName=LINE_INTAKE_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(e).encode("utf-8"),
    )


# functools.cache：1回目に読んだ値を覚えておき、2回目からは Secrets Manager に聞かない（worker の API キーと同じ）。
# 値がまだ入っていなければ、ここで例外になり 500 を返す（作者が値を入れるまでは、LINE からのリクエストは失敗する）
@functools.cache
def channel_secret() -> str:
    """Secrets Manager から LINE のチャネルシークレットを読む。"""
    return secrets.get_secret_value(SecretId=LINE_CHANNEL_SECRET_ARN)["SecretString"]
