# 入力値。コードに直接書きたくないもの、環境ごとに変えたいものを外に出す。
#
# 値の渡し方は3つある（優先度は下が強い）:
#   1. default（下に書いたもの）
#   2. terraform.tfvars ファイル      ← 今回これ。gitignore 済み
#   3. 環境変数 TF_VAR_alert_email
#   4. コマンドライン -var="alert_email=..."

variable "alert_email" {
  type        = string
  description = "DLQ にメッセージが入ったときの通知先。空なら購読を作らない"

  # default を書かないと「必須」になり、入力を求められる。
  # ここでは「未設定なら通知なしで動く」ようにしたいので空文字を既定にする。
  default = ""
}

variable "vision_model_id" {
  type        = string
  description = "写真を解析するモデル（Bedrock のモデルID。jp. は付けない）"

  # 本命は Claude（anthropic.claude-haiku-4-5-20251001-v1:0）。
  # Anthropic の利用申請がサポート待ちなので、それまで Nova 2 Lite を代役にする。
  # どちらも「jp. の推論プロファイル経由・送信先は東京と大阪」で形が同じなので、
  # 承認されたらこの1行を変えるだけで差し替わる。
  default = "amazon.nova-2-lite-v1:0"
}

variable "analyzer" {
  type        = string
  description = "bedrock なら本物のモデルを呼ぶ。dummy なら呼ばずに決め打ちの答えを返す"

  # このアカウントでは Bedrock のモデル呼び出しが止められている（サポート待ち）。
  # それまで dummy で保存の流れを作り、解除されたら bedrock に戻す。
  default = "dummy"

  # 決まった値以外を渡したら、plan の時点で止める（打ち間違いを apply まで持ち込まない）
  validation {
    condition     = contains(["bedrock", "dummy"], var.analyzer)
    error_message = "analyzer は bedrock か dummy のどちらか。"
  }
}

variable "token_validity_hours" {
  type        = number
  description = "Cognito の ID トークン・アクセストークンの有効期限（時間）"

  # dev は12時間：CLI（make token）で1日に1回ログインすれば作業が続けられるように。
  # 本番は 1 にする。アクセストークンは API Gateway が署名だけで確かめる（Cognito に問い合わせない）ので、
  # 発行した後で取り消せない。盗まれたときに使われてしまう時間を短くするため。
  # 画面（ステップ9）はリフレッシュトークンで裏側から自動で取り直すので、1時間でも利用者は困らない。
  default = 12

  validation {
    condition     = var.token_validity_hours >= 1 && var.token_validity_hours <= 24
    error_message = "token_validity_hours は 1〜24（Cognito が許す範囲）。"
  }
}

variable "enable_cloudfront" {
  type        = bool
  description = "画面の公開に CloudFront を使うか（false の間は作らない）"

  # 2026-10-13：このアカウントは「確認が済むまで CloudFront を新しく作れない」状態（クレジットカードの支払いの確定待ち）。
  # その間は画面を Cloudflare Workers で公開し、CloudFront のディストリビューションとそれに付くバケットポリシーを作らない。
  # 解除されたら true にして make apply → make deploy-web の公開先を S3 に戻す。
  default = false
}
