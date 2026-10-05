"""worker（handler.py）の分かれ道を、偽の AWS の上で確かめる。

本物の AWS で確かめると apply・migrate が要り、最終試行の確認には 6分 × 3回 待つ。
ここでは同じことを数秒・課金なしで、何度でもやり直せる。
"""

import json

import boto3
import pytest
from botocore.exceptions import ClientError

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


def put_photo(key: str) -> None:
    """偽のバケットに写真を置く。中身は本物の画像でなくてよい（モデルも偽物にするので、誰も中を見ない）。

    worker フィクスチャを使うテストの中で呼ぶこと（moto が動いている間でないと、偽のバケットが無い）。
    """
    boto3.client("s3").put_object(Bucket=BUCKET, Key=key, Body=b"fake image bytes")


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


def test_モデルの答えがJSONとして読めなければanalysis_failedにする(worker, sent_sql, monkeypatch):
    put_photo("uploads/wallet.jpg")

    # モデルの答えを決め打ちにする。JSON だけを出すよう頼んでいるのに、文章で答えてきた場合
    answer = "申し訳ありませんが、この画像に写っている物を判別できません。"
    monkeypatch.setattr(worker, "ask_model", lambda image_bytes, image_format: answer)

    result = worker.handler(sqs_event("uploads/wallet.jpg"), FakeLambdaContext())

    # 同じ写真を渡し直しても同じ答えが返るだけなので、恒久的な失敗。やり直さずに正常に終わる
    assert result == {"ok": True}

    assert len(sent_sql) == 1
    sql, values = sent_sql[0]
    assert sql == worker.FAIL_SQL
    # 人が画面で理由を読めるように、モデルの答えの頭が ai_error に残っている
    assert values["ai_error"] == f"モデルの答えが JSON として読めない: {answer}"


# --- ここから下は、道具をもう2つ使う ---

# モデルの正しい答え（JSON の文字列）。成功の経路と、冪等性のテストで使う
GOOD_ANSWER = json.dumps({"category": "財布", "color": "黒", "brand": None, "features": ["二つ折り"]})


def throttling_error() -> ClientError:
    """Bedrock の混雑（ThrottlingException）を真似た例外。一時的な失敗の代表。

    boto3 は AWS のエラーをすべて ClientError として投げ、種類はその中の Code に入っている。
    """
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}},
        "Converse",
    )


