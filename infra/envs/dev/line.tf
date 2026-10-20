# ===========================================================================
# LINE（ステップ11）
# ===========================================================================
#
# ボット（LINE 公式アカウント）は、スタッフとの1対1のトーク（写真の投稿）と、落とし物専用のグループ（知らせ）で使う。
# どちらでも何か起きるたびに、LINE のサーバーがここの URL（POST /line/webhook）にリクエストを送ってくる。
#
#   LINE ──POST /line/webhook──→ API Gateway ──→ Lambda: line-webhook（backend/line/webhook.py）
#                                                    署名を確かめ、届いたイベントを1つずつ
#                                                    line-intake に非同期で渡し、すぐ 200 を返す
#                                                         │
#                                                         ▼
#                                                  Lambda: line-intake（backend/line/intake.py）
#                                                    イベントの種類で処理を選ぶ:
#                                                    ・グループに招待された … グループ ID をログに出す（人が terraform.tfvars の
#                                                      line_group_id に写す）。落とし物グループが決まった後なら、ほかのグループからは退出する
#                                                    ・1対1で写真が届いた   … 送り主がそのグループのメンバーか確かめる →
#                                                      写真を LINE から取ってくる → items に行を作る → S3 の uploads/ に置く
#                                                      （ここから先は、画面から登録したときと同じ S3 → SQS → worker）
#
# 関数を2つに分けるのは、次の2つのため:
#   ・LINE への返事を待たせない。DB（Aurora）が一時停止していると、起きるまで十数秒かかる。
#     API Gateway は30秒で打ち切るうえ、LINE も「webhook はすぐ返事をして、重い処理は後で」と勧めている
#   ・権限を分ける。インターネットから呼ばれる line-webhook は、署名を確かめる鍵を読むことと line-intake を呼ぶことしかできない。
#     DB にも S3 にも触れないので、入口に万一穴があっても、そこから DB は触れない
# 画面から登録するときの「API（受け付けてすぐ返す）→ S3 → SQS → worker（重い処理）」と同じ形。
#
# main.tf とは別のファイルにしているが、Terraform は同じフォルダの .tf をすべてまとめて読むので、
# main.tf の中に書いたのと同じ扱いになる（aws_apigatewayv2_api.main などをそのまま参照できる）。
# LINE の部分だけを1か所で読めるように分けた。

# ---------------------------------------------------------------------------
# LINE の鍵の置き場所（Secrets Manager）
# ---------------------------------------------------------------------------
#
# Anthropic の API キーと同じく、Terraform が作るのは「入れ物」だけ。値は作者が CLI で入れる
# （tfstate にもコードにも鍵が残らない）。
#
# 2つに分けているのは、読む Lambda が違うため:
#   チャネルシークレット       … 届いたリクエストの署名を確かめる鍵。line-webhook だけが読む
#   チャネルアクセストークン   … こちらから LINE の API を呼ぶ鍵（メンバーか確かめる・写真を取ってくる・返事・グループに送る）。
#                                line-intake と api（確定したらグループに知らせる）が読む
# 1つにまとめると、使わない鍵まで読めてしまう。1つ月 $0.40。

resource "aws_secretsmanager_secret" "line_channel_secret" {
  name                    = "photo-intake-dev/line-channel-secret"
  recovery_window_in_days = 0 # destroy したらすぐ消す（anthropic_api_key と同じ理由）
}

resource "aws_secretsmanager_secret" "line_channel_access_token" {
  name                    = "photo-intake-dev/line-channel-access-token"
  recovery_window_in_days = 0
}

# ---------------------------------------------------------------------------
# line-webhook（LINE から呼ばれる入口）
# ---------------------------------------------------------------------------

resource "aws_iam_role" "line_webhook" {
  name = "photo-intake-dev-line-webhook"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

# 【権限ポリシー】ログを書く（全 Lambda に共通。AWS 既製のもの）。
resource "aws_iam_role_policy_attachment" "line_webhook_logs" {
  role       = aws_iam_role.line_webhook.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# 【権限ポリシー】署名を確かめる鍵（チャネルシークレット）を読む。アクセストークンは読まない。
resource "aws_iam_role_policy" "line_webhook_secret" {
  name = "read-line-channel-secret"
  role = aws_iam_role.line_webhook.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.line_channel_secret.arn
    }]
  })
}

# 【権限ポリシー】line-intake を呼ぶ。呼んでよい相手は line-intake だけに絞る。
resource "aws_iam_role_policy" "line_webhook_invoke_intake" {
  name = "invoke-line-intake"
  role = aws_iam_role.line_webhook.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.line_intake.arn
    }]
  })
}

# webhook.py 1ファイルだけを zip にする（worker と同じ。ライブラリは Lambda に最初からある boto3 と標準ライブラリだけ使う）
data "archive_file" "line_webhook" {
  type        = "zip"
  source_file = "${path.module}/../../../backend/line/webhook.py"
  output_path = "${path.module}/.build/line-webhook.zip"
}

resource "aws_cloudwatch_log_group" "line_webhook" {
  name              = "/aws/lambda/photo-intake-dev-line-webhook"
  retention_in_days = 14
}

