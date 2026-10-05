"""worker（handler.py）の分かれ道を、偽の AWS の上で確かめる。

本物の AWS で確かめると apply・migrate が要り、最終試行の確認には 6分 × 3回 待つ。
ここでは同じことを数秒・課金なしで、何度でもやり直せる。
"""

import json

import pytest

from conftest import BUCKET


# --- テストで使う道具 ---


def sqs_event(key: str, receive_count: int = 1) -> dict:
    """SQS 経由で Lambda に届く event を組み立てる（S3 の通知が body に文字列で入った二重の入れ子）。

    receive_count は「何回目の配信か」。最終試行のテストで 3 にする。
    """
    s3_notification = {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": key}}}]}
    return {
        "Records": [
            {
                "body": json.dumps(s3_notification),
                # SQS は数字を文字列で入れてくるので、本物に合わせて str にする
                "attributes": {"ApproximateReceiveCount": str(receive_count)},
            }
        ]
    }


class FakeLambdaContext:
    """Lambda が handler に渡す context の偽物。

    handler は register_lambda_context(context) で「あと何ミリ秒で強制終了か」を Powertools に渡している。
    Powertools が使うのはこのメソッドだけなので、これだけ用意すればよい。
    """

    def get_remaining_time_in_millis(self) -> int:
        return 60_000  # main.tf の worker のタイムアウト（60秒）に合わせた


@pytest.fixture
def sent_sql(worker, monkeypatch):
    """execute_sql を「送ろうとした SQL を記録するだけ」の偽物に差し替える。

    moto は SQL を実行しない（Data API の偽物は、決めておいた答えを返すだけ）。
    だから DB の中身ではなく「どの場面で、どの SQL を、どの値で送ろうとしたか」を確かめる。
    UPDATE の条件（status = 'analyzing'）そのものが正しいかは、5d-3 で本物の Aurora で確かめ済み。

    戻り値のリストに、送った順に (SQL, {パラメータ名: 値}) が入る。
    """
    sent: list[tuple[str, dict]] = []

    def fake_execute_sql(sql: str, parameters: list) -> dict:
        values = {p["name"]: p["value"]["stringValue"] for p in parameters}
        sent.append((sql, values))
        return {"numberOfRecordsUpdated": 1}  # 「1行更新できた」と答える

    # テストの間だけ handler.execute_sql をすり替える。テストが終わると monkeypatch が元に戻す
    monkeypatch.setattr(worker, "execute_sql", fake_execute_sql)
    return sent


# --- テスト ---


def test_未対応の形式はその場でanalysis_failedにする(worker, sent_sql):
    result = worker.handler(sqs_event("uploads/memo.txt"), FakeLambdaContext())

    # 例外を投げずに正常に終わる＝SQS はメッセージを消し、やり直さない
    assert result == {"ok": True}

    # 送った SQL は1本だけで、それは失敗を書く SQL
    assert len(sent_sql) == 1
    sql, values = sent_sql[0]
    assert sql == worker.FAIL_SQL
    assert values["s3_key"] == "uploads/memo.txt"
    assert values["ai_error"] == "未対応の形式: .txt"
