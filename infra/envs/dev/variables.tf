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
  description = "写真を解析する先。anthropic＝Claude API、bedrock＝Bedrock、dummy＝呼ばずに決め打ちの答え"

  # このアカウントでは Bedrock のモデル呼び出しが止められている（サポート待ち。クレジットカードの支払いの確定が要る見込み）。
  # 2026-10-13 から Anthropic の API を直接呼ぶ（anthropic）。解除されたら bedrock に戻せる（写真が AWS の外に出ないため）。
  default = "anthropic"

  # 決まった値以外を渡したら、plan の時点で止める（打ち間違いを apply まで持ち込まない）
  validation {
    condition     = contains(["anthropic", "bedrock", "dummy"], var.analyzer)
    error_message = "analyzer は anthropic・bedrock・dummy のどれか。"
  }
}

variable "anthropic_model_id" {
  type        = string
  description = "analyzer = anthropic のときに使うモデル（Anthropic の API のモデル名）"

  # Claude Haiku 5.5（2026-10-07 公開）。写真から種別・色・ブランド・特徴を取り出す程度なら十分で、いちばん安くて速い。
  # 入力 $0.10・出力 $0.50（100万トークンあたり）で、Haiku 4.5 の1/10。
  # handler.py の ask_claude_api は 5.5 向けに書いてある（temperature を送らない・effort を送る）ので、
  # 4.5 以前に戻すときは handler.py も直す。
  # Bedrock のモデルID（anthropic.claude-haiku-5-5）とは書き方が違うので、vision_model_id とは分けている
  default = "claude-haiku-5-5"
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

variable "line_group_id" {
  type        = string
  description = "落とし物専用の LINE グループの ID（C から始まる33文字）。写真を登録してよいのは、このグループのメンバーだけ"

  # 空の間は、だれの写真も登録しない（グループが決まるまで、ボットを友だちに追加しただけの人が登録できないように）。
  # ID は、ボットをグループに招待したときに届く join イベントから取る（line-intake のログに出る）。
  # 人がログから写してここに書くことが、「このグループを落とし物グループにしてよい」という承認になる。
  #
  # PoC は1施設なので、グループ ID は一度決めたら変わらない設定として持つ。tfvars はローカルのファイルなので、
  # destroy しても残る（DB に置くと、作り直すたびにボットを招待し直すことになる）。
  # 複数の施設にするなら、施設の表と承認の手順を足して、グループを施設に結びつけて DB に置く。
  default = ""

  validation {
    condition     = var.line_group_id == "" || can(regex("^C[0-9a-f]{32}$", var.line_group_id))
    error_message = "line_group_id は空か、C から始まる33文字（C + 16進数32文字）の LINE のグループ ID。"
  }
}