resource "aws_lambda_function" "line_webhook" {
  function_name = "photo-intake-dev-line-webhook"
  role          = aws_iam_role.line_webhook.arn

  filename         = data.archive_file.line_webhook.output_path
  source_code_hash = data.archive_file.line_webhook.output_base64sha256

  runtime = "python3.12"
  handler = "webhook.handler" # webhook.py の handler

  # 署名を確かめて line-intake に渡すだけなので、1秒もかからない（line-intake の終わりは待たない）
  timeout     = 10
  memory_size = 256

  environment {
    variables = {
      LOG_LEVEL = "INFO"

      # 鍵そのものではなく、鍵が入ったシークレットの場所（ARN）を渡す（worker の Anthropic のキーと同じ）
      LINE_CHANNEL_SECRET_ARN = aws_secretsmanager_secret.line_channel_secret.arn

      # イベントを渡す先
      LINE_INTAKE_FUNCTION_NAME = aws_lambda_function.line_intake.function_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.line_webhook]
}

# ---------------------------------------------------------------------------
# line-intake（写真を登録する。line-webhook からしか呼ばれない）
# ---------------------------------------------------------------------------

resource "aws_iam_role" "line_intake" {
  name = "photo-intake-dev-line-intake"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

# 【権限ポリシー】ログを書く。
resource "aws_iam_role_policy_attachment" "line_intake_logs" {
  role       = aws_iam_role.line_intake.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# 【権限ポリシー】LINE の API を呼ぶ鍵（チャネルアクセストークン）を読む。チャネルシークレットは読まない。
resource "aws_iam_role_policy" "line_intake_token" {
  name = "read-line-channel-access-token"
  role = aws_iam_role.line_intake.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.line_channel_access_token.arn
    }]
  })
}

# 【権限ポリシー】items に行を作る（Data API）。API の api_db と同じ中身。
resource "aws_iam_role_policy" "line_intake_db" {
  name = "query-db"
  role = aws_iam_role.line_intake.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "rds-data:ExecuteStatement"
        Resource = aws_rds_cluster.main.arn
      },
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_rds_cluster.main.master_user_secret[0].secret_arn
      },
    ]
  })
}

# 【権限ポリシー】LINE から取ってきた写真を uploads/ の下に置く。
# API と違って読まない（GetObject は付けない）。置いた後に読むのは worker。
resource "aws_iam_role_policy" "line_intake_s3" {
  name = "put-uploads"
  role = aws_iam_role.line_intake.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "${aws_s3_bucket.uploads.arn}/uploads/*"
    }]
  })
}

data "archive_file" "line_intake" {
  type        = "zip"
  source_file = "${path.module}/../../../backend/line/intake.py"
  output_path = "${path.module}/.build/line-intake.zip"
}

resource "aws_cloudwatch_log_group" "line_intake" {
  name              = "/aws/lambda/photo-intake-dev-line-intake"
  retention_in_days = 14
}

resource "aws_lambda_function" "line_intake" {
  function_name = "photo-intake-dev-line-intake"
  role          = aws_iam_role.line_intake.arn

  filename         = data.archive_file.line_intake.output_path
  source_code_hash = data.archive_file.line_intake.output_base64sha256

  runtime = "python3.12"
  handler = "intake.handler" # intake.py の handler

  # 長いのは、DB が一時停止から起きるのを待つときだけ（API と同じく最大20秒待つ）。
  # それに、メンバーか確かめる・写真を取ってくる・置く時間を足した。
  # line-webhook から非同期で呼ばれ、API Gateway を通らないので、30秒の打ち切りは受けない。
  timeout     = 60
  memory_size = 256

  environment {
    variables = {
      LOG_LEVEL = "INFO"

      LINE_CHANNEL_ACCESS_TOKEN_ARN = aws_secretsmanager_secret.line_channel_access_token.arn

      # 落とし物グループ。写真を登録してよいのは、このグループのメンバーだけ（空の間は、だれの写真も登録しない）
      LINE_GROUP_ID = var.line_group_id

      # Data API に渡す3点セット（API・worker と同じもの）
      DB_CLUSTER_ARN = aws_rds_cluster.main.arn
      DB_SECRET_ARN  = aws_rds_cluster.main.master_user_secret[0].secret_arn
      DB_NAME        = aws_rds_cluster.main.database_name

      # 写真を置くバケット
      UPLOAD_BUCKET = aws_s3_bucket.uploads.bucket
    }
  }

  depends_on = [aws_cloudwatch_log_group.line_intake]
}

# 非同期で呼ばれた line-intake が失敗したとき、Lambda が自動でやり直す回数（2 は AWS の既定値。見えるように書いておく）。
# やり直しは1分後・さらに2分後。DB が起ききらずに失敗しても、やり直す頃には起きている。
# 同じ写真を2回登録しないように、S3 のキーは LINE のメッセージ ID から決める（intake.py）。
resource "aws_lambda_function_event_invoke_config" "line_intake" {
  function_name          = aws_lambda_function.line_intake.function_name
  maximum_retry_attempts = 2
}

