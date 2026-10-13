<script setup lang="ts">
// 一番外側の部品。ログインしていなければフォーム、していれば上部のメニューと、URL に応じたページを出す
import { onMounted, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import { ApiError, getMe } from './api'
import { getAccessToken, logOut } from './auth'
import LoginForm from './components/LoginForm.vue'
import { me } from './session'

const loading = ref(true)
const error = ref<string | null>(null)

// 画面が出たときに1回（React の useEffect(…, [])）。前回のログインが残っていれば、そのまま /me を読む
onMounted(loadMe)

async function loadMe(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const token = await getAccessToken()
    if (token === null) {
      me.value = null // トークンが無い ＝ ログインしていない。API は呼ばない
      return
    }
    me.value = await getMe()
  } catch (e) {
    error.value = e instanceof ApiError ? `${e.status}：${e.message}` : String(e)
  } finally {
    loading.value = false // return で抜けても finally は必ず通る
  }
}

async function onLogOut(): Promise<void> {
  await logOut()
  me.value = null
}
</script>

<template>
  <h1>photo-intake</h1>
  <!-- v-if / v-else-if / v-else：条件で出し分ける（React の三項演算子・&&） -->
  <p v-if="loading">読み込み中…</p>
  <LoginForm v-else-if="me === null" @logged-in="loadMe" />
  <template v-else>
    <nav>
      <!-- RouterLink：ページを読み込み直さずに URL を切り替えるリンク（Next.js の <Link>） -->
      <RouterLink to="/">登録</RouterLink>
      <RouterLink to="/items">一覧</RouterLink>
      <span>{{ me.groups.join('・') }}</span>
      <button @click="onLogOut">ログアウト</button>
    </nav>
    <!-- RouterView：今の URL に対応するページ（router.ts の一覧）がここに出る -->
    <RouterView />
  </template>
  <p v-if="error" class="error">{{ error }}</p>
</template>

<style scoped>
nav {
  display: flex;
  gap: 16px;
  align-items: center;
  margin-bottom: 16px;
}
</style>
