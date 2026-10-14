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

# CORS：画面（ブラウザ）から、署名付きURLへ写真を PUT してよいかの許可（9c）。
#
# 画面のオリジン（localhost:5173 など）と、S3 のオリジン（bucket.s3.ap-northeast-1.amazonaws.com）は別。
# ブラウザは PUT の前に preflight request を送り、S3 がここの設定で答える。curl は確かめないので、8c では要らなかった。
#
# これは「公開する」設定ではない。許すのは「この画面のブラウザが、ここに呼び出しを送ること」だけで、
# 中身を読み書きできるかは、これまでどおり署名（署名付きURL）と権限で決まる。上のパブリックアクセスのブロックもそのまま。
# 写真を見る GET（詳細の photo_url）は <img src> で読むだけなので、CORS は要らない（JS で中身を読むときだけ要る）。
resource "aws_s3_bucket_cors_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  cors_rule {
    allowed_origins = local.web_origins # API Gateway の CORS と同じ一覧（API を呼ぶ画面 ＝ 写真を置く画面）
    allowed_methods = ["PUT"]
    allowed_headers = ["content-type"] # 署名付きURLに含めた Content-Type を付けて送るため
    max_age_seconds = 300
  }
}

# バケットポリシー：HTTPS 以外のリクエストを全部拒否する（AWS が勧める定番の設定。9e で追加）。
#
# 署名付きURLの署名は https:// か http:// かを区別しないので、URL を http:// に書き換えても通ってしまいうる。
# HTTP だと写真が暗号化されずに回線を流れる。aws:SecureTransport は「このリクエストは HTTPS か」を表す条件。
# Deny（拒否）は Allow（許可）より強いので、誰がどんな権限を持っていても、HTTP なら断られる。
# 画面・Lambda・aws コマンドはもともと HTTPS なので、困る人はいない。web バケットにも同じ文を書いている。
resource "aws_s3_bucket_policy" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.uploads.arn, "${aws_s3_bucket.uploads.arn}/*"] # バケットそのものと、中のすべてのファイル
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  # パブリックアクセスのブロックを先に入れてから、ポリシーを付ける（順番を Terraform に教える）
  depends_on = [aws_s3_bucket_public_access_block.uploads]
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

# SQS が同じメッセージを worker に渡す回数の上限。この回数だけ渡しても処理できなければ、DLQ に移す。
#
# この数は2か所で使う：本体キューの maxReceiveCount と、worker の環境変数 MAX_RECEIVE_COUNT。
# worker は「この回数目の配信が最終試行」と判断して analysis_failed を書くので、
# 2つがずれると、書くべき回に書かない（または早すぎる回に書く）。
# 数字を2か所に直接書くと片方だけ変えてしまうので、ここに1回だけ書いて両方から参照する。
#
# locals は1つのファイルにいくつ書いてもよい。使う場所の近くに置いている。
locals {
  max_receive_count = 3

  # worker（Lambda）が1回の実行に使ってよい最大の秒数。
  # 写真の読み込み・Bedrock の待ち時間に加えて、DB が一時停止から起きるのを最大25秒待つ
  # （handler.py の execute_sql）ので、30秒では足りず60秒にした。
  # 本体キューの可視性タイムアウトはこれの6倍で決める（下の aws_sqs_queue.ingest）。
  worker_timeout_seconds = 60
}

