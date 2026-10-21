"""落とし主からの問い合わせ（requests）を扱う API のルートをまとめたファイル。

一覧 … GET /requests（問い合わせの受付 A）

問い合わせを作るのは LINE の問い合わせ用アカウント（line/inquiry.py）で、API は読むだけ。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import get_current_user
from db import select_rows
from routers.items import UtcDatetime

# ルートの入れ物。app.py が app.include_router(requests.router) でアプリにつなぐ
router = APIRouter()

# 一度に返す件数の上限（items の一覧と同じ）
LIST_LIMIT = 50


class RequestSummary(BaseModel):
    """一覧（GET /requests）で返す1件分の値。

    LINE のユーザー ID（line_user_id）は返さない。画面では使わず、お客さまを指す値なので、要らないところに出さない。
    """

    id: str
    receipt: str  # 受付番号（"A-0012" の形）。お客さまに伝えた番号と同じ
    description: str  # お客さまが送った本文
    created_at: UtcDatetime  # 受け付けた日時


# --- 指揮役 ---


# ログインしていれば staff も admin も見られる。落とし物グループへの知らせ（スタッフ全員に届く）で
# 「管理画面で確認してください」と伝えているため
@router.get("/requests", dependencies=[Depends(get_current_user)])
def list_requests() -> list[RequestSummary]:
    """問い合わせの一覧を返す。受け付けた日時の新しい順。"""
    rows = select_requests()
    return [to_request_summary(row) for row in rows]


# --- 部品 ---

SELECT_REQUESTS_SQL = f"""
SELECT id, receipt_no, description, created_at
FROM requests
ORDER BY created_at DESC
LIMIT {LIST_LIMIT}
"""


def select_requests() -> list[dict[str, Any]]:
    """一覧に出す行を DB から読む。受け付けた日時の新しい順に、最大 LIST_LIMIT 件。"""
    return select_rows(SELECT_REQUESTS_SQL, [])


def to_request_summary(row: dict[str, Any]) -> RequestSummary:
    """DB の1行を、一覧の1件分の値にする。受付番号を、お客さまに見せた形にそろえる。"""
    return RequestSummary(
        id=row["id"],
        receipt=format_receipt(row["receipt_no"]),
        description=row["description"],
        created_at=row["created_at"],
    )


def format_receipt(receipt_no: int) -> str:
    """受付番号を、お客さまに見せる形にする（12 → "A-0012"）。

    line/inquiry.py の format_receipt と同じ形にする（お客さまへの返事と画面で、同じ番号に見えるように）。
    API の zip には line/ のファイルが入らないので、import せずに同じ1行を書いている。
    2つが同じ答えを返すことは tests/test_api.py で確かめている。
    """
    return f"A-{receipt_no:04d}"
