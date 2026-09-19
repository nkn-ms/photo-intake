-- 物品の取り込みと照合 初期スキーマ
-- Aurora Serverless v2 (PostgreSQL) に Data API 経由で適用する。
-- CREATE EXTENSION は Terraform では張れないので、初回デプロイ後に流す。

create extension if not exists vector;

create type item_status as enum (
  'analyzing',        -- SQS に積んだ直後。worker 待ち
  'analysis_failed',  -- 解析が失敗した。DLQ に落ちた分がここに見える
  'pending_review',   -- AI 解析済み、人の確認待ち
  'confirmed',        -- 担当者が内容を確定
  'matched',          -- 照会（requests）と紐付いた
  'closed',           -- 引き渡し・出荷などで完了
  'archived'          -- 保管期限切れ・廃棄
);

-- 登録された物品。写真1枚につき1行。
create table items (
  id                uuid primary key default gen_random_uuid(),

  -- S3 のキー。これが自然キーになるので、SQS の二重配信で行が増えない
  s3_key            text not null unique,

  status            item_status not null default 'analyzing',

  registered_at     timestamptz not null,
  location          text,                 -- 取得・保管場所
  registered_by     text not null,        -- Cognito の sub

  -- ▼ AI の生出力。人が修正しても「上書きしない」。
  --   精度を後から測るには、AI が何と答えたかを残しておく必要がある。
  ai_result         jsonb,
  ai_model          text,                 -- どのモデルの結果か（比較・再現のため）
  ai_analyzed_at    timestamptz,
  ai_error          text,                 -- analysis_failed のときの理由

  -- ▼ 人が確定した内容。ai_result との差分が「AI の間違い」になる
  category          text,
  color             text,
  brand             text,
  features          text,
  confirmed_by      text,
  confirmed_at      timestamptz,

  -- 保管期限。業務ごとに意味が変わる
  -- （在庫なら棚卸し期限、拾得物なら遺失物法の3ヶ月、など）
  retention_deadline date,

  -- 確定済みなら確定内容、未確定なら AI 出力を埋め込む（確定時に貼り直す）
  embedding         vector(1024),

  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index on items (status);
create index on items (registered_at desc);
create index on items (retention_deadline) where status in ('confirmed', 'matched');

-- 照会。「こういうものを探している」を自由文で受け取る。
-- これと items.embedding のコサイン距離で候補を出す＝この仕組みの本体。
create table requests (
  id                   uuid primary key default gen_random_uuid(),
  description          text not null,     -- 「黒い革の鍵ケース、水曜に渋谷駅で」
  occurred_on          date,
  location             text,
  line_user_id         text,              -- 通知先。無ければ通知しない
  notify_enabled       boolean not null default false,
  embedding            vector(1024),
  created_at           timestamptz not null default now()
);

-- HNSW を使う。件数が小さいうちは全件走査でも足りるが、
-- 「件数が増えたらどうする」への答えを先に用意しておくために張っている。
create index on items    using hnsw (embedding vector_cosine_ops);
create index on requests using hnsw (embedding vector_cosine_ops);
