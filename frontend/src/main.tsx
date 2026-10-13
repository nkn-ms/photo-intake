import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from 'react-router/dom'
import './index.css'
import { configureAuth } from './auth'
import { router } from './router'

// 画面を作る前に、どの Cognito にログインするかを Amplify に教える（1回だけ）
configureAuth()

// RouterProvider：URL に応じて、router.tsx の一覧からページを選んで出す
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
)
