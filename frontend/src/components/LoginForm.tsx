// ログインのフォーム。ログインできたら、親（App）から受け取った onLoggedIn を呼ぶ
import { useState, type FormEvent } from 'react'
import { logIn } from '../auth'

export default function LoginForm({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false) // 送信中は二重に押せないようにする
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault() // 送信で画面が再読み込みされないようにする
    setSubmitting(true)
    setError(null)
    try {
      await logIn(email, password)
      onLoggedIn()
    } catch (err) {
      setError(toMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={onSubmit}>
      <label>
        メールアドレス
        <input
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label>
        パスワード
        <input
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <button disabled={submitting}>ログイン</button>
      {error && <p className="error">{error}</p>}
    </form>
  )
}

/**
 * ログインの失敗を、画面に出す文にする。
 * Cognito は「ユーザーがいない」も「パスワードが違う」も同じ NotAuthorizedException で返す
 * （8a で、存在しないユーザーを見分けられないように設定した）。
 */
function toMessage(e: unknown): string {
  if (e instanceof Error && e.name === 'NotAuthorizedException') {
    return 'メールアドレスかパスワードが違います'
  }
  return e instanceof Error ? e.message : String(e)
}
