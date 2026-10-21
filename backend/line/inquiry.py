"""落とし主からの問い合わせを受け付ける Lambda: line-inquiry（問い合わせの受付 A）。

line-inquiry-webhook（webhook.py。スタッフ用の受け口と同じコード）が署名を確かめたあと、
問い合わせ用アカウントに届いたイベントを1つずつ、非同期でここに渡してくる。
  ・1対1のトークで文字が届いた … 上限を確かめる → requests に行を作る → 受付番号（例：A-0012）を返事する
  ・1対1のトークで文字以外（写真・スタンプなど）が届いた … 「文字で送ってください」と返事する
  ・それ以外（友だちの追加・グループでの発言など） … 何もしない

お客さまに返すのは受付番号だけ。落とし物の ID・写真・候補は見せない
（見せると、持ち主でない人が「それは私のです」と名乗れる。照らし合わせはスタッフが確かめてから連絡する）。

スタッフへの知らせは送らない。問い合わせは管理画面の一覧（GET /requests）で見る。
スタッフ用アカウントの落とし物グループは、社内の落とし物の情報を共有する場所なので、お客さまの問い合わせは混ぜない。

悪意のある使い方への備え:
  ・大量に送る … 1人あたり直近24時間に5件まで・1件500文字まで。超えた分は保存せず、そう返事するだけ
  ・なりすまし … webhook.py が LINE の署名を確かめている（LINE 以外からのリクエストはここまで来ない）

失敗したとき（DB が起きない・LINE が一時的に応えない）は例外のまま終わり、Lambda が1分後・さらに2分後にやり直す。
やり直しても二重に受け付けないように、requests.line_message_id を UNIQUE にしてある（マイグレーション 003）。
"""

import logging
import os
import urllib.error
from typing import Any

import line_common

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# 以下は line_inquiry.tf が環境変数で渡す（DB の3点セットは line_common.py が読む）
# 問い合わせ用アカウントのアクセストークンの場所。お客さまへの返事に使う
LINE_INQUIRY_ACCESS_TOKEN_ARN = os.environ["LINE_INQUIRY_ACCESS_TOKEN_ARN"]

MAX_LENGTH = 500  # 1件の問い合わせの文字数の上限
DAILY_LIMIT = 5  # 1人あたり、直近24時間に受け付ける件数の上限


# --- お客さまに送る文 ---


def received_message(receipt: str) -> str:
    """受け付けたときの返事。受付番号と、この後どうなるかを伝える。見つかったかどうかは、ここでは伝えない。"""
    return (
        f"お問い合わせを受け付けました。受付番号は {receipt} です。\n"
        "係の者が確認し、該当する落とし物が見つかった場合は、こちらからご連絡します。\n"
        "お問い合わせの際は、受付番号をお伝えください。"
    )


TOO_LONG_MESSAGE = f"お問い合わせは{MAX_LENGTH}文字以内でお送りください。"
LIMIT_MESSAGE = (
    f"お問い合わせは24時間に{DAILY_LIMIT}件まで受け付けています。時間をおいてお送りください。"
)
TEXT_ONLY_MESSAGE = (
    "お問い合わせは文字でお送りください。\n"
    "落とし物の種類・色・ブランドなどの特徴と、落とした日時・場所を書いてください。"
)


# --- 指揮役 ---


def handler(event: dict[str, Any], context: object) -> None:
    """line-inquiry-webhook から非同期で呼ばれる入口。event は LINE のイベント1つ。"""
    if is_message_from_user(event, "text"):
        accept_inquiry(
            message_id=event["message"]["id"],
            user_id=event["source"]["userId"],
            text=event["message"]["text"],
            reply_token=event["replyToken"],
        )
    elif is_message_from_user(event):
        answer(event["source"]["userId"], event["replyToken"], TEXT_ONLY_MESSAGE)
    else:
        logger.info(
            "ignored: type=%s source=%s", event.get("type"), event.get("source", {}).get("type")
        )


