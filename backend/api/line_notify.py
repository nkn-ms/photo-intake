"""落とし物グループに LINE で知らせる（ステップ11 の L5）。

管理者が確定（POST /items/{item_id}/confirm）して、書き込みが成功した後に routers/items.py から呼ぶ。
確定より先には送らない。送った後で確定が失敗すると、確定していない落とし物を知らせてしまうため。

知らせに失敗しても、確定は成功のまま返す（ここで例外を外に出さない）。
LINE の不調で確定の操作ができなくなるほうが困るため。失敗はログに残す。

LINE の API は、line-intake・worker と同じく Python の標準の urllib で呼ぶ（LINE の SDK は使わない）。
"""

import functools
import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

logger = logging.getLogger()

# Lambda では Terraform（main.tf）が環境変数で渡す。ローカル（uvicorn）では空のまま起動できるようにしておく
LINE_CHANNEL_ACCESS_TOKEN_ARN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN_ARN", "")
LINE_GROUP_ID = os.environ.get("LINE_GROUP_ID", "")  # 落とし物グループ。空なら知らせない

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
# 確定のボタンを押した人は、送り終わるまで待つことになる。LINE が遅いときに長く待たせないよう、短めに打ち切る
LINE_TIMEOUT_SECONDS = 5

# 拾得日時を日本時間で書く。zoneinfo（"Asia/Tokyo"）でもよいが、日本に夏時間は無いので +9時間の固定で足りる
JST = timezone(timedelta(hours=9), "JST")

secrets = boto3.client("secretsmanager")


def notify_line_group(
    category: str | None,
    color: str | None,
    brand: str | None,
    registered_at: datetime,
    location: str | None,
) -> None:
    """確定した落とし物を、落とし物グループに知らせる。値は人が確定したもの（AI の答えではない）。"""
    if not LINE_GROUP_ID:
        logger.info("LINE_GROUP_ID is empty, skip notification")
        return

    text = notification_text(category, color, brand, registered_at, location)
    # 失敗の種類（LINE が 4xx・5xx を返した・つながらない・時間切れ・鍵を読めない）を問わず、確定は止めない。
    # だから Exception をまとめて受け止める。ここで止めた失敗は、ログを見て気づく
    try:
        push(LINE_GROUP_ID, text)
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


def push(to: str, text: str) -> None:
    """LINE の push で、文字のメッセージを1通送る。2xx でなければ HTTPError になる。

    push は、こちらから好きなときに送れる代わりに、無料の通数（月200通）に数えられる
    （届いたメッセージへの返事（reply）は数えられない。line-intake の受付の返事は reply）。
    """
    body = {"to": to, "messages": [{"type": "text", "text": text}]}
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
