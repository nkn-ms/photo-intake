"""拾得物（items）を扱う API のルートをまとめたファイル。

登録 … POST /items（8c）
一覧 … GET /items（8d-1）
詳細 … GET /items/{item_id}（8d-1）
確定 … POST /items/{item_id}/confirm（8d-2。admin だけ）
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import boto3
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException
from pydantic import AfterValidator, BaseModel, Field

from auth import User, get_current_user, require_admin
from db import execute_sql, select_rows, text_or_null

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
    """登録（POST /items）のときに、画面から受け取る値（リクエストの本文）。"""

    content_type: ContentType  # これから PUT する写真の形式
    location: str | None = None  # 拾得場所（任意。送られなければ None）
    # 拾得日時（任意）。省略したら登録した時刻にする。保管期限（＋3か月）を数える起点
    registered_at: datetime | None = None


class CreatedItem(BaseModel):
    """登録（POST /items）の結果として、画面に返す値（レスポンスの本文）。"""

    item_id: str  # 作った items の行の ID（UUID）
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
INSERT INTO items (s3_key, registered_by, registered_at, location)
VALUES (:s3_key, :registered_by, CAST(:registered_at AS TIMESTAMPTZ), :location)
RETURNING id
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
            {"name": "location", "value": text_or_null(new_item.location)},  # 任意。無ければ NULL
        ],
    )
    # RETURNING id で返ってきた値を取り出す。結果の1行目の1列目が、作った行の ID
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
    """一覧（GET /items）で返す1件分の値。

    どの拾得物かがひと目で分かるだけの列に絞っている（すべての列は詳細の ItemDetail で返す）。
    """

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
    """拾得物の一覧を返す。拾得日時の新しい順。?status=pending_review のように状態で絞れる。"""
    rows = select_items(status)
    return [ItemSummary.model_validate(row) for row in rows]


# --- 部品 ---

# ai_result->>'category'：jsonb の中の "category" を文字列で取り出す（PostgreSQL の書き方）。
# CAST(:status AS TEXT) IS NULL：絞り込みが無ければ（null なら）全件。
#   CAST が無いと、PostgreSQL が「null の型が分からない」と言って失敗する。
SELECT_ITEMS_SQL = f"""
SELECT id, status, category, ai_result->>'category' AS ai_category,
       location, registered_at, created_at
FROM items
WHERE CAST(:status AS TEXT) IS NULL OR status = CAST(:status AS TEXT)
ORDER BY registered_at DESC
LIMIT {LIST_LIMIT}
"""


def select_items(status: str | None) -> list[dict[str, Any]]:
    """一覧に出す行を DB から読む。

    status が None なら全件、値があればその状態の行だけ。拾得日時の新しい順に、最大 LIST_LIMIT 件。
    """
    return select_rows(SELECT_ITEMS_SQL, [{"name": "status", "value": text_or_null(status)}])


# ===========================================================================
# 詳細：GET /items/{item_id}
# ===========================================================================


class ItemDetail(BaseModel):
    """詳細（GET /items/{item_id}）と、確定の後に返す値。

    AI の答え（ai_〜）と、人が確定した値（category など）を分けたまま返す。
    """

    id: str
    status: ItemStatus
    photo_url: str  # 写真を見るための署名付きURL（GET）
    registered_at: UtcDatetime
    location: str | None
    registered_by: str  # 登録した人の sub（Cognito のユーザー ID）

    # ▼ AI の答え。人が直しても上書きしない（ここと人の値の差が「AI の間違い」）
    ai_result: dict[str, Any] | None
    ai_model: str | None
    ai_analyzed_at: UtcDatetime | None
    ai_error: str | None  # analysis_failed の理由

    # ▼ 人が確定した値（8d-2 の確定で入る）
    category: str | None
    color: str | None
    brand: str | None
    features: list[str] | None  # AI の features と同じ配列の形（002 で jsonb に変えた）
    confirmed_by: str | None
    confirmed_at: UtcDatetime | None
    retention_deadline: date | None  # 保管期限（拾得日 + 3か月）

    # ▼ 確定の後の手続き（まだ書き込む処理が無いので、今はいつも null）
    police_reported_at: UtcDatetime | None  # 警察に届け出た日時
    # 落とし主の問い合わせ（遺失届）と結び付いたときの、その ID（ステップ10）
    matched_request_id: str | None
    matched_at: UtcDatetime | None  # 結び付けた日時
    created_at: UtcDatetime
    updated_at: UtcDatetime


# --- 指揮役 ---


@router.get("/items/{item_id}", dependencies=[Depends(get_current_user)])
def get_item(item_id: uuid.UUID) -> ItemDetail:
    """拾得物1件の詳細を返す。写真を見るための署名付きURLも付ける。

    item_id を uuid.UUID 型にしているので、UUID の形でなければ FastAPI が 422 で弾く（DB まで行かない）。
    """
    row = select_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="その拾得物は見つかりません")
    return to_item_detail(row)


# --- 部品 ---

# embedding（1024個の数字）は画面で使わないので読まない
SELECT_ITEM_SQL = """
SELECT id, status, s3_key, registered_at, location, registered_by,
       ai_result, ai_model, ai_analyzed_at, ai_error,
       category, color, brand, features, confirmed_by, confirmed_at, retention_deadline,
       police_reported_at, matched_request_id, matched_at, created_at, updated_at
