"""SQL を送る共通の処理（Data API）。ルートの部品（insert_〜・select_〜）はここの execute_sql を通して SQL を送る。"""

# 型注釈を実行時に評価しない（TYPE_CHECKING の中でしか import しない型を、注釈に書けるようにするため）
from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import boto3

# boto3 の型（開発用。Lambda には入っていないので、検査のときだけ読む。handler.py と同じ）
if TYPE_CHECKING:
    from types_boto3_rds_data.type_defs import (
        ExecuteStatementResponseTypeDef,
        SqlParameterTypeDef,
    )

# Lambda では Terraform が環境変数で渡す。ローカル（uvicorn）では空のまま起動できるようにしておく
# （/health と /docs はローカルでも動く。/items は AWS が要るのでローカルでは動かない）。
DB_CLUSTER_ARN = os.environ.get("DB_CLUSTER_ARN", "")
DB_SECRET_ARN = os.environ.get("DB_SECRET_ARN", "")
DB_NAME = os.environ.get("DB_NAME", "")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")  # Lambda が自動で入れる

# クライアントは関数の外で1回だけ作る（worker と同じ。実行環境が使い回されるので、2回目以降は作り直さない）
rds_data = boto3.client("rds-data", region_name=REGION)

# Aurora は5分使われないと一時停止し、起きるまで十数秒かかる。
# その間に SQL を送ると DatabaseResumingException が返るので、5秒おきに送り直す（最大 5秒 × 4回 = 20秒）。
# Lambda のタイムアウト（28秒）と API Gateway の打ち切り（30秒）より手前で諦めて、503 を返す。
DB_RESUME_WAIT_SECONDS = 5
DB_RESUME_MAX_ATTEMPTS = 5


def execute_sql(
    sql: str, parameters: list[SqlParameterTypeDef]
) -> ExecuteStatementResponseTypeDef | None:
    """Data API で SQL を1本送る。DB が起きる途中なら待って送り直し、それでも起きなければ None。"""
    for attempt in range(1, DB_RESUME_MAX_ATTEMPTS + 1):
        try:
            return rds_data.execute_statement(
                resourceArn=DB_CLUSTER_ARN,
                secretArn=DB_SECRET_ARN,
                database=DB_NAME,
                sql=sql,
                parameters=parameters,
            )
        except rds_data.exceptions.DatabaseResumingException:
            if attempt < DB_RESUME_MAX_ATTEMPTS:
                time.sleep(DB_RESUME_WAIT_SECONDS)
    return None
