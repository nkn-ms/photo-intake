/**
 * ページの一覧（URL → 表示する部品）。
 *
 * Next.js はフォルダの置き方で URL が決まるが、React Router はここに一覧を書く。
 * App が全体の枠（ログインの確認・上部のメニュー）で、children のページは App の <Outlet /> に出る。
 * どのページもログインが前提（ログインしていなければ App がフォームを出し、ページは出さない）。
 */
import { createBrowserRouter } from 'react-router'
import App from './App'
import RegisterForm from './components/RegisterForm'
import ItemDetailPage from './pages/ItemDetailPage'
import ItemListPage from './pages/ItemListPage'

// createBrowserRouter：URL を /items/… の普通の形にする（# の付く形ではなく）。
// 公開先（Cloudflare Pages）で /items/… を直接開かれても index.html が返るので、そのまま動く
export const router = createBrowserRouter([
  {
    path: '/',
    Component: App,
    children: [
      { index: true, Component: RegisterForm }, // 登録（スマホで撮る人が最初に開く画面）。index は「/ そのもの」
      { path: 'items', Component: ItemListPage }, // 一覧
      // :id は URL の一部を引数として受け取る書き方（Next.js の [id]）。ページの中で useParams() で読む
      { path: 'items/:id', Component: ItemDetailPage }, // 詳細と確定
    ],
  },
])
