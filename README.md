# photo-intake

**画像をアップロードすると、非同期で AI が内容を構造化して台帳にし、
自由文から意味検索で照合できるサーバーレス基盤。**

AWS 上のインフラはすべて Terraform で構築する。

想定する使い方は「物品の登録と照合」。在庫・備品の棚卸し、中古品の出品と探索、
拾得物の管理など、**「写真で登録して、言葉で探す」業務に共通して使える形**にしている。

> 🚧 **開発中。** 現在は S3 バケットと SQS キューまで。1つずつ追加していく。

## 目指す構成

```
スマホ / PC
  └→ CloudFront + S3（React + Vite / SPA）
       └→ Cognito で認証
       └→ API Gateway (HTTP API) → Lambda (FastAPI + Mangum)
              ├→ 署名付きURL の発行
              └→ Aurora Serverless v2 (PostgreSQL + pgvector) ※Data API
  └→ S3 に直接 PUT
       └→ S3 Event → SQS → Lambda (worker)
              ├→ Bedrock : 画像 → 構造化JSON + 埋め込み
              ├→ Aurora に保存
              └→ LINE で通知
       └→ 失敗3回 → DLQ → CloudWatch Alarm → SNS
```

## 設計判断

### なぜ SQS を挟むのか
画像の AI 解析には数秒かかる。同期処理ではアップロード API がタイムアウトするか、
クライアントを待たせることになる。**アップロード完了の応答だけ即座に返し、解析は後ろで回す。**
この分離がこの構成の中心にある。

### なぜ Aurora Serverless v2 + Data API なのか
登録された物品と照会の突合が本質的な要件で、条件の組み合わせで絞り込むためアクセスパターンが
固定できない。よって RDB を選んだ。さらに `pgvector` でベクトル類似検索まで同じ DB で賄える。

通常 RDB を使うと連鎖でコストが増える。

```
RDB は VPC 内にしか置けない → Lambda を VPC に入れる
  → VPC 内の Lambda は既定で外に出られない
    → S3 / Bedrock / 外部 API に届かせるため NAT Gateway（月 $45）が要る
```

**Data API は HTTPS で SQL を受けるため、Lambda を VPC の外に置いたまま DB を叩ける。**
結果として NAT Gateway も VPC エンドポイントも不要になる。
VPC は Aurora の置き場として作るが、IGW も NAT も置いていない。

### なぜ Lambda で、ECS Fargate ではないのか
実行が断続的でリクエスト数も少ないため、常時起動のコストが無駄になる。
**常時負荷があるなら Fargate を選ぶ。**

### 失敗したらどうなるか
SQS が3回リトライし、それでも失敗したメッセージは DLQ に退避する。
DLQ にメッセージが入ると CloudWatch Alarm が SNS 経由で通知する。
該当レコードは `analysis_failed` 状態になり、管理画面から手動入力に回せる。**業務は止まらない。**

### AI の出力を上書きしない
`ai_result` に AI の生出力を、別カラムに人が確定した内容を持つ。
**両方を残すことで「AI がどれだけ当たっていたか」を後から測れる。**
上書きしてしまうと精度が永久に測定できなくなる。

### 冪等性
SQS は at-least-once 配信なので、同じメッセージが複数回届きうる。
AWS Lambda Powertools の `@idempotent` が DynamoDB に実行済みキーを記録し、二重実行を防ぐ。

### state 管理
tfstate は S3 に置き、S3 ネイティブロック（`use_lockfile`）で同時実行を防ぐ。
ローカルに置くと ①他人の変更を上書きする ②失うと復旧できない ③CI から触れない。

> Terraform 1.11 で `dynamodb_table` によるロックが非推奨になったため、ロック専用テーブルは持たない。

### drift（コードと実物のズレ）
コンソールで手作業をすると `terraform plan` に差分として現れる。
**手で直さず、コードを直す運用にしている。**

## セキュリティ

- 画像には個人情報が写りうるため、**S3 はパブリックアクセスを全ブロック**。
  閲覧・アップロードは**有効期限付きの署名付き URL** のみ
- 認証情報は長期のアクセスキーを使わず、**IAM Identity Center の一時認証情報**を使う
- DB のパスワードは Terraform が値を知らない（`manage_master_user_password` で
  AWS 側に生成させる）ため、**コードにも state にも平文で現れない**

## コスト

| 状態 | 月額 |
|---|---|
| `make destroy` 済み | **$0** |
| 稼働中 | 実測値を追記予定 |

Aurora Serverless v2 は最小容量 0 ACU（自動一時停止）に設定しているため、
無通信が続けばコンピュート課金は止まる。使わない期間は `make destroy` で消す。

## 使い方

```bash
make whoami    # どのアカウント・どのロールで作業しているか確認
make login     # 認証セッションが切れたら

make fmt       # 整形
make validate  # 文法と参照の検査（AWS に触らない）

make init      # provider の準備
make plan      # 差分の表示（何も変更しない）
make apply     # AWS に反映
make destroy   # 作ったものを消す
```

## 構成

```
infra/
└── envs/dev/          root module。terraform はここで実行する
backend/
└── migrations/        DB スキーマ
```

ディレクトリは**中身ができた時点で作る。** 空のディレクトリを先に置くと、
リポジトリが「あるはずのもの」について嘘をつくため。