resource "aws_sqs_queue" "ingest" {
  name = "photo-intake-dev-ingest"

  # Lambda のタイムアウトより長くする。
  # 短いと、まだ処理中なのにメッセージが再表示され、別の実行にも配信される
  # （＝二重処理）。AWS は関数タイムアウトの6倍を推奨しているので 60 × 6 = 360秒（6分）。
  #
  # 余裕を持たせても害は小さい。失敗したときに再配信されるまで長く待つだけで、
  # DLQ へ落ちるまでの時間が延びるだけだから（いまは 6分 × 2回 ＝ 約12分で DLQ）。
  visibility_timeout_seconds = local.worker_timeout_seconds * 6

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingest_dlq.arn

    # 3回受信されても消されなかったら DLQ へ移す。
    # 「処理が完了した」= 受信側が明示的に削除した、という意味なので、
    # 削除されずに可視性タイムアウトが切れた回数がここで数えられる。
    maxReceiveCount = local.max_receive_count
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
# 冪等性の記録テーブル（DynamoDB）
# ---------------------------------------------------------------------------
#
# SQS は同じ通知を2回渡すことがある（at-least-once）。
# worker は処理を始める前にここを見て、「もう処理した／いま誰かが処理中」の通知なら処理しない。
# 書き込むのは worker 自身ではなく Powertools の @idempotent（6-3 で入れる）。
#
# 1行 ＝ 1つの通知。行の見分け方（id）は S3 のキーから作る。
# 列の名前（id / expiration）は Powertools の既定に合わせている。変えると Powertools 側にも設定が要る。
#
# Aurora と違い、置いておくだけでは課金されない（読み書きした回数とデータ量だけ）。
resource "aws_dynamodb_table" "idempotency" {
  name = "photo-intake-dev-idempotency"

  # オンデマンド課金：読み書きした分だけ払う。
  # もう1つの PROVISIONED は「毎秒◯回まで」を先に決めて、使わなくてもその分を払う方式。
  billing_mode = "PAY_PER_REQUEST"

  # パーティションキー＝行を引くためのキー。この値で1行を特定する（RDB の主キーにあたる）。
  # Terraform では hash_key という名前になっている（AWS 内部で値をハッシュして置き場所を決めるため）。
  hash_key = "id"

  # DynamoDB はスキーマを持たないので、列を全部は宣言しない。
  # 宣言が要るのはキーに使う列だけ。S = 文字列。
  attribute {
    name = "id"
    type = "S"
  }

  # TTL（Time To Live）：expiration 列の時刻を過ぎた行を、DynamoDB が自動で消す。
  # Powertools は既定で「1時間後」を書き込む。古い記録を自分で掃除しなくて済む。
  # 消すのは無料。ただし時刻ちょうどではなく、数日以内のどこかで消える。
  ttl {
    attribute_name = "expiration"
    enabled        = true
  }
}

# ---------------------------------------------------------------------------
# worker Lambda の IAM ロール
# ---------------------------------------------------------------------------
#
# ロールには性格の違う2種類のポリシーが付く。両方そろって初めて動く。
#
#   信頼ポリシー … 「誰が」このロールを引き受けられるか（→ Lambda サービス）
#                   ロール本体（aws_iam_role）の中に書く。コンソールでは「信頼関係」タブ。
#                   Principal（誰に）を書く＝リソースベースのポリシー。
#   権限ポリシー … このロールを引き受けた者が「何をできるか」（→ ログ・SQS・S3・Bedrock）
#                   ロールとは別のブロックで書き、role = で「どのロールに与えるか」を指す。
#                   コンソールでは「許可」タブ。Principal は書かない＝アイデンティティベース。
#
# 関数との関係：aws_lambda_function.worker の role = でこのロールを指定する。
# 実行のたびに Lambda サービスがこのロールを引き受け、一時的な鍵を関数に渡す。
# だから handler.py にはアクセスキーが1つも出てこない。

# 【信頼ポリシー】このロールを「誰が引き受けられるか」。
#
# Lambda サービスが、関数を実行するときにこのロールになりすます。
# これが無いと AWS は「あなたにこのロールを使う資格はない」と拒否する。
# 昨日のキューポリシーが「受け取る側の許可」だったのに対し、
# これは「名乗る側の許可」にあたる。
resource "aws_iam_role" "worker" {
  name = "photo-intake-dev-worker" # ロールの名前（AWS 上の名前）

  # 信頼ポリシー：このロールを lambda.amazonaws.com（Lambda サービス）が引き受けてよい
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
# 既製のポリシーは「付ける」だけなので、ポリシーの名前（name）は書かない。
resource "aws_iam_role_policy_attachment" "worker_logs" {
  role       = aws_iam_role.worker.name # このロールに与える
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# 【権限ポリシー その2】SQS からの受信。
#
# AWS 既製の AWSLambdaSQSQueueExecutionRole もあるが、あれは Resource が "*" で
# 「アカウント内のすべてのキュー」を許してしまう。
# ここは自前で書いて、このキュー1つだけに絞る（最小権限）。
resource "aws_iam_role_policy" "worker_sqs" {
  name = "read-ingest-queue"      # ポリシーの名前（ロールの名前ではない）
  role = aws_iam_role.worker.name # このポリシーを与えるロール

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

# 【権限ポリシー その3】S3 から写真を読む。
#
# 通知に入っているのは「どのファイルか」だけで、写真そのものは入っていない。
# worker が自分で取りに行くので、読む権限が要る。
# uploads/ の下だけに絞る（通知の filter_prefix と同じ範囲）。
resource "aws_iam_role_policy" "worker_s3" {
  name = "read-uploads"           # ポリシーの名前
  role = aws_iam_role.worker.name # このポリシーを与えるロール

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:GetObject"
      Resource = "${aws_s3_bucket.uploads.arn}/uploads/*"
    }]
  })
}

# 【権限ポリシー その4】Bedrock のモデルを呼ぶ。
#
# 写真を解析するモデルは推論プロファイル（jp.）経由でしか呼べない。
# このとき AWS は2段階で権限を確かめる:
#   ① 推論プロファイルを使ってよいか      … 東京にある「振り分け係」
#   ② 振り分け先のモデル本体を使ってよいか … 東京・大阪の両方
# ②が片方でも欠けると「大阪に振られたときだけ失敗する」という再現しにくい障害になる。
#
# 埋め込み（Titan）は東京で直接呼べるので、モデル本体1つでよい。
#
# モデル本体の ARN はアカウントIDの欄が空（::）。AWS が持っているもので、
# 自分のアカウントのものではないため。
locals {
  bedrock_regions = ["ap-northeast-1", "ap-northeast-3"] # jp. の送信先（東京・大阪）
  embed_model_id  = "amazon.titan-embed-text-v2:0"
}

resource "aws_iam_role_policy" "worker_bedrock" {
  name = "invoke-bedrock"         # ポリシーの名前
  role = aws_iam_role.worker.name # このポリシーを与えるロール

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      # Converse API で呼ぶときも、必要な権限は InvokeModel（Converse という権限は無い）
      Action = "bedrock:InvokeModel"
      Resource = concat(
        # ① 推論プロファイル
        ["arn:aws:bedrock:ap-northeast-1:${data.aws_caller_identity.current.account_id}:inference-profile/jp.${var.vision_model_id}"],
        # ② 送信先のモデル本体（リージョンの数だけ作る）
        [for r in local.bedrock_regions : "arn:aws:bedrock:${r}::foundation-model/${var.vision_model_id}"],
        # ③ 埋め込みのモデル（文章を数字の並びに変える。ステップ10の類似検索で使う）。推論プロファイルを通さずに呼ぶ
        ["arn:aws:bedrock:ap-northeast-1::foundation-model/${local.embed_model_id}"],
      )
    }]
  })
}

