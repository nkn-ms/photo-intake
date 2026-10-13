/**
 * ログインしている人を、どの画面からも読めるように1か所に置く。
 *
 * ファイルの一番外で作った ref は、import したどの部品でも同じ1つを指す（アプリ全体で共有される）。
 * 小さなアプリなら、これで状態管理のライブラリ（Pinia。React なら Redux・Zustand）の代わりになる。
 * 書き込むのは App.vue（ログイン・ログアウトのとき）だけ。画面（pages/）は読むだけ。
 */
import { computed, ref } from 'vue'
import type { Me } from './api'

/** ログインしている人。null ならログインしていない */
export const me = ref<Me | null>(null)

/**
 * ログインしている人が admin（拾得物の確定など、管理の操作ができる人）なら true。
 * Cognito の admin グループに入っているかを、/me が返した groups で見ている。
 * ログインしていない（me が null）ときは false。
 *
 * 使い道：admin でない人（staff）には、確定のフォームを出さない。
 * ただし画面で隠すのは「押しても 403（権限が無いというエラー）になるボタンを見せない」ためだけ。
 * 画面の判定はブラウザの中で書き換えられるので、本当に守っているのは API 側の require_admin（backend/api/auth.py）。
 *
 * computed：元の値（me）が変わると、自動で計算し直される値（React の useMemo に近い）。
 */
export const isAdmin = computed(() => me.value?.groups.includes('admin') ?? false)
