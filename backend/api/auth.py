"""認証：リクエストしてきた人を、JWT の中身から作る。

■ 認証は API Gateway がやり、このアプリは結果を読むだけ
  JWT が本物か・期限内かは、API Gateway の JWT オーソライザーが Lambda を呼ぶ前に確かめる。
  通ったリクエストだけが届き、JWT の中身（クレーム）は event の requestContext に入ってくる。
  このアプリは JWT を自分で確かめず、その中身を信じて使う。

  信じてよい理由：この Lambda には API Gateway からしか届かない。
  Lambda に公開の URL は付けず、「呼んでよいのはこの API Gateway だけ」という許可（aws_lambda_permission）を
  Terraform で書く（8b-4）。逆に言えば、別の入口を開けたら、この前提は崩れる。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel


class User(BaseModel):
    """リクエストしてきた人。JWT の中身から作る。"""

    sub: str  # Cognito のユーザーID。items.registered_by に入れる値
    groups: list[str]  # 所属するグループ（staff / admin）。認可（8d）に使う


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
