"""LINE のイベントを処理する Lambda: line-intake（ステップ11）。

line-webhook（webhook.py）が署名を確かめたあと、LINE のイベントを1つずつ、非同期でここに渡してくる。
どのイベントをどう扱うかは、ここで決める:
  ・ボットがグループに招待された … グループ ID をログに出す（人が terraform.tfvars の line_group_id に写す）。
                                   落とし物グループが決まった後なら、それ以外のグループからは退出する
                                   （ボットが落とし物グループにしか入っていない状態を保つため）
  ・1対1のトークで写真が届いた   … 送り主が落とし物グループのメンバーか確かめる → 写真を LINE から取ってくる →
                                   「受け付けました」と返事する → items に行を作る → S3 の uploads/ に置く
                                   （ここから先は、画面から登録したときと同じ S3 → SQS → worker）
  ・それ以外                       … 何もしない

失敗したとき（LINE・S3・DB が一時的に応えない）は例外のまま終わる。
非同期で呼ばれているので、Lambda が1分後・さらに2分後にやり直す（line.tf の aws_lambda_function_event_invoke_config）。
やり直しても二重に登録しないように、S3 のキーは LINE のメッセージ ID から決め、items には ON CONFLICT DO NOTHING で入れる。

LINE の API は、worker が Claude を呼ぶのと同じく、Python の標準の urllib で呼ぶ（LINE の SDK は使わない）。
使う API が少なく、ファイル1つを zip にするだけで置けるため。
"""

# 注釈（: や -> の後ろ）を実行時に評価しない。下の TYPE_CHECKING の中でしか import しない型を、注釈に書けるようにするため（worker と同じ）
from __future__ import annotations

import functools
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3

# boto3 の型（types-boto3）は開発用のパッケージで Lambda には入っていないので、mypy が検査するときだけ import する（worker と同じ）
if TYPE_CHECKING:
    from types_boto3_rds_data.type_defs import (
        ExecuteStatementResponseTypeDef,
        SqlParameterTypeDef,
    )

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

secrets = boto3.client("secretsmanager")  # LINE のアクセストークンを読む
rds_data = boto3.client("rds-data")  # Data API（HTTPS で SQL を送る）
s3 = boto3.client("s3")  # 写真を置く

# 以下は line.tf が環境変数で渡す
LINE_CHANNEL_ACCESS_TOKEN_ARN = os.environ["LINE_CHANNEL_ACCESS_TOKEN_ARN"]
LINE_GROUP_ID = os.environ.get(
    "LINE_GROUP_ID", ""
)  # 落とし物グループ。空の間は、だれの写真も登録しない
DB_CLUSTER_ARN = os.environ["DB_CLUSTER_ARN"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
DB_NAME = os.environ["DB_NAME"]
UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]

LINE_API = "https://api.line.me/v2/bot"
LINE_DATA_API = "https://api-data.line.me/v2/bot"  # 写真などの中身は、別のドメインから取る
LINE_TIMEOUT_SECONDS = 10

# LINE から返ってくる写真の形式 → S3 のキーの拡張子。worker（handler.py の IMAGE_FORMATS）が読める形だけ。
# LINE のアプリから送った写真は、ふつう JPEG になって届く
EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


# --- 指揮役 ---


def handler(event: dict[str, Any], context: object) -> None:
    """line-webhook から非同期で呼ばれる入口。event は LINE のイベント1つ（webhook の本文の events の1要素）。"""
    source = event.get("source", {})

    if event.get("type") == "join" and source.get("type") == "group":
        on_join(source["groupId"])
    elif is_photo_from_user(event):
        register_photo(
            message_id=event["message"]["id"],
            user_id=source["userId"],
            sent_at_ms=event["timestamp"],
            reply_token=event["replyToken"],
        )
    else:
        logger.info("ignored: type=%s source=%s", event.get("type"), source.get("type"))


