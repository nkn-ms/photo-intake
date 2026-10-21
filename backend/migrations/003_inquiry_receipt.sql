-- LINE の問い合わせ用アカウントで受け付けるための列と索引を、requests に足す（2026-10-21・問い合わせの受付 A）
--
-- 落とし主に返すのは受付番号だけ。requests の id（UUID）は長くて口頭で伝えにくいので、
-- 電話や窓口でも言える短い番号を別に持つ。
-- 落とし物の ID（items.id）は落とし主に見せない決まりなので、受付番号は items とは別の番号にする。
--
-- 今ある DB には、このファイルだけを流す（make migrate MIGRATIONS=backend/migrations/003_inquiry_receipt.sql）。
-- make migrate はセミコロンで文を区切るので、コメントの中にセミコロンを書かない。

-- 文1：受付番号。GENERATED ALWAYS AS IDENTITY は、行を足すたびに DB が 1, 2, 3 … と番号を振る列。
-- ALWAYS にしているので、アプリが番号を指定して入れることはできない（番号の重複や飛ばし書きを防ぐ）。
-- 今ある行にも、この文を流した時点で番号が振られる。
-- 番号は飛ぶことがある（INSERT が失敗したり、文2の ON CONFLICT で行を足さなかったりしても、番号は1つ進むため）。
-- 受付番号は「問い合わせを区別する」ためのもので、件数を数えるものではないので、飛んでも困らない。
-- 画面と LINE では「A-0012」の形で見せる（頭の A と 0 埋めはアプリが付ける。DB は数だけを持つ）。
ALTER TABLE requests
  ADD COLUMN receipt_no BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE;

-- 文2：LINE のメッセージの ID。LINE は、受け口の返事が遅いと同じメッセージを送り直すことがある。
-- UNIQUE にしておけば、同じメッセージの2回目は INSERT … ON CONFLICT DO NOTHING で行が増えず、
-- 受付番号も2つ振られない。
-- LINE 以外（将来の画面のフォームなど）から入った行は null のまま。UNIQUE は null 同士を重複と見なさない。
ALTER TABLE requests
  ADD COLUMN line_message_id TEXT UNIQUE;

-- 文3：1人1日5件の上限を数えるための索引。
-- 数える SQL は「この LINE のユーザーが、直近24時間に何件送ったか」
--（WHERE line_user_id = :user AND created_at > now() - INTERVAL '1 day'）。
-- この2列の順で索引があれば、そのユーザーの行だけを新しい順にたどれるので、表が大きくなっても数えるのが速い。
CREATE INDEX requests_line_user_id_created_at_idx
  ON requests (line_user_id, created_at DESC);
