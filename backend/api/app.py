"""API の Lambda。API Gateway（HTTP API）の後ろで動く FastAPI のアプリ。

■ 認証は API Gateway がやり、このアプリは結果を読むだけ
  JWT が本物か・期限内かは、API Gateway の JWT オーソライザーが Lambda を呼ぶ前に確かめる。
  通ったリクエストだけが届き、JWT の中身（クレーム）は event の requestContext に入ってくる。
  このアプリは JWT を自分で確かめず、その中身を信じて使う。

  信じてよい理由：この Lambda には API Gateway からしか届かない。
  Lambda に公開の URL は付けず、「呼んでよいのはこの API Gateway だけ」という許可（aws_lambda_permission）を
  Terraform で書く（8b-4）。逆に言えば、別の入口を開けたら、この前提は崩れる。

■ Mangum
  Lambda に届く event（dict）を、FastAPI が分かる HTTP のリクエストに変換するアダプタ。
  変換前の event そのものは request.scope["aws.event"] に残してくれる。

■ 書き方
  ルートの関数が指揮役。部品（read_〜・is_〜・make_〜・insert_〜）は値を返すだけで、例外を投げない。
  401・503 などの判断は、指揮役のガード節（if ...: raise）に並べる。
"""

# 型注釈を実行時に評価しない（TYPE_CHECKING の中でしか import しない型を、注釈に書けるようにするため）
from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

import boto3
from botocore.config import Config
from fastapi import Depends, FastAPI, HTTPException, Request
from mangum import Mangum
from pydantic import BaseModel

# boto3 の型（開発用。Lambda には入っていないので、検査のときだけ読む。handler.py と同じ）
if TYPE_CHECKING:
    from types_boto3_rds_data.type_defs import (
        ExecuteStatementResponseTypeDef,
        SqlParameterTypeDef,
    )

app = FastAPI(title="photo-intake API")

# Lambda では Terraform が環境変数で渡す。ローカル（uvicorn）では空のまま起動できるようにしておく
# （/health と /docs はローカルでも動く。/items は AWS が要るのでローカルでは動かない）。
DB_CLUSTER_ARN = os.environ.get("DB_CLUSTER_ARN", "")
DB_SECRET_ARN = os.environ.get("DB_SECRET_ARN", "")
DB_NAME = os.environ.get("DB_NAME", "")
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")  # Lambda が自動で入れる

# クライアントは関数の外で1回だけ作る（worker と同じ。実行環境が使い回されるので、2回目以降は作り直さない）
rds_data = boto3.client("rds-data", region_name=REGION)
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


class User(BaseModel):
    """リクエストしてきた人。JWT の中身から作る。"""

    sub: str  # Cognito のユーザーID。items.registered_by に入れる値
    groups: list[str]  # 所属するグループ（staff / admin）。認可（8d）に使う


# ===========================================================================
# 認証：リクエストしてきた人
# ===========================================================================

# --- 指揮役 ---


def get_current_user(request: Request) -> User:
    """JWT の中身から、リクエストしてきた人を作る。

    ルートの関数の引数に Annotated[User, Depends(get_current_user)] と書くと、
    FastAPI がルートの関数より先にこれを呼んで、結果を渡してくれる（依存性の注入）。
    """
    claims = read_claims(request)
    if claims is None:
        raise HTTPException(
            status_code=401, detail="認証されていない（API Gateway を通っていない）"
        )
    if not is_access_token(claims):
        raise HTTPException(status_code=401, detail="アクセストークンを送ってください")
    return User(sub=claims["sub"], groups=parse_groups(claims.get("cognito:groups", "")))


# --- 部品 ---