# 【権限ポリシー その5】Data API で DB に SQL を送る。
#
# 2つそろって初めて SQL が通る:
#   ・rds-data:ExecuteStatement   … このクラスターの Data API に SQL を送ってよい
#   ・secretsmanager:GetSecretValue … DB のパスワードが入ったシークレットを使ってよい
# パスワードそのものは worker に渡らない。worker が渡すのはシークレットの ARN だけで、
# 中身を取り出して DB にログインするのは Data API。ただし「使ってよいか」は呼んだ側の権限で判定される。
#
# aws_rds_cluster.main はこのファイルの後ろのほうで定義しているが、Terraform は書いた順番を気にしない。
# 参照から作る順番を決めるので、クラスターができてからこのポリシーが作られる。
resource "aws_iam_role_policy" "worker_db" {
  name = "query-db"               # ポリシーの名前
  role = aws_iam_role.worker.name # このポリシーを与えるロール

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "rds-data:ExecuteStatement"
        Resource = aws_rds_cluster.main.arn # ★このクラスターだけ
      },
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_rds_cluster.main.master_user_secret[0].secret_arn # ★このシークレットだけ
      },
    ]
  })
}

# 【権限ポリシー その6】冪等性の記録テーブルを読み書きする。
#
# 実際に読み書きするのは worker のコードではなく Powertools の @idempotent。
# ただし Powertools は worker の中で動くので、権限は worker のロールに付ける。
# Powertools が使う操作は次の4つだけ:
#   PutItem    … 「処理中」の行を書く（行がまだ無いときだけ書く、という条件付き）
#   GetItem    … 書けなかったとき、既にある行を読んで「済み／処理中」を確かめる
#   UpdateItem … 処理が終わったら「済み」と結果を書き込む
#   DeleteItem … 処理が失敗したら行を消す（次の配信でやり直せるように）
resource "aws_iam_role_policy" "worker_idempotency" {
  name = "use-idempotency-table"  # ポリシーの名前
  role = aws_iam_role.worker.name # このポリシーを与えるロール

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
      ]
      Resource = aws_dynamodb_table.idempotency.arn # ★このテーブルだけ
    }]
  })
}

# Anthropic の API キーを入れるシークレット（2026-10-13：Bedrock が使えない間、Claude API を直接呼ぶため）。
#
# Terraform が作るのは「入れ物」だけで、キーの値は書かない。値は作者が CLI（aws secretsmanager put-secret-value）で入れる。
# こうすると、キーが tfstate にもコードにも残らない（DB のパスワードを Secrets Manager に生成させたのと同じ考え方）。
resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name = "photo-intake-dev/anthropic-api-key"

  # destroy したらすぐ消す。既定（7〜30日の猶予）のままだと、その間は同じ名前で作り直せず、destroy → apply が失敗する
  recovery_window_in_days = 0
}

# 【権限ポリシー その7】Anthropic の API キーを読む。
resource "aws_iam_role_policy" "worker_anthropic_key" {
  name = "read-anthropic-api-key" # ポリシーの名前
  role = aws_iam_role.worker.name # このポリシーを与えるロール

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.anthropic_api_key.arn # ★このシークレットだけ
    }]
  })
}

# ---------------------------------------------------------------------------
# worker Lambda 本体
# ---------------------------------------------------------------------------

# Python のファイルを zip に固める。
# Terraform に同梱されている archive provider の機能で、外部ツールは要らない。
#
# zip に入れるのは自分で書いた handler.py だけ。
# 外部ライブラリ（Powertools）はレイヤーで足すので、ここに入れなくてよい（下の locals）。
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

# Powertools（AWS 公式の Lambda 用ライブラリ）のレイヤー。
#
# レイヤー＝関数の zip とは別に、Lambda に足せる「ライブラリの zip」。
# 関数が起動するとき、レイヤーの中身も一緒に展開されて import できるようになる。
# npm install した node_modules を、別の箱で配っているようなもの。
#
# Powertools は AWS が自分のアカウント（017000801446）でレイヤーとして公開している。
# 自分で pip install して zip に詰める必要がない。
#
# 名前の読み方：V3（Powertools のメジャー版）-python312（runtime と揃える）-x86_64（CPU の種類。Lambda の既定）
# 末尾の :38 はレイヤーの版。番号を固定しているので、AWS が新しい版を出しても勝手には変わらない
# （2026-10-03 に公式ドキュメントで確認した最新）。
locals {
  powertools_layer_arn = "arn:aws:lambda:ap-northeast-1:017000801446:layer:AWSLambdaPowertoolsPythonV3-python312-x86_64:38"
}

