<script setup lang="ts">
// 拾得物の登録。写真を選ぶ（スマホならカメラが開く）→ 縮小・EXIF の削除 → POST /items → 署名付きURLに PUT
import { ref } from 'vue'
import { ApiError, createItem, uploadPhoto } from '../api'
import { shrinkPhoto } from '../image'

const photo = ref<Blob | null>(null) // 縮小・EXIF を消した後の写真（送るのはこちら）
const previewUrl = ref<string | null>(null) // 縮小後の写真を画面に出すための URL
const sizes = ref<{ before: number; after: number } | null>(null) // 縮小の前後の大きさ（KB）
const location = ref('')
const submitting = ref(false)
const error = ref<string | null>(null)
const registeredId = ref<string | null>(null) // 登録できた行の ID

/** 写真が選ばれたら、その場で縮小して、縮小後の写真を見せる（送る前に、何が送られるかが見える）。 */
async function onFileChange(event: Event): Promise<void> {
  const file = (event.target as HTMLInputElement).files?.[0]
  clearPhoto()
  error.value = null
  registeredId.value = null
  if (file === undefined) {
    return
  }
  try {
    photo.value = await shrinkPhoto(file)
    previewUrl.value = URL.createObjectURL(photo.value)
    sizes.value = { before: toKB(file.size), after: toKB(photo.value.size) }
  } catch (e) {
    error.value = `写真を読み込めませんでした（${e instanceof Error ? e.message : String(e)}）`
  }
}

/** 登録する。行を作ってから（API）、写真を置く（S3）。順番は API の create_item のコメントのとおり。 */
async function onSubmit(): Promise<void> {
  if (photo.value === null) {
    error.value = '写真を選んでください'
    return
  }
  submitting.value = true
  error.value = null
  try {
    const created = await createItem({
      content_type: 'image/jpeg', // image.ts はいつも JPEG で書き出す
      location: location.value === '' ? null : location.value,
    })
    await uploadPhoto(created, photo.value)
    registeredId.value = created.item_id
    clearPhoto()
    location.value = ''
  } catch (e) {
    error.value = e instanceof ApiError ? `${e.status}：${e.message}` : String(e)
  } finally {
    submitting.value = false
  }
}

/** 選んだ写真を手放す。createObjectURL で作った URL は、使い終わったら revokeObjectURL で返す（メモリのため）。 */
function clearPhoto(): void {
  if (previewUrl.value !== null) {
    URL.revokeObjectURL(previewUrl.value)
  }
  photo.value = null
  previewUrl.value = null
  sizes.value = null
}

function toKB(bytes: number): number {
  return Math.round(bytes / 1024)
}
</script>

<template>
  <form @submit.prevent="onSubmit">
    <h2>拾得物を登録する</h2>
    <label>
      写真
      <!-- accept="image/*"：画像だけ選べる。capture="environment"：スマホでは背面カメラが開く -->
      <input type="file" accept="image/*" capture="environment" @change="onFileChange" />
    </label>
    <template v-if="previewUrl !== null && sizes !== null">
      <img :src="previewUrl" alt="送る写真" class="preview" />
      <p>{{ sizes.before }} KB → {{ sizes.after }} KB（縮小し、撮影場所などの情報を消しました）</p>
    </template>
    <label>
      拾得場所（任意）
      <input v-model="location" type="text" placeholder="例：2階 東口のベンチ" />
    </label>
    <button :disabled="submitting || photo === null">登録する</button>
    <p v-if="registeredId !== null">登録しました（解析中）。ID：{{ registeredId }}</p>
    <p v-if="error" class="error">{{ error }}</p>
  </form>
</template>

<style scoped>
/* scoped：このファイルの中の要素にだけ効く CSS（ほかの部品の img には効かない） */
.preview {
  max-width: 100%;
  max-height: 240px;
  object-fit: contain;
}
</style>
