"""SQS から S3 のイベントを受け取る worker。

このステップでは受け取った内容をログに出すだけ。
Bedrock での解析と DB への保存は後のステップで足す。
"""

import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def handler(event, context):
    """Lambda のエントリポイント。

    event の形は SQS 経由の場合こうなる:
        {"Records": [{"body": "<S3イベントのJSON文字列>", ...}, ...]}

    S3 のイベント自体も {"Records": [...]} という形なので、
    二重に入れ子になっている点に注意。
    """
    # 何件まとめて届いたか。SQS は複数件をまとめて渡してくることがある。
    logger.info("received %d record(s)", len(event.get("Records", [])))

    for record in event.get("Records", []):
        # body は文字列なので、JSON として読み直す必要がある
        body = json.loads(record["body"])

        # S3 のテスト通知（バケット設定時に送られる）には Records が無い
        if "Records" not in body:
            logger.info("skipped non-s3 event: %s", body.get("Event", "unknown"))
            continue

        for s3_event in body["Records"]:
            bucket = s3_event["s3"]["bucket"]["name"]
            key = s3_event["s3"]["object"]["key"]
            size = s3_event["s3"]["object"]["size"]

            logger.info("object created: s3://%s/%s (%d bytes)", bucket, key, size)

    # 正常終了すると、Lambda が SQS のメッセージを自動で削除する。
    # 例外を投げると削除されず、可視性タイムアウト後に再配信される。
    return {"ok": True}