resource "aws_lambda_function" "worker" {
  function_name = "photo-intake-dev-worker"
  role          = aws_iam_role.worker.arn # この関数はこのロールで動く（Lambda の API は ARN で受け取る）

  filename = data.archive_file.worker.output_path

  # zip の中身が変わったときだけ再デプロイさせるための指紋。
  # これが無いと、コードを書き換えても Terraform が気づかない。
  source_code_hash = data.archive_file.worker.output_base64sha256

  runtime = "python3.12"
  handler = "handler.handler" # <ファイル名>.<関数名>

  layers = [local.powertools_layer_arn] # 上の locals

  timeout     = local.worker_timeout_seconds # 上の locals。キューの可視性タイムアウトと連動
  memory_size = 256

  environment {
    variables = {
      LOG_LEVEL = "INFO"

      # 推論プロファイルのID（jp. 付き）。IAM の ① と同じものを指す。
      # handler.py は os.environ["VISION_MODEL_ID"] で読むので、これが無いと起動時に落ちる。
      VISION_MODEL_ID = "jp.${var.vision_model_id}"

      # anthropic / bedrock / dummy の切り替え。variables.tf の analyzer を参照
      ANALYZER = var.analyzer

      # analyzer = anthropic のときに使う。キーそのものではなく、キーが入ったシークレットの場所（ARN）を渡す。
      # キーを環境変数に直接入れると、Lambda のコンソールで誰でも読めてしまう
      ANTHROPIC_MODEL_ID           = var.anthropic_model_id
      ANTHROPIC_API_KEY_SECRET_ARN = aws_secretsmanager_secret.anthropic_api_key.arn

      # Data API に渡す3点セット（make sql と同じもの）。
      # ARN は「置き場所」であって秘密ではないので、環境変数に入れてよい。
      DB_CLUSTER_ARN = aws_rds_cluster.main.arn
      DB_SECRET_ARN  = aws_rds_cluster.main.master_user_secret[0].secret_arn
      DB_NAME        = aws_rds_cluster.main.database_name

      # 本体キューの maxReceiveCount と同じ数（上の locals）。
      # 環境変数は文字列しか入らないので tostring で "3" にする。handler.py 側で int に戻す。
      MAX_RECEIVE_COUNT = tostring(local.max_receive_count)

      # 冪等性の記録テーブルの名前。handler.py が Powertools に渡す（6-3）
      IDEMPOTENCY_TABLE = aws_dynamodb_table.idempotency.name
    }
  }

  # ロググループを先に作らせる。参照していないので明示が必要。
  depends_on = [aws_cloudwatch_log_group.worker]
}

# 「このキューにメッセージが来たら、この関数を起動する」という紐付け。
#
# Lambda が SQS を見に行く（ポーリングする）形になる。
#
# S3 → Lambda を直接繋ぐこともできる（非同期呼び出し）。その場合も Lambda の内部に見えないキューがあり、
# 失敗したら自動で最大2回やり直し、失敗時の送り先（DLQ にあたるもの）も設定できる。
# それでも SQS を挟んでいるのは、直結では次のことができないため:
#   ・やり直しの間隔を決める（直結は約1分・約2分で固定。こちらは可視性タイムアウトで6分）
#   ・何回目の配信かを知る（ApproximateReceiveCount。最終試行で analysis_failed を書くのに使う）
#   ・このキューの分だけ同時に動く台数を絞る（maximum_concurrency）
#   ・溜まっている件数を見る、DLQ から元のキューへ戻す（redrive）、14日まで保持する（直結は6時間）
# 直結でも同じイベントが2回届くことはある（at-least-once）。冪等性は SQS の有無に関係なく要る。
resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = aws_sqs_queue.ingest.arn
  function_name    = aws_lambda_function.worker.arn

  # 1回の呼び出しで最大何件まとめて渡すか。1件ずつにしている。
  #
  # 10件まとめると、次の2つが起きる:
  #   ・巻き添え：1件が一時的なエラーで raise すると、成功した9件も含めて10件すべてが渡し直される。
  #     9件も受け取った回数が1つ増えるので、最終試行で analysis_failed にされることがある
  #   ・タイムアウト：worker は1件ずつ順番に処理する。Bedrock が1件5秒かかると、10件で 60秒に届く
  #
  # 代わりに、写真が一度にたくさん来ると同時に動く worker の台数が増える（Bedrock の混雑を招きやすい）。
  # そうなったら maximum_concurrency（同時に動かす台数の上限）を足す。
  # 失敗した分だけを返す「部分的なバッチ応答」（ReportBatchItemFailures）で巻き添えは防げるが、
  # タイムアウトは防げないので採らなかった。
  batch_size = 1
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

# ---------------------------------------------------------------------------
# Aurora Serverless v2（PostgreSQL）
# ---------------------------------------------------------------------------
#
# ★ここから「置いておくだけで課金される」リソース。
#   使わない期間は make destroy で消す。
#
# Aurora は「クラスター」と「インスタンス」の2段になっている。
#   クラスター   ＝ DB の設定とデータ（ストレージ）。データはここに保存される
#   インスタンス ＝ SQL を実際に処理する計算機。これが無いと SQL を受け付けない
# 課金の大部分は、インスタンスが動いている時間（ACU × 時間）。

resource "aws_rds_cluster" "main" {
  cluster_identifier = "photo-intake-dev"

  engine         = "aurora-postgresql"
  engine_version = "16.14" # 東京で選べる 16 系の最新（2026-09-24 に CLI で確認）

  # Serverless v2 はこの書き方になる（"serverless" は旧 v1 用で、別物）。
  engine_mode = "provisioned"

  database_name   = "photo_intake" # 最初に作られるデータベースの名前
  master_username = "app"

  # パスワードは自分で決めず、AWS に生成させて Secrets Manager に保管させる。
  # → パスワードがコードにも state にも現れない。
  # Data API で SQL を送るときは、この Secrets Manager の ARN を渡す。
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.db.name
  vpc_security_group_ids = [aws_security_group.db.id]

  # ★これが Data API。HTTPS で SQL を受け付ける入口を開ける。
  # Lambda を VPC の外に置いたまま DB を叩けるのは、これがあるから。
  enable_http_endpoint = true

  serverlessv2_scaling_configuration {
    min_capacity             = 0   # 0 = 使っていない間は一時停止する（課金はストレージ代だけ）
    max_capacity             = 1   # 上限。自動停止が効かなかったときの最悪額を決める
    seconds_until_auto_pause = 300 # 最後の SQL から5分で停止。設定できる最小値
  }

  storage_encrypted = true # 保存データの暗号化。AWS 管理の鍵を使うので追加料金なし

  # --- 作り直す前提の学習環境なので、消しやすくしている ---
  # 本番ではすべて逆にする（消すのを難しくする）。
  backup_retention_period = 1     # 自動バックアップの保持日数。1 が最小
  skip_final_snapshot     = true  # 削除時に最後のバックアップを取らない
  deletion_protection     = false # true だと destroy が拒否される
}

