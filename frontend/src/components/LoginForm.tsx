// ログインのフォーム。ログインできたら、親（App）から受け取った onLoggedIn を呼ぶ
import Alert from '@mui/material/Alert'
import Button from '@mui/material/Button'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
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
    <Paper variant="outlined" sx={{ p: 4 }}>
      <Stack component="form" spacing={2} onSubmit={onSubmit}>
        <Typography variant="h5" component="h2" sx={{ fontWeight: 700 }}>
          ログイン
        </Typography>
        <TextField
          label="メールアドレス"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <TextField
          label="パスワード"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {/* loading：true の間は押せなくなり、くるくる回る印が出る（MUI v7 から Button に入った機能） */}
        <Button type="submit" variant="contained" size="large" loading={submitting}>
          ログイン
        </Button>
        {error && <Alert severity="error">{error}</Alert>}
      </Stack>
    </Paper>
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
