"""拾得物（items）のルート。登録（8c）・一覧と詳細（8d-1）。確定は 8d-2 で足す。"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import boto3
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException
from pydantic import AfterValidator, BaseModel

from auth import User, get_current_user
from db import execute_sql, select_rows

# ルートの入れ物。app.py が app.include_router(items.router) でアプリにつなぐ
router = APIRouter()

UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")  # Lambda が自動で入れる

s3 = boto3.client(
    "s3",
    region_name=REGION,
    # 署名付きURLの宛先を、東京の窓口（bucket.s3.ap-northeast-1.amazonaws.com）にする。
    # 指定しないと地域の入っていない古い形の URL（bucket.s3.amazonaws.com）になり、
    # 作ったばかりのバケットだと別の窓口へ転送（307）されて、PUT が失敗することがある。
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(
        signature_version="s3v4",  # 東京の S3 は新しい方式（v4）の署名しか受け付けない
        s3={"addressing_style": "virtual"},  # バケット名を URL のホスト名に入れる形
    ),
)

# 署名付きURLの有効期限（秒）。この間に PUT しなければ、URL は使えなくなる
UPLOAD_URL_EXPIRES_SECONDS = 300
# 写真を見るための署名付きURLの有効期限（秒）。画面は詳細を開くたびに取り直す
PHOTO_URL_EXPIRES_SECONDS = 300

# items.status の値（001_init.sql の check 制約と同じ）。一覧の絞り込みで、これ以外は 422
ItemStatus = Literal[
    "analyzing",
    "analysis_failed",
    "pending_review",
    "confirmed",
    "matched",
    "closed",
    "archived",
]


def assume_utc(value: datetime) -> datetime:
    """時差の付いていない日時を UTC とみなす。

    Data API は timestamptz の列を "2026-10-08 11:19:43.865155" のように、UTC で・時差を付けずに返す。
    そのままだと「どこの時刻か分からない日時」になり、画面が日本時間と取り違えるので、UTC だと書き足す。
    レスポンスでは "2026-10-08T11:19:43.865155Z" になる（末尾の Z が UTC の印）。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# 日時の列にはこの型を使う。Pydantic が datetime に変換した後で、assume_utc を通す
UtcDatetime = Annotated[datetime, AfterValidator(assume_utc)]


# ===========================================================================
# 登録：POST /items
# ===========================================================================

# 受け付ける写真の形式。これ以外は FastAPI が 422 で弾く（未対応の形式を、worker まで流さない）。
# 拡張子は worker（handler.py の IMAGE_FORMATS）が読める形に合わせる。
ContentType = Literal["image/jpeg", "image/png", "image/webp", "image/gif"]
EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class NewItem(BaseModel):
    """登録のときに受け取るもの（リクエストの本文）。"""

    content_type: ContentType  # これから PUT する写真の形式
    location: str | None = None  # 拾得場所
    registered_at: datetime | None = None  # 拾得日時。省略したら今。保管期限（＋3か月）の起点


class CreatedItem(BaseModel):
    """登録の結果（レスポンスの本文）。"""

    item_id: str  # 作った行の ID
    upload_url: str  # 写真を PUT する先（署名付きURL）
    upload_headers: dict[
        str, str
    ]  # PUT するときに付けるヘッダー。署名に含まれているので、違うと S3 が拒否する
    expires_in: int  # upload_url が使える残り秒数


# --- 指揮役 ---


@router.post("/items", status_code=201)
def create_item(new_item: NewItem, user: Annotated[User, Depends(get_current_user)]) -> CreatedItem:
    """拾得物を登録する。行を作ってから（analyzing）、写真を置くための署名付きURLを返す。

    順番が大事：行を先に作る。URL を先に渡すと、写真が先に届いて worker が動いたとき、
    行がまだ無いことがある（worker は行が無い写真を処理しない）。
    """
    s3_key = make_upload_key(new_item.content_type)
    item_id = insert_item(s3_key, user.sub, new_item)
    upload_url = make_upload_url(s3_key, new_item.content_type)
    return CreatedItem(
        item_id=item_id,
        upload_url=upload_url,
        upload_headers={"Content-Type": new_item.content_type},
        expires_in=UPLOAD_URL_EXPIRES_SECONDS,
    )


