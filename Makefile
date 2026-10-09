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

# plan / apply / destroy の前に、API の Lambda の zip の中身（.build/api）を組み立てる。
# Terraform は plan の時点でそのフォルダを読んで zip にするので、無いと失敗する。
plan: build-api  ## 何が変わるかを表示するだけ。変更しない
	$(TF) plan

apply: build-api ## 実際に AWS に反映する。yes の入力を求められる
	$(TF) apply

destroy: build-api ## 作ったものを消す
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

# バケットの中身を全部消す。バケット自体は残る（消すのは make destroy）。
# destroy の前に使う。中身が残っていると Terraform はバケットを消せない。
# バージョニングを切っているので、消したファイルは戻せない。先に一覧を出し、yes と打ったときだけ消す。
# DB の items の行は残る（写真だけが消え、行と合わなくなる）。
.PHONY: empty-bucket
empty-bucket:     ## S3 の中身を全部消す（一覧を見せ、yes で実行）
	@b=$(BUCKET); \
	n=$$(aws s3 ls "s3://$$b/" --recursive | wc -l | tr -d ' '); \
	if [ "$$n" = 0 ]; then echo "s3://$$b は空です"; exit 0; fi; \
	aws s3 rm "s3://$$b/" --recursive --dryrun; \
	read -p "上の $$n 件を消します。よければ yes: " ans; \
	[ "$$ans" = yes ] || { echo "やめました"; exit 0; }; \
	aws s3 rm "s3://$$b/" --recursive

# S3 の通知と同じ形のメッセージを、手で SQS に送る。重複配信（同じ通知が2回届く）の再現用。
#   make resend KEY=uploads/wallet.jpg
# worker が読むのは bucket.name と object.key だけなので、それだけ入れている。
# 本物の通知ではキーが URL エンコードされる（空白は + など）。英数字だけのキーならそのままでよい。
.PHONY: resend
KEY ?= uploads/test.txt
resend:           ## S3 の通知と同じ形のメッセージを手で送る
	aws sqs send-message --queue-url $(QUEUE) \
	  --message-body '{"Records":[{"s3":{"bucket":{"name":"$(BUCKET)"},"object":{"key":"$(KEY)"}}}]}'

# --- DB ---
.PHONY: sql

# Data API で SQL を1本送る。
#   make sql SQL="SELECT version()"
# ARN は terraform output（ローカルの state）から取るので、ここでは AWS に触らない。
# 一時停止中だと DatabaseResumingException が返るので、十数秒おいて打ち直す。
SQL ?= SELECT 1
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

