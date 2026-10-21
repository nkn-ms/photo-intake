"""LINE の Lambda（line-intake・line-inquiry）が共通で使う部品。

intake.py（スタッフ用アカウント：写真の登録）と inquiry.py（問い合わせ用アカウント：問い合わせの受付）の両方が使う:
  ・LINE の API を呼ぶ（line_request・reply・push）
  ・Data API で SQL を送る。DB が一時停止から起きる途中なら、起きるまで待って送り直す（execute_sql）
  ・Secrets Manager から鍵を読む（secret_string）

LINE の API を呼ぶ関数は、どの公式アカウントの鍵で呼ぶかを、最初の引数（鍵が入ったシークレットの ARN）で受け取る。
2つの Lambda は別の公式アカウントとして LINE を呼ぶ（intake.py はスタッフ用、inquiry.py は問い合わせ用の鍵）ので、
このファイルでは鍵を1つに決め打ちできない。

line-webhook（webhook.py）はこのファイルを使わない。インターネットから呼ばれる関数は、署名を確かめる鍵を読むことと
次の関数を呼ぶことしかしないようにしている（DB の場所も知らない）ので、DB の環境変数を読むこのファイルは入れない。

Lambda では、使う関数の zip にこのファイルも一緒に入れる（line.tf・line_inquiry.tf の archive_file）。
zip の一番上に置かれるので、import line_common として読まれる。
"""

# 注釈を実行時に評価しない。TYPE_CHECKING の中でしか import しない型を注釈に書けるようにするため（intake.py と同じ）
from __future__ import annotations

import functools
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

import boto3

# boto3 の型（types-boto3）は開発用のパッケージで Lambda には入っていないので、mypy が検査するときだけ import する
if TYPE_CHECKING:
    from types_boto3_rds_data.type_defs import (
        ExecuteStatementResponseTypeDef,
        SqlParameterTypeDef,
    )

logger = logging.getLogger()

secrets = boto3.client("secretsmanager")  # LINE の鍵を読む
rds_data = boto3.client("rds-data")  # Data API（HTTPS で SQL を送る）

# Data API に渡す3点セット。line.tf・line_inquiry.tf が環境変数で渡す
DB_CLUSTER_ARN = os.environ["DB_CLUSTER_ARN"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
DB_NAME = os.environ["DB_NAME"]

LINE_API = "https://api.line.me/v2/bot"
LINE_DATA_API = "https://api-data.line.me/v2/bot"  # 写真などの中身は、別のドメインから取る
LINE_TIMEOUT_SECONDS = 10


# --- LINE の API ---


def line_request(token_arn: str, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    """LINE の API にリクエストを1本送り、返事（urlopen の結果）を返す。2xx でなければ HTTPError になる。

    token_arn は、どの公式アカウントとして呼ぶか（そのアカウントのチャネルアクセストークンが入ったシークレットの ARN）。
    body を渡したときは、JSON にして本文に入れる（返事や知らせを送るときに使う）。
    """
    headers = {"Authorization": f"Bearer {secret_string(token_arn)}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(request, timeout=LINE_TIMEOUT_SECONDS)


def reply(token_arn: str, reply_token: str, text: str) -> bool:
    """届いたメッセージに返事をする（送った人の1対1のトークに出る）。返事できたら True を返す。

    返事（reply）は、無料の通数（月200通）に数えられない。こちらから送る push とは違い、
    届いたイベントに付いてくる返事のトークン（replyToken）を使い、1つのトークンで1回だけ、受け取ってから短い時間だけ使える。
    DB の起動待ちで1分後のやり直しになると、トークンが切れていることがある。

    返事に失敗しても、例外にしない（ログに残して False を返す）。返事は知らせるだけで、登録や受付のほうが大事なため。
    失敗したときにどうするかは呼ぶ側が決める（intake.py は何もしない。inquiry.py は push で送り直す）。
    """
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    try:
        line_request(token_arn, "POST", f"{LINE_API}/message/reply", body).close()
    except urllib.error.HTTPError as e:  # LINE が 2xx 以外を返した（トークンの期限切れなど）
        logger.warning("reply failed (%d): %s", e.code, e.read()[:200])
        return False
    except urllib.error.URLError as e:  # LINE に繋がらなかった・時間切れ
        logger.warning("reply failed: %s", e.reason)
        return False
    return True


def push(token_arn: str, to: str, text: str) -> None:
    """こちらから相手（ユーザー・グループ）に文字を送る。

    push は無料の通数（月200通）に数えられる。送り先の to には、その公式アカウントが入っているグループの ID か、
    その公式アカウントを友だちに追加したユーザーの ID だけを指定できる。
    失敗したら HTTPError のまま返す。続けるか止めるかは呼ぶ側が決める。
    """
    body = {"to": to, "messages": [{"type": "text", "text": text}]}
    line_request(token_arn, "POST", f"{LINE_API}/message/push", body).close()


# --- DB ---

# DB が一時停止から起きる途中なら、起きるまで待って送り直す（worker の execute_sql と同じ）。
# 1分後の Lambda のやり直しに任せるより早く終わる。
DB_RESUME_WAIT_SECONDS = 5  # 送り直すまでに待つ秒数
DB_RESUME_MAX_ATTEMPTS = 6  # SQL を送る回数の上限（1回目を含む。待つのは最大 5秒 × 5回 = 25秒）


def execute_sql(sql: str, parameters: list[SqlParameterTypeDef]) -> ExecuteStatementResponseTypeDef:
    """Data API で SQL を1本送る。DB が起きる途中なら、起きるまで待って送り直す。"""
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
            if attempt == DB_RESUME_MAX_ATTEMPTS:
                raise  # 25秒待っても起きなかった。Lambda のやり直し（1分後）に任せる
            logger.info(
                "DB is resuming, retry in %ds (%d/%d)",
                DB_RESUME_WAIT_SECONDS,
                attempt,
                DB_RESUME_MAX_ATTEMPTS,
            )
            time.sleep(DB_RESUME_WAIT_SECONDS)
    # ここには来ない（ループは必ず return か raise で抜ける）。mypy のために置いている（worker と同じ）
    raise AssertionError("unreachable")


# --- 鍵 ---


# functools.cache：ARN ごとに、1回目に読んだ値を覚えておき、2回目からは Secrets Manager に聞かない
@functools.cache
def secret_string(arn: str) -> str:
    """Secrets Manager から、シークレットの値（文字列）を読む。"""
    return secrets.get_secret_value(SecretId=arn)["SecretString"]
