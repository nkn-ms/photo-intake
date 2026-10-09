"""API（app.py・auth.py・db.py・routers/items.py）の分かれ道を、AWS に繋がずに確かめる。

■ 呼び方
  Lambda に届くのと同じ形の event（API Gateway HTTP API・payload 2.0）を作って、app.handler に渡す。
  JWT の中身を読むところ（read_claims・is_access_token・parse_groups）も本番と同じ道を通る。
  JWT が本物かどうかは API Gateway が確かめる仕事なので、ここでは「確かめ済みの中身」を event に入れるだけ。

■ DB
  Data API を呼ぶところ（db.rds_data.execute_statement）だけを偽物に差し替える。
  偽物は、送られた SQL と値を記録し、決めておいた答えを順に返す。
  execute_sql・select_rows・DB の起動待ち・例外ハンドラーは本物のまま動く。

■ S3
  署名付きURLは AWS に問い合わせずに作れる（偽の認証情報で署名するだけ）ので、偽物は要らない。
"""

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import BUCKET

import app
import db

# --- JWT の中身（API Gateway が確かめ済みのもの）---

# HTTP API のオーソライザーを通ると、cognito:groups は "[staff admin]" という1本の文字列になって届く
STAFF = {"sub": "staff-sub", "token_use": "access", "cognito:groups": "[staff]"}
ADMIN = {"sub": "admin-sub", "token_use": "access", "cognito:groups": "[admin]"}
ID_TOKEN = {"sub": "staff-sub", "token_use": "id", "cognito:groups": "[staff]"}

ITEM_ID = "03e36dc9-77dd-49d7-9cf1-9e72f35a80a4"

# Data API が返す行の形（formatRecordsAs="JSON"）。日時は時差なしの UTC、jsonb は JSON の文字列
PENDING_ROW: dict[str, Any] = {
    "id": ITEM_ID,
    "status": "pending_review",
    "s3_key": "uploads/aeca10bb.jpg",
    "registered_at": "2026-10-08 21:03:31.910116",
    "location": None,
    "registered_by": "staff-sub",
    "ai_result": '{"category": "財布", "color": "黒", "brand": null, "features": ["二つ折り", "革製"]}',
    "ai_model": "dummy",
    "ai_analyzed_at": "2026-10-08 21:04:04.964401",
    "ai_error": None,
    "category": None,
    "color": None,
    "brand": None,
    "features": None,
    "confirmed_by": None,
    "confirmed_at": None,
    "retention_deadline": None,
    "police_reported_at": None,
    "matched_request_id": None,
    "matched_at": None,
    "created_at": "2026-10-08 21:03:54.608481",
    "updated_at": "2026-10-08 21:04:04.964401",
}
CONFIRMED_ROW: dict[str, Any] = {
    **PENDING_ROW,
    "status": "confirmed",
    "category": "財布",
    "features": '["二つ折り", "革製"]',
    "confirmed_by": "admin-sub",
    "confirmed_at": "2026-10-09 01:27:43.558587",
    "retention_deadline": "2027-01-09",
}


# --- テストで使う道具 ---


def call(
    method: str, path: str, *, claims: dict | None = None, body: dict | None = None, query: str = ""
) -> tuple[int, Any, dict]:
    """API Gateway から Lambda に届く event を作って app.handler に渡し、(ステータス, 本文, ヘッダー) を返す。

    claims を渡すと「オーソライザーを通ったリクエスト」になる。渡さなければトークンなし。
    """
    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": query,
        "headers": {"host": "test", "content-type": "application/json"},
        "requestContext": {
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "pytest",
            },
            "stage": "$default",
        },
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }
    if claims is not None:
        event["requestContext"]["authorizer"] = {"jwt": {"claims": claims, "scopes": None}}
    response = app.handler(event, None)
    return response["statusCode"], json.loads(response["body"]), response["headers"]


class FakeDataApi:
    """db.rds_data.execute_statement の偽物。

    answers に入れた答えを、呼ばれた順に1つずつ返す（例外を入れておくと、それを投げる）。
    calls に、送られた SQL と値を {"sql": …, "values": {名前: 値}} の形で記録する。
    """

    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        # 値は {"stringValue": "…"} の形で送られるので、中身だけ取り出す。
        # NULL は {"isNull": True} で送られるので、None として記録する
        values = {
            p["name"]: None if "isNull" in p["value"] else p["value"]["stringValue"]
            for p in kwargs["parameters"]
        }
        self.calls.append({"sql": kwargs["sql"], "values": values})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def data_api(monkeypatch: pytest.MonkeyPatch) -> FakeDataApi:
    """Data API を偽物に差し替える。DB の起動待ちの間隔は 0秒にする（テストで本当に5秒待たない）。"""
    fake = FakeDataApi()
    monkeypatch.setattr(db.rds_data, "execute_statement", fake)
    monkeypatch.setattr(db, "DB_RESUME_WAIT_SECONDS", 0)
    return fake


