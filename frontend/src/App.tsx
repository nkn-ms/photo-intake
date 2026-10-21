// 一番外側の部品。ログインしていなければフォーム、していれば上部のメニューと、URL に応じたページを出す。
// 公開のページ（router.tsx で handle に public: true）は、ログインしていなくても出す
import LogoutIcon from '@mui/icons-material/Logout'
import AppBar from '@mui/material/AppBar'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import Container from '@mui/material/Container'
import Toolbar from '@mui/material/Toolbar'
import Typography from '@mui/material/Typography'
import Alert from '@mui/material/Alert'
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useMatches } from 'react-router'
import { getMe, type Me } from './api'
import { getAccessToken, logOut } from './auth'
import LoginForm from './components/LoginForm'
import { toErrorMessage } from './format'
import { MeContext } from './session'

/** router.tsx でページに付ける handle の形 */
export interface RouteHandle {
  public?: boolean // true ならログインしていなくても出す
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null) // ログインしている人。null ならログインしていない
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // useMatches：今の URL に当たっているルート（App と、その中のページ）の一覧。どれかが public なら公開のページ
  const isPublicPage = useMatches().some((match) => (match.handle as RouteHandle | undefined)?.public === true)

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
    // 公開のページは、ログインの確認を待たずに出す。useMe() は使えない（MeContext の外）
    if (isPublicPage) {
      return (
        <Container maxWidth="lg" sx={{ py: 3 }}>
          <Outlet />
        </Container>
      )
    }
    if (loading) {
      return (
        <Box sx={{ display: 'flex', justifyContent: 'center', mt: 8 }}>
          <CircularProgress />
        </Box>
      )
    }
    if (me === null) {
      return (
        <Container maxWidth="xs" sx={{ mt: 8 }}>
          <LoginForm onLoggedIn={loadMe} />
        </Container>
      )
    }
    return (
      // ここから内側のページは、useMe() でログインしている人を読める（session.ts）
      <MeContext value={me}>
        <Container maxWidth="md" sx={{ py: 3 }}>
          <Outlet />
        </Container>
      </MeContext>
    )
  }

  return (
    <>
      <AppBar position="sticky" elevation={0}>
        <Toolbar sx={{ gap: 1 }}>
          <Typography variant="h6" component="h1" sx={{ fontWeight: 700, mr: 2 }}>
            photo-intake
          </Typography>
          {me && (
            <>
              {/* component={NavLink}：MUI のボタンの見た目のまま、中身を React Router のリンクにする。
                  NavLink は今のページのリンクに .active を付けるので、それを太字にする */}
              <Button color="inherit" component={NavLink} to="/" end sx={navSx}>
                登録
              </Button>
              <Button color="inherit" component={NavLink} to="/items" sx={navSx}>
                一覧
              </Button>
              <Button color="inherit" component={NavLink} to="/requests" sx={navSx}>
                問い合わせ
              </Button>
            </>
          )}
          {/* 構成はログインしていなくても見られるので、メニューにいつも出す */}
          <Button color="inherit" component={NavLink} to="/architecture" sx={navSx}>
            構成
          </Button>
          <Box sx={{ flexGrow: 1 }} />
          {me && (
            <>
              {me.groups.map((group) => (
                <Chip
                  key={group}
                  label={group}
                  size="small"
                  sx={{ color: 'inherit', borderColor: 'rgba(255,255,255,0.6)' }}
                  variant="outlined"
                />
              ))}
              <Button color="inherit" startIcon={<LogoutIcon />} onClick={onLogOut}>
                ログアウト
              </Button>
            </>
          )}
        </Toolbar>
      </AppBar>
      {body()}
      {error && (
        <Container maxWidth="sm">
          <Alert severity="error">{error}</Alert>
        </Container>
      )}
    </>
  )
}

// メニューのボタンの見た目。今のページ（.active）だけ下線を付ける
const navSx = {
  '&.active': { textDecoration: 'underline', textUnderlineOffset: '6px', fontWeight: 700 },
}