# SQL を処理する計算機を1台。
#
# 本番なら別の AZ にもう1台置いて、片方が止まっても切り替わるようにする。
# 学習用なので1台だけ（2台にすると課金も2倍）。
resource "aws_rds_cluster_instance" "main" {
  identifier         = "photo-intake-dev-1"
  cluster_identifier = aws_rds_cluster.main.id

  instance_class = "db.serverless" # 「容量は ACU で自動伸縮」という意味
  engine         = aws_rds_cluster.main.engine
  engine_version = aws_rds_cluster.main.engine_version
}

# apply の最後に表示させる値。4c で CLI から SQL を送るときに使う。
output "db_cluster_arn" {
  value = aws_rds_cluster.main.arn
}

output "db_secret_arn" {
  value = aws_rds_cluster.main.master_user_secret[0].secret_arn
}

# ---------------------------------------------------------------------------
# Cognito（ログイン）
# ---------------------------------------------------------------------------
#
# User Pool ＝ ユーザーの名簿と、ログインの窓口。
# ログインに成功すると、Cognito は JWT（署名付きのトークン）を返す。
# 中には「誰か（sub）」「どのグループか（cognito:groups）」「いつまで有効か」が書いてある。
# API Gateway はこの JWT を確かめてから Lambda を呼ぶ（8b で作る）。
#
# 置いておくだけでは課金されない。少人数のユーザーなら無料枠の範囲。

resource "aws_cognito_user_pool" "main" {
  name = "photo-intake-dev"

  # ログインに使う名前をメールアドレスにする（別に「ユーザー名」を決めさせない）
  username_attributes = ["email"]

  # ユーザーを作れるのは管理者だけ。自分で登録する機能（セルフサインアップ）は切る。
  # 施設の職員だけが使う社内向けのシステムなので、誰でも登録できる必要がない。
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # 作り直す前提の学習環境なので、destroy で消せるようにしておく（本番では ACTIVE にする）
  deletion_protection = "INACTIVE"
}

# グループ ＝ ユーザーに付ける札。
# ユーザーがログインすると、所属するグループの名前が JWT の cognito:groups に入る。
# API はそれを見て「確定（confirmed）にできるのは admin だけ」のように分ける（8d）。
resource "aws_cognito_user_group" "staff" {
  name         = "staff"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "拾得物を撮影して登録する職員"
}

resource "aws_cognito_user_group" "admin" {
  name         = "admin"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "AI の結果を確認して確定する管理者"
}

# アプリクライアント ＝「このアプリからのログインを受け付ける」という登録。
# 画面（ステップ9）も、8a で試す CLI も、この ID を使ってログインする。
resource "aws_cognito_user_pool_client" "web" {
  name         = "photo-intake-dev-web"
  user_pool_id = aws_cognito_user_pool.main.id

  # シークレット（秘密の鍵）を持たせない。
  # ブラウザで動く画面に秘密を入れても、開発者ツールで誰でも見られるので意味がない。
  generate_secret = false

  # このアプリクライアントで受け付けるログインの方式（下の3つ）
  explicit_auth_flows = [
    # SRP：パスワードそのものを送らずに、「知っている」ことだけを証明する方式。画面ではこちらを使う
    "ALLOW_USER_SRP_AUTH",
    # パスワードを（HTTPS で暗号化して）そのまま送る方式。CLI から試すのが簡単なので、学習用に開けている
    "ALLOW_USER_PASSWORD_AUTH",
    # リフレッシュトークンで、ログインし直さずにトークンを取り直す
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # 「そのユーザーはいない」と「パスワードが違う」を同じエラーにする。
  # 分けると、どのメールアドレスが登録されているかを外から調べられてしまう。
  prevent_user_existence_errors = "ENABLED"

  # トークンの有効期限。ID・アクセストークンは variables.tf の token_validity_hours（dev は12時間、本番は1時間）。
  # リフレッシュトークンは既定と同じ30日（これが切れたら、もう一度パスワードでログインする）。
  access_token_validity  = var.token_validity_hours
  id_token_validity      = var.token_validity_hours
  refresh_token_validity = 30

  # 上の数字の単位。3つとも書く決まり（1つでも省くと、その分が既定の単位になって紛らわしい）
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

# CLI でユーザーを作ったりログインしたりするときに使う
output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

# ---------------------------------------------------------------------------
# API の Lambda（FastAPI）
# ---------------------------------------------------------------------------
#
# 中身は backend/api/app.py と、FastAPI などのライブラリ。
# make build-api がそれを .build/api に集め、ここで zip にする（make plan / apply の前に自動で走る）。
# 呼ぶのは API Gateway（8b-4 で作る）。worker と違って、SQS ではなく HTTP のリクエストで起動する。

# 実行ロール。形は worker のロールと同じ（信頼ポリシー：Lambda サービスが引き受けてよい）。
# worker とは分ける。API と worker はやることが違うので、与える権限も違う（最小権限）。
resource "aws_iam_role" "api" {
  name = "photo-intake-dev-api"

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
# 必要になった権限を、必要になったときに足す（8c で下の2つを足した）。
resource "aws_iam_role_policy_attachment" "api_logs" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# 【権限ポリシー】Data API で items に行を作る（8c）。worker の query-db と同じ2つ。
resource "aws_iam_role_policy" "api_db" {
  name = "query-db"
  role = aws_iam_role.api.name

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

# 【権限ポリシー】uploads/ の下に写真を置く（8c）・写真を読む（8d）。
#
# API 自身は写真を置きも読みもしない。それでもこの権限が要るのは、署名付きURLの仕組みのため:
#   ・API は「uploads/xxx.jpg に PUT してよい」という URL を、自分の権限で署名して作る（AWS には問い合わせない）
#   ・ブラウザ（今は curl）がその URL に PUT したとき、S3 は「署名した者（このロール）に PutObject の権限があるか」を確かめる
# つまり署名付きURLは、このロールの権限を、URL を持つ人に5分だけ貸し出すもの。
# 詳細（GET /items/{id}）で返す「写真を見るための URL」も同じで、こちらは GetObject を貸し出す。
# だから範囲を uploads/ の下だけに絞っておく（貸し出せるのも uploads/ の下だけになる）。
resource "aws_iam_role_policy" "api_s3" {
  name = "put-get-uploads"
  role = aws_iam_role.api.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject"]
      Resource = "${aws_s3_bucket.uploads.arn}/uploads/*"
    }]
  })
}