def rows(*records: dict) -> dict:
    """SELECT の答え（formatRecordsAs="JSON" の形）。"""
    return {"formattedRecords": json.dumps(list(records))}


def updated(count: int) -> dict:
    """UPDATE の答え（書き換えた行の数）。"""
    return {"numberOfRecordsUpdated": count}


def db_resuming() -> ClientError:
    """DB が一時停止から起きる途中のときに Data API が投げる例外。"""
    return db.rds_data.exceptions.DatabaseResumingException(
        {"Error": {"Code": "DatabaseResumingException", "Message": "resuming"}}, "ExecuteStatement"
    )


# ===========================================================================
# 認証（auth.py）
# ===========================================================================


def test_トークンが無ければ401(data_api):
    status, _, _ = call("GET", "/me")
    assert status == 401
    assert data_api.calls == []


def test_IDトークンは401(data_api):
    status, body, _ = call("GET", "/me", claims=ID_TOKEN)
    assert status == 401
    assert body["detail"] == "アクセストークンを送ってください"


def test_meはsubとグループの一覧を返す(data_api):
    # "[staff admin]" という1本の文字列が、一覧に分けられること
    status, body, _ = call("GET", "/me", claims={**STAFF, "cognito:groups": "[staff admin]"})
    assert status == 200
    assert body == {"sub": "staff-sub", "groups": ["staff", "admin"]}


# ===========================================================================
# 登録：POST /items
# ===========================================================================


def test_登録すると行を作って署名付きURLを返す(data_api):
    data_api.answers = [{"records": [[{"stringValue": ITEM_ID}]]}]  # INSERT … RETURNING id の答え

    status, body, _ = call("POST", "/items", claims=STAFF, body={"content_type": "image/jpeg"})

    assert status == 201
    assert body["item_id"] == ITEM_ID
    sent = data_api.calls[0]["values"]
    assert sent["registered_by"] == "staff-sub"  # JWT の sub が、登録した人として入る
    assert sent["location"] is None  # 任意の項目は NULL
    assert sent["s3_key"].startswith("uploads/") and sent["s3_key"].endswith(".jpg")
    # 写真を置く URL は、東京の窓口・このバケット・この行のキー
    assert body["upload_url"].startswith(f"https://{BUCKET}.s3.ap-northeast-1.amazonaws.com/{sent['s3_key']}?")
    assert body["upload_headers"] == {"Content-Type": "image/jpeg"}


def test_対応していない形式は422でDBに触らない(data_api):
    status, _, _ = call("POST", "/items", claims=STAFF, body={"content_type": "image/bmp"})
    assert status == 422
    assert data_api.calls == []


# ===========================================================================
# 一覧：GET /items
# ===========================================================================


def test_一覧は人の種別とAIの種別を分けて返す(data_api):
    data_api.answers = [
        rows(
            {
                "id": ITEM_ID,
                "status": "pending_review",
                "category": None,
                "ai_category": "財布",
                "location": None,
                "registered_at": "2026-10-08 21:03:31.910116",
                "created_at": "2026-10-08 21:03:54.608481",
            }
        )
    ]

    status, body, _ = call("GET", "/items", claims=STAFF)

    assert status == 200
    assert body[0]["category"] is None
    assert body[0]["ai_category"] == "財布"
    assert body[0]["registered_at"] == "2026-10-08T21:03:31.910116Z"  # UTC の印 Z が付く


def test_一覧は状態で絞れる(data_api):
    data_api.answers = [rows(), rows()]

    call("GET", "/items", claims=STAFF, query="status=pending_review")
    call("GET", "/items", claims=STAFF)

    assert data_api.calls[0]["values"]["status"] == "pending_review"
    assert data_api.calls[1]["values"]["status"] is None  # 絞り込みなし＝全件


def test_一覧の状態の名前が違えば422(data_api):
    status, _, _ = call("GET", "/items", claims=STAFF, query="status=foo")
    assert status == 422
    assert data_api.calls == []


# ===========================================================================
# 詳細：GET /items/{item_id}
# ===========================================================================


def test_詳細はjsonbを戻して写真のURLを付ける(data_api):
    data_api.answers = [rows(PENDING_ROW)]

    status, body, _ = call("GET", f"/items/{ITEM_ID}", claims=STAFF)

    assert status == 200
    assert body["ai_result"]["features"] == ["二つ折り", "革製"]  # 文字列ではなく辞書・一覧に戻っている
    assert body["photo_url"].startswith(
        f"https://{BUCKET}.s3.ap-northeast-1.amazonaws.com/{PENDING_ROW['s3_key']}?"
    )
    assert "s3_key" not in body  # 画面に要らない列は返さない


