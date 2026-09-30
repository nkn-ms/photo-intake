"""SQS から S3 のイベントを受け取る worker。

写真を Bedrock のモデルに渡し、返ってきた答えを items に保存する。

失敗は2種類に分けて扱う:
  ・恒久的な失敗（何度やっても同じ）→ その場で analysis_failed にする。例外は投げない
  ・一時的な失敗（時間をおけば通るかも）→ 例外を投げて SQS に渡し直させる
"""

import json
import logging
import os
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# クライアントは関数の外で1回だけ作る。
# Lambda は同じ実行環境を使い回すので、2回目以降の呼び出しでは作り直さずに済む。
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")
rds_data = boto3.client("rds-data")  # Data API（HTTPS で SQL を送る）

# Data API に渡す3点セット。make sql が --resource-arn / --secret-arn / --database で渡しているのと同じもの。
DB_CLUSTER_ARN = os.environ["DB_CLUSTER_ARN"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
DB_NAME = os.environ["DB_NAME"]

# 推論プロファイルのID（jp. 付き）。Terraform が環境変数で渡す。
# jp. の付かないモデルIDを渡すと、Nova 2 Lite も Claude も「直接は呼べない」と失敗する。
VISION_MODEL_ID = os.environ["VISION_MODEL_ID"]

# "bedrock" なら本物のモデルを呼ぶ。"dummy" なら呼ばずに決め打ちの答えを返す。
# アカウントで Bedrock が止められている間（サポート待ち）も、保存の流れを先に作るため。
ANALYZER = os.environ.get("ANALYZER", "bedrock")

# items.ai_model に記録する名前。ダミーの行を後から見分けて、精度の集計から外せるようにする。
AI_MODEL = "dummy" if ANALYZER == "dummy" else VISION_MODEL_ID

# ダミーの答え。本物のモデルに頼んでいるのと同じ形にしておく。
DUMMY_ANSWER = json.dumps(
    {
        "category": "財布",
        "color": "黒",
        "brand": None,
        "features": ["二つ折り", "革製", "角に擦れ"],
    },
    ensure_ascii=False,  # 日本語を 財 のような形にせず、そのまま出す
)

# 拡張子 → Converse API の format。
# format に "jpg" という値は無い（"jpeg" だけ）。Playground で JPG を選べなかったのと同じ種類の落とし穴。
IMAGE_FORMATS = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".gif": "gif",
    ".webp": "webp",
}

PROMPT = """この写真は、施設で拾われた落とし物です。
写っている物について、次の形の JSON だけを出力してください。説明文は付けないでください。

{"category": "種別（例: 財布、傘、スマートフォン）",
 "color": "主な色",
 "brand": "ブランド名。読み取れなければ null",
 "features": ["目立つ特徴を短く。最大5つ"]}"""


class PermanentError(Exception):
    """何度やり直しても同じ結果になる失敗。

    これを投げると、handler が受け止めて analysis_failed を書く。
    SQS には「成功した」と返すので、渡し直し（3分×3回の無駄）が起きない。
    """


def handler(event, context):
    """Lambda のエントリポイント。

    event の形は SQS 経由の場合こうなる:
        {"Records": [{"body": "<S3イベントのJSON文字列>", ...}, ...]}

    S3 のイベント自体も {"Records": [...]} という形なので、
    二重に入れ子になっている点に注意。
    """
    logger.info("received %d record(s)", len(event.get("Records", [])))

    for record in event.get("Records", []):
        body = json.loads(record["body"])

        # S3 のテスト通知（バケット設定時に送られる）には Records が無い
        if "Records" not in body:
            logger.info("skipped non-s3 event: %s", body.get("Event", "unknown"))
            continue

        for s3_event in body["Records"]:
            bucket = s3_event["s3"]["bucket"]["name"]
            # S3 の通知に入るキーは URL エンコードされている（空白は +、日本語は %E3%81... になる）。
            # そのまま get_object に渡すと「そんなファイルは無い」と言われるので、元に戻す。
            key = unquote_plus(s3_event["s3"]["object"]["key"])
            try:
                analyze(bucket, key)
            except PermanentError as e:
                # ここで受け止めるので、例外は handler の外に出ない＝SQS はメッセージを消す。
                # PermanentError 以外（一時的な失敗）は受け止めずに外へ出す＝SQS が渡し直す。
                logger.warning("analysis failed: %s: %s", key, e)
                mark_failed(key, str(e))

    # 正常終了すると、Lambda が SQS のメッセージを自動で削除する。
    # 例外を投げると削除されず、可視性タイムアウト後に再配信される。
    return {"ok": True}


