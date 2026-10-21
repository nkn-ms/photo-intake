"""line-inquiry（line/inquiry.py）を、AWS にも LINE にも繋がずに確かめる。

確かめること：イベントの振り分け、上限（文字数・直近24時間の件数）、受付番号の返事、
やり直しで二重に受け付けないこと、そしてどの公式アカウントの鍵で送ったか（問い合わせ用だけ）。
LINE の API と DB は line_common.py にあるので、line_common の関数を、呼ばれた内容を覚えておくだけの偽物に差し替える。
"""

import io
import urllib.error
from email.message import Message
from typing import Any

import pytest

import inquiry
import line_common

GROUP = "C" + "2" * 32  # グループで発言したイベントを作るときだけ使う
CUSTOMER = "U" + "4" * 32
INQUIRY_TOKEN = inquiry.LINE_INQUIRY_ACCESS_TOKEN_ARN


class FakeWorld:
    """LINE と DB の代わり。呼ばれた内容を覚えておく。"""

    def __init__(self) -> None:
        self.replies: list[tuple[str, dict]] = []  # 返事（鍵の場所, 本文）
        self.pushes: list[tuple[str, dict]] = []  # push（鍵の場所, 本文）
        self.reply_status = 200  # 返事のときに LINE が返す番号（トークン切れを試すときに変える）
        self.push_status = 200  # push のときに LINE が返す番号
        self.recent = 0  # 直近24時間の件数として DB が返す数
        self.next_receipt_no: int | None = (
            12  # 行を作ったときに DB が振る番号。None は「もう受け付けている」
        )
        self.inserts: list[list[Any]] = []  # INSERT のパラメーター

    def line_request(
        self, token_arn: str, method: str, url: str, body: dict | None = None
    ) -> io.BytesIO:
        if url.endswith("/message/reply"):
            self.replies.append((token_arn, body or {}))
            status = self.reply_status
        else:
            self.pushes.append((token_arn, body or {}))
            status = self.push_status
        if status != 200:
            raise urllib.error.HTTPError(url, status, "error", Message(), io.BytesIO(b"{}"))
        return io.BytesIO()

    def execute_sql(self, sql: str, parameters: list[Any]) -> dict:
        if sql == inquiry.COUNT_RECENT_SQL:
            return {"records": [[{"longValue": self.recent}]]}
        self.inserts.append(parameters)
        if self.next_receipt_no is None:
            return {"records": []}
        return {"records": [[{"longValue": self.next_receipt_no}]]}

    def texts(self, sent: list[tuple[str, dict]]) -> list[tuple[str, str]]:
        """送ったものを（鍵の場所, 文字）の一覧にする。"""
        return [(token, body["messages"][0]["text"]) for token, body in sent]


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch) -> FakeWorld:
    """どのテストでも、LINE と DB には繋がない。"""
    fake = FakeWorld()
    monkeypatch.setattr(line_common, "line_request", fake.line_request)
    monkeypatch.setattr(line_common, "execute_sql", fake.execute_sql)
    return fake


def message_event(message: dict, source_type: str = "user") -> dict:
    source = {"type": source_type, "userId": CUSTOMER}
    if source_type == "group":
        source["groupId"] = GROUP
    return {
        "type": "message",
        "timestamp": 1760000000000,
        "source": source,
        "message": {"id": "5900000000001", **message},
        "replyToken": "reply-token-1",
    }


def text_event(text: str = "黒い革の財布を、昨日の夕方に3番線のホームで落としました") -> dict:
    return message_event({"type": "text", "text": text})


# --- 受け付ける ---


def test_受け付けたら受付番号を返す(world: FakeWorld) -> None:
    inquiry.handler(text_event(), None)

    assert world.inserts == [
        [
            {
                "name": "description",
                "value": {"stringValue": "黒い革の財布を、昨日の夕方に3番線のホームで落としました"},
            },
            {"name": "line_user_id", "value": {"stringValue": CUSTOMER}},
            {"name": "line_message_id", "value": {"stringValue": "5900000000001"}},
        ]
    ]
    # お客さまへの返事だけを、問い合わせ用の鍵で送る。
    # 落とし物グループ（社内の落とし物の情報を共有する場所）には知らせない。問い合わせは管理画面の一覧で見る
    assert world.texts(world.replies) == [(INQUIRY_TOKEN, inquiry.received_message("A-0012"))]
    assert world.pushes == []


