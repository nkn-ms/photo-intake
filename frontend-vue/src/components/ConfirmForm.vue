<script setup lang="ts">
// 拾得物の内容を確定するフォーム。詳細ページが、admin のときだけ出す。
// 最初は AI の答えを入れておき、人が写真と見比べて直してから確定する。
// フォームを隠すのは画面の都合にすぎず、本当に止めるのは API（admin でなければ 403、確定できない状態なら 409）
import { ref } from 'vue'
import { ApiError, confirmItem, type Confirmation, type ItemDetail } from '../api'

const props = defineProps<{ itemId: string; initial: Confirmation }>()
// 確定できたら、確定後の詳細を親（詳細ページ）に渡す。親はそれで表示を入れ替える
const emit = defineEmits<{ confirmed: [detail: ItemDetail] }>()

// 入力欄の値。最初の値は props から1回だけ写す（親の値が後で変わっても、入力中の値は上書きしない）
const category = ref(props.initial.category)
const color = ref(props.initial.color ?? '')
const brand = ref(props.initial.brand ?? '')
const features = ref((props.initial.features ?? []).join('\n')) // 特徴は1行に1つ
const submitting = ref(false)
const error = ref<string | null>(null)

async function onSubmit(): Promise<void> {
  const confirmation = toConfirmation()
  // 空白だけの種別は、input の required では止まらない。API に送れば 422 になるが、その前にここで止める
  if (confirmation.category === '') {
    error.value = '種別を入れてください'
    return
  }

  submitting.value = true
  error.value = null
  try {
    emit('confirmed', await confirmItem(props.itemId, confirmation))
  } catch (e) {
    // 409 の文（「ほかの人が先に状態を変えました。読み直してください」など）は API が日本語で返す
    error.value = e instanceof ApiError ? `${e.status}：${e.message}` : String(e)
  } finally {
    submitting.value = false
  }
}

// 入力欄の文字を、API に送る形にする。空欄は null（「分からない」）にして、空の文字列を保存しない
function toConfirmation(): Confirmation {
  const lines = features.value
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
  return {
    category: category.value.trim(),
    color: emptyToNull(color.value),
    brand: emptyToNull(brand.value),
    features: lines.length > 0 ? lines : null,
  }
}

function emptyToNull(text: string): string | null {
  const trimmed = text.trim()
  return trimmed === '' ? null : trimmed
}
</script>

<template>
  <form @submit.prevent="onSubmit">
    <h3>内容を確定する</h3>
    <label>
      種別（必須）
      <input v-model="category" type="text" required />
    </label>
    <label>
      色
      <input v-model="color" type="text" />
    </label>
    <label>
      ブランド
      <input v-model="brand" type="text" placeholder="分からなければ空のまま" />
    </label>
    <label>
      特徴（1行に1つ）
      <textarea v-model="features" rows="4"></textarea>
    </label>
    <button :disabled="submitting">確定する</button>
    <p v-if="error" class="error">{{ error }}</p>
  </form>
</template>
