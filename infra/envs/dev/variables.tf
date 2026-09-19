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