def test_返事のトークンが切れていたらpushで受付番号を届ける(world: FakeWorld) -> None:
    # DB の起動待ちで1分後にやり直したときは、返事のトークンが切れている
    world.reply_status = 400
    inquiry.handler(text_event(), None)
    assert world.texts(world.pushes) == [(INQUIRY_TOKEN, inquiry.received_message("A-0012"))]
    assert world.pushes[0][1]["to"] == CUSTOMER


def test_返事もpushもできなくてもエラーにしない(world: FakeWorld) -> None:
    # 受付は保存済みなので、やり直さない（やり直しても「もう受け付けている」で何も送らずに終わるだけ）
    world.reply_status = 400
    world.push_status = 500
    inquiry.handler(text_event(), None)
    assert len(world.inserts) == 1
    assert len(world.pushes) == 1


def test_同じメッセージのやり直しでは何も送らない(world: FakeWorld) -> None:
    world.next_receipt_no = None  # ON CONFLICT で行を作らなかった
    inquiry.handler(text_event(), None)
    assert world.replies == []
    assert world.pushes == []


# --- 上限 ---


def test_500文字ちょうどは受け付ける(world: FakeWorld) -> None:
    inquiry.handler(text_event("あ" * inquiry.MAX_LENGTH), None)
    assert len(world.inserts) == 1


def test_500文字を超えたら保存せずにそう返事する(world: FakeWorld) -> None:
    inquiry.handler(text_event("あ" * (inquiry.MAX_LENGTH + 1)), None)
    assert world.inserts == []
    assert world.texts(world.replies) == [(INQUIRY_TOKEN, inquiry.TOO_LONG_MESSAGE)]
    assert world.pushes == []


def test_直近24時間に4件なら受け付ける(world: FakeWorld) -> None:
    world.recent = inquiry.DAILY_LIMIT - 1
    inquiry.handler(text_event(), None)
    assert len(world.inserts) == 1


def test_直近24時間に5件送っていたら保存せずにそう返事する(world: FakeWorld) -> None:
    world.recent = inquiry.DAILY_LIMIT
    inquiry.handler(text_event(), None)
    assert world.inserts == []
    assert world.texts(world.replies) == [(INQUIRY_TOKEN, inquiry.LIMIT_MESSAGE)]
    assert world.pushes == []


# --- 振り分け ---


def test_写真が届いたら文字で送るよう返事する(world: FakeWorld) -> None:
    inquiry.handler(message_event({"type": "image", "contentProvider": {"type": "line"}}), None)
    assert world.inserts == []
    assert world.texts(world.replies) == [(INQUIRY_TOKEN, inquiry.TEXT_ONLY_MESSAGE)]


def test_グループでの発言には何もしない(world: FakeWorld) -> None:
    inquiry.handler(
        message_event({"type": "text", "text": "こんにちは"}, source_type="group"), None
    )
    assert world.inserts == []
    assert world.replies == []
    assert world.pushes == []


def test_友だちの追加には何もしない(world: FakeWorld) -> None:
    follow = {"type": "follow", "timestamp": 1, "source": {"type": "user", "userId": CUSTOMER}}
    inquiry.handler(follow, None)
    assert world.replies == []


# --- 受付番号の形 ---


@pytest.mark.parametrize(
    ("receipt_no", "expected"),
    [(1, "A-0001"), (12, "A-0012"), (9999, "A-9999"), (12345, "A-12345")],
)
def test_受付番号は4桁にそろえてAを付ける(receipt_no: int, expected: str) -> None:
    assert inquiry.format_receipt(receipt_no) == expected