class FakeModel:
    """ask_model の偽物。何回呼ばれたかを数える。

    answers に並べた順に1つずつ使う。例外が入っていれば投げ、文字列なら答えとして返す。
    「1回目は混雑、2回目は正しい答え」のような筋書きを作れる。
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, image_bytes, image_format):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_正しい答えならpending_reviewで保存する(worker, sent_sql, monkeypatch):
    put_photo("uploads/wallet.jpg")
    monkeypatch.setattr(worker, "ask_model", FakeModel(GOOD_ANSWER))

    result = worker.handler(sqs_event("uploads/wallet.jpg"), FakeLambdaContext())

    assert result == {"ok": True}
    assert len(sent_sql) == 1
    sql, values = sent_sql[0]
    assert sql == worker.UPDATE_SQL
    # AI の生の答えが、そのまま ai_result に入る（人の修正は別の列。§4 の「上書きしない」）
    assert json.loads(values["ai_result"]) == json.loads(GOOD_ANSWER)


def test_一時的な失敗は最終試行でなければraiseしてanalysis_failedを書かない(worker, sent_sql, monkeypatch):
    put_photo("uploads/wallet.jpg")
    monkeypatch.setattr(worker, "ask_model", FakeModel(throttling_error()))

    # 1回目の配信。例外が handler の外まで出る＝SQS はメッセージを消さず、あとで渡し直す
    # （pytest.raises は「この中で ClientError が投げられること」を確かめる。投げられなければ失敗）
    with pytest.raises(ClientError):
        worker.handler(sqs_event("uploads/wallet.jpg", receive_count=1), FakeLambdaContext())

    # 次の配信で成功するかもしれないので、まだ何も書かない
    assert sent_sql == []


def test_一時的な失敗の後は次の配信でやり直せる(worker, sent_sql, monkeypatch):
    """Powertools は、中身が例外で終わると「処理中」の記録を消す。だから2回目の配信で中身が動く。

    もし記録が消えずに残ると、2回目は「別の実行が処理中」と判断され、解析されないまま SQS から消える。
    """
    put_photo("uploads/wallet.jpg")
    model = FakeModel(throttling_error(), GOOD_ANSWER)  # 1回目は混雑、2回目は正しい答え
    monkeypatch.setattr(worker, "ask_model", model)

    with pytest.raises(ClientError):
        worker.handler(sqs_event("uploads/wallet.jpg", receive_count=1), FakeLambdaContext())
    result = worker.handler(sqs_event("uploads/wallet.jpg", receive_count=2), FakeLambdaContext())

    assert result == {"ok": True}
    assert model.calls == 2  # 2回目の配信でも、モデルがもう一度呼ばれた
    assert [sql for sql, _ in sent_sql] == [worker.UPDATE_SQL]


def test_一時的な失敗でも最終試行ならanalysis_failedを書いてからraiseする(worker, sent_sql, monkeypatch):
    put_photo("uploads/wallet.jpg")
    monkeypatch.setattr(worker, "ask_model", FakeModel(throttling_error()))

    # 3回目（conftest の MAX_RECEIVE_COUNT = 3）。この後は DLQ に落ちる
    with pytest.raises(ClientError):
        worker.handler(sqs_event("uploads/wallet.jpg", receive_count=3), FakeLambdaContext())

    # 画面（DB）には analysis_failed を書き、さらに raise して DLQ → アラームで開発者にも知らせる
    assert len(sent_sql) == 1
    sql, values = sent_sql[0]
    assert sql == worker.FAIL_SQL
    assert values["ai_error"].startswith("3回試して失敗: ClientError:")
    assert "ThrottlingException" in values["ai_error"]


def test_同じ通知が2回届いたら2回目は解析しない(worker, sent_sql, monkeypatch):
    put_photo("uploads/wallet.jpg")
    model = FakeModel(GOOD_ANSWER)  # 答えは1つだけ。2回呼ばれたら pop できずにテストが落ちる
    monkeypatch.setattr(worker, "ask_model", model)

    first = worker.handler(sqs_event("uploads/wallet.jpg"), FakeLambdaContext())
    second = worker.handler(sqs_event("uploads/wallet.jpg"), FakeLambdaContext())  # 重複配信

    assert first == second == {"ok": True}
    assert model.calls == 1  # モデルは1回しか呼ばれていない（Bedrock の料金も1回分）
    assert [sql for sql, _ in sent_sql] == [worker.UPDATE_SQL]  # 保存も1回だけ


def test_同じ通知を別の実行が処理中なら待たずに終わる(worker, sent_sql, monkeypatch):
    """本番では2台の Lambda が同時に同じ通知を受け取る場面。

    テストでは同時に2つ動かす代わりに、1つ目の実行がモデルの答えを待っている間に、
    2つ目の handler を呼んで再現する（1つ目の「処理中」の記録が DynamoDB にある状態）。
    """
    put_photo("uploads/wallet.jpg")
    results_of_second = []

    def model_that_receives_duplicate(image_bytes, image_format):
        # 1つ目がまだ処理中のところに、同じ通知が届く
        results_of_second.append(
            worker.handler(sqs_event("uploads/wallet.jpg"), FakeLambdaContext())
        )
        return GOOD_ANSWER

    monkeypatch.setattr(worker, "ask_model", model_that_receives_duplicate)

    first = worker.handler(sqs_event("uploads/wallet.jpg"), FakeLambdaContext())

    # 2つ目は IdempotencyAlreadyInProgressError を受けて、解析せずに正常に終わった
    # （失敗ではないので analysis_failed も書かない。1つ目が失敗しても、そのメッセージは SQS に残る）
    assert results_of_second == [{"ok": True}]
    assert first == {"ok": True}
    assert [sql for sql, _ in sent_sql] == [worker.UPDATE_SQL]  # 保存したのは1つ目だけ