def analyze(bucket: str, key: str) -> None:
    """写真1枚を読み、モデルに渡して、答えをログに出す。"""
    ext = os.path.splitext(key)[1].lower()
    image_format = IMAGE_FORMATS.get(ext)
    if image_format is None:
        raise PermanentError(f"未対応の形式: {ext or '(拡張子なし)'}")

    # 通知には「どのファイルか」しか入っていないので、写真そのものを取りに行く
    image_bytes = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    logger.info("downloaded s3://%s/%s (%d bytes)", bucket, key, len(image_bytes))

    answer = ask_model(image_bytes, image_format)
    logger.info("model answer: %s", answer)

    # 同じ写真・同じ temperature=0 で渡し直しても、同じ答えが返ってくるだけなので恒久的な失敗。
    # （本物のモデルは ```json で囲んで返すことがある。その扱いは Bedrock が使えるようになってから決める）
    try:
        ai_result = json.loads(answer)
    except json.JSONDecodeError:
        # 答えの頭だけを残す。人が画面で見て「なぜ失敗したか」が分かるように
        raise PermanentError(f"モデルの答えが JSON として読めない: {answer[:200]}")
    save_result(key, ai_result)


# 行は API（ステップ8）が署名付きURLを発行するときに作る。worker は UPDATE だけを担当する。
# API ができるまでは make sql で手で INSERT しておく。
#
# status = 'analyzing' を条件に入れているので、次の2つの場合は1行も更新されない:
#   ・行が無い（登録されていない写真）
#   ・もう解析済み（SQS の2回目の配信）→ 人が確認した後の結果を上書きしないで済む
#
# :名前 はパラメータ。値は SQL の文字列に埋め込まず、parameters で別に渡す（SQL インジェクション対策）。
UPDATE_SQL = """
update items
   set status         = 'pending_review',
       ai_result      = :ai_result,
       ai_model       = :ai_model,
       ai_analyzed_at = now(),
       updated_at     = now()
 where s3_key = :s3_key
   and status = 'analyzing'
"""


def save_result(key: str, ai_result: dict) -> None:
    """AI の答えを items に保存して、status を pending_review に進める。"""
    response = rds_data.execute_statement(
        resourceArn=DB_CLUSTER_ARN,
        secretArn=DB_SECRET_ARN,
        database=DB_NAME,
        sql=UPDATE_SQL,
        parameters=[
            # typeHint="JSON"：文字列として渡すが、DB 側では jsonb として扱ってもらう
            {
                "name": "ai_result",
                "value": {"stringValue": json.dumps(ai_result, ensure_ascii=False)},
                "typeHint": "JSON",
            },
            {"name": "ai_model", "value": {"stringValue": AI_MODEL}},
            {"name": "s3_key", "value": {"stringValue": key}},
        ],
    )

    if response["numberOfRecordsUpdated"] == 0:
        logger.warning("no row updated (未登録の写真か、解析済み): %s", key)
    else:
        logger.info("saved: %s -> pending_review", key)


# 条件は UPDATE_SQL と同じ（status = 'analyzing' のときだけ）。
# ai_model も残すのは、「どのモデルが JSON を返し損ねたか」を後で数えられるようにするため。
FAIL_SQL = """
update items
   set status     = 'analysis_failed',
       ai_error   = :ai_error,
       ai_model   = :ai_model,
       updated_at = now()
 where s3_key = :s3_key
   and status = 'analyzing'
"""


def mark_failed(key: str, reason: str) -> None:
    """解析をあきらめたことを items に記録する。画面から手入力に回せるようにするため。"""
    response = rds_data.execute_statement(
        resourceArn=DB_CLUSTER_ARN,
        secretArn=DB_SECRET_ARN,
        database=DB_NAME,
        sql=FAIL_SQL,
        parameters=[
            {"name": "ai_error", "value": {"stringValue": reason}},
            {"name": "ai_model", "value": {"stringValue": AI_MODEL}},
            {"name": "s3_key", "value": {"stringValue": key}},
        ],
    )

    if response["numberOfRecordsUpdated"] == 0:
        logger.warning("no row updated (未登録の写真か、解析済み): %s", key)
    else:
        logger.info("saved: %s -> analysis_failed", key)


def ask_model(image_bytes: bytes, image_format: str) -> str:
    """写真をモデルに渡して、答え（文字列）を返す。"""
    if ANALYZER == "dummy":
        logger.info("ANALYZER=dummy: Bedrock は呼ばずに決め打ちの答えを返す")
        return DUMMY_ANSWER

    # Converse API：モデルが変わっても同じ形で呼べる
    try:
        response = bedrock.converse(
            modelId=VISION_MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"image": {"format": image_format, "source": {"bytes": image_bytes}}},
                        {"text": PROMPT},
                    ],
                }
            ],
            # temperature=0：毎回なるべく同じ答えにする（抽出の作業なので、創造性は要らない）
            inferenceConfig={"maxTokens": 512, "temperature": 0},
        )
    except bedrock.exceptions.ValidationException as e:
        # ValidationException は「送った中身が受け付けられない」という意味（画像が大きすぎる、など）。
        # 同じ中身を送り直しても通らないので恒久的な失敗。
        # ThrottlingException（混雑）などはここで受け止めず、外へ出して SQS に渡し直させる。
        raise PermanentError(f"Bedrock が受け付けなかった: {e}")

    usage = response["usage"]  # 入出力のトークン数。料金はこれで決まる
    logger.info(
        "tokens: input=%d output=%d", usage["inputTokens"], usage["outputTokens"]
    )
    return response["output"]["message"]["content"][0]["text"]