# worker は handler.py 1ファイルだったので source_file。API はライブラリごとなのでフォルダ（source_dir）。
data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../../../.build/api"
  output_path = "${path.module}/.build/api.zip"
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/photo-intake-dev-api"
  retention_in_days = 14
}

resource "aws_lambda_function" "api" {
  function_name = "photo-intake-dev-api"
  role          = aws_iam_role.api.arn

  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  runtime = "python3.12"
  handler = "app.handler" # app.py の handler（Mangum）

  # CPU の種類。make build-api が x86_64 用のライブラリを入れているので、ここも揃える（Lambda の既定も x86_64）。
  # 揃っていないと、起動した瞬間に pydantic_core が読み込めずに落ちる。
  architectures = ["x86_64"]

  # ふだんは1秒もかからない。長いのは DB が一時停止から起きるのを待つときだけ（app.py で最大20秒待つ）。
  # API Gateway の HTTP API は30秒で打ち切るので、その手前の28秒にしておく。
  timeout = 28

  # Lambda はメモリに比例して CPU も増える。FastAPI と Pydantic の読み込み（コールドスタート）を速くするため、
  # worker（256）より多めにしている。使った時間 × メモリで課金されるが、手で試す程度なら誤差。
  memory_size = 512

  environment {
    variables = {
      # Data API に渡す3点セット（worker と同じもの）
      DB_CLUSTER_ARN = aws_rds_cluster.main.arn
      DB_SECRET_ARN  = aws_rds_cluster.main.master_user_secret[0].secret_arn
      DB_NAME        = aws_rds_cluster.main.database_name

      # 署名付きURLで写真を置かせるバケット
      UPLOAD_BUCKET = aws_s3_bucket.uploads.bucket
    }
  }

  depends_on = [aws_cloudwatch_log_group.api]
}

# ---------------------------------------------------------------------------
# API Gateway（HTTP API）
# ---------------------------------------------------------------------------
#
# 公開の入口。URL を持ち、届いたリクエストを API の Lambda に渡す。
# 部品は5つ。HTTP のリクエストは上から順に通る:
#
#   api         … 入口そのもの（URL の持ち主）
#   stage       … 公開の単位。URL の末尾が決まる。$default にすると URL に段階名が付かない
#   route       … 「どのメソッド・パスのリクエストを、どう扱うか」。認証の有無もここで決める
#   authorizer  … route に付ける「JWT の検査係」。通らなければ 401 で、Lambda は呼ばれない
#   integration … route の行き先（この Lambda に渡す）
#
# 置いておくだけでは課金されない。100万リクエストあたり約1ドル。

resource "aws_apigatewayv2_api" "main" {
  name          = "photo-intake-dev"
  protocol_type = "HTTP" # HTTP API（新しく安いほう）。REST API ではない

  # CORS：別のオリジン（画面を配った場所）のブラウザから、この API を呼んでよいかの許可（9b）。
  # ブラウザは Authorization ヘッダー付きの呼び出しの前に、OPTIONS のリクエストで許可を確かめる（preflight request）。
  # ここを書くと、API Gateway がその preflight request に自分で答える（Lambda は呼ばれない）。
  # curl は preflight request を送らないので、今まではこの設定が無くても呼べていた。
  cors_configuration {
    allow_origins = local.web_origins
    allow_methods = ["GET", "POST"]
    allow_headers = ["authorization", "content-type"]
    # 503 の Retry-After を画面の JS から読めるようにする（既定では読めるヘッダーが限られている）
    expose_headers = ["retry-after"]
    max_age        = 300 # preflight request の結果をブラウザが覚えておく秒数（毎回送らなくて済む）
  }
}

locals {
  # API を呼んでよい・写真を PUT してよい画面のオリジン（API Gateway と uploads バケットの CORS で使う）:
  #   ・ローカルの開発サーバー（npm run dev）
  #   ・CloudFront で公開した画面（9e）
  #   ・Cloudflare Workers で公開した画面（CloudFront を作れない間の代わり。variables.tf の enable_cloudfront）
  # CloudFront の URL は、作ったときだけ足す（for で「作った分だけ」並べる。作っていなければ空の一覧）
  web_origins = concat(
    ["http://localhost:5173", "https://photo-intake.nkn-ms.workers.dev"],
    [for d in aws_cloudfront_distribution.web : "https://${d.domain_name}"],
  )
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default" # URL の末尾に段階名（/dev など）を付けない
  auto_deploy = true       # route などを変えたら自動で公開し直す（手で「デプロイ」しなくてよい）

  # 流量の上限（スロットリング）。超えた分は Lambda を呼ばずに 429 で返す。
  # 公開の URL なので、誰かに連打されても Lambda の課金が膨らまないようにする保険。
  # rate＝1秒あたりの平均、burst＝一瞬だけ許す量。手で試す分には十分大きい。
  default_route_settings {
    throttling_rate_limit  = 10
    throttling_burst_limit = 20
  }
}

