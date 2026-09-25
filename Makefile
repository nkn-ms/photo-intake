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
DLQ    = $(shell $(AWSP) sqs get-queue-url --queue-name photo-intake-dev-ingest-dlq --query QueueUrl --output text)

# ファイルを1つ置いて通知を発火させる。FILE= で差し替えられる。
#   make upload FILE=~/Desktop/photo.jpg
FILE ?= /tmp/test.txt
upload:           ## uploads/ にファイルを置く
	@test -f $(FILE) || echo "test" > $(FILE)
	aws s3 cp $(FILE) s3://$(BUCKET)/uploads/$(notdir $(FILE))

# visibility-timeout を 2 にしているのは、0 だとメッセージが隠れず
# 1回の受信で同じものが何度も返るため（＝at-least-once と冪等性の話そのもの）。
# 2秒だけ隠して、すぐ戻す。覗くだけで消さない。
#
# receive-message はメッセージが無いとき「何も出力しない」ので、
# 空入力を JSON として読まないよう保護している。
PEEK = python3 -c "import sys,json; d=sys.stdin.read().strip(); ms=(json.loads(d).get('Messages',[]) if d else []); [print(json.dumps(json.loads(m['Body']),indent=2,ensure_ascii=False)) for m in ms] or print('  (メッセージなし)')"

peek:             ## 本体キューと DLQ の中身を覗く（消さない）
	@echo "--- 本体キュー ---"
	@aws sqs get-queue-attributes --queue-url $(QUEUE) --attribute-names ApproximateNumberOfMessages --query 'Attributes.ApproximateNumberOfMessages' --output text | sed 's/^/  件数: /'
	@aws sqs receive-message --queue-url $(QUEUE) --visibility-timeout 2 --max-number-of-messages 10 --output json | $(PEEK)
	@echo "--- DLQ ---"
	@aws sqs get-queue-attributes --queue-url $(DLQ) --attribute-names ApproximateNumberOfMessages --query 'Attributes.ApproximateNumberOfMessages' --output text | sed 's/^/  件数: /'
	@aws sqs receive-message --queue-url $(DLQ) --visibility-timeout 2 --max-number-of-messages 10 --output json | $(PEEK)

purge:            ## キューを空にする
	aws sqs purge-queue --queue-url $(QUEUE)

# --- DB ---
.PHONY: sql

# Data API で SQL を1本送る。
#   make sql SQL="select version()"
# ARN は terraform output（手元の state）から取るので、ここでは AWS に触らない。
# 一時停止中だと DatabaseResumingException が返るので、十数秒おいて打ち直す。
SQL ?= select 1
sql:              ## Data API で SQL を1本送る
	aws rds-data execute-statement \
	  --resource-arn "$$($(TF) output -raw db_cluster_arn)" \
	  --secret-arn "$$($(TF) output -raw db_secret_arn)" \
	  --database photo_intake \
	  --sql "$(SQL)"

# マイグレーションのファイルを、セミコロンで区切って N 番目の文だけ扱う。
# Data API には1回に1文ずつ送る。
#   make stmt N=2       … 2番目の文を表示するだけ（AWS に触らない）
#   make stmt-run N=2   … 2番目の文を Data API で流す
#
# awk の RS=';' は「セミコロンを区切りとして、ファイルをかたまりに分ける」、
# NR==n は「n 番目のかたまりだけ出す」という意味。
# ※ コメントの中にセミコロンを書くと区切りがずれるので書かないこと。
.PHONY: stmt stmt-run
MIGRATION ?= backend/migrations/001_init.sql
STMT = awk -v RS=';' -v n=$(N) 'NR==n' $(MIGRATION)

stmt:             ## マイグレーションの N 番目の文を表示する
	@$(STMT)

stmt-run:         ## マイグレーションの N 番目の文を Data API で流す
	aws rds-data execute-statement \
	  --resource-arn "$$($(TF) output -raw db_cluster_arn)" \
	  --secret-arn "$$($(TF) output -raw db_secret_arn)" \
	  --database photo_intake \
	  --sql "$$($(STMT))"

# マイグレーションの文を 1 番から順にすべて流す。空の DB に1回だけ流す前提。
#
# ① まず DB を起こす。一時停止中は DatabaseResumingException が返るので、
#    select 1 が通るまで10秒おきに打ち直す（一度起きれば5分は止まらない）。
#    上限は12回（約2分）。ログイン切れや apply 忘れだと何回打っても通らないので、
#    上限なしだと何も表示されないまま永遠に待つことになる。
#    待っている間の出力は捨てる（>/dev/null 2>&1）。12回目に失敗したときだけ、
#    捨てずにもう一度 select 1 を送って、本当の理由を画面に出す。
# ② 起きてから文を順に流す。どれか1つでも失敗したらそこで止まる。
#    ここは打ち直さない。SQL の書き間違いだと永遠に終わらなくなるため。
#
# ※ 途中で失敗すると、それまでに作ったものは残る（トランザクションにしていない）。
#   dev なら make destroy → make apply で作り直せばよい。
# 文の数は awk で数える（NF は「空白以外の中身があるかたまり」だけを数えるため）。
.PHONY: migrate
migrate:          ## マイグレーションの文をすべて順に流す
	@echo "--- DB を起こす（止まっていたら起きるまで待つ）"
	@for i in $$(seq 1 12); do \
	  $(MAKE) -s sql SQL="select 1" >/dev/null 2>&1 && break; \
	  [ $$i -eq 12 ] && { echo "DB に繋がらない。最後のエラー："; $(MAKE) -s sql; exit 1; }; \
	  echo "  起動を待っています… ($$i/12)"; sleep 10; \
	done
	@total=$$(awk -v RS=';' 'NF{c++} END{print c}' $(MIGRATION)); \
	for n in $$(seq 1 $$total); do \
	  echo "--- 文 $$n/$$total"; \
	  $(MAKE) -s stmt-run N=$$n || exit 1; \
	done; \
	echo "--- 完了：$(MIGRATION) の文 $$total 個をすべて流した"
