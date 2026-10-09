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