def on_join(group_id: str) -> None:
    """ボットがグループに招待されたとき。"""
    # 落とし物グループを決めるときは、ここに出た ID を terraform.tfvars の line_group_id に写す
    logger.info("joined group: %s", group_id)

    # ボットが落とし物グループにしか入っていない状態を保つ。
    # ボットを友だちに追加した人なら、だれでも自分のグループにボットを招待できる。友だちになるのは:
    #   ・スタッフ全員（写真を1対1のトークで送るには、ボットを友だちに追加する必要がある）。
    #     悪意が無くても、家族のグループなどに間違えて招待することがある
    #   ・ボットの ID や QR コードを知った人（公式アカウントは、友だちの追加を断れない）
    # LINE には「このグループにだけ入れる」という設定が無い（設定できるのは、全部のグループへの招待を断ることだけ）。
    # だから、落とし物グループが決まった後に、それ以外のグループに招待されたら、ここで退出する。
    # 退出しなくても、知らせは落とし物グループにしか送らず、写真もそのメンバーからしか登録しないので、データは漏れない。
    # 退出するのは、知らない人のグループにボットが並ぶことと、そこでの発言のたびに Lambda が動く無駄を避けるため。
    if LINE_GROUP_ID and group_id != LINE_GROUP_ID:
        leave_group(group_id)
        logger.info("left group (not the lost-and-found group): %s", group_id)


def register_photo(message_id: str, user_id: str, sent_at_ms: int, reply_token: str) -> None:
    """1対1のトークで届いた写真を、拾得物として登録する。"""
    if not is_group_member(user_id):  # 1. 落とし物グループのメンバーか
        logger.info("ignored photo from non-member: %s", user_id)
        return

    image, content_type = fetch_photo(message_id)  # 2. LINE から写真を取ってくる
    extension = EXTENSIONS.get(content_type)
    if extension is None:
        logger.warning(
            "ignored photo: unsupported content type %s (message %s)", content_type, message_id
        )
        return

    reply(reply_token, RECEIVED_MESSAGE)  # 3. 送った人に「受け付けました」と返事する

    # 4. 置き場所を決める。メッセージ ID から決めるので、やり直しても同じキーになる
    s3_key = f"uploads/line-{message_id}{extension}"
    insert_item(s3_key, f"line:{user_id}", sent_at_ms)  # 5. items に行を作る（status は analyzing）
    put_photo(s3_key, image, content_type)  # 6. S3 に置く → ここから先は worker
    logger.info("registered photo: %s from %s", s3_key, user_id)


# --- 部品 ---


def is_photo_from_user(event: dict[str, Any]) -> bool:
    """1対1のトークで写真が送られてきたイベントか。

    source.type が "user" なら1対1のトーク（"group" ならグループ。グループに送られた写真は登録しない）。
    contentProvider.type が "line" なら、写真の中身は LINE のサーバーにあり、こちらから取ってこられる
    （LINE のアプリから送った写真は必ず "line"。"external" は別のサーバーにある画像で、取ってくる先が違う）。
    """
    message = event.get("message", {})
    return (
        event.get("type") == "message"
        and event.get("source", {}).get("type") == "user"
        and message.get("type") == "image"
        and message.get("contentProvider", {}).get("type") == "line"
    )


