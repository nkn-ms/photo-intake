# ===========================================================================
# LINE：落とし主からの問い合わせ（問い合わせ用の公式アカウント）
# ===========================================================================
#
# line.tf のボット（スタッフ用）とは別の公式アカウントを使う。お客さまが友だちに追加して、探している物を文字で送る。
# LINE の Webhook URL は公式アカウントごとに1つなので、受け口の URL も別にする（POST /line/inquiry/webhook）。
#
#   LINE ──POST /line/inquiry/webhook──→ API Gateway ──→ Lambda: line-inquiry-webhook（backend/line/webhook.py）
#                                                            スタッフ用の line-webhook と同じコード。
#                                                            署名を確かめる鍵と渡す先だけを、環境変数で変えている
#                                                                 │
#                                                                 ▼
#                                                          Lambda: line-inquiry（backend/line/inquiry.py）
#                                                            ・1対1で文字が届いた … 上限（1人 直近24時間に5件・500文字）を確かめる →
#                                                              requests に行を作る → 受付番号（例：A-0012）だけを返事する
#                                                            ・写真やスタンプが届いた … 「文字で送ってください」と返事する
#
# お客さまに返すのは受付番号だけ。落とし物の ID・写真・候補は見せない
# （見せると、持ち主でない人が「それは私のです」と名乗れるため。照らし合わせはスタッフが確かめてから連絡する）。
#
# スタッフには LINE で知らせない。問い合わせは管理画面の一覧（GET /requests）で見る。
# スタッフ用アカウントの落とし物グループは、社内の落とし物の情報を共有する場所なので、お客さまの問い合わせは混ぜない。
# だから line-inquiry は、スタッフ用アカウントの鍵を1つも読めない。
#
# 関数を2つに分ける理由は line.tf と同じ（LINE への返事を待たせない・インターネットから呼ばれる関数は DB に触れない）。

# ---------------------------------------------------------------------------
# 問い合わせ用アカウントの鍵の置き場所（Secrets Manager）
# ---------------------------------------------------------------------------
#
# スタッフ用（line.tf）と同じく、Terraform が作るのは入れ物だけ。値は作者が CLI で入れる。
# 公式アカウントごとに鍵が違うので、スタッフ用とは別に2つ作る（1つ月 $0.40）。

resource "aws_secretsmanager_secret" "line_inquiry_channel_secret" {
  name                    = "photo-intake-dev/line-inquiry-channel-secret"
  recovery_window_in_days = 0 # destroy したらすぐ消す（line.tf と同じ）
}

resource "aws_secretsmanager_secret" "line_inquiry_channel_access_token" {
  name                    = "photo-intake-dev/line-inquiry-channel-access-token"
  recovery_window_in_days = 0
}

# ---------------------------------------------------------------------------
# line-inquiry-webhook（LINE から呼ばれる入口）
# ---------------------------------------------------------------------------

resource "aws_iam_role" "line_inquiry_webhook" {
  name = "photo-intake-dev-line-inquiry-webhook"

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
resource "aws_iam_role_policy_attachment" "line_inquiry_webhook_logs" {
  role       = aws_iam_role.line_inquiry_webhook.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# 【権限ポリシー】問い合わせ用アカウントのチャネルシークレットだけを読む。
# スタッフ用のチャネルシークレットは読めないので、スタッフ用のアカウントに届いたリクエストを、この受け口は通さない。
resource "aws_iam_role_policy" "line_inquiry_webhook_secret" {
  name = "read-line-inquiry-channel-secret"
  role = aws_iam_role.line_inquiry_webhook.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.line_inquiry_channel_secret.arn
    }]
  })
}

# 【権限ポリシー】line-inquiry だけを呼べる。
resource "aws_iam_role_policy" "line_inquiry_webhook_invoke" {
  name = "invoke-line-inquiry"
  role = aws_iam_role.line_inquiry_webhook.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.line_inquiry.arn
    }]
  })
}

resource "aws_cloudwatch_log_group" "line_inquiry_webhook" {
  name              = "/aws/lambda/photo-intake-dev-line-inquiry-webhook"
  retention_in_days = 14
}

