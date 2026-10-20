"""落とし物グループに LINE で知らせる（ステップ11 の L5）。

管理者が確定（POST /items/{item_id}/confirm）して、書き込みが成功した後に routers/items.py から呼ぶ。
確定より先には送らない。送った後で確定が失敗すると、確定していない落とし物を知らせてしまうため。

知らせに失敗しても、確定は成功のまま返す（ここで例外を外に出さない）。
LINE の不調で確定の操作ができなくなるほうが困るため。失敗はログに残す。

知らせには写真も付ける。LINE の画像メッセージは、LINE のサーバーが写真の URL から取りに行く作りなので、
ログイン無しで開ける URL が要る。そこで写真を知らせの写真のバケット（notify）に、落とし物の ID の名前でコピーし、
写真専用の CloudFront の URL を送る（理由と割り切りは infra/envs/dev/line.tf の「知らせに写真を付ける」のコメント）。
写真を付けられないとき（CloudFront が無い・形式が合わない・コピーに失敗した）は、文字だけで知らせる。

LINE の API は、line-intake・worker と同じく Python の標準の urllib で呼ぶ（LINE の SDK は使わない）。
"""

import functools
import json
import logging
import os
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

logger = logging.getLogger()

# Lambda では Terraform（main.tf）が環境変数で渡す。ローカル（uvicorn）では空のまま起動できるようにしておく
LINE_CHANNEL_ACCESS_TOKEN_ARN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN_ARN", "")
LINE_GROUP_ID = os.environ.get("LINE_GROUP_ID", "")  # 落とし物グループ。空なら知らせない
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")  # 写真の原本があるバケット（コピー元）
# 写真のコピーを置くバケット（notify バケット）
NOTIFY_PHOTO_BUCKET = os.environ.get("NOTIFY_PHOTO_BUCKET", "")
# 写真の URL のうちファイル名より前の部分（例：https://yyyy.cloudfront.net）。空なら写真を付けない
NOTIFY_PHOTO_BASE_URL = os.environ.get("NOTIFY_PHOTO_BASE_URL", "")

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
# 確定のボタンを押した人は、送り終わるまで待つことになる。LINE が遅いときに長く待たせないよう、短めに打ち切る
LINE_TIMEOUT_SECONDS = 5

# 拾得日時を日本時間で書く。zoneinfo（"Asia/Tokyo"）でもよいが、日本に夏時間は無いので +9時間の固定で足りる
JST = timezone(timedelta(hours=9), "JST")

# LINE の画像メッセージが受け付ける形式は JPEG と PNG だけ。拡張子と、コピーに付ける Content-Type の組。
# 画面から登録した写真は必ず JPEG にしてから送っていて（frontend/src/image.ts）、LINE から届く写真も JPEG なので、ふつうは合う
LINE_IMAGE_TYPES = {".jpg": "image/jpeg", ".png": "image/png"}

secrets = boto3.client("secretsmanager")
s3 = boto3.client("s3")


def notify_line_group(
    category: str | None,
    color: str | None,
    brand: str | None,
    registered_at: datetime,
    location: str | None,
    item_id: uuid.UUID,
    s3_key: str,
) -> None:
    """確定した落とし物を、落とし物グループに知らせる。値は人が確定したもの（AI の答えではない）。

    item_id は落とし物の ID。知らせに付ける写真のコピーの名前に使う。
    s3_key は uploads バケットにある写真の原本のキー。知らせに付ける写真は、ここからコピーする。
    """
    if not LINE_GROUP_ID:
        logger.info("LINE_GROUP_ID is empty, skip notification")
        return

    messages = [text_message(notification_text(category, color, brand, registered_at, location))]
    photo_url = publish_photo(item_id, s3_key)
    if photo_url is not None:
        messages.append(image_message(photo_url))

    # 失敗の種類（LINE が 4xx・5xx を返した・つながらない・時間切れ・鍵を読めない）を問わず、確定は止めない。
    # だから Exception をまとめて受け止める。ここで止めた失敗は、ログを見て気づく
    try:
        push(LINE_GROUP_ID, messages)
    except Exception:
        logger.exception("LINE notification failed")


def notification_text(
    category: str | None,
    color: str | None,
    brand: str | None,
    registered_at: datetime,
    location: str | None,
) -> str:
    """知らせの文面を作る。値が無い欄は「—」にする。"""
    lines = [
        "落とし物のお知らせ",
        f"種別：{category or '—'}",
        f"色：{color or '—'}",
        f"ブランド：{brand or '—'}",
        f"拾得日時：{registered_at.astimezone(JST):%m/%d %H:%M}",
        f"拾得場所：{location or '—'}",
    ]
    return "\n".join(lines)