# JWT の検査係。Cognito（8a）が発行したトークンだけを通す。
#   identity_sources … トークンをどこから取り出すか（Authorization ヘッダー。"Bearer <トークン>" の形）
#   issuer           … JWT の iss がこれと一致するか（うちの User Pool が発行したか）
#   audience         … JWT の aud（アクセストークンでは client_id）がこれと一致するか（うちのアプリ向けか）
# 署名と期限（exp）も確かめる。検査は API Gateway の中で済み、Cognito には問い合わせない
# （だから速いが、発行後のトークンを取り消せない。トークンの期限を短くする理由）。
resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.main.id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = "https://${aws_cognito_user_pool.main.endpoint}"
    audience = [aws_cognito_user_pool_client.web.id]
  }
}

# 行き先：API の Lambda。
#   AWS_PROXY … リクエストを丸ごと event にして Lambda に渡し、Lambda の戻り値をそのまま HTTP のレスポンスにする
#   2.0       … event の形の版。app.py（Mangum）と make invoke-api が前提にしている形
resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

# ルート（どのメソッド・パスのリクエストを受け付け、認証を求めるか）:
#   GET /health                   … 認証なし（API が動いているかの確認用）
#   GET /{proxy+}・POST /{proxy+} … それ以外すべて。JWT 必須
# 「既定で認証必須、例外だけを開ける」形。FastAPI にルートを足しても、ここを書き換える必要がなく、
# 書き忘れて認証なしで公開してしまうこともない。どのパスがあるかは FastAPI（app.py）が決める。
#
# 8b では ANY /{proxy+}（どのメソッドでも）だった。9b で GET と POST の2本に分けた理由:
#   ANY だと、ブラウザの preflight request（OPTIONS）もこのルートに当たり、JWT を求めて 401 で弾きうる
#   （preflight request には Authorization ヘッダーが付かない。公式ドキュメントは「どのメソッドでも当たる」$default ルートについて
#   同じ注意を書いている）。OPTIONS に当たるルートを無くしておけば、API Gateway が CORS の設定を使って自分で preflight request に答える。
#   使っているメソッドだけを開ける、という意味でも狭いほうがよい（PUT・DELETE を足すときはここにも足す）。
resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /health"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
  # authorization_type を書かない＝NONE（認証なし）
}

