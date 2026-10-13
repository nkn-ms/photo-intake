/**
 * API（API Gateway → Lambda の FastAPI）を呼ぶ処理。
 *
 * 画面の部品は fetch を直接書かず、ここの関数（getMe など）を呼ぶ。
 * アクセストークンを付ける・エラーを ApiError にそろえる、を request の1か所にまとめる。
 */
import { getAccessToken } from './auth'

/** API が 2xx 以外を返したときの例外。画面はこれを捕まえて、ステータスで出し分ける。 */
export class ApiError extends Error {
  // HTTP のステータス番号。401 ログインしていない・403 権限が無い・404 見つからない・
  // 409 今の状態ではできない・422 送った値の形が違う・503 DB が起動中、など
  readonly status: number
  readonly retryAfterSeconds: number | null // 503 のとき「何秒後にやり直せばよいか」

  constructor(status: number, message: string, retryAfterSeconds: number | null) {
    super(message)
    this.status = status
    this.retryAfterSeconds = retryAfterSeconds
  }
}

// ===========================================================================
// ルートごとの関数（API の app.py・routers/items.py と1対1）
// ===========================================================================

/** GET /me の返り値（API の auth.py の User と同じ形） */
export interface Me {
  sub: string
  groups: string[]
}

/** ログインしている人を API に聞く。sub（Cognito のユーザー ID）と、入っているグループ（staff / admin）が返る。 */
export function getMe(): Promise<Me> {
  return request<Me>('GET', '/me')
}

/** POST /items に送るもの（API の routers/items.py の NewItem と同じ形） */
export interface NewItem {
  content_type: 'image/jpeg' | 'image/png' | 'image/webp' | 'image/gif'
  location?: string | null // 拾得場所（任意）
  registered_at?: string | null // 拾得日時（任意。省略したら API が今の時刻にする）
}

/** POST /items の返り値（API の CreatedItem と同じ形） */
export interface CreatedItem {
  item_id: string
  upload_url: string // 写真を PUT する先（署名付きURL）
  upload_headers: Record<string, string> // PUT のときに付けるヘッダー（署名に含まれている）
  expires_in: number // upload_url が使える残り秒数
}

/** 拾得物を登録する。API が行を作り（analyzing）、写真を置くための署名付きURLを返す。 */
export function createItem(newItem: NewItem): Promise<CreatedItem> {
  return request<CreatedItem>('POST', '/items', newItem)
}

/** 拾得物の状態（API の routers/items.py の ItemStatus と同じ7つ） */
export type ItemStatus =
  | 'analyzing' // 写真を待っている・AI が解析中
  | 'analysis_failed' // 解析が最後まで失敗した（人が手で入力して確定できる）
  | 'pending_review' // AI の答えが出て、人の確認待ち
  | 'confirmed' // admin が確定した
  | 'matched' // 落とし主の問い合わせと結び付いた（ステップ10）
  | 'closed' // 返却などで終わった
  | 'archived' // 保管期限を過ぎて片付けた

/**
 * GET /items の1件分（API の ItemSummary と同じ形）。
 * 日時は文字列で届く（JSON に日時の型は無い）。'2026-10-13T01:23:33Z' のような UTC の形。
 */
export interface ItemSummary {
  id: string
  status: ItemStatus
  category: string | null // 人が確定した種別（未確定なら null）
  ai_category: string | null // AI が答えた種別（解析前・失敗なら null）
  location: string | null
  registered_at: string
  created_at: string
}

/** 一覧を新しい順に50件読む。status を渡すと、その状態の拾得物だけにしぼる。 */
export function listItems(status?: ItemStatus): Promise<ItemSummary[]> {
  if (status === undefined) {
    return request<ItemSummary[]>('GET', '/items')
  }
  // URLSearchParams：?status=… の部分を作る（値に記号が入っても正しく変換してくれる）
  const query = new URLSearchParams({ status })
  return request<ItemSummary[]>('GET', `/items?${query}`)
}

/** GET /items/{id} と確定の返り値（API の ItemDetail と同じ形）。AI の答えと人の値を分けたまま持つ。 */
export interface ItemDetail {
  id: string
  status: ItemStatus
  photo_url: string // 写真を見るための署名付きURL（GET。5分で使えなくなる）
  registered_at: string
  location: string | null
  registered_by: string // 登録した人の sub

