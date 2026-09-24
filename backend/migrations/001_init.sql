-- 拾得物（落とし物）の登録と、落とし主の問い合わせとの照合 初期スキーマ
-- Aurora Serverless v2 (PostgreSQL) に Data API 経由で適用する。
-- DB の中身（拡張機能・テーブル）は Terraform では作れないので、この SQL で作る。

create extension if not exists vector;

-- 落とし主からの問い合わせ。「こういうものを探している」を自由文で受け取る。
-- これと items.embedding のコサイン距離で候補を出す＝この仕組みの本体。
-- items から参照される（items.matched_request_id）ので、items より先に作る。
create table requests (
  id                   uuid primary key default gen_random_uuid(),
  description          text not null,     -- 「黒い革の鍵ケース、水曜に渋谷駅で」
  occurred_on          date,              -- 落とした日
  location             text,              -- 落とした場所
  line_user_id         text,              -- 通知先。無ければ通知しない
  notify_enabled       boolean not null default false,
  embedding            vector(1024),
  created_at           timestamptz not null default now()
);

-- 拾得物。写真1枚につき1行。
create table items (
  id                uuid primary key default gen_random_uuid(),

  -- S3 のキー。これが自然キーになるので、SQS の二重配信で行が増えない
  s3_key            text not null unique,

  -- 状態の流れ：
  --   analyzing → pending_review → confirmed → matched → closed
  --            ↘ analysis_failed（人が手で入力して pending_review へ）
  --                               confirmed → archived（保管期限切れ）
  --
  -- enum 型ではなく text + check にしている。
  --   ・値の削除や名前の変更が制約の書き換えだけで済む（PoC は要件が変わる前提）
  --   ・Data API はパラメータを文字列で渡すので、enum だと毎回 ::型名 の変換が要る
  status            text not null default 'analyzing'
                    check (status in (
                      'analyzing',        -- SQS に積んだ直後。worker 待ち
                      'analysis_failed',  -- 解析が失敗した。DLQ に落ちた分がここに見える
                      'pending_review',   -- AI 解析済み、人の確認待ち
                      'confirmed',        -- 担当者が内容を確定
                      'matched',          -- 問い合わせ（requests）と紐付いた
                      'closed',           -- 落とし主に返還した
                      'archived'          -- 保管期限切れ
                    )),

  registered_at     timestamptz not null,
  location          text,                 -- 拾得場所
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

  -- 遺失物法の保管期限（拾得日 + 3ヶ月）。確定時に入れる
  retention_deadline date,

  -- 警察に届け出た日時。null なら未届出。
  -- status とは別の軸（確定済みでも照合済みでも届出の有無はありうる）なので、別の列にしている。
  police_reported_at timestamptz,

  -- 一致した問い合わせ。status = 'matched' になったときに入れる。
  -- 拾得物を返せる相手は1人だけなので、items 側に1列で持つ
  --（1つの問い合わせが複数の拾得物に当たるのは許す。カバンと鍵が別々に見つかる、など）。
  -- 候補の一覧はその場でベクトル検索して出すだけなので保存しない。
  -- LINE で誰に知らせるか（requests.line_user_id）は、ここからたどる。
  matched_request_id uuid references requests(id),
  matched_at         timestamptz,

  -- 確定済みなら確定内容、未確定なら AI 出力を埋め込む（確定時に貼り直す）
  embedding         vector(1024),

  created_at        timestamptz not null default now(),
  -- ※ 更新時に自動では変わらない。更新する SQL の側で now() を入れる
  updated_at        timestamptz not null default now()
);

create index on items (status);
create index on items (registered_at desc);
create index on items (retention_deadline) where status in ('confirmed', 'matched');

-- HNSW を使う。件数が小さいうちは全件走査でも足りるが、
-- 「件数が増えたらどうする」への答えを先に用意しておくために張っている。
create index on items    using hnsw (embedding vector_cosine_ops);
create index on requests using hnsw (embedding vector_cosine_ops);
