# アップロードされた画像を置く S3 バケット。
# まずはこれ1個だけ。動かして、コンソールで見て、消して、また作る。

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    # data "archive_file" 用。zip を作るためだけの provider。
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }

  # state はいまローカル（このディレクトリの terraform.tfstate）に置いている。
  # S3 に移すのは後のステップで扱う。まずは中身を自分の目で見る。
}

provider "aws" {
  region = "ap-northeast-1"
}

# S3 のバケット名は「全世界で一意」でなければならない。
# アカウントIDを付けて、他人と衝突しないようにする。
# data は「AWS に問い合わせて読むだけ」。何も作らない。
data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "uploads" {
  bucket = "photo-intake-dev-uploads-${data.aws_caller_identity.current.account_id}"
}

# パブリックアクセスを明示的に全部ブロックする。
#
# いまも AWS の既定でオンになっているが、「既定」は「保証」ではない。
# コードに書くことで、誰かがコンソールでオフにしたときに
# plan に差分として現れるようになる。
resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# 画像の解析を後ろで回すためのキュー。
#
# 解析には数秒かかる。同期処理だとアップロードの応答がその分待たされるので、
# 「アップロードされた」という事実だけをここに積んで、処理は別で回す。
#
# バケット名と違ってアカウントIDを付けていないのは、SQS の名前が
# 「アカウント内・リージョン内で一意」であればよいため（S3 は全世界で一意）。
# 処理できなかったメッセージの退避先（Dead Letter Queue）。
#
# 捨てると失敗に気づけない。戻し続けると壊れた1件がキューを詰まらせる。
# 「脇に避けて、後から調査できる形で残す」のが DLQ。
resource "aws_sqs_queue" "ingest_dlq" {
  name = "photo-intake-dev-ingest-dlq"

  # 14日（SQS の上限）。調査して手を打つまでの時間を最大限確保する。
  # 本体のキューと違い、DLQ に入ったものは急いで消す理由がない。
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "ingest" {
  name = "photo-intake-dev-ingest"

  # Lambda のタイムアウト（30秒）より長くする。
  # 短いと、まだ処理中なのにメッセージが再表示され、別の実行にも配信される
  # （＝二重処理）。AWS は関数タイムアウトの6倍を推奨しているので 30 × 6 = 180。
  #
  # 余裕を持たせても害は小さい。失敗したときに再配信されるまで長く待つだけで、
  # DLQ へ落ちるまでの時間が延びるだけだから。
  visibility_timeout_seconds = 180

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingest_dlq.arn

    # 3回受信されても消されなかったら DLQ へ移す。
    # 「処理が完了した」= 受信側が明示的に削除した、という意味なので、
    # 削除されずに可視性タイムアウトが切れた回数がここで数えられる。
    maxReceiveCount = 3
  })
}

# S3 がこのキューにメッセージを送れるようにする許可。
#
# SQS は「誰が送ってよいか」をキュー側で持つ（リソースベースのポリシー）。
# 何も書かないと、このアカウントで IAM 権限を持つ人しか送れない。
# S3 は「AWS のサービス」であって IAM ユーザーではないので、明示的に許可が要る。
resource "aws_sqs_queue_policy" "ingest" {
  queue_url = aws_sqs_queue.ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.ingest.arn

      # 「S3 からなら何でも」ではなく「このバケットからのみ」に絞る。
      # これが無いと、他人のアカウントの S3 バケットからもこのキューに送り込める
      # （Confused Deputy = 権限を持つ第三者を騙して代理実行させる攻撃）。
      Condition = {
        ArnLike = { "aws:SourceArn" = aws_s3_bucket.uploads.arn }
      }
    }]
  })
}

# バケットに「ファイルが置かれたらキューに通知する」設定を足す。
resource "aws_s3_bucket_notification" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  queue {
    queue_arn = aws_sqs_queue.ingest.arn

    # ObjectCreated:* = PUT / POST / COPY / マルチパート完了 をまとめて指す
    events = ["s3:ObjectCreated:*"]

    # uploads/ の下に置かれたものだけ。将来サムネイル等を別の場所に置いても
    # 無限ループ（書き込み → 通知 → 書き込み）にならないようにするため。
    filter_prefix = "uploads/"
  }

  # ★この行が無いと失敗する。
  # S3 は通知の設定時に「本当に送れるか」テスト送信を行うので、
  # その時点でキューのポリシーが存在していないと拒否される。
  # このリソースはポリシーを参照していないため、Terraform は順序を推測できない。
  depends_on = [aws_sqs_queue_policy.ingest]
}

