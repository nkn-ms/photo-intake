"""SQS から S3 のイベントを受け取る worker。

このステップでは、写真を Bedrock のモデルに渡して、返ってきた答えをログに出すだけ。
DB への保存は後のステップで足す（先に「AI が何を返すか」を目で見てから保存の形を決める）。
"""

import json
import logging
import os
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# クライアントは関数の外で1回だけ作る。
# Lambda は同じ実行環境を使い回すので、2回目以降の呼び出しでは作り直さずに済む。
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")

# 推論プロファイルのID（jp. 付き）。Terraform が環境変数で渡す。
# jp. の付かないモデルIDを渡すと、Nova 2 Lite も Claude も「直接は呼べない」と失敗する。
VISION_MODEL_ID = os.environ["VISION_MODEL_ID"]

# 拡張子 → Converse API の format。
# format に "jpg" という値は無い（"jpeg" だけ）。Playground で JPG を選べなかったのと同じ種類の落とし穴。
IMAGE_FORMATS = {
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


def handler(event, context):
    """Lambda のエントリポイント。

    event の形は SQS 経由の場合こうなる:
        {"Records": [{"body": "<S3イベントのJSON文字列>", ...}, ...]}

    S3 のイベント自体も {"Records": [...]} という形なので、
    二重に入れ子になっている点に注意。
    """
    logger.info("received %d record(s)", len(event.get("Records", [])))

    for record in event.get("Records", []):
        body = json.loads(record["body"])

        # S3 のテスト通知（バケット設定時に送られる）には Records が無い
        if "Records" not in body:
            logger.info("skipped non-s3 event: %s", body.get("Event", "unknown"))
            continue

        for s3_event in body["Records"]:
            bucket = s3_event["s3"]["bucket"]["name"]
            # S3 の通知に入るキーは URL エンコードされている（空白は +、日本語は %E3%81... になる）。
            # そのまま get_object に渡すと「そんなファイルは無い」と言われるので、元に戻す。
            key = unquote_plus(s3_event["s3"]["object"]["key"])
            analyze(bucket, key)

    # 正常終了すると、Lambda が SQS のメッセージを自動で削除する。
    # 例外を投げると削除されず、可視性タイムアウト後に再配信される。
    return {"ok": True}


def analyze(bucket: str, key: str) -> None:
    """写真1枚を読み、モデルに渡して、答えをログに出す。"""
    ext = os.path.splitext(key)[1].lower()
    image_format = IMAGE_FORMATS.get(ext)
    if image_format is None:
        # 失敗の扱い（analysis_failed）は後のステップで決める。いまはログだけ。
        logger.warning("unsupported file type, skipped: %s", key)
        return

    # 通知には「どのファイルか」しか入っていないので、写真そのものを取りに行く
    image_bytes = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    logger.info("downloaded s3://%s/%s (%d bytes)", bucket, key, len(image_bytes))

    # Converse API：モデルが変わっても同じ形で呼べる
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

    answer = response["output"]["message"]["content"][0]["text"]
    usage = response["usage"]  # 入出力のトークン数。料金はこれで決まる

    logger.info("model answer: %s", answer)
    logger.info(
        "tokens: input=%d output=%d", usage["inputTokens"], usage["outputTokens"]
    )
