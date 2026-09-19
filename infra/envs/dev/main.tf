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
resource "aws_sqs_queue" "ingest" {
  name = "photo-intake-dev-ingest"
}