# --- 部品 ---


def make_upload_key(content_type: str) -> str:
    """写真の置き場所（S3 のキー）を決める。毎回ランダムな ID なので、1つのキー ＝ 1回の登録になる。

    ファイル名は使わない。同じ名前の写真が2回登録されても、別のキーになる（6 で決めた冪等性の前提）。
    """
    return f"uploads/{uuid.uuid4()}{EXTENSIONS[content_type]}"


INSERT_ITEM_SQL = """
insert into items (s3_key, registered_by, registered_at, location)
values (:s3_key, :registered_by, cast(:registered_at as timestamptz), :location)
returning id
"""


def insert_item(s3_key: str, registered_by: str, new_item: NewItem) -> str:
    """items に行を作り（status は既定の analyzing）、その ID を返す。"""
    registered_at = new_item.registered_at or datetime.now(UTC)
    response = execute_sql(
        INSERT_ITEM_SQL,
        [
            {"name": "s3_key", "value": {"stringValue": s3_key}},
            {"name": "registered_by", "value": {"stringValue": registered_by}},
            # Data API に日時の型を直接渡すと書式の決まりが細かいので、文字列で渡して SQL の側で変換する
            {"name": "registered_at", "value": {"stringValue": registered_at.isoformat()}},
            # 拾得場所は任意。無ければ NULL を入れる
            {
                "name": "location",
                "value": {"stringValue": new_item.location}
                if new_item.location is not None
                else {"isNull": True},
            },
        ],
    )
    # returning id の結果。1行目の1列目
    item_id: str = response["records"][0][0]["stringValue"]
    return item_id


def make_upload_url(s3_key: str, content_type: str) -> str:
    """写真を PUT するための署名付きURLを作る。

    AWS には問い合わせず、この Lambda の実行ロールの権限で署名するだけ。
    PUT されたときに S3 が「署名した者（実行ロール）に PutObject の権限があるか」を確かめる。
    ContentType も署名に含めるので、違う形式で PUT すると S3 が拒否する。
    ※ PUT の署名付きURLでは、ファイルの大きさは制限できない（画面で縮小してから送る。ステップ9）。
    """
    url: str = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": UPLOAD_BUCKET, "Key": s3_key, "ContentType": content_type},
        ExpiresIn=UPLOAD_URL_EXPIRES_SECONDS,
        HttpMethod="PUT",
    )
    return url


# ===========================================================================
# 一覧：GET /items
# ===========================================================================

# 一度に返す件数の上限。ページをめくる仕組みは、画面（ステップ9）で要るようなら足す
LIST_LIMIT = 50


class ItemSummary(BaseModel):
    """一覧の1件。どれが何かがひと目で分かるだけの列。"""

    id: str
    status: ItemStatus
    category: str | None  # 人が確定した種別（未確定なら null）
    ai_category: str | None  # AI が答えた種別（解析前・失敗なら null）。人の値とは混ぜない
    location: str | None
    registered_at: UtcDatetime
    created_at: UtcDatetime


# --- 指揮役 ---


# dependencies=[...]：ログインしていることだけ確かめる（誰かという値はこのルートでは使わない）
@router.get("/items", dependencies=[Depends(get_current_user)])
def list_items(status: ItemStatus | None = None) -> list[ItemSummary]:
    """拾得物の一覧。拾得日時の新しい順。?status=pending_review のように状態で絞れる。"""
    rows = select_items(status)
    return [ItemSummary.model_validate(row) for row in rows]


# --- 部品 ---

# ai_result->>'category'：jsonb の中の "category" を文字列で取り出す（PostgreSQL の書き方）。
# cast(:status as text) is null：絞り込みが無ければ（null なら）全件。
#   cast が無いと、PostgreSQL が「null の型が分からない」と言って失敗する。
SELECT_ITEMS_SQL = f"""
select id, status, category, ai_result->>'category' as ai_category,
       location, registered_at, created_at
from items
where cast(:status as text) is null or status = cast(:status as text)
order by registered_at desc
limit {LIST_LIMIT}
"""