FROM items
WHERE id = CAST(:id AS UUID)
"""


def select_item(item_id: uuid.UUID) -> dict[str, Any] | None:
    """1件の行を読む。無ければ None（→ 指揮役が 404）。詳細と確定で使う。

    None は「調べた結果、無い」だけを意味する（DB が起きないときは DatabaseResumingError が飛ぶ）。
    """
    rows = select_rows(SELECT_ITEM_SQL, [{"name": "id", "value": {"stringValue": str(item_id)}}])
    return rows[0] if rows else None


def to_item_detail(row: dict[str, Any]) -> ItemDetail:
    """DB の行を、詳細の返り値（ItemDetail）にする。詳細と確定の両方で使う共通の処理。

    jsonb の列（ai_result・features）は Data API から JSON の文字列で届くので、辞書・一覧に戻す。
    写真を見るための署名付きURLもここで付ける。ItemDetail に無い列（s3_key）は Pydantic が捨てる。
    """
    return ItemDetail.model_validate(
        {
            **row,
            "ai_result": parse_jsonb(row["ai_result"]),
            "features": parse_jsonb(row["features"]),
            "photo_url": make_photo_url(row["s3_key"]),
        }
    )


def parse_jsonb(raw: str | None) -> Any:
    """jsonb の列を Python の値（辞書・一覧）にする。Data API は jsonb を JSON の文字列のまま返すため。NULL は None。"""
    return json.loads(raw) if raw is not None else None


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


# ===========================================================================
# 確定：POST /items/{item_id}/confirm（admin だけ）
# ===========================================================================

# 確定できる状態。pending_review（AI の答えを人が直す）と、analysis_failed（AI が失敗した写真に人が手で入力する）
CONFIRMABLE_STATUSES = ("pending_review", "analysis_failed")


class Confirmation(BaseModel):
    """確定のときに受け取るもの（admin が確かめた値）。AI の答え（ai_result）には触らない。"""

    category: str = Field(min_length=1)  # 種別は必須。空の文字列も 422
    color: str | None = None
    brand: str | None = None
    features: list[str] | None = None  # ["二つ折り", "革製"]。AI の features と同じ形


# --- 指揮役 ---


@router.post("/items/{item_id}/confirm")
def confirm_item(
    item_id: uuid.UUID,
    confirmation: Confirmation,
    user: Annotated[User, Depends(require_admin)],  # admin でなければ 403（auth.py）
) -> ItemDetail:
    """admin が内容を確定する。status を confirmed にし、人の値・確定した人・保管期限を入れる。"""
    row = select_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="その拾得物は見つかりません")
    if not is_confirmable(row["status"]):
        raise HTTPException(
            status_code=409, detail=f"状態が {row['status']} の拾得物は確定できません"
        )

    updated = update_confirmed(item_id, user.sub, confirmation)  # 書き込む
    if not updated:
        # 上で確かめてから書き込むまでの間に、ほかの人が先に確定した（admin が2人同時に押した、など）
        raise HTTPException(
            status_code=409, detail="ほかの人が先に状態を変えました。読み直してください"
        )
    return get_item(item_id)  # 確定した後の行を、詳細と同じ形で返す（画面はこれで表示を更新できる）


# --- 部品 ---


def is_confirmable(status: str) -> bool:
    """その状態の拾得物を確定できるなら True（pending_review・analysis_failed のとき）。"""
    return status in CONFIRMABLE_STATUSES


# retention_deadline（保管期限）：拾得日 + 3か月。拾得日は日本時間で数える
#   （UTC のままだと、日本時間の朝9時より前に拾った物の日付が前日になる）。
# WHERE の status の条件：確かめてから書き込むまでの間に状態が変わっていたら、1行も書き換えない。
#   読んで・確かめて・書く、の隙間で起きる取り合いを、DB の側で止める書き方。
CONFIRMABLE_SQL_LIST = ", ".join(f"'{status}'" for status in CONFIRMABLE_STATUSES)
UPDATE_CONFIRMED_SQL = f"""
UPDATE items SET
  status             = 'confirmed',
  category           = :category,
  color              = :color,
  brand              = :brand,
  features           = CAST(:features AS JSONB),
  confirmed_by       = :confirmed_by,
  confirmed_at       = now(),
  retention_deadline = CAST(CAST(registered_at AT TIME ZONE 'Asia/Tokyo' AS DATE) + INTERVAL '3 months' AS DATE),
  updated_at         = now()
WHERE id = CAST(:id AS UUID)
  AND status IN ({CONFIRMABLE_SQL_LIST})
"""


def update_confirmed(item_id: uuid.UUID, confirmed_by: str, confirmation: Confirmation) -> bool:
    """確定の内容を書き込む。書き換えられたら True、状態が変わっていて1行も書き換えなかったら False。"""
    features = (
        json.dumps(confirmation.features, ensure_ascii=False)
        if confirmation.features is not None
        else None
    )
    response = execute_sql(
        UPDATE_CONFIRMED_SQL,
        [
            {"name": "id", "value": {"stringValue": str(item_id)}},
            {"name": "category", "value": {"stringValue": confirmation.category}},
            {"name": "color", "value": text_or_null(confirmation.color)},
            {"name": "brand", "value": text_or_null(confirmation.brand)},
            {"name": "features", "value": text_or_null(features)},
            {"name": "confirmed_by", "value": {"stringValue": confirmed_by}},
        ],
    )
    return response["numberOfRecordsUpdated"] == 1
