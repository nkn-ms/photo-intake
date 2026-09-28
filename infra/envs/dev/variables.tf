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
