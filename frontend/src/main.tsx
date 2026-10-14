import CssBaseline from '@mui/material/CssBaseline'
import { ThemeProvider } from '@mui/material/styles'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from 'react-router/dom'
import { configureAuth } from './auth'
import { router } from './router'
import { theme } from './theme'

// 画面を作る前に、どの Cognito にログインするかを Amplify に教える（1回だけ）
configureAuth()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* ThemeProvider：theme.ts の色・文字を、中のすべての MUI の部品に配る */}
    <ThemeProvider theme={theme}>
      {/* CssBaseline：ブラウザごとの見た目の差（余白など）をならす */}
      <CssBaseline />
      {/* RouterProvider：URL に応じて、router.tsx の一覧からページを選んで出す */}
      <RouterProvider router={router} />
    </ThemeProvider>
  </StrictMode>,
)