def read_claims(request: Request) -> dict[str, Any] | None:
    """API Gateway が確かめ済みの JWT の中身を、event から取り出す。無ければ None。

    event の形（HTTP API の JWT オーソライザーを通った場合）:
        {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "...", ...}}}}}

    ローカルで動かしたとき（API Gateway が前にいない）や、認証なしのルートから呼ばれたときは None になる。
    """
    event = request.scope.get("aws.event")
    try:
        claims: dict[str, Any] = event["requestContext"]["authorizer"]["jwt"]["claims"]  # type: ignore[index]
    except (TypeError, KeyError):
        return None
    return claims


def is_access_token(claims: dict[str, Any]) -> bool:
    """アクセストークンなら True、それ以外（ID トークンなど）なら False。

    API Gateway のオーソライザーは iss・aud（または client_id）・署名・期限しか見ないので、
    ID トークン（aud がアプリの ID）でも通してしまう。API が受け付けるのはアクセストークンだけにする。
    ID トークンは画面が「誰がログインしたか」を知るためのもので、API に送るものではない。
    """
    return claims.get("token_use") == "access"


def parse_groups(raw: str) -> list[str]:
    """cognito:groups を一覧にする。

    JWT の中では ["staff", "admin"] という配列だが、HTTP API のオーソライザーを通ると
    "[staff admin]" という1本の文字列に変わって届く（API Gateway の仕様）。
    角かっこを外して、空白で区切る。グループに入っていなければ "" で、空の一覧になる。
    """
    return raw.strip("[]").split()


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


@app.post("/items", status_code=201)
def create_item(new_item: NewItem, user: Annotated[User, Depends(get_current_user)]) -> CreatedItem:
    """拾得物を登録する。行を作ってから（analyzing）、写真を置くための署名付きURLを返す。

    順番が大事：行を先に作る。URL を先に渡すと、写真が先に届いて worker が動いたとき、
    行がまだ無いことがある（worker は行が無い写真を処理しない）。
    """
    s3_key = make_upload_key(new_item.content_type)
    item_id = insert_item(s3_key, user.sub, new_item)
    if item_id is None:
        raise HTTPException(
            status_code=503,
            detail="DB が起動中です。数秒後にやり直してください",
            headers={"Retry-After": "5"},  # 何秒後にやり直せばよいか（HTTP の決まりのヘッダー）
        )
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


def insert_item(s3_key: str, registered_by: str, new_item: NewItem) -> str | None:
    """items に行を作り（status は既定の analyzing）、その ID を返す。DB が起きなければ None。"""
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
    if response is None:
        return None
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


# --- 共通の下回り：Data API ---

# Aurora は5分使われないと一時停止し、起きるまで十数秒かかる。
# その間に SQL を送ると DatabaseResumingException が返るので、5秒おきに送り直す（最大 5秒 × 4回 = 20秒）。
# Lambda のタイムアウト（28秒）と API Gateway の打ち切り（30秒）より手前で諦めて、503 を返す。
DB_RESUME_WAIT_SECONDS = 5
DB_RESUME_MAX_ATTEMPTS = 5


def execute_sql(
    sql: str, parameters: list[SqlParameterTypeDef]
) -> ExecuteStatementResponseTypeDef | None:
    """Data API で SQL を1本送る。DB が起きる途中なら待って送り直し、それでも起きなければ None。"""
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
            if attempt < DB_RESUME_MAX_ATTEMPTS:
                time.sleep(DB_RESUME_WAIT_SECONDS)
    return None


# ===========================================================================
# ルート（認証・登録以外）
# ===========================================================================


@app.get("/health")
def health() -> dict[str, str]:
    """生きているかを返すだけ。API Gateway では認証なしのルートにする（8b-4）。"""
    return {"status": "ok"}


@app.get("/me")
def me(user: Annotated[User, Depends(get_current_user)]) -> User:
    """リクエストしてきた人を、そのまま返す。認証の通り道を確かめるためのルート。"""
    return user


# Lambda のエントリポイント（Terraform で handler = "app.handler" と指定する）。
# lifespan="off"：アプリの起動・終了時の処理（DB の接続を開く等）を使っていないので、Mangum に探させない。
handler = Mangum(app, lifespan="off")