def select_items(status: str | None) -> list[dict[str, Any]]:
    """一覧の行を読む。"""
    return select_rows(
        SELECT_ITEMS_SQL,
        [
            {
                "name": "status",
                "value": {"stringValue": status} if status is not None else {"isNull": True},
            }
        ],
    )


# ===========================================================================
# 詳細：GET /items/{item_id}
# ===========================================================================


class ItemDetail(BaseModel):
    """詳細。AI の答え（ai_〜）と、人が確定した値（category など）を分けたまま返す。"""

    id: str
    status: ItemStatus
    photo_url: str  # 写真を見るための署名付きURL（GET）
    registered_at: UtcDatetime
    location: str | None
    registered_by: str  # 登録した人の sub

    # ▼ AI の答え。人が直しても上書きしない（ここと人の値の差が「AI の間違い」）
    ai_result: dict[str, Any] | None
    ai_model: str | None
    ai_analyzed_at: UtcDatetime | None
    ai_error: str | None  # analysis_failed の理由

    # ▼ 人が確定した値（8d-2 の確定で入る）
    category: str | None
    color: str | None
    brand: str | None
    features: str | None
    confirmed_by: str | None
    confirmed_at: UtcDatetime | None
    retention_deadline: date | None  # 保管期限（拾得日 + 3か月）

    police_reported_at: UtcDatetime | None
    matched_request_id: str | None
    matched_at: UtcDatetime | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


# --- 指揮役 ---


@router.get("/items/{item_id}", dependencies=[Depends(get_current_user)])
def get_item(item_id: uuid.UUID) -> ItemDetail:
    """拾得物の詳細。写真を見るための署名付きURLを付けて返す。

    item_id を uuid.UUID 型にしているので、UUID の形でなければ FastAPI が 422 で弾く（DB まで行かない）。
    """
    rows = select_item(item_id)
    if not rows:
        raise HTTPException(status_code=404, detail="その拾得物は見つかりません")
    row = rows[0]
    ai_result = parse_ai_result(row["ai_result"])
    photo_url = make_photo_url(row["s3_key"])
    # 読んだ列に、加工した2つを上書きして渡す。ItemDetail に無い列（s3_key）は Pydantic が捨てる
    return ItemDetail.model_validate({**row, "ai_result": ai_result, "photo_url": photo_url})


# --- 部品 ---

# embedding（1024個の数字）は画面で使わないので読まない
SELECT_ITEM_SQL = """
select id, status, s3_key, registered_at, location, registered_by,
       ai_result, ai_model, ai_analyzed_at, ai_error,
       category, color, brand, features, confirmed_by, confirmed_at, retention_deadline,
       police_reported_at, matched_request_id, matched_at, created_at, updated_at
from items
where id = cast(:id as uuid)
"""


def select_item(item_id: uuid.UUID) -> list[dict[str, Any]]:
    """1件の行を読む。見つかれば1行、無ければ空の一覧（→ 指揮役が 404）。"""
    return select_rows(SELECT_ITEM_SQL, [{"name": "id", "value": {"stringValue": str(item_id)}}])


def parse_ai_result(raw: str | None) -> dict[str, Any] | None:
    """ai_result（jsonb）を辞書にする。Data API は jsonb を JSON の文字列のまま返すため。解析前は None。"""
    if raw is None:
        return None
    ai_result: dict[str, Any] = json.loads(raw)
    return ai_result


def make_photo_url(s3_key: str) -> str:
    """写真を見るための署名付きURL（GET）を作る。make_upload_url の GET 版。

    この Lambda の実行ロールの GetObject の権限を、URL を持つ人に5分だけ貸し出す。
    """
    url: str = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": UPLOAD_BUCKET, "Key": s3_key},
        ExpiresIn=PHOTO_URL_EXPIRES_SECONDS,
    )
    return url
