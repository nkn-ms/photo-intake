// 一番外側の部品。ログインしていなければフォーム、していれば上部のメニューと、URL に応じたページを出す
import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router'
import { getMe, type Me } from './api'
import { getAccessToken, logOut } from './auth'
import LoginForm from './components/LoginForm'
import { toErrorMessage } from './format'
import { MeContext } from './session'

export default function App() {
  const [me, setMe] = useState<Me | null>(null) // ログインしている人。null ならログインしていない
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 画面が出たときに1回。前回のログインが残っていれば、そのまま /me を読む
  // （開発中は StrictMode が2回呼ぶので、/me も2回出る。本番のビルドでは1回）
  useEffect(() => {
    void loadMe()
  }, [])

  async function loadMe(): Promise<void> {
    setLoading(true)
    setError(null)
    try {
      const token = await getAccessToken()
      if (token === null) {
        setMe(null) // トークンが無い ＝ ログインしていない。API は呼ばない
        return
      }
      setMe(await getMe())
    } catch (e) {
      setError(toErrorMessage(e))
    } finally {
      setLoading(false) // return で抜けても finally は必ず通る
    }
  }

  async function onLogOut(): Promise<void> {
    await logOut()
    setMe(null)
  }

  // 状態ごとに出すものを、ガード節で上から順に決める
  function body() {
    if (loading) {
      return <p>読み込み中…</p>
    }
    if (me === null) {
      return <LoginForm onLoggedIn={loadMe} />
    }
    return (
      // ここから内側のページは、useMe() でログインしている人を読める（session.ts）
      <MeContext value={me}>
        <nav>
          {/* NavLink：ページを読み込み直さずに URL を切り替えるリンク。end は「/ ちょうどのときだけ今のページ扱い」 */}
          <NavLink to="/" end>
            登録
          </NavLink>
          <NavLink to="/items">一覧</NavLink>
          <span>{me.groups.join('・')}</span>
          <button onClick={onLogOut}>ログアウト</button>
        </nav>
        {/* Outlet：今の URL に対応するページ（router.tsx の children）がここに出る */}
        <Outlet />
      </MeContext>
    )
  }

  return (
    <>
      <h1>photo-intake</h1>
      {body()}
      {error && <p className="error">{error}</p>}
    </>
  )
}
