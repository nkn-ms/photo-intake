/**
 * 写真を送る前に、ブラウザの中で縮小し、EXIF（撮影場所の GPS・機種・日時など）を消す。
 *
 * 仕組み：写真を canvas（画面の中の描画用の板）に描き直し、JPEG として書き出す。
 * 書き出した画像には元の EXIF が付かないので、縮小と EXIF の削除が1回の描き直しで済む。
 *
 * 画面の側でやる理由:
 *   ・署名付きURLの PUT では、ファイルの大きさを S3 の側で制限できない（API の make_upload_url のコメント）
 *   ・GPS は拾得場所の個人情報になりうる。S3 に置く前に消せば、どこにも残らない
 */

// 長い辺の上限。Claude（Bedrock）は約 1568px を超える画像を受け取った側で縮小して読むので、それより大きく送っても意味が無い。
// 画像の AI の料金は画素の数に比例するので、小さいほうが安い。ブランドの文字や傷を読むには足りる大きさ
const MAX_EDGE = 1568
// JPEG の画質（0〜1）。0.85 は見た目の劣化がほぼ分からず、大きさは十分小さくなる、よく使われる値
const JPEG_QUALITY = 0.85

/** 縮小し、EXIF を消した JPEG を返す。読めない形式（HEIC を Chrome で開いた、など）なら例外。 */
export async function shrinkPhoto(file: File): Promise<Blob> {
  // 写真を読み込む。imageOrientation: 'from-image' は、EXIF の「向き」の情報を見て正しい向きに回してから読む指定。
  // （スマホの写真は、画素は横向きのまま「縦に回して表示せよ」という情報だけを EXIF に持っていることが多い。
  //   EXIF を消すとこの情報も消えるので、消す前に回しておかないと、横倒しの写真になる）
  const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
  const size = fitWithin(bitmap.width, bitmap.height, MAX_EDGE)

  // 画面に出さない canvas（OffscreenCanvas）に、縮めた大きさで描き直す
  const canvas = new OffscreenCanvas(size.width, size.height)
  const context = canvas.getContext('2d')
  if (context === null) {
    throw new Error('この端末では画像を縮小できません')
  }
  context.drawImage(bitmap, 0, 0, size.width, size.height)
  bitmap.close() // 読み込んだ画像のメモリを手放す

  // JPEG として書き出す。ここで作られる画像には EXIF が付かない
  return await canvas.convertToBlob({ type: 'image/jpeg', quality: JPEG_QUALITY })
}

/**
 * 縦横の比を保ったまま、長い辺が maxEdge 以下になる大きさを返す。もともと小さければそのまま（拡大はしない）。
 * 例：4032×3024 → 1568×1176
 */
export function fitWithin(
  width: number,
  height: number,
  maxEdge: number,
): { width: number; height: number } {
  const scale = Math.min(1, maxEdge / Math.max(width, height))
  return { width: Math.round(width * scale), height: Math.round(height * scale) }
}