resource "aws_lambda_function" "line_inquiry_webhook" {
  function_name = "photo-intake-dev-line-inquiry-webhook"
  role          = aws_iam_role.line_inquiry_webhook.arn

  # zip は line.tf のもの（webhook.py 1ファイル）をそのまま使う。コードはスタッフ用の受け口と同じ
  filename         = data.archive_file.line_webhook.output_path
  source_code_hash = data.archive_file.line_webhook.output_base64sha256

  runtime = "python3.12"
  handler = "webhook.handler"

  timeout     = 10
  memory_size = 256

  environment {
    variables = {
      LOG_LEVEL = "INFO"

      # スタッフ用の受け口との違いは、この2つだけ
      LINE_CHANNEL_SECRET_ARN = aws_secretsmanager_secret.line_inquiry_channel_secret.arn
      FORWARD_FUNCTION_NAME   = aws_lambda_function.line_inquiry.function_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.line_inquiry_webhook]
}

# ---------------------------------------------------------------------------
# line-inquiry（問い合わせを受け付ける。line-inquiry-webhook からしか呼ばれない）
# ---------------------------------------------------------------------------

resource "aws_iam_role" "line_inquiry" {
  name = "photo-intake-dev-line-inquiry"

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
resource "aws_iam_role_policy_attachment" "line_inquiry_logs" {
  role       = aws_iam_role.line_inquiry.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# 【権限ポリシー】問い合わせ用アカウントのアクセストークンだけを読む（お客さまに受付番号を返事する）。
# チャネルシークレットは読まない。スタッフ用アカウントの鍵も読まない。
resource "aws_iam_role_policy" "line_inquiry_token" {
  name = "read-line-inquiry-access-token"
  role = aws_iam_role.line_inquiry.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.line_inquiry_channel_access_token.arn
    }]
  })
}

# 【権限ポリシー】requests に行を作り、件数を数える（Data API）。line-intake の line_intake_db と同じ中身。
# S3 には触らない（問い合わせは文字だけ）。
resource "aws_iam_role_policy" "line_inquiry_db" {
  name = "query-db"
  role = aws_iam_role.line_inquiry.name

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

# inquiry.py と、スタッフ用（line.tf の line-intake）と共通の line_common.py の2ファイルを zip にする（line.tf と同じ書き方）
data "archive_file" "line_inquiry" {
  type        = "zip"
  output_path = "${path.module}/.build/line-inquiry.zip"

  source {
    content  = file("${path.module}/../../../backend/line/inquiry.py")
    filename = "inquiry.py"
  }

  source {
    content  = file("${path.module}/../../../backend/line/line_common.py")
    filename = "line_common.py"
  }
}

resource "aws_cloudwatch_log_group" "line_inquiry" {
  name              = "/aws/lambda/photo-intake-dev-line-inquiry"
  retention_in_days = 14
}

resource "aws_lambda_function" "line_inquiry" {
  function_name = "photo-intake-dev-line-inquiry"
  role          = aws_iam_role.line_inquiry.arn

  filename         = data.archive_file.line_inquiry.output_path
  source_code_hash = data.archive_file.line_inquiry.output_base64sha256

  runtime = "python3.12"
  handler = "inquiry.handler" # inquiry.py の handler

  # line-intake と同じ。長いのは DB が一時停止から起きるのを待つときだけ
  timeout     = 60
  memory_size = 256

  environment {
    variables = {
      LOG_LEVEL = "INFO"

      # お客さまへの返事に使う鍵（問い合わせ用アカウント）
      LINE_INQUIRY_ACCESS_TOKEN_ARN = aws_secretsmanager_secret.line_inquiry_channel_access_token.arn

      # Data API に渡す3点セット（API・worker・line-intake と同じもの）
      DB_CLUSTER_ARN = aws_rds_cluster.main.arn
      DB_SECRET_ARN  = aws_rds_cluster.main.master_user_secret[0].secret_arn
      DB_NAME        = aws_rds_cluster.main.database_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.line_inquiry]
}

# 失敗したときの自動のやり直し（line-intake と同じく 2回）。
# 同じメッセージを2回受け付けないように、requests.line_message_id を UNIQUE にしてある（マイグレーション 003）。
resource "aws_lambda_function_event_invoke_config" "line_inquiry" {
  function_name          = aws_lambda_function.line_inquiry.function_name
  maximum_retry_attempts = 2
}

# ---------------------------------------------------------------------------
# API Gateway：POST /line/inquiry/webhook
# ---------------------------------------------------------------------------
#
# スタッフ用の POST /line/webhook と同じ API Gateway に、ルートをもう1本足す。
# JWT は求めない（LINE は Cognito のトークンを持っていない）。代わりに line-inquiry-webhook が署名を確かめる。

resource "aws_apigatewayv2_integration" "line_inquiry_webhook" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.line_inquiry_webhook.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "line_inquiry_webhook" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /line/inquiry/webhook"
  target    = "integrations/${aws_apigatewayv2_integration.line_inquiry_webhook.id}"
}

# この API の POST /line/inquiry/webhook のルートからだけ、line-inquiry-webhook を呼べる
resource "aws_lambda_permission" "line_inquiry_webhook_api_gateway" {
  statement_id  = "AllowInvokeFromApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.line_inquiry_webhook.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/POST/line/inquiry/webhook"
}

# 問い合わせ用アカウントの LINE の設定画面（Messaging API の Webhook URL）に貼る URL
output "line_inquiry_webhook_url" {
  value = "${aws_apigatewayv2_stage.default.invoke_url}line/inquiry/webhook"
}
