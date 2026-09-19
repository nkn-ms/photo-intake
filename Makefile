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