resource "aws_apigatewayv2_route" "authenticated" {
  for_each = toset(["GET", "POST"]) # 同じ形のルートを、メソッドごとに1本ずつ作る

  api_id    = aws_apigatewayv2_api.main.id
  route_key = "${each.key} /{proxy+}" # {proxy+} は「どんなパスでも」
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# ★リソースベースのポリシー：「この API Gateway は、この Lambda を呼んでよい」
#
# API Gateway は、Lambda を呼ぶときにロール（帽子）をかぶらない。
# だから呼ばれる側の Lambda の入口に、名簿として書く（SQS のキューポリシーと同じ考え方）。
#
#   principal  … 誰に許すか（API Gateway というサービス）
#   source_arn … ★そのうち、どの API からの呼び出しか。これで「この API だけ」に絞る。
#                書かないと「API Gateway なら、どの API からでも（他人のアカウントの API でも）」になる。
#                /*/* は「どのステージの、どのルートからでも」
#
# これが無いと、API Gateway が Lambda を呼んだ瞬間に拒否され、curl には 500 が返る。
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowInvokeFromApiGateway" # 名簿の1行の名前（コンソールに出る）
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

output "api_url" {
  value = aws_apigatewayv2_stage.default.invoke_url
}

# ===========================================================================
# 画面の公開（9e）：S3（ビルドした画面のファイル）+ CloudFront（配る窓口）
# ===========================================================================
#
# npm run build で frontend/dist にできる index.html・JS・CSS を S3 に置き、CloudFront が世界中に配る。
# Vite の SPA はサーバーで動く処理が無い（ファイルを返すだけ）ので、Lambda も EC2 も要らない。
#
#   ブラウザ ──HTTPS──→ CloudFront ──（OAC で署名）──→ S3（web バケット。公開しない）
#
# なぜ S3 を直接公開しないか:
#   ・S3 の静的ウェブサイト機能は HTTP だけで、HTTPS にできない。
#     HTTPS でないと、ログイン（Amplify の SRP）が使う暗号の機能（crypto.subtle）をブラウザが使わせない
#     （localhost だけは例外で HTTP でも使える。だからローカルでは困らなかった）
#   ・バケットを公開しないまま、CloudFront からだけ読ませられる（uploads と同じく、パブリックアクセスは全部ブロック）

# ビルドした画面を置くバケット。写真の uploads とは分ける
# （uploads は利用者が写真を PUT する場所、こちらは誰にでも配る画面。権限も中身の作り方も違う）。
resource "aws_s3_bucket" "web" {
  bucket = "photo-intake-dev-web-${data.aws_caller_identity.current.account_id}"

  # 中にファイルが残っていても destroy で消せるようにする。
  # uploads と違って、中身は make deploy-web でいつでも作り直せるので、消えて困るものが無い
  force_destroy = true
}

# uploads と同じく、パブリックアクセスを全部ブロックする。
# CloudFront に読ませる許可（後で書くバケットポリシー）は「公開」には当たらないので、これと両立する。
resource "aws_s3_bucket_public_access_block" "web" {
  bucket = aws_s3_bucket.web.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# OAC（Origin Access Control）：CloudFront が web バケットにファイルを取りに行くとき、リクエストに署名を付ける設定。
# 署名を付けるだけでは何も守れない。バケットポリシー（下）に「この CloudFront の署名が付いたリクエストだけ読ませる」と書いて、組で使う。
resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "photo-intake-dev-web"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always" # 毎回署名する
  signing_protocol                  = "sigv4"  # AWS の署名の方式（署名付きURLと同じ）
}

# AWS が用意している「キャッシュの決め方」のひな形。名前で探して読むだけ（data なので何も作らない）。
# CachingOptimized：URL だけでファイルを見分け、既定で1日（最長1年）エッジで覚えておく。静的なファイル向け
data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

# ディストリビューション：CloudFront の設定1つ分。作ると https://xxxx.cloudfront.net の URL が1つもらえる。
# 作るのにも消すのにも数分かかる（世界中の拠点に設定を配るため）。
resource "aws_cloudfront_distribution" "web" {
  # count：作る数。enable_cloudfront が false なら 0 個（作らない）。以降は aws_cloudfront_distribution.web[0] で指す
  count = var.enable_cloudfront ? 1 : 0

  enabled             = true
  comment             = "photo-intake-dev web"
  default_root_object = "index.html"     # https://xxxx.cloudfront.net/ で来たときに返すファイル
  price_class         = "PriceClass_200" # 配る拠点の範囲。日本を含み、料金の高い一部の地域（南米など）を除く

  # オリジン：ファイルを取りに行く元。web バケットを、上の OAC で署名して読む
  origin {
    origin_id                = "web" # このディストリビューションの中での名前（下の target_origin_id が指す）
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  # どの URL のリクエストも、このやり方で返す
  default_cache_behavior {
    target_origin_id       = "web"
    viewer_protocol_policy = "redirect-to-https" # http:// で来たら https:// に転送する
    allowed_methods        = ["GET", "HEAD"]     # 画面のファイルを読むだけなので、書き込みのメソッドは通さない
    cached_methods         = ["GET", "HEAD"]
    compress               = true # JS・CSS を gzip / brotli で縮めて送る
    cache_policy_id        = data.aws_cloudfront_cache_policy.caching_optimized.id
  }

  # SPA のための設定：バケットに無いパスでも index.html を返す。
  #
  # /items/9d46… を直接開くと（再読み込み・ブックマーク・リンクの共有）、CloudFront は S3 に
  # 「items/9d46…」というファイルを取りに行くが、そんなファイルは無い。そのまま返すとエラーの画面になる。
  # 代わりに index.html を 200 で返せば、ブラウザで vue-router が URL を見て詳細ページを出す
  # （router.ts の createWebHistory のコメントで「9e で要る」と書いた設定。ローカルでは Vite が同じことをしていた）。
  #
  # 404 だけでなく 403 も書くのは、S3 が「無い」を 403 で返すため。CloudFront にはバケットの一覧を見る権限を
  # 渡していないので、S3 は「無い」のか「読めない」のかを言わずに 403 にする（何があるかを外から探られないように）。
  #
  # 割り切り：本当に無いファイル（消えた古い JS など）を頼まれても、index.html が 200 で返る。
  # 分けたくなったら、拡張子の無いパスだけを index.html に書き換える CloudFront Functions に切り替える。
  custom_error_response {
    error_code         = 403
    response_code      = 200
    response_page_path = "/index.html"
  }
  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
  }

  # 国による制限はしない（書く決まりのブロック）
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # HTTPS の証明書は CloudFront の既定（*.cloudfront.net）を使う。自分のドメインを使うときは ACM の証明書に替える
  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# web バケットのバケットポリシー（2つの文）:
#   ①この CloudFront だけに、ファイルを読ませる（OAC の署名付きリクエストを受け付ける）
#   ②HTTPS 以外のリクエストは拒否する（uploads と同じ文）
resource "aws_s3_bucket_policy" "web" {
  count = var.enable_cloudfront ? 1 : 0 # ①がディストリビューションを指すので、作るときだけ作る

  bucket = aws_s3_bucket.web.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontRead"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject" # 読むだけ。一覧（ListBucket）は渡さない（だから無いファイルは 403 になる）
        Resource  = "${aws_s3_bucket.web.arn}/*"

        # 「CloudFront なら何でも」ではなく「このディストリビューションからのみ」に絞る。
        # これが無いと、他人のアカウントの CloudFront からもこのバケットを読めてしまう（SQS のキューポリシーと同じ考え方）
        Condition = {
          StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.web[0].arn }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.web.arn, "${aws_s3_bucket.web.arn}/*"]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.web]
}

# 画面を開く URL。スマホではこれを開く
output "web_url" {
  # one：要素が1個ならそれを、0個なら null を返す（CloudFront を作っていない間は null）
  value = one([for d in aws_cloudfront_distribution.web : "https://${d.domain_name}"])
}

# make deploy-web が使う（ファイルを置く先と、キャッシュを消す相手）
output "web_bucket" {
  value = aws_s3_bucket.web.id
}

output "web_distribution_id" {
  value = one(aws_cloudfront_distribution.web[*].id) # [*]：作った分の id を一覧にする
}