# 通知の配信先をまとめる箱（トピック）。
#
# アラームから直接メールを送ることはできない。必ず SNS を経由する。
# 間に挟むことで、宛先（メール / Slack / Lambda / SMS）を後から
# 増やしても、アラーム側を変更しなくて済む。
resource "aws_sns_topic" "alerts" {
  name = "photo-intake-dev-alerts"
}

# メールの購読。
#
# ★作成しただけでは届かない。AWS から「購読を確認してください」という
#   メールが来るので、リンクをクリックするまで Pending 状態のまま。
#   これは迷惑メール対策（他人のアドレスを勝手に登録できないようにするため）。
#
# count は「このリソースをいくつ作るか」。0 なら作らない。
# alert_email が空のときは購読を作らず、通知なしで動くようにしている。
resource "aws_sns_topic_subscription" "alerts_email" {
  count = var.alert_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# DLQ にメッセージが入ったら鳴らす。
#
# 「Lambda が落ちたらどう気づくか」への答えがこれ。
# DLQ に退避するだけでは誰も見に行かないので、能動的に知らせる必要がある。
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name = "photo-intake-dev-dlq-not-empty"

  # 監視する対象。AWS の各サービスが自動で出しているメトリクスを指定する。
  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions  = { QueueName = aws_sqs_queue.ingest_dlq.name }

  # 5分ごとに、その期間の最大値を見る。
  # Average にすると「5分のうち1分だけ1件」が 0.2 に薄まって閾値を下回るので、
  # 「一度でも入ったか」を知りたい場合は Maximum を使う。
  statistic          = "Maximum"
  period             = 300
  evaluation_periods = 1

  # 0 より大きい = 1件でも入ったら鳴らす
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0

  # SQS は静かなときメトリクスを送らないことがある。
  # その「データ無し」を異常と見なさない（既定だと INSUFFICIENT_DATA になり紛らわしい）。
  treat_missing_data = "notBreaching"

  alarm_description = "DLQ にメッセージが入った = 3回受信しても処理されなかった"
  alarm_actions     = [aws_sns_topic.alerts.arn]
  ok_actions        = [aws_sns_topic.alerts.arn]
}

# ---------------------------------------------------------------------------
# worker Lambda の IAM ロール
# ---------------------------------------------------------------------------

# 【信頼ポリシー】このロールを「誰が引き受けられるか」。
#
# Lambda サービスが、関数を実行するときにこのロールになりすます。
# これが無いと AWS は「あなたにこのロールを使う資格はない」と拒否する。
# 昨日のキューポリシーが「受け取る側の許可」だったのに対し、
# これは「名乗る側の許可」にあたる。
resource "aws_iam_role" "worker" {
  name = "photo-intake-dev-worker"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole" # ロールを引き受ける、という動作そのもの
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

# 【権限ポリシー その1】CloudWatch Logs への書き込み。
#
# AWS が用意している既製のポリシーを使う。
# ログ出力は全 Lambda に共通で、これを自前で書く利点がない。
resource "aws_iam_role_policy_attachment" "worker_logs" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# 【権限ポリシー その2】SQS からの受信。
#
# AWS 既製の AWSLambdaSQSQueueExecutionRole もあるが、あれは Resource が "*" で
# 「アカウント内のすべてのキュー」を許してしまう。
# ここは自前で書いて、このキュー1つだけに絞る（最小権限）。
resource "aws_iam_role_policy" "worker_sqs" {
  name = "read-ingest-queue"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "sqs:ReceiveMessage",    # メッセージを取る
        "sqs:DeleteMessage",     # 処理できたら消す（これが無いと無限に再配信される）
        "sqs:GetQueueAttributes" # 件数などの確認。イベントソースマッピングが使う
      ]
      Resource = aws_sqs_queue.ingest.arn # ★このキューだけ
    }]
  })
}

# ---------------------------------------------------------------------------
# worker Lambda 本体
# ---------------------------------------------------------------------------

# Python のファイルを zip に固める。
# Terraform に同梱されている archive provider の機能で、外部ツールは要らない。
#
# いまは外部ライブラリを使っていないのでこれで足りる。
# 依存が増えたら terraform-aws-modules/lambda/aws か Lambda Layer に切り替える。
data "archive_file" "worker" {
  type        = "zip"
  source_file = "${path.module}/../../../backend/worker/handler.py"
  output_path = "${path.module}/.build/worker.zip"
}

