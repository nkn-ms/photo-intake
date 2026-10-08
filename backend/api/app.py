"""API の Lambda。API Gateway（HTTP API）の後ろで動く FastAPI のアプリ。

■ ファイルの分け方
  app.py（ここ）   … 入口。アプリを作り、ルーターをつなぎ、Lambda のエントリポイントを置く
  auth.py          … 認証。JWT の中身からリクエストしてきた人を作る
  db.py            … SQL を送る共通の処理（Data API・DB の起動待ち）
  routers/items.py … /items のルート

■ Mangum
  Lambda に届く event（dict）を、FastAPI が分かる HTTP のリクエストに変換するアダプタ。
  変換前の event そのものは request.scope["aws.event"] に残してくれる。

■ 書き方
  ルートの関数が指揮役。部品（read_〜・is_〜・make_〜・insert_〜）は値を返すだけで、例外を投げない。
  401・503 などの判断は、指揮役のガード節（if ...: raise）に並べる。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from mangum import Mangum

from auth import User, get_current_user
from routers import items

app = FastAPI(title="photo-intake API")
app.include_router(items.router)


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