def test_詳細が無ければ404(data_api):
    data_api.answers = [rows()]
    status, _, _ = call("GET", f"/items/{ITEM_ID}", claims=STAFF)
    assert status == 404


def test_IDがUUIDの形でなければ422でDBに触らない(data_api):
    status, _, _ = call("GET", "/items/abc", claims=STAFF)
    assert status == 422
    assert data_api.calls == []


# ===========================================================================
# 確定：POST /items/{item_id}/confirm
# ===========================================================================

CONFIRMATION = {"category": "財布", "color": "黒", "features": ["二つ折り", "革製"]}


def test_staffは確定できず403でDBに触らない(data_api):
    status, body, _ = call("POST", f"/items/{ITEM_ID}/confirm", claims=STAFF, body=CONFIRMATION)
    assert status == 403
    assert body["detail"] == "admin だけができる操作です"
    assert data_api.calls == []


def test_adminが確定すると人の値と保管期限が入った詳細を返す(data_api):
    data_api.answers = [rows(PENDING_ROW), updated(1), rows(CONFIRMED_ROW)]  # 読む → 書く → 読み直す

    status, body, _ = call("POST", f"/items/{ITEM_ID}/confirm", claims=ADMIN, body=CONFIRMATION)

    assert status == 200
    sent = data_api.calls[1]["values"]
    assert sent["confirmed_by"] == "admin-sub"  # 確定した人は JWT の sub
    assert sent["brand"] is None  # 送らなかった項目は NULL
    assert json.loads(sent["features"]) == ["二つ折り", "革製"]  # 配列を JSON の文字列にして送る
    assert body["status"] == "confirmed"
    assert body["features"] == ["二つ折り", "革製"]
    assert body["retention_deadline"] == "2027-01-09"
    assert body["ai_result"]["features"] == ["二つ折り", "革製"]  # AI の答えは残っている


def test_確定済みは409で書き込まない(data_api):
    data_api.answers = [rows(CONFIRMED_ROW)]

    status, body, _ = call("POST", f"/items/{ITEM_ID}/confirm", claims=ADMIN, body=CONFIRMATION)

    assert status == 409
    assert body["detail"] == "状態が confirmed の拾得物は確定できません"
    assert len(data_api.calls) == 1  # 読んだだけ。UPDATE は送っていない


def test_確かめた後に先を越されたら409(data_api):
    # 読んだときは pending_review だったが、書くときには誰かが確定していた（書き換えた行が0）
    data_api.answers = [rows(PENDING_ROW), updated(0)]

    status, body, _ = call("POST", f"/items/{ITEM_ID}/confirm", claims=ADMIN, body=CONFIRMATION)

    assert status == 409
    assert body["detail"] == "ほかの人が先に状態を変えました。読み直してください"


def test_確定する物が無ければ404(data_api):
    data_api.answers = [rows()]
    status, _, _ = call("POST", f"/items/{ITEM_ID}/confirm", claims=ADMIN, body=CONFIRMATION)
    assert status == 404


def test_種別が空なら422でDBに触らない(data_api):
    status, _, _ = call("POST", f"/items/{ITEM_ID}/confirm", claims=ADMIN, body={"category": ""})
    assert status == 422
    assert data_api.calls == []


# ===========================================================================
# DB が起きない（app.py の例外ハンドラー）
# ===========================================================================


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/items", {"content_type": "image/jpeg"}),
        ("GET", "/items", None),
        ("GET", f"/items/{ITEM_ID}", None),
    ],
)
def test_DBが起きなければどのルートでも503とRetryAfter(data_api, method, path, body):
    # 起動待ちの上限の回数だけ、ずっと「起きる途中」を返す
    data_api.answers = [db_resuming() for _ in range(db.DB_RESUME_MAX_ATTEMPTS)]

    status, _, headers = call(method, path, claims=STAFF, body=body)

    assert status == 503
    assert headers["retry-after"] == "5"
    assert len(data_api.calls) == db.DB_RESUME_MAX_ATTEMPTS  # あきらめる前に、上限まで送り直した


def test_DBが途中で起きれば待ったうえで普通に返す(data_api):
    data_api.answers = [db_resuming(), db_resuming(), rows()]  # 3回目で起きた

    status, body, _ = call("GET", "/items", claims=STAFF)

    assert status == 200
    assert body == []
    assert len(data_api.calls) == 3
