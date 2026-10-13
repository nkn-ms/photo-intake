"""API の Lambda。API Gateway（HTTP API）の後ろで動く FastAPI のアプリ。

■ ファイルの分け方
  app.py（ここ）   … 入口。アプリを作り、ルーターをつなぎ、Lambda のエントリポイントを置く
  auth.py          … 認証。JWT の中身からリクエストしてきた人を作る
  db.py            … SQL を送る共通の処理（Data API）
  routers/items.py … /items のルート

■ Mangum
  Lambda に届く event（dict）を、FastAPI が分かる HTTP のリクエストに変換するアダプタ。
  変換前の event そのものは request.scope["aws.event"] に残してくれる。

■ 書き方
  ルートの関数が指揮役。部品（read_〜・is_〜・make_〜・insert_〜）は値を返すだけで、例外を投げない。
  401・404 などの判断は、指揮役のガード節（if ...: raise）に並べる。

  例外：DB が起動中のとき（503）だけは、db.py が DatabaseResumingError を投げ、
  下の例外ハンドラーが受け取る。どのルートでも同じ 503 なので、ルートごとには書かない。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mangum import Mangum

from auth import User, get_current_user
from db import DatabaseResumingError
from routers import items

app = FastAPI(title="photo-intake API")
app.include_router(items.router)


# 例外ハンドラー：どのルートで DatabaseResumingError が投げられても、ここで受け取って 503 にする。
# ルートの関数がこの例外を捕まえなければ、FastAPI がここに渡してくれる。
@app.exception_handler(DatabaseResumingError)
async def db_resuming(request: Request, exc: DatabaseResumingError) -> JSONResponse:
    """DB（Aurora）が一時停止から起動している途中のときに呼ばれる。503 を返し、数秒後にやり直してもらう。

    やり直すのは画面（frontend/src/api.ts）。Retry-After の秒数だけ待ってから、同じリクエストを出し直す。

    async def は FastAPI の例に合わせただけ（中で何も待たないので、動きは普通の関数と同じ）。
    """
    return JSONResponse(
        status_code=503,
        content={"detail": "DB が起動中です。数秒後にやり直してください"},
        headers={"Retry-After": "5"},  # 何秒後にやり直せばよいか（HTTP の決まりのヘッダー）
    )


@app.get("/health")
def health() -> dict[str, str]:
    """API が動いているかを確かめるためのルート。{"status": "ok"} を返すだけ。

    ログインしなくても呼べるように、API Gateway では認証なしのルートにする（8b-4）。
    """
    return {"status": "ok"}


@app.get("/me")
def me(user: Annotated[User, Depends(get_current_user)]) -> User:
    """リクエストしてきた人を、そのまま返す。認証の通り道を確かめるためのルート。"""
    return user


# Lambda のエントリポイント（Terraform で handler = "app.handler" と指定する）。
# lifespan="off"：アプリの起動・終了時の処理（DB の接続を開く等）を使っていないので、Mangum に探させない。
handler = Mangum(app, lifespan="off")
