-- items.features（人が確定した特徴）を、文字列（text）から jsonb の配列に変える（2026-10-09・8d-2）
--
-- 理由：AI の答え（ai_result の features）は ["二つ折り", "革製"] という配列。
-- 人の値も同じ形で持てば、AI と人の答えをそのまま比べられる（AI の精度を測る、がこの設計の肝）。
-- 文字列だと「、」で切り直す必要があり、区切りの書き方がぶれると比べられなくなる。
--
-- DB の形の変更は、001_init.sql を書き換えずに、新しい番号のファイルを足して積み重ねる
--（もう流した 001 を書き換えても、今ある DB には反映されないため）。

-- 文1：型を変える。USING は「今ある値を新しい型にどう変換するか」。
-- 今は全部の行で null の想定だが、文字列が入っていても捨てずに、1つだけ入った配列にする
ALTER TABLE items
  ALTER COLUMN features TYPE JSONB
  USING CASE WHEN features IS NULL THEN NULL ELSE jsonb_build_array(features) END;

-- 文2：配列以外（"革製" のような文字列や、{...} のようなオブジェクト）が入らないようにする。
-- jsonb はどんな JSON でも入ってしまうので、形の決まりは CHECK 制約で書く
ALTER TABLE items
  ADD CONSTRAINT items_features_is_array
  CHECK (features IS NULL OR jsonb_typeof(features) = 'array');
