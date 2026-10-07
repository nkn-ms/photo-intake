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
"""

from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from mangum import Mangum
from pydantic import BaseModel

app = FastAPI(title="photo-intake API")


class User(BaseModel):
    """リクエストしてきた人。JWT の中身から作る。"""

    sub: str  # Cognito のユーザーID。items.registered_by に入れる値
    groups: list[str]  # 所属するグループ（staff / admin）。認可（8d）に使う


# --- 指揮役：リクエストしてきた人を返す ---


def get_current_user(request: Request) -> User:
    """JWT の中身から、リクエストしてきた人を作る。

    ルートの関数の引数に Annotated[User, Depends(get_current_user)] と書くと、
    FastAPI がルートの関数より先にこれを呼んで、結果を渡してくれる（依存性の注入）。
    """
    claims = read_claims(request)
    return User(sub=claims["sub"], groups=parse_groups(claims.get("cognito:groups", "")))


# --- 部品 ---


def read_claims(request: Request) -> dict[str, Any]:
    """API Gateway が確かめ済みの JWT の中身を、event から取り出す。

    event の形（HTTP API の JWT オーソライザーを通った場合）:
        {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "...", ...}}}}}

    無ければ 401。ローカルで動かしたとき（API Gateway が前にいない）は、ここに来る。
    """
    event = request.scope.get("aws.event")
    try:
        claims: dict[str, Any] = event["requestContext"]["authorizer"]["jwt"]["claims"]  # type: ignore[index]
    except (TypeError, KeyError):
        raise HTTPException(
            status_code=401, detail="認証されていない（API Gateway を通っていない）"
        )
    return claims


def parse_groups(raw: str) -> list[str]:
    """cognito:groups を一覧にする。

    JWT の中では ["staff", "admin"] という配列だが、HTTP API のオーソライザーを通ると
    "[staff admin]" という1本の文字列に変わって届く（API Gateway の仕様）。
    角かっこを外して、空白で区切る。グループに入っていなければ "" で、空の一覧になる。
    """
    return raw.strip("[]").split()


# --- ルート ---


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
