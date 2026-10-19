"""LINE の受け口（line/webhook.py）を、AWS にも LINE にも繋がずに確かめる。

確かめること：署名の確かめ（401 か 200 か）と、届いたイベントを line-intake に渡すこと。
チャネルシークレットは Secrets Manager から読む代わりに、テスト用の値に差し替える。
line-intake を呼ぶ部分（lambda_client）は、呼ばれた内容を覚えておくだけの偽物に差し替える。
署名はテストの中で、LINE と同じ計算（HMAC-SHA256 → base64）で作る。
"""

import base64
import hashlib
import hmac
import json

import pytest

import webhook

SECRET = "test-channel-secret"


@pytest.fixture(autouse=True)
def fake_channel_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """どのテストでも、Secrets Manager の代わりにテスト用のチャネルシークレットを返す。"""
    monkeypatch.setattr(webhook, "channel_secret", lambda: SECRET)


class FakeLambdaClient:
    """boto3 の Lambda クライアントの代わり。invoke に渡されたものを覚えておくだけ。"""

    def __init__(self) -> None:
        self.invocations: list[dict] = []

    def invoke(self, **kwargs: object) -> None:
        self.invocations.append(kwargs)


@pytest.fixture(autouse=True)
def lambda_client(monkeypatch: pytest.MonkeyPatch) -> FakeLambdaClient:
    """どのテストでも、line-intake を本当には呼ばない。呼ばれた内容はテストから見られる。"""
    fake = FakeLambdaClient()
    monkeypatch.setattr(webhook, "lambda_client", fake)
    return fake


def sign(body: bytes, secret: str = SECRET) -> str:
    """LINE がするのと同じ計算で署名を作る。"""
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def api_event(body: str, signature: str | None, base64_encoded: bool = False) -> dict:
    """API Gateway（payload 2.0）から Lambda に届く event の、このテストで使う部分だけを組み立てる。"""
    headers = {} if signature is None else {"x-line-signature": signature}
    return {"headers": headers, "body": body, "isBase64Encoded": base64_encoded}


# 日本語を入れておく。UTF-8 のバイト列に戻す処理が正しくないと、ここで署名が合わなくなる
BODY = json.dumps(
    {"destination": "U0", "events": [{"type": "message", "message": {"text": "鍵"}}]},
    ensure_ascii=False,
)


def test_正しい署名なら受け取る() -> None:
    response = webhook.handler(api_event(BODY, sign(BODY.encode())), None)
    assert response["statusCode"] == 200


def test_署名が合わなければ断る() -> None:
    # 別の鍵で作った署名 ＝ チャネルシークレットを知らない誰かが作った署名
    response = webhook.handler(api_event(BODY, sign(BODY.encode(), "wrong-secret")), None)
    assert response["statusCode"] == 401


def test_本文が書き換えられていたら断る() -> None:
    signature = sign(BODY.encode())
    tampered = BODY.replace("鍵", "財布")
    response = webhook.handler(api_event(tampered, signature), None)
    assert response["statusCode"] == 401


def test_署名のヘッダーが無ければ断る() -> None:
    response = webhook.handler(api_event(BODY, None), None)
    assert response["statusCode"] == 401


def test_base64で渡された本文でも署名を確かめられる() -> None:
    encoded = base64.b64encode(BODY.encode()).decode()
    response = webhook.handler(api_event(encoded, sign(BODY.encode()), base64_encoded=True), None)
    assert response["statusCode"] == 200


# --- line-intake に渡す ---


def signed_event(events: list[dict]) -> dict:
    """events を本文に入れ、正しい署名を付けたリクエストを作る。"""
    body = json.dumps({"destination": "U0", "events": events})
    return api_event(body, sign(body.encode()))


# 1対1のトークで写真が送られてきたイベント（LINE のアプリから送ったときの形）
PHOTO = {
    "type": "message",
    "timestamp": 1760000000000,
    "source": {"type": "user", "userId": "U" + "1" * 32},
    "message": {"type": "image", "id": "5800000000001", "contentProvider": {"type": "line"}},
}
# ボットがグループに招待されたイベント
JOIN = {
    "type": "join",
    "timestamp": 1760000000000,
    "source": {"type": "group", "groupId": "C" + "2" * 32},
}


def test_イベントをそのままline_intakeに渡す(lambda_client: FakeLambdaClient) -> None:
    response = webhook.handler(signed_event([PHOTO]), None)

    assert response["statusCode"] == 200
    assert len(lambda_client.invocations) == 1
    call = lambda_client.invocations[0]
    assert call["FunctionName"] == "test-line-intake"
    assert call["InvocationType"] == "Event"  # 非同期（終わるのを待たない）
    assert json.loads(call["Payload"]) == PHOTO


def test_イベントが2つなら1つずつ渡す(lambda_client: FakeLambdaClient) -> None:
    webhook.handler(signed_event([PHOTO, JOIN]), None)
    assert [json.loads(c["Payload"]) for c in lambda_client.invocations] == [PHOTO, JOIN]


def test_検証ボタンの空のイベントでは呼ばない(lambda_client: FakeLambdaClient) -> None:
    response = webhook.handler(signed_event([]), None)
    assert response["statusCode"] == 200
    assert lambda_client.invocations == []


def test_署名が合わなければ渡さない(lambda_client: FakeLambdaClient) -> None:
    body = json.dumps({"destination": "U0", "events": [PHOTO]})
    response = webhook.handler(api_event(body, sign(body.encode(), "wrong-secret")), None)
    assert response["statusCode"] == 401
    assert lambda_client.invocations == []
