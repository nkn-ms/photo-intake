.PHONY: whoami login init plan apply destroy fmt validate

ENV ?= dev
TF  = terraform -chdir=infra/envs/$(ENV)

AWS_PROFILE ?= photo-intake
export AWS_PROFILE

# --- 認証 ---
whoami:          ## いまどのアカウント・どのロールで作業しているか
	aws sts get-caller-identity

login:           ## セッション（4時間）が切れたら打ち直す
	aws sso login

# --- AWS に触らない ---
fmt:             ## コードを整形する
	terraform fmt -recursive infra

validate:        ## 文法と参照が正しいか検査する
	$(TF) validate

# --- AWS に触る ---
init:            ## provider を用意する（npm install に近い）
	$(TF) init

plan:            ## 何が変わるかを表示するだけ。変更しない
	$(TF) plan

apply:           ## 実際に AWS に反映する。yes の入力を求められる
	$(TF) apply

destroy:         ## 作ったものを消す
	$(TF) destroy

# --- 動作確認用 ---
.PHONY: upload peek purge

# $(shell ...) は make の export を引き継がないので、明示的にプロファイルを渡す。
# 遅延評価（= であって := ではない）にしてあるので、使うターゲットでだけ AWS を呼ぶ。
AWSP   = AWS_PROFILE=$(AWS_PROFILE) aws
BUCKET = photo-intake-dev-uploads-$(shell $(AWSP) sts get-caller-identity --query Account --output text)
QUEUE  = $(shell $(AWSP) sqs get-queue-url --queue-name photo-intake-dev-ingest --query QueueUrl --output text)

# ファイルを1つ置いて通知を発火させる。FILE= で差し替えられる。
#   make upload FILE=~/Desktop/photo.jpg
FILE ?= /tmp/test.txt
upload:           ## uploads/ にファイルを置く
	@test -f $(FILE) || echo "test" > $(FILE)
	aws s3 cp $(FILE) s3://$(BUCKET)/uploads/$(notdir $(FILE))

# visibility-timeout を 2 にしているのは、0 だとメッセージが隠れず
# 1回の受信で同じものが何度も返るため（＝at-least-once と冪等性の話そのもの）。
# 2秒だけ隠して、すぐ戻す。覗くだけで消さない。
peek:             ## キューのメッセージを覗く（消さない）
	@aws sqs get-queue-attributes --queue-url $(QUEUE) --attribute-names ApproximateNumberOfMessages --query Attributes --output json
	@aws sqs receive-message --queue-url $(QUEUE) --visibility-timeout 2 --max-number-of-messages 10 --output json | python3 -c "import sys,json; ms=json.load(sys.stdin).get('Messages',[]); [print(json.dumps(json.loads(m['Body']),indent=2,ensure_ascii=False)) for m in ms] or print('(メッセージなし)')"

purge:            ## キューを空にする
	aws sqs purge-queue --queue-url $(QUEUE)
