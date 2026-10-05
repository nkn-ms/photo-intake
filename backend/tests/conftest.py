"""全部のテストに共通の準備。pytest がテストより先に自動で読み込む（Jest の setupFiles に当たる）。

handler.py は import された瞬間に、環境変数を読み、boto3 のクライアントを作る。
だから次の順番を守る:
  1. 環境変数を用意する（このファイルの一番上。テストが1つも動く前）
  2. moto を始める（aws フィクスチャ）
  3. handler.py を import する（worker フィクスチャ）
3 を 2 より先にやると、クライアントが moto に横取りされず、本物の AWS に繋ごうとする。
"""

import os

import boto3
import pytest
from moto import mock_aws

# --- 1. 環境変数 ---

# 偽の認証情報。moto の横取りが万一効かなくても、本物の AWS には繋がらない（鍵が偽物なので弾かれる）。
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "ap-northeast-1"
# シェルに AWS_PROFILE（Makefile が export するもの）が残っていても、SSO の本物の認証情報を使わせない
os.environ.pop("AWS_PROFILE", None)

# Lambda では Terraform が渡している環境変数。値は偽物でよい（moto は ARN の中身を確かめない）
BUCKET = "test-uploads"
os.environ["IDEMPOTENCY_TABLE"] = "test-idempotency"
os.environ["DB_CLUSTER_ARN"] = "arn:aws:rds:ap-northeast-1:123456789012:cluster:test"
os.environ["DB_SECRET_ARN"] = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:test"
os.environ["DB_NAME"] = "photo_intake"
os.environ["VISION_MODEL_ID"] = "jp.amazon.nova-2-lite-v1:0"
os.environ["MAX_RECEIVE_COUNT"] = "3"
# Bedrock は moto で偽物にできないので、本物を呼ばない側にしておく。答えはテストごとに差し替える
os.environ["ANALYZER"] = "dummy"


# --- 2. moto（偽の AWS）---


@pytest.fixture
def aws():
    """テスト1つの間だけ、偽の AWS を用意する。

    with mock_aws() の中では、boto3 のリクエストがすべてメモリ上の偽物に届く。
    テストが終わると with を抜けて、偽物の中身（バケット・テーブル）は全部消える。
    だからテスト同士で、前のテストの冪等性の記録が残るようなことは起きない。
    """
    with mock_aws():
        # 写真を置くバケット。東京リージョンでは作る場所の指定が要る（us-east-1 以外の決まり）
        boto3.client("s3").create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"},
        )

        # 冪等性の記録用テーブル。main.tf の aws_dynamodb_table.idempotency と同じ形（キーは id）
        boto3.client("dynamodb").create_table(
            TableName=os.environ["IDEMPOTENCY_TABLE"],
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # yield の前が準備、後が片付け（Jest の beforeEach / afterEach を1つの関数に書いたもの）。
        # ここでテスト本体が動く。後片付けは with を抜けることで済む
        yield


# --- 3. handler.py ---


@pytest.fixture
def worker(aws):
    """moto を始めた後で handler.py を import して、テストに渡す。

    引数に aws と書いてあるので、pytest は先に aws を準備してからこれを呼ぶ（＝順番が保証される）。
    """
    import handler

    return handler
