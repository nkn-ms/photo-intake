"""LINE の受け口（line/webhook.py）の署名の確かめを、AWS にも LINE にも繋がずに確かめる。

チャネルシークレットは Secrets Manager から読む代わりに、テスト用の値に差し替える。
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
