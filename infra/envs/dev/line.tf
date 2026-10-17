# ===========================================================================
# LINE の受け口（ステップ11）
# ===========================================================================
#
# ボット（LINE 公式アカウント）は、スタッフとの1対1のトーク（写真の投稿）と、落とし物専用のグループ（知らせ）で使う。
# どちらでも何か起きるたびに、LINE のサーバーがここの URL（POST /line/webhook）にリクエストを送ってくる。
#
#   LINE ──POST /line/webhook──→ API Gateway ──→ Lambda: line（backend/line/webhook.py）
#
# main.tf とは別のファイルにしているが、Terraform は同じフォルダの .tf をすべてまとめて読むので、
# main.tf の中に書いたのと同じ扱いになる（aws_apigatewayv2_api.main などをそのまま参照できる）。
# LINE の部分だけを1か所で読めるように分けた。
#
# 今（L3）は、署名を確かめて、届いたものをログに残すだけ。S3 や DB の権限は、写真を登録する L4 で足す。

# ---------------------------------------------------------------------------
# LINE の鍵の置き場所（Secrets Manager）
# ---------------------------------------------------------------------------
#
# Anthropic の API キーと同じく、Terraform が作るのは「入れ物」だけ。値は作者が CLI で入れる
# （tfstate にもコードにも鍵が残らない）。
#
# 2つに分けているのは、読む Lambda が違うため:
#   チャネルシークレット       … 届いたリクエストの署名を確かめる鍵。line だけが読む
#   チャネルアクセストークン   … こちらから LINE の API を呼ぶ鍵（写真を取ってくる・グループに送る）。line と worker が読む
# 1つにまとめると、worker が使わないシークレットまで読めてしまう。1つ月 $0.40。

resource "aws_secretsmanager_secret" "line_channel_secret" {
  name                    = "photo-intake-dev/line-channel-secret"
  recovery_window_in_days = 0 # destroy したらすぐ消す（anthropic_api_key と同じ理由）
}

resource "aws_secretsmanager_secret" "line_channel_access_token" {
  name                    = "photo-intake-dev/line-channel-access-token"
  recovery_window_in_days = 0
}

# ---------------------------------------------------------------------------
# line Lambda の実行ロール
# ---------------------------------------------------------------------------
#
# api・worker とは分ける。LINE の鍵を読めるのはこのロール（と、送るときの worker）だけにする。

resource "aws_iam_role" "line" {
  name = "photo-intake-dev-line"

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
resource "aws_iam_role_policy_attachment" "line_logs" {
  role       = aws_iam_role.line.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# 【権限ポリシー】LINE の鍵を2つとも読む（署名を確かめる・写真を取ってくる）。
resource "aws_iam_role_policy" "line_secrets" {
  name = "read-line-secrets"
  role = aws_iam_role.line.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "secretsmanager:GetSecretValue"
      Resource = [
        aws_secretsmanager_secret.line_channel_secret.arn,
        aws_secretsmanager_secret.line_channel_access_token.arn,
      ]
    }]
  })
}

# ---------------------------------------------------------------------------
# line Lambda 本体
# ---------------------------------------------------------------------------

# webhook.py 1ファイルだけを zip にする（worker と同じ。ライブラリは Lambda に最初からある boto3 と標準ライブラリだけ使う）
data "archive_file" "line" {
  type        = "zip"
  source_file = "${path.module}/../../../backend/line/webhook.py"
  output_path = "${path.module}/.build/line.zip"
}

resource "aws_cloudwatch_log_group" "line" {
  name              = "/aws/lambda/photo-intake-dev-line"
  retention_in_days = 14
}

resource "aws_lambda_function" "line" {
  function_name = "photo-intake-dev-line"
  role          = aws_iam_role.line.arn

  filename         = data.archive_file.line.output_path
  source_code_hash = data.archive_file.line.output_base64sha256

  runtime = "python3.12"
  handler = "webhook.handler" # webhook.py の handler

  # LINE は、返事が遅いと「届かなかった」とみなす。今は受け取ってログに残すだけなので1秒もかからない。
  # L4 で写真を取ってきて S3 に置くようになったら、かかる時間を見て決め直す。
  timeout     = 10
  memory_size = 256

  environment {
    variables = {
      LOG_LEVEL = "INFO"

      # 鍵そのものではなく、鍵が入ったシークレットの場所（ARN）を渡す（worker の Anthropic のキーと同じ）
      LINE_CHANNEL_SECRET_ARN       = aws_secretsmanager_secret.line_channel_secret.arn
      LINE_CHANNEL_ACCESS_TOKEN_ARN = aws_secretsmanager_secret.line_channel_access_token.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.line]
}

# ---------------------------------------------------------------------------
# API Gateway：POST /line/webhook
# ---------------------------------------------------------------------------
#
# main.tf の API Gateway（aws_apigatewayv2_api.main）に、ルートを1本足す。URL は API と同じドメインになる。

# 行き先：line Lambda（API の Lambda と同じ形。payload 2.0）
resource "aws_apigatewayv2_integration" "line" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.line.invoke_arn
  payload_format_version = "2.0"
}

# JWT を求めない（authorization_type を書かない＝NONE）。LINE は Cognito のトークンを持っていないため。
# 代わりに、line Lambda が LINE の署名を確かめる（L3）。
#
# main.tf には「POST /{proxy+}（どんなパスでも・JWT 必須）」のルートがあるが、ぶつからない。
# API Gateway は、具体的に書いたパス（/line/webhook）を {proxy+} より優先して当てる。
resource "aws_apigatewayv2_route" "line_webhook" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /line/webhook"
  target    = "integrations/${aws_apigatewayv2_integration.line.id}"
}

# 「この API Gateway は、line Lambda を呼んでよい」（main.tf の api_gateway と同じ考え方）。
# source_arn で、この API の、POST /line/webhook のルートからの呼び出しだけに絞る。
resource "aws_lambda_permission" "line_api_gateway" {
  statement_id  = "AllowInvokeFromApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.line.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/POST/line/webhook"
}

# LINE の設定画面（Messaging API の Webhook URL）に貼る URL。
# invoke_url は末尾が / なので、そのまま line/webhook をつなぐ
output "line_webhook_url" {
  value = "${aws_apigatewayv2_stage.default.invoke_url}line/webhook"
}