  // ▼ AI の答え。モデルが返した JSON をそのまま保存したもので、項目がそろっている保証は無い。
  // だから型は「中身の分からない辞書」にしておき、使う側で1項目ずつ確かめる
  ai_result: Record<string, unknown> | null
  ai_model: string | null
  ai_analyzed_at: string | null
  ai_error: string | null // analysis_failed の理由

  // ▼ 人が確定した値
  category: string | null
  color: string | null
  brand: string | null
  features: string[] | null
  confirmed_by: string | null
  confirmed_at: string | null
  retention_deadline: string | null // 保管期限。'2027-01-13' のような日付だけの形

  // ▼ 確定の後の手続き（今はいつも null）
  police_reported_at: string | null
  matched_request_id: string | null
  matched_at: string | null
  created_at: string
  updated_at: string
}

/** 拾得物を1件読む。無ければ ApiError（404）。 */
export function getItem(id: string): Promise<ItemDetail> {
  // encodeURIComponent：URL の中で特別な意味を持つ文字（/ や ?）を、ただの文字として送れる形に変える
  return request<ItemDetail>('GET', `/items/${encodeURIComponent(id)}`)
}

/** POST /items/{id}/confirm に送るもの（API の Confirmation と同じ形）。admin が確かめた値。 */
export interface Confirmation {
  category: string // 必須。空の文字列は 422
  color: string | null
  brand: string | null
  features: string[] | null
}

/**
 * 拾得物を確定する（admin だけ）。確定後の詳細が返る。
 * 失敗は ApiError：403 admin でない・404 無い・409 確定できない状態か、ほかの人が先に状態を変えた。
 */
export function confirmItem(id: string, confirmation: Confirmation): Promise<ItemDetail> {
  return request<ItemDetail>('POST', `/items/${encodeURIComponent(id)}/confirm`, confirmation)
}

// ===========================================================================
// S3（署名付きURL）
// ===========================================================================

/**
 * 署名付きURLに写真を PUT する。API は通らず、S3 に直接送る（make register の②と同じ）。
 *
 * アクセストークンは付けない。S3 は JWT を知らず、URL の署名だけで確かめるため。
 * ヘッダーは API が返した upload_headers をそのまま付ける（Content-Type が署名と違うと S3 が 403 で拒否する）。
 * 届いた後は S3 → SQS → worker が動き、行が analyzing → pending_review に進む。
 */
export async function uploadPhoto(created: CreatedItem, photo: Blob): Promise<void> {
  const response = await fetch(created.upload_url, {
    method: 'PUT',
    headers: created.upload_headers,
    body: photo,
  })
  if (!response.ok) {
    // S3 のエラーの本文は XML なので読まない。403 なら URL の期限切れ（5分）か Content-Type の違い
    throw new ApiError(response.status, `写真を送れませんでした（S3：${response.status}）`, null)
  }
}

// ===========================================================================
// 共通の処理
// ===========================================================================

/**
 * API を1回呼ぶ。アクセストークンがあれば Authorization: Bearer に付ける。
 * 2xx なら本文（JSON）を返し、それ以外なら ApiError を投げる。
 */
async function request<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const token = await getAccessToken()
  const headers: Record<string, string> = {}
  if (token !== null) {
    headers['Authorization'] = `Bearer ${token}`
  }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }

  // VITE_API_URL は末尾が / なので、path の先頭の / を外してつなぐ（Makefile の make call と同じ）
  const response = await fetch(import.meta.env.VITE_API_URL + path.replace(/^\//, ''), {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!response.ok) {
    throw await toApiError(response)
  }
  return (await response.json()) as T
}

/**
 * 2xx 以外のレスポンスを ApiError にする。
 *
 * 本文の形は、誰が返したかで違う:
 *   ・FastAPI（app.py・routers）… {"detail": "admin だけができる操作です"}
 *   ・FastAPI の 422            … {"detail": [{…どの項目がなぜ違うか…}]}（一覧）
 *   ・API Gateway の 401        … {"message": "Unauthorized"}（Lambda まで届いていない）
 */
async function toApiError(response: Response): Promise<ApiError> {
  const body = await response.json().catch(() => null) // 本文が JSON でないこともある
  const message =
    typeof body?.detail === 'string'
      ? body.detail
      : typeof body?.message === 'string'
        ? body.message
        : `${response.status} ${response.statusText}`
  // Retry-After は API Gateway の CORS 設定（expose_headers）で、JS から読めるようにしてある
  const retryAfter = response.headers.get('retry-after')
  return new ApiError(response.status, message, retryAfter !== null ? Number(retryAfter) : null)
}
