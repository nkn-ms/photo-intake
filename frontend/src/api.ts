/**
 * API（API Gateway → Lambda の FastAPI）を呼ぶ処理。
 *
 * 画面の部品は fetch を直接書かず、ここの関数（getMe など）を呼ぶ。
 * アクセストークンを付ける・エラーを ApiError にそろえる、を request の1か所にまとめる。
 */
import { getAccessToken } from './auth'

/** API が 2xx 以外を返したときの例外。画面はこれを捕まえて、ステータスで出し分ける。 */
export class ApiError extends Error {
  readonly status: number // 401・403・404・409・422・503 など
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

/** ログインしている人（sub とグループ）。 */
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
