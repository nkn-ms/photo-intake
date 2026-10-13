/**
 * ページの一覧（URL → 表示する部品）。
 *
 * Next.js はフォルダの置き方で URL が決まるが、vue-router はここに一覧を書く。
 * どのページもログインが前提（ログインしていなければ App.vue がフォームを出し、ページは出さない）。
 */
import { createRouter, createWebHistory } from 'vue-router'
import RegisterForm from './components/RegisterForm.vue'
import ItemDetailPage from './pages/ItemDetailPage.vue'
import ItemListPage from './pages/ItemListPage.vue'

export const router = createRouter({
  // URL を /items/… の普通の形にする（# の付く形 /#/items/… ではなく）。
  // 公開したとき（9e）、/items/… を直接開かれても index.html を返すよう、CloudFront に設定が要る
  history: createWebHistory(),
  routes: [
    { path: '/', component: RegisterForm }, // 登録（スマホで撮る人が最初に開く画面）
    { path: '/items', component: ItemListPage }, // 拾得物の一覧（状態で絞り込める）
    // :id は URL の一部を引数として受け取る書き方（Next.js の [id]）。props: true で、部品の props.id に渡す
    // 拾得物1件の詳細。admin なら、この画面で内容を確定する
    { path: '/items/:id', component: ItemDetailPage, props: true },
  ],
})