def is_group_member(user_id: str) -> bool:
    """送り主が、落とし物グループのメンバーか。LINE に聞く。

    「グループのメンバーのプロフィールを取得する」API は、メンバーなら 200、メンバーでなければ 404 を返す
    （2026-10-19、未認証のアカウントで 200 と 404 が返ることを curl で確かめた）。
    """
    if not LINE_GROUP_ID:
        # 落とし物グループがまだ決まっていない。だれもメンバーではない扱いにする
        return False
    try:
        line_request("GET", f"{LINE_API}/group/{LINE_GROUP_ID}/member/{user_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise  # 404 以外（LINE の一時的な不調など）はやり直してもらう
    return True


def fetch_photo(message_id: str) -> tuple[bytes, str]:
    """LINE から写真の中身を取ってくる。中身のバイト列と、形式（Content-Type）を返す。"""
    with line_request("GET", f"{LINE_DATA_API}/message/{message_id}/content") as response:
        content_type: str = response.headers.get_content_type()  # "image/jpeg; ..." の ; より前だけ
        image: bytes = response.read()
    logger.info("fetched photo: message %s (%s, %d bytes)", message_id, content_type, len(image))
    return image, content_type


# 写真を受け付けたときの返事（送った人の1対1のトークにだけ出る）。この後に何が起きるかを伝える
# （グループへの知らせは、管理者が確定したときに送る）
RECEIVED_MESSAGE = "写真を受け付けました。管理者が確認後、落とし物グループにお知らせします。"


def reply(reply_token: str, text: str) -> None:
    """届いたメッセージに返事をする（送った人の1対1のトークに出る）。

    返事（reply）は、無料の通数（月200通）に数えられない。こちらから送る push とは違い、
    届いたイベントに付いてくる返事のトークン（replyToken）を使い、1つのトークンで1回だけ、受け取ってから短い時間だけ使える。
    だから DB に書く前（写真を取ってきた直後）に返す。DB の起動待ちで1分後のやり直しになると、トークンが切れているため。

    返事に失敗しても、登録は続ける（例外にしない）。返事は知らせるだけで、登録のほうが大事なため。
    やり直しの2回目では、トークンが使用済みか期限切れなので、ここは必ず失敗する（ログに残すだけ）。
    """
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    try:
        line_request("POST", f"{LINE_API}/message/reply", body).close()
    except urllib.error.HTTPError as e:
        logger.warning("reply failed (%d): %s", e.code, e.read()[:200])


def leave_group(group_id: str) -> None:
    """ボットがグループから退出する。"""
    line_request("POST", f"{LINE_API}/group/{group_id}/leave").close()


def line_request(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    """LINE の API にリクエストを1本送り、返事（urlopen の結果）を返す。2xx でなければ HTTPError になる。

    body を渡したときは、JSON にして本文に入れる（返事を送るときに使う）。
    """
    headers = {"Authorization": f"Bearer {channel_access_token()}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(request, timeout=LINE_TIMEOUT_SECONDS)


# items に行を作る。API（POST /items）の INSERT と同じ形で、拾得場所（location）は空のまま（画面で入れる）。
# ON CONFLICT (s3_key) DO NOTHING：同じ写真でやり直したとき、s3_key（UNIQUE）がぶつかるので何もしない
INSERT_ITEM_SQL = """
INSERT INTO items (s3_key, registered_by, registered_at)
VALUES (:s3_key, :registered_by, CAST(:registered_at AS TIMESTAMPTZ))
ON CONFLICT (s3_key) DO NOTHING
"""


def insert_item(s3_key: str, registered_by: str, sent_at_ms: int) -> None:
    """items に行を作る（status は既定の analyzing）。拾得日時は、LINE で写真が送られた時刻にする。"""
    registered_at = datetime.fromtimestamp(sent_at_ms / 1000, tz=UTC)
    execute_sql(
        INSERT_ITEM_SQL,
        [
            {"name": "s3_key", "value": {"stringValue": s3_key}},
            {"name": "registered_by", "value": {"stringValue": registered_by}},
            {"name": "registered_at", "value": {"stringValue": registered_at.isoformat()}},
        ],
    )


def put_photo(s3_key: str, image: bytes, content_type: str) -> None:
    """写真を S3 に置く。置いた瞬間に S3 → SQS → worker が動き出す。"""
    s3.put_object(Bucket=UPLOAD_BUCKET, Key=s3_key, Body=image, ContentType=content_type)


# DB が一時停止から起きる途中なら、起きるまで待って送り直す（worker の execute_sql と同じ）。
# 1分後の Lambda のやり直しに任せるより早く登録できる。
DB_RESUME_WAIT_SECONDS = 5  # 送り直すまでに待つ秒数
DB_RESUME_MAX_ATTEMPTS = 6  # SQL を送る回数の上限（1回目を含む。待つのは最大 5秒 × 5回 = 25秒）


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
                raise  # 25秒待っても起きなかった。Lambda のやり直し（1分後）に任せる
            logger.info(
                "DB is resuming, retry in %ds (%d/%d)",
                DB_RESUME_WAIT_SECONDS,
                attempt,
                DB_RESUME_MAX_ATTEMPTS,
            )
            time.sleep(DB_RESUME_WAIT_SECONDS)
    # ここには来ない（ループは必ず return か raise で抜ける）。mypy のために置いている（worker と同じ）
    raise AssertionError("unreachable")


# functools.cache：1回目に読んだ値を覚えておき、2回目からは Secrets Manager に聞かない（worker の API キーと同じ）
@functools.cache
def channel_access_token() -> str:
    """Secrets Manager から LINE のチャネルアクセストークンを読む。"""
    return secrets.get_secret_value(SecretId=LINE_CHANNEL_ACCESS_TOKEN_ARN)["SecretString"]