def accept_inquiry(message_id: str, user_id: str, text: str, reply_token: str) -> None:
    """1対1のトークで届いた文字を、問い合わせとして受け付ける。"""
    if len(text) > MAX_LENGTH:  # 1. 長すぎないか
        answer(user_id, reply_token, TOO_LONG_MESSAGE)
        return
    if count_recent(user_id) >= DAILY_LIMIT:  # 2. 直近24時間の件数が上限に達していないか
        logger.info("rate limited: %s", user_id)
        answer(user_id, reply_token, LIMIT_MESSAGE)
        return

    receipt_no = insert_request(
        message_id, user_id, text
    )  # 3. requests に行を作り、受付番号をもらう
    if receipt_no is None:
        # 同じメッセージを前に受け付けている（やり直し）。返事も知らせも済んでいるので、何もしない
        logger.info("already accepted: message %s", message_id)
        return

    receipt = format_receipt(receipt_no)
    answer(user_id, reply_token, received_message(receipt))  # 4. お客さまに受付番号を返す
    logger.info("accepted inquiry %s from %s", receipt, user_id)


# --- 部品 ---


def is_message_from_user(event: dict[str, Any], message_type: str | None = None) -> bool:
    """1対1のトークでメッセージが届いたイベントか。message_type を渡したときは、その種類（"text" など）のときだけ True。

    source.type が "user" なら1対1のトーク（"group" ならグループ。問い合わせ用のボットはグループでは何もしない）。
    """
    message = event.get("message", {})
    return (
        event.get("type") == "message"
        and event.get("source", {}).get("type") == "user"
        and (message_type is None or message.get("type") == message_type)
    )


def format_receipt(receipt_no: int) -> str:
    """受付番号を、お客さまに見せる形にする（12 → "A-0012"）。

    頭の "A-" は問い合わせの番号であることを示す。4桁に満たない分は 0 で埋める（10000 からは5桁のまま）。
    """
    return f"A-{receipt_no:04d}"


def answer(user_id: str, reply_token: str, text: str) -> None:
    """お客さまに文字を送る。まず返事（reply・無料）で送り、できなければ push で送る。

    DB の起動待ちで1分後のやり直しになったときは、返事のトークンが切れていて reply が失敗する。
    受付番号はお客さまが後で問い合わせるのに要るので、その場合は push（無料の通数に数える）で確実に届ける。
    push も失敗したら、ログに残して終わる（受付は済んでいる。やり直すと二重に受け付けたことになるため）。
    """
    if line_common.reply(LINE_INQUIRY_ACCESS_TOKEN_ARN, reply_token, text):
        return
    try:
        line_common.push(LINE_INQUIRY_ACCESS_TOKEN_ARN, user_id, text)
    except urllib.error.URLError:  # HTTPError（LINE が 2xx 以外を返した）も含む
        logger.exception("push to user failed: %s", user_id)


# この LINE のユーザーが、直近24時間に送った問い合わせの件数。
# (line_user_id, created_at) の索引（マイグレーション 003）で、表が大きくなっても速く数えられる。
# 数えてから行を作るまでの間に同じ人の別のメッセージが来ると、6件目が入ることがある。厳密な上限ではなく、大量の送信を止めるためのもの
COUNT_RECENT_SQL = """
SELECT count(*)
FROM requests
WHERE line_user_id = :line_user_id
  AND created_at > now() - INTERVAL '1 day'
"""


def count_recent(user_id: str) -> int:
    """この LINE のユーザーが、直近24時間に送った問い合わせの件数を数える。"""
    result = line_common.execute_sql(
        COUNT_RECENT_SQL, [{"name": "line_user_id", "value": {"stringValue": user_id}}]
    )
    count: int = result["records"][0][0]["longValue"]
    return count


# requests に行を作り、DB が振った受付番号を返してもらう。
# ON CONFLICT (line_message_id) DO NOTHING：やり直しで同じメッセージがもう一度来たら、行を作らない（RETURNING は0行になる）
INSERT_REQUEST_SQL = """
INSERT INTO requests (description, line_user_id, line_message_id)
VALUES (:description, :line_user_id, :line_message_id)
ON CONFLICT (line_message_id) DO NOTHING
RETURNING receipt_no
"""


def insert_request(message_id: str, user_id: str, text: str) -> int | None:
    """requests に行を作り、受付番号を返す。同じメッセージを前に受け付けていたら None を返す。"""
    result = line_common.execute_sql(
        INSERT_REQUEST_SQL,
        [
            {"name": "description", "value": {"stringValue": text}},
            {"name": "line_user_id", "value": {"stringValue": user_id}},
            {"name": "line_message_id", "value": {"stringValue": message_id}},
        ],
    )
    records = result.get("records", [])
    if not records:
        return None
    receipt_no: int = records[0][0]["longValue"]
    return receipt_no