# ---------------------------------------------------------------------------
# API（main.tf の Lambda: api）から落とし物グループに知らせる
# ---------------------------------------------------------------------------
#
# 管理者が確定（POST /items/{item_id}/confirm）したら、API が LINE の push でグループに知らせる（backend/api/line_notify.py）。
# 環境変数（アクセストークンの場所・グループ ID・写真を置く場所）は main.tf の aws_lambda_function.api に書いてある。
# LINE に関わる権限は、このファイルにまとめる。

# 【権限ポリシー】API の実行ロールに、LINE の API を呼ぶ鍵（チャネルアクセストークン）を読む権限を足す。
# チャネルシークレット（署名を確かめる鍵）は読まない。API は LINE からのリクエストを受けないため。
resource "aws_iam_role_policy" "api_line_token" {
  name = "read-line-channel-access-token"
  role = aws_iam_role.api.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.line_channel_access_token.arn
    }]
  })
}

# 知らせに写真を付ける。
#
# LINE の画像メッセージは、写真の URL を送り、LINE のサーバーがその URL から写真を取りに行く。
# だから、ログイン無しで開ける HTTPS の URL が要る。uploads の署名付き URL は使えない
# （API のロールの一時的な認証で署名するので、数時間で切れる。グループの過去の知らせの写真が見られなくなる）。
#
# そこで確定したときに、API が写真を web バケットの notify/ の下にコピーし、CloudFront の URL を送る。
#   ・ファイルの名前は落とし物の ID（DB がランダムに作る UUID）にする。1つの URL から、ほかの写真の URL は推し量れない
#   ・30日たったら消す（下のライフサイクルの決まり）
#   ・新しく公開のバケットは作らない。web バケットはもう CloudFront を通してだけ読める（S3 は非公開のまま）
#
# 割り切り：URL を知っている人は、30日間はログイン無しで写真を見られる。URL が出るのは落とし物グループの中だけ。
# 本番では写真専用のバケットに分けたほうが、権限を絞りやすい（画面のファイルと写真が同じバケットに入らない）。

# 【権限ポリシー】API の実行ロールに、web バケットの notify/ の下に写真を置く権限を足す。
# コピーには、元を読む権限（uploads/ の GetObject）も要る。これは main.tf の api_s3 にもうある。
resource "aws_iam_role_policy" "api_notify_photo" {
  name = "put-notify-photo"
  role = aws_iam_role.api.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "${aws_s3_bucket.web.arn}/notify/*" # 画面のファイル（index.html など）は書き換えられない
    }]
  })
}

# ライフサイクルの決まり：S3 が1日1回見回り、決まりに合うファイルを自動で消す（消すのは無料）。
# notify/ の下のファイルだけを、置いてから30日で消す。画面のファイルは消さない。
# 消えた後に LINE で写真を開くと、写真は出ない（CloudFront は無いファイルにも index.html を返すので、画像として開けない）。
#
# 注意：画面を web バケットに置くときに `aws s3 sync --delete` を使うなら、`--exclude "notify/*"` を付ける。
# 付けないと、画面に無いファイルとして notify/ の写真まで消してしまう。
resource "aws_s3_bucket_lifecycle_configuration" "web" {
  bucket = aws_s3_bucket.web.id

  rule {
    id     = "expire-notify-photos"
    status = "Enabled"

    filter {
      prefix = "notify/"
    }

    expiration {
      days = 30
    }
  }
}

# ---------------------------------------------------------------------------
# API Gateway：POST /line/webhook
# ---------------------------------------------------------------------------
#
# main.tf の API Gateway（aws_apigatewayv2_api.main）に、ルートを1本足す。URL は API と同じドメインになる。

# 行き先：line-webhook（API の Lambda と同じ形。payload 2.0）
resource "aws_apigatewayv2_integration" "line_webhook" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.line_webhook.invoke_arn
  payload_format_version = "2.0"
}

# JWT を求めない（authorization_type を書かない＝NONE）。LINE は Cognito のトークンを持っていないため。
# 代わりに、line-webhook が LINE の署名を確かめる。
#
# main.tf には「POST /{proxy+}（どんなパスでも・JWT 必須）」のルートがあるが、ぶつからない。
# API Gateway は、具体的に書いたパス（/line/webhook）を {proxy+} より優先して当てる。
resource "aws_apigatewayv2_route" "line_webhook" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /line/webhook"
  target    = "integrations/${aws_apigatewayv2_integration.line_webhook.id}"
}

# 「この API Gateway は、line-webhook を呼んでよい」（main.tf の api_gateway と同じ考え方）。
# source_arn で、この API の、POST /line/webhook のルートからの呼び出しだけに絞る。
resource "aws_lambda_permission" "line_webhook_api_gateway" {
  statement_id  = "AllowInvokeFromApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.line_webhook.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/POST/line/webhook"
}

# LINE の設定画面（Messaging API の Webhook URL）に貼る URL。
# invoke_url は末尾が / なので、そのまま line/webhook をつなぐ
output "line_webhook_url" {
  value = "${aws_apigatewayv2_stage.default.invoke_url}line/webhook"
}
