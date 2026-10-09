<script setup lang="ts">
// 一番外側の部品。ログインしていなければフォーム、していれば登録の画面を出す。
// 一覧・詳細の画面は 9d でここに足す
import { onMounted, ref } from 'vue'
import { ApiError, getMe, type Me } from './api'
import { getAccessToken, logOut } from './auth'
import LoginForm from './components/LoginForm.vue'
import RegisterForm from './components/RegisterForm.vue'

const me = ref<Me | null>(null) // ログインしている人。null ならログインしていない
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
  <section v-else>
    <p>ログイン中：{{ me.sub }}（{{ me.groups.join('・') }}）</p>
    <button @click="onLogOut">ログアウト</button>
    <RegisterForm />
  </section>
  <p v-if="error" class="error">{{ error }}</p>
</template>
