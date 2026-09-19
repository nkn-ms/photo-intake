# アップロードされた画像を置く S3 バケット。
# まずはこれ1個だけ。動かして、コンソールで見て、消して、また作る。

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
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
