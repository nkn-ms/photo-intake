"""SQL を送る共通の処理（Data API）。ルートの部品（insert_〜・select_〜）はここを通して SQL を送る。

  execute_sql … SQL を1本送る。INSERT・UPDATE はこれを使う
  select_rows … SELECT を送り、結果を「列名 → 値」の辞書の一覧で返す。中で execute_sql を使う
  text_or_null … SQL に渡す文字列の値を作る。None なら NULL

■ DB が起動中のときは、待たずに例外 DatabaseResumingError を投げる（None は返さない）
  ルートでは何も書かなくてよい。app.py の例外ハンドラーが受け取って 503 にする。
  どのルートでも同じ 503 を返すので、ルートごとに確かめる書き方をやめて1か所にまとめた。
"""

# 型注釈を実行時に評価しない（TYPE_CHECKING の中でしか import しない型を、注釈に書けるようにするため）
from __future__ import annotations

import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import boto3

# boto3 の型（開発用。Lambda には入っていないので、検査のときだけ読む。handler.py と同じ）
if TYPE_CHECKING:
    from types_boto3_rds_data.type_defs import (
        ExecuteStatementResponseTypeDef,
        FieldTypeDef,
        SqlParameterTypeDef,
    )

# Lambda では Terraform が環境変数で渡す。ローカル（uvicorn）では空のまま起動できるようにしておく
# （/health と /docs はローカルでも動く。/items は AWS が要るのでローカルでは動かない）。
DB_CLUSTER_ARN = os.environ.get("DB_CLUSTER_ARN", "")
DB_SECRET_ARN = os.environ.get("DB_SECRET_ARN", "")
DB_NAME = os.environ.get("DB_NAME", "")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")  # Lambda が自動で入れる

# Lambda では、logger に書いたものが CloudWatch Logs に出る（worker の handler.py と同じ書き方）
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# クライアントは関数の外で1回だけ作る（worker と同じ。実行環境が使い回されるので、2回目以降は作り直さない）
rds_data = boto3.client("rds-data", region_name=REGION)

# Aurora は5分使われないと一時停止し、起きるまで十数秒〜数十秒かかる（一晩止まった後は30秒を超えた）。
# その間に SQL を送ると、3〜4秒たってから DatabaseResumingException が返る（2026-10-13 に測った）。
#
# ここでは待たずに、すぐ DatabaseResumingError を投げる（app.py が 503 と Retry-After にする）。
# 待ってやり直すのは画面（frontend/src/api.ts）の役目。Lambda の中で待つと、
#   ・Lambda の制限時間（28秒）と API Gateway の打ち切り（30秒）を超えることがある（10/13 の朝の 500）
#   ・待っている間も Lambda の課金が続く
# 画面の側には30秒の上限が無いので、起動に何十秒かかっても待ち続けられる。


class DatabaseResumingError(Exception):
    """DB（Aurora）が一時停止から起動している途中であることを表す例外。execute_sql が投げる。

    どこで投げられても、app.py の例外ハンドラー（db_resuming）が受け取って 503 と Retry-After を返す。
    """


def execute_sql(
    sql: str, parameters: list[SqlParameterTypeDef], *, as_json: bool = False
) -> ExecuteStatementResponseTypeDef:
    """Data API で SQL を1本送る。DB が起動中なら、送り直さずに DatabaseResumingError を投げる。

    DatabaseResumingException が返ったとき、SQL はまだ実行されていない。
    だから画面がやり直しても、同じ行が2つできることはない。
    as_json=True なら、SELECT の結果を列名付きの JSON（文字列）で返してもらう（select_rows が使う）。
    """
    sent = time.monotonic()  # 送った時刻。「起動中」と返るまでの秒数を測る
    try:
        return rds_data.execute_statement(
            resourceArn=DB_CLUSTER_ARN,
            secretArn=DB_SECRET_ARN,
            database=DB_NAME,
            sql=sql,
            parameters=parameters,
            formatRecordsAs="JSON" if as_json else "NONE",
        )
    except rds_data.exceptions.DatabaseResumingException as e:
        # 「起動中」と返るまでの秒数を記録しておく（測った 3〜4秒から変わっていないかを、後でログで確かめられる）
        logger.info("db resuming: took %.1fs", time.monotonic() - sent)
        # from e：元の例外（boto3 の DatabaseResumingException）を、原因としてつないだまま投げ直す
        raise DatabaseResumingError() from e


def select_rows(sql: str, parameters: list[SqlParameterTypeDef]) -> list[dict[str, Any]]:
    """SELECT を送り、結果を「列名 → 値」の辞書の一覧で返す。DB が起動中なら DatabaseResumingError。

    Data API の結果は、そのままだと列名の無い形で返ってくる:
        records = [[{"stringValue": "9d46…"}, {"stringValue": "pending_review"}], ...]
    formatRecordsAs="JSON" を付けると、列名付きの JSON の文字列で返ってくる（make items と同じ指定）:
        formattedRecords = '[{"id": "9d46…", "status": "pending_review"}, ...]'
    それを json.loads で Python の辞書の一覧にする。1行も無ければ空の一覧。

    注意：jsonb の列（ai_result）は、辞書ではなく JSON の文字列のまま入っている。読む側で json.loads する。
    """
    response = execute_sql(sql, parameters, as_json=True)
    rows: list[dict[str, Any]] = json.loads(response.get("formattedRecords", "[]"))
    return rows


def text_or_null(value: str | None) -> FieldTypeDef:
    """SQL に渡す文字列の値を作る。None なら NULL を渡す。

    Data API では NULL を {"isNull": True} と書く決まり。任意の項目（拾得場所・色など）で毎回使うので、1つにまとめる。
    """
    return {"stringValue": value} if value is not None else {"isNull": True}