# ログの置き場を先に作る。
#
# 作らなくても Lambda が自動で作るが、その場合「保持期間＝無期限」になり
# ログが永遠に溜まって課金され続ける。明示的に作って期限を付ける。
# 名前は /aws/lambda/<関数名> でなければならない（AWS の規約）。
resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/photo-intake-dev-worker"
  retention_in_days = 14
}

resource "aws_lambda_function" "worker" {
  function_name = "photo-intake-dev-worker"
  role          = aws_iam_role.worker.arn

  filename = data.archive_file.worker.output_path

  # zip の中身が変わったときだけ再デプロイさせるための指紋。
  # これが無いと、コードを書き換えても Terraform が気づかない。
  source_code_hash = data.archive_file.worker.output_base64sha256

  runtime = "python3.12"
  handler = "handler.handler" # <ファイル名>.<関数名>

  timeout     = 30
  memory_size = 256

  environment {
    variables = {
      LOG_LEVEL = "INFO"
    }
  }

  # ロググループを先に作らせる。参照していないので明示が必要。
  depends_on = [aws_cloudwatch_log_group.worker]
}

# 「このキューにメッセージが来たら、この関数を起動する」という紐付け。
#
# Lambda が SQS を見に行く（ポーリングする）形になる。
# S3 → Lambda を直接繋ぐこともできるが、それだと失敗時のリトライと
# DLQ を自前で用意することになる。SQS を挟んでいるのはそのため。
resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = aws_sqs_queue.ingest.arn
  function_name    = aws_lambda_function.worker.arn

  # 1回の呼び出しで最大何件まとめて渡すか。
  # 多いほど効率的だが、1件の失敗でバッチ全体が再配信される点に注意。
  batch_size = 10
}

# ---------------------------------------------------------------------------
# Aurora を置くためのネットワーク（VPC）
# ---------------------------------------------------------------------------
#
# Aurora は「VPC の中のサブネット」にしか置けない。だから VPC は作る。
# ただし外と通信させる出入口（インターネットゲートウェイ / NAT ゲートウェイ）は作らない。
#
# Lambda からは Data API（HTTPS で SQL を送る仕組み）で DB を叩くので、
# Lambda を VPC に入れる必要がない。VPC に入れなければ NAT Gateway（月 $45）も要らない。
# この VPC は「DB の置き場所」としてだけ存在する。
#
# VPC・サブネット・セキュリティグループ・サブネットグループは、どれも無料。

resource "aws_vpc" "main" {
  # この VPC の中で使う IP アドレスの範囲。10.0.0.0〜10.0.255.255（約6.5万個）。
  # 10.x.x.x は「プライベート用」に予約された範囲で、インターネット上には存在しない。
  cidr_block = "10.0.0.0/16"

  tags = { Name = "photo-intake-dev" }
}

# サブネット＝VPC の中の区画。1つのサブネットは必ず1つの AZ（データセンター）に属する。
#
# Aurora は「2つ以上の AZ にサブネットがあること」を作成の条件にしている。
# 片方のデータセンターが止まっても、もう片方で DB を立て直せるようにするため。
#
# for_each は「この map の要素の数だけ作る」。ここでは2つできる。
# （ap-northeast-1b は新しいアカウントでは使えないので、a と c を使う）
resource "aws_subnet" "db" {
  for_each = {
    "ap-northeast-1a" = "10.0.1.0/24" # 10.0.1.0〜10.0.1.255
    "ap-northeast-1c" = "10.0.2.0/24" # 10.0.2.0〜10.0.2.255
  }

  vpc_id            = aws_vpc.main.id
  availability_zone = each.key
  cidr_block        = each.value

  tags = { Name = "photo-intake-dev-db-${each.key}" }
}

# 「Aurora はこのサブネットのどれかに置いてよい」という名簿。
# Aurora 側はサブネットを直接指定せず、この名簿を指定する。
resource "aws_db_subnet_group" "db" {
  name       = "photo-intake-dev-db"
  subnet_ids = [for s in aws_subnet.db : s.id]
}

# セキュリティグループ＝リソース単位のファイアウォール。
#
# ★受信（ingress）を1つも開けていない。それでも Data API では DB に繋がる。
# Data API は AWS の内部経路で DB に届くので、ここを通らないため。
# 「ポートを1つも開けていないのに SQL が通る」を 4c で実際に確かめる。
resource "aws_security_group" "db" {
  name        = "photo-intake-dev-db"
  description = "Aurora. No ingress: accessed only via the Data API."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "photo-intake-dev-db" }
}