# マイグレーションのファイルを番号順に、それぞれの文を 1 番から順にすべて流す。
#   make migrate                                                   … 全部（destroy → apply した後の空の DB 用）
#   make migrate MIGRATIONS=backend/migrations/002_features_jsonb.sql … 指定したファイルだけ（今ある DB に足す用）
# 流し済みのファイルを覚える仕組みは無い。今ある DB に全部を流すと、001 の CREATE TABLE が
# 「もうある」で失敗して止まる（壊れはしない）。今ある DB には、新しいファイルだけを指定する。
#
# ① まず DB を起こす。一時停止中は DatabaseResumingException が返るので、
#    SELECT 1 が通るまで10秒おきに打ち直す（一度起きれば5分は止まらない）。
#    上限は12回（約2分）。ログイン切れや apply 忘れだと何回打っても通らないので、
#    上限なしだと何も表示されないまま永遠に待つことになる。
#    待っている間の出力は捨てる（>/dev/null 2>&1）。12回目に失敗したときだけ、
#    捨てずにもう一度 SELECT 1 を送って、本当の理由を画面に出す。
# ② 起きてから文を順に流す。どれか1つでも失敗したらそこで止まる。
#    ここは打ち直さない。SQL の書き間違いだと永遠に終わらなくなるため。
#
# ※ 途中で失敗すると、それまでに作ったものは残る（トランザクションにしていない）。
#   dev なら make destroy → make apply で作り直せばよい。
# 文の数は awk で数える（NF は「空白以外の中身があるかたまり」だけを数えるため）。
.PHONY: migrate
# $(wildcard …) は一致するファイルの一覧、$(sort …) はそれを名前順に並べる（001 → 002 → …）
MIGRATIONS ?= $(sort $(wildcard backend/migrations/*.sql))
migrate:          ## マイグレーションの文をすべて順に流す
	@echo "--- DB を起こす（止まっていたら起きるまで待つ）"
	@for i in $$(seq 1 12); do \
	  $(MAKE) -s sql SQL="SELECT 1" >/dev/null 2>&1 && break; \
	  [ $$i -eq 12 ] && { echo "DB に繋がらない。最後のエラー："; $(MAKE) -s sql; exit 1; }; \
	  echo "  起動を待っています… ($$i/12)"; sleep 10; \
	done
	@for f in $(MIGRATIONS); do \
	  total=$$(awk -v RS=';' 'NF{c++} END{print c}' $$f); \
	  for n in $$(seq 1 $$total); do \
	    echo "--- $$f 文 $$n/$$total"; \
	    $(MAKE) -s stmt-run N=$$n MIGRATION=$$f || exit 1; \
	  done; \
	done; \
	echo "--- 完了：$(MIGRATIONS)"

# --- Cognito ---
.PHONY: user token password

# Cognito の規則を満たす強いパスワードを作り、画面に出さずにクリップボードへ入れる（AWS には触らない）。
#   make password  → パスワードマネージャーに貼って保存 → make user / make token の入力にも貼る
# ランダムな18バイトを文字にして、末尾に Aa1! を足す（大文字・小文字・数字・記号を必ず含めるため）。
password:         ## 強いパスワードを作ってクリップボードに入れる
	@printf '%s' "$$(openssl rand -base64 18)Aa1!" | pbcopy
	@echo "クリップボードに入れました。パスワードマネージャーに貼って保存してください"

# User Pool とアプリクライアントの ID は terraform output（ローカルの state）から取る。
POOL   = $$($(TF) output -raw cognito_user_pool_id)
CLIENT = $$($(TF) output -raw cognito_client_id)

# ユーザーを1人作って、グループに入れる（admin だけがユーザーを作れる設定なので、管理者の権限で作る）。
#   make user EMAIL=you@example.com GROUP=staff
# パスワードは画面に出さずに入力させる（コマンドに書くとシェルの履歴に残るため）。
# パスワードの規則は Cognito の既定：8文字以上で、大文字・小文字・数字・記号をそれぞれ1つ以上含む。
#   --message-action SUPPRESS … 招待メールを送らない（CLI で作って、そのまま使うため）
#   --permanent               … 「初回ログインでパスワードを変えさせる」状態にしない
EMAIL ?= staff@example.com
GROUP ?= staff
user:             ## Cognito にユーザーを作ってグループに入れる
	@test -n "$(EMAIL)" || { echo "EMAIL= を指定してください"; exit 1; }
	@read -s -p "パスワード: " PW; echo; \
	aws cognito-idp admin-create-user --user-pool-id "$(POOL)" --username "$(EMAIL)" \
	  --user-attributes Name=email,Value="$(EMAIL)" Name=email_verified,Value=true \
	  --message-action SUPPRESS >/dev/null && \
	aws cognito-idp admin-set-user-password --user-pool-id "$(POOL)" --username "$(EMAIL)" \
	  --password "$$PW" --permanent && \
	aws cognito-idp admin-add-user-to-group --user-pool-id "$(POOL)" --username "$(EMAIL)" \
	  --group-name "$(GROUP)" && \
	echo "作成: $(EMAIL)（グループ: $(GROUP)）"

# ログインしてトークンを受け取り、JWT の中身（真ん中の部分）を読める形で表示する。
#   make token EMAIL=you@example.com
# アクセストークンは .build/access_token に保存する（8b で API に付けて送る。.build/ は gitignore 済み）。
# JWT は「ヘッダー.中身.署名」を . でつないだもの。中身は Base64 なので、暗号を解かなくても読める。
DECODE_JWT = python3 -c "import sys,json,base64; p=sys.stdin.read().strip().split('.')[1]; print(json.dumps(json.loads(base64.urlsafe_b64decode(p+'='*(-len(p)%4))),indent=2,ensure_ascii=False))"
token:            ## ログインして JWT の中身を表示する
	@test -n "$(EMAIL)" || { echo "EMAIL= を指定してください"; exit 1; }
	@mkdir -p .build
	@read -s -p "パスワード: " PW; echo; \
	aws cognito-idp initiate-auth --client-id "$(CLIENT)" --auth-flow USER_PASSWORD_AUTH \
	  --auth-parameters USERNAME="$(EMAIL)",PASSWORD="$$PW" --output json > .build/auth.json || exit 1; \
	python3 -c "import json; r=json.load(open('.build/auth.json'))['AuthenticationResult']; open('.build/access_token','w').write(r['AccessToken']); open('.build/id_token','w').write(r['IdToken'])"; \
	rm .build/auth.json; \
	echo "--- ID トークンの中身"; $(DECODE_JWT) < .build/id_token; \
	echo "--- アクセストークンの中身"; $(DECODE_JWT) < .build/access_token

# --- API の Lambda の組み立て ---
.PHONY: build-api

# API の Lambda に載せるもの（自分のコード＋FastAPI などのライブラリ）を .build/api に集める。
# Terraform（archive_file）がこのフォルダを zip にして Lambda に上げる。AWS には触らない。
#
# ① pyproject.toml の api グループの、uv.lock で決まった版の一覧を書き出す
# ② その一覧を「Lambda 用（Linux・x86_64・Python 3.12）」の版で .build/api に入れる。
#    Pydantic の中身（pydantic-core）は機械語に変換済みの部品で、Mac 用の版は Lambda では動かないため。
#    --only-binary :all: … 変換済みの版だけを使う（ローカルで変換し始めて、Mac 用ができてしまうのを防ぐ）
# ③ 自分のコード（backend/api の中身。routers/ などのフォルダごと）を同じフォルダに入れる。
#    __pycache__（ローカルの Python が作るキャッシュ）は載せない
# 毎回フォルダを消して作り直す（前回の残りが紛れ込まないように）。
API_BUILD = .build/api
build-api:        ## API の Lambda に載せるものを .build/api に集める
	@rm -rf $(API_BUILD) && mkdir -p $(API_BUILD)
	@cd backend && uv export --quiet --only-group api --no-hashes --format requirements-txt > ../.build/api-requirements.txt
	@uv pip install --quiet --target $(API_BUILD) \
	  --python-platform x86_64-manylinux2014 --python-version 3.12 --only-binary :all: \
	  -r .build/api-requirements.txt
	@rsync -a --exclude '__pycache__' backend/api/ $(API_BUILD)/
	@echo "組み立て完了: $(API_BUILD)（$$(du -sh $(API_BUILD) | cut -f1)）"

# API Gateway を通さずに、API の Lambda を直接1回呼ぶ（8b-3 の確認用）。
#   make invoke-api            → GET /health
#   make invoke-api ROUTE=/me  → GET /me（API Gateway を通っていないので 401 になるのが正しい）
# event は API Gateway（HTTP API）が送ってくる形を、最小限だけ真似たもの。
# Lambda の上で pydantic_core（Lambda 用の機械語）が読み込めるか、の確認にもなる。
.PHONY: invoke-api
ROUTE ?= /health
invoke-api:       ## API の Lambda を直接呼ぶ（API Gateway を通さない）
	@mkdir -p .build
	@aws lambda invoke --function-name photo-intake-dev-api --cli-binary-format raw-in-base64-out \
	  --payload '{"version":"2.0","routeKey":"GET $(ROUTE)","rawPath":"$(ROUTE)","rawQueryString":"","headers":{"host":"local"},"requestContext":{"http":{"method":"GET","path":"$(ROUTE)","protocol":"HTTP/1.1","sourceIp":"127.0.0.1","userAgent":"make"},"stage":"$$default"},"isBase64Encoded":false}' \
	  .build/invoke.json >/dev/null
	@cat .build/invoke.json; echo

# API Gateway の URL に curl でリクエストを出す（8b-4 から）。
#   make call                  → GET /me に、make token で保存したアクセストークンを付けて出す
#   make call ROUTE=/health    → GET /health（認証なしのルート）
#   make call TOKEN=none       → トークンを付けない → 401 になるのが正しい
#   make call TOKEN=id         → ID トークンを付ける → API Gateway は通すが、app.py が 401 で弾く
# -i … ステータス行やヘッダーも表示する
# api_url は末尾が / なので、ROUTE の先頭の / を外してつなぐ。
.PHONY: call
API_URL = $$($(TF) output -raw api_url)
TOKEN ?= access
call: ROUTE = /me
call:             ## API Gateway の URL に curl でリクエストを出す
	@if [ "$(TOKEN)" = none ]; then \
	  curl -s -i "$(API_URL)$(patsubst /%,%,$(ROUTE))"; \
	else \
	  curl -s -i -H "Authorization: Bearer $$(cat .build/$(TOKEN)_token)" "$(API_URL)$(patsubst /%,%,$(ROUTE))"; \
	fi; echo

# 拾得物を確定する（8d-2）。admin だけができる。人が確かめた値は決め打ち（財布・黒・二つ折り・革製）。
#   make confirm ID=<items の id>
# トークンは make token で最後にログインした人のもの。staff なら 403、admin なら 200 になるのが正しい。
.PHONY: confirm
confirm:          ## 拾得物を確定する（admin だけ）
	@test -n "$(ID)" || { echo "ID= に items の id を指定してください（make call ROUTE=/items で見られる）"; exit 1; }
	@curl -s -i -X POST -H "Authorization: Bearer $$(cat .build/access_token)" -H "Content-Type: application/json" \
	  -d '{"category": "財布", "color": "黒", "features": ["二つ折り", "革製"]}' \
	  "$(API_URL)items/$(ID)/confirm"; echo

# 写真を登録する（8c）。画面がやることを、curl で順にやる。
#   make register FILE=wallet.jpg
# ① POST /items（アクセストークンを付けて）→ API が items に行を作り、署名付きURLを返す
# ② その URL に写真を PUT する（API は通らない。S3 に直接）。Content-Type は①で渡したものと同じにする
# ③ あとは S3 → SQS → worker が動く。make items で status が analyzing → pending_review に変わるのを見る
# 拡張子から Content-Type を決める。API が受け付けない形式なら、ここで止める。
.PHONY: register items
register:         ## 写真を登録する（POST /items → 署名付きURLに PUT）
	@test -f "$(FILE)" || { echo "FILE= に写真を指定してください"; exit 1; }
	@case "$$(echo '$(FILE)' | tr 'A-Z' 'a-z')" in \
	  *.jpg|*.jpeg) ct=image/jpeg ;; *.png) ct=image/png ;; *.webp) ct=image/webp ;; *.gif) ct=image/gif ;; \
	  *) echo "対応していない形式です（jpg / png / webp / gif）"; exit 1 ;; \
	esac; \
	echo "--- ① POST /items（$${ct}）"; \
	curl -s -X POST -H "Authorization: Bearer $$(cat .build/access_token)" -H "Content-Type: application/json" \
	  -d "{\"content_type\": \"$$ct\"}" "$(API_URL)items" | tee .build/register.json; echo; \
	url=$$(python3 -c "import json; print(json.load(open('.build/register.json'))['upload_url'])") || exit 1; \
	echo "--- ② PUT（署名付きURLへ）"; \
	curl -s -o /dev/null -w "HTTP %{http_code}\n" -X PUT -H "Content-Type: $$ct" --upload-file "$(FILE)" "$$url"

# 最近登録した行を5件見る（Data API。--format-records-as JSON で列名付きの JSON にする）
items:            ## 最近登録した items を5件表示する
	@aws rds-data execute-statement \
	  --resource-arn "$$($(TF) output -raw db_cluster_arn)" \
	  --secret-arn "$$($(TF) output -raw db_secret_arn)" \
	  --database photo_intake --format-records-as JSON \
	  --sql "SELECT id, status, s3_key, registered_by, ai_model, created_at FROM items ORDER BY created_at DESC LIMIT 5" \
	  --query formattedRecords --output text | python3 -m json.tool --no-ensure-ascii
