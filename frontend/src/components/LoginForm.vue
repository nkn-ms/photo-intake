<script setup lang="ts">
// ログインのフォーム。ログインできたら、親（App.vue）に logged-in を知らせる
import { ref } from 'vue'
import { logIn } from '../auth'

// 親に知らせる出来事（イベント）の一覧。React で onLoggedIn を props で受け取るのに当たる
const emit = defineEmits<{ 'logged-in': [] }>()

const email = ref('')
const password = ref('')
const submitting = ref(false) // 送信中は二重に押せないようにする
const error = ref<string | null>(null)

async function onSubmit(): Promise<void> {
  submitting.value = true
  error.value = null
  try {
    await logIn(email.value, password.value)
    emit('logged-in')
  } catch (e) {
    error.value = toMessage(e)
  } finally {
    submitting.value = false
  }
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
</script>

<template>
  <!-- @submit.prevent：送信で画面が再読み込みされないようにする（React の e.preventDefault()） -->
  <form @submit.prevent="onSubmit">
    <label>
      メールアドレス
      <!-- v-model：入力欄と email を両方向につなぐ（React の value ＋ onChange をまとめたもの） -->
      <input v-model="email" type="email" autocomplete="username" required />
    </label>
    <label>
      パスワード
      <input v-model="password" type="password" autocomplete="current-password" required />
    </label>
    <button :disabled="submitting">ログイン</button>
    <p v-if="error" class="error">{{ error }}</p>
  </form>
</template>
