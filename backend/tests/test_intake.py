"""line-intake（line/intake.py）を、AWS にも LINE にも繋がずに確かめる。

確かめること：イベントの振り分けと、写真を登録する手順（メンバーか → 取ってくる → 行を作る → 置く）。
外とやりとりする部分（LINE の API・DB・S3）は、呼ばれた内容を覚えておくだけの偽物に差し替える。
"""

import io
import urllib.error
from email.message import Message
from typing import Any

import pytest

import intake

GROUP = "C" + "2" * 32
MEMBER = "U" + "1" * 32


class FakeResponse(io.BytesIO):
    """urlopen の返事の代わり。本文と Content-Type を持つ。"""

    def __init__(self, body: bytes = b"", content_type: str = "application/json") -> None:
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type


class FakeWorld:
    """LINE・DB・S3 の代わり。呼ばれた内容を覚えておく。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []  # LINE に送ったリクエスト（メソッド, URL）
        self.sql: list[list[Any]] = []  # DB に送った SQL のパラメーター
        self.puts: list[tuple[str, bytes, str]] = []  # S3 に置いたもの（キー, 中身, 形式）
        self.members = {MEMBER}  # 落とし物グループのメンバー
        self.photo = FakeResponse(b"jpeg-bytes", "image/jpeg")  # LINE から取ってくる写真
        self.member_api_status = 200  # メンバーのときに LINE が返す番号（不調を試すときに変える）

    def line_request(self, method: str, url: str) -> FakeResponse:
        self.requests.append((method, url))
        if "/member/" in url:
            user_id = url.rsplit("/", 1)[1]
            status = self.member_api_status if user_id in self.members else 404
            if status != 200:
                raise urllib.error.HTTPError(url, status, "error", Message(), None)
        if url.endswith("/content"):
            return self.photo
        return FakeResponse()

    def execute_sql(self, sql: str, parameters: list[Any]) -> dict:
        self.sql.append(parameters)
        return {}

    def put_photo(self, s3_key: str, image: bytes, content_type: str) -> None:
        self.puts.append((s3_key, image, content_type))


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch) -> FakeWorld:
    """どのテストでも、LINE・DB・S3 には繋がない。落とし物グループは GROUP に決まっている状態にする。"""
    fake = FakeWorld()
    monkeypatch.setattr(intake, "line_request", fake.line_request)
    monkeypatch.setattr(intake, "execute_sql", fake.execute_sql)
    monkeypatch.setattr(intake, "put_photo", fake.put_photo)
    monkeypatch.setattr(intake, "LINE_GROUP_ID", GROUP)
    return fake


def photo_event(user_id: str = MEMBER, source_type: str = "user") -> dict:
    """写真が送られてきたイベント（LINE のアプリから送ったときの形）。"""
    source = {"type": source_type, "userId": user_id}
    if source_type == "group":
        source["groupId"] = GROUP
    return {
        "type": "message",
        "timestamp": 1760000000000,  # 2025-10-09T08:53:20Z
        "source": source,
        "message": {"type": "image", "id": "5800000000001", "contentProvider": {"type": "line"}},
    }


def join_event(group_id: str) -> dict:
    return {"type": "join", "timestamp": 1, "source": {"type": "group", "groupId": group_id}}


# --- 写真 ---


def test_メンバーの写真は行を作ってS3に置く(world: FakeWorld) -> None:
    intake.handler(photo_event(), None)

    assert world.sql == [
        [
            {"name": "s3_key", "value": {"stringValue": "uploads/line-5800000000001.jpg"}},
            {"name": "registered_by", "value": {"stringValue": f"line:{MEMBER}"}},
            {"name": "registered_at", "value": {"stringValue": "2025-10-09T08:53:20+00:00"}},
        ]
    ]
    assert world.puts == [("uploads/line-5800000000001.jpg", b"jpeg-bytes", "image/jpeg")]


def test_メンバーでなければ何もしない(world: FakeWorld) -> None:
    intake.handler(photo_event(user_id="U" + "9" * 32), None)
    assert world.sql == []
    assert world.puts == []


def test_落とし物グループが決まっていなければLINEに聞かずに何もしない(
    world: FakeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(intake, "LINE_GROUP_ID", "")
    intake.handler(photo_event(), None)
    assert world.requests == []
    assert world.sql == []


def test_グループに送られた写真は登録しない(world: FakeWorld) -> None:
    intake.handler(photo_event(source_type="group"), None)
    assert world.requests == []
    assert world.sql == []


def test_LINEの不調はエラーにしてやり直してもらう(world: FakeWorld) -> None:
    world.member_api_status = 500
    with pytest.raises(urllib.error.HTTPError):
        intake.handler(photo_event(), None)
    assert world.sql == []


def test_読めない形式の写真は登録しない(world: FakeWorld) -> None:
    world.photo = FakeResponse(b"heic-bytes", "image/heic")
    intake.handler(photo_event(), None)
    assert world.sql == []
    assert world.puts == []


def test_写真の取得より先に行を作らない(world: FakeWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    # 写真を取ってこられなかったら、写真の無い行を残さない
    def broken_line_request(method: str, url: str) -> FakeResponse:
        if url.endswith("/content"):
            raise urllib.error.URLError("timeout")
        return FakeResponse()

    monkeypatch.setattr(intake, "line_request", broken_line_request)
    with pytest.raises(urllib.error.URLError):
        intake.handler(photo_event(), None)
    assert world.sql == []


# --- グループへの招待 ---


def test_落とし物グループ以外に招待されたら退出する(world: FakeWorld) -> None:
    other = "C" + "3" * 32
    intake.handler(join_event(other), None)
    assert world.requests == [("POST", f"{intake.LINE_API}/group/{other}/leave")]


def test_落とし物グループへの招待では退出しない(world: FakeWorld) -> None:
    intake.handler(join_event(GROUP), None)
    assert world.requests == []


def test_落とし物グループが決まる前の招待ではIDをログに出して残る(
    world: FakeWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(intake, "LINE_GROUP_ID", "")
    with caplog.at_level("INFO"):
        intake.handler(join_event(GROUP), None)
    assert f"joined group: {GROUP}" in caplog.text
    assert world.requests == []