def publish_photo(item_id: uuid.UUID, s3_key: str) -> str | None:
    """写真の原本を notify バケットにコピーし、LINE に渡す URL を返す。付けられないときは None。

    コピーの名前は落とし物の ID にする（<落とし物の ID>.jpg）。
      ・落とし物の ID は DB がランダムに作る UUID（gen_random_uuid）なので、1つの URL からほかの写真の URL は推し量れない。
        原本のキーは使わない。LINE から来た写真のキーは、数字だけの LINE のメッセージ ID で推し量りやすいため
      ・原本（items の s3_key）とコピーを、落とし物の ID で突き合わせられる。写真を消すときに両方を消せる
      ・同じ落とし物は1回しか確定できないので、コピーが2枚できることはない
    コピーは30日で消える（line.tf のライフサイクルの決まり）。

    決まり：落とし物の ID をお客さま（落とし主）に見せない。ID を知っていると、この写真の URL を組み立てられるため。
    スタッフ（管理画面の URL に ID が出る）と落とし物グループのメンバーは、もともと写真を見られる人なので問題ない。
    """
    if not NOTIFY_PHOTO_BASE_URL:
        logger.info("NOTIFY_PHOTO_BASE_URL is empty, notify without photo")
        return None
    extension = os.path.splitext(s3_key)[1]
    content_type = LINE_IMAGE_TYPES.get(extension)
    if content_type is None:
        logger.info("LINE cannot show %s, notify without photo", extension)
        return None

    file_name = f"{item_id}{extension}"
    # S3 の中でコピーする（Lambda が写真を読み込んで書き直さなくてよい）。
    # Content-Type は原本のものを引き継がず、拡張子から決めて付け直す（MetadataDirective="REPLACE"）。
    # 原本に何が付いていても、ブラウザと LINE には必ず画像として渡す（CloudFront の nosniff と組で、HTML として開かれない）。
    # コピーに失敗しても、文字だけで知らせる（写真が無いことより、知らせが届かないことのほうが困るため）
    try:
        s3.copy_object(
            Bucket=NOTIFY_PHOTO_BUCKET,
            Key=file_name,
            CopySource={"Bucket": UPLOAD_BUCKET, "Key": s3_key},
            ContentType=content_type,
            MetadataDirective="REPLACE",
        )
    except Exception:
        logger.exception("photo copy failed, notify without photo")
        return None
    return f"{NOTIFY_PHOTO_BASE_URL}/{file_name}"


def text_message(text: str) -> dict[str, Any]:
    """LINE の文字のメッセージ（吹き出し1つ）。"""
    return {"type": "text", "text": text}


def image_message(url: str) -> dict[str, Any]:
    """LINE の画像のメッセージ（吹き出し1つ）。

    originalContentUrl はタップして開いたときの写真、previewImageUrl はトークに並ぶ小さい写真（1MB まで）。
    小さい写真を別に作らず、同じ URL を渡す。縮めた写真は 1MB より十分小さいため（LINE から来た写真は100KB ほど）。
    """
    return {"type": "image", "originalContentUrl": url, "previewImageUrl": url}


def push(to: str, messages: list[dict[str, Any]]) -> None:
    """LINE の push で、メッセージ（吹き出し5つまで）を送る。2xx でなければ HTTPError になる。

    push は、こちらから好きなときに送れる代わりに、無料の通数（月200通）に数えられる
    （届いたメッセージへの返事（reply）は数えられない。line-intake の受付の返事は reply）。
    1回の push で吹き出しを2つ（文字と写真）送っても、数えるのは1通。
    """
    body = {"to": to, "messages": messages}
    request = urllib.request.Request(
        LINE_PUSH_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {channel_access_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    urllib.request.urlopen(request, timeout=LINE_TIMEOUT_SECONDS).close()
    logger.info("notified group: %s", to)


# 1回目に読んだ値を覚えておき、2回目からは Secrets Manager に聞かない（worker・line-intake と同じ）
@functools.cache
def channel_access_token() -> str:
    """Secrets Manager から LINE のチャネルアクセストークンを読む。"""
    return secrets.get_secret_value(SecretId=LINE_CHANNEL_ACCESS_TOKEN_ARN)["SecretString"]
