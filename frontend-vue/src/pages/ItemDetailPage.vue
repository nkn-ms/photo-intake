<script setup lang="ts">
// 拾得物1件の詳細を出す画面。写真と、AI の答えと人が確定した値を横に並べて出す。
// admin が見ていて、確定できる状態なら、下に確定のフォーム（ConfirmForm.vue）を出す
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ApiError, getItem, type Confirmation, type ItemDetail, type ItemStatus } from '../api'
import ConfirmForm from '../components/ConfirmForm.vue'
import { formatDateTime, STATUS_LABELS } from '../format'
import { isAdmin } from '../session'

// 確定できる状態。API の CONFIRMABLE_STATUSES と同じ値だが、ここではフォームを出すかどうかにだけ使う。
// ずれても困るのは「出したフォームが 409 で断られる」ことだけで、確定できない行が確定されることは無い
const CONFIRMABLE_STATUSES: ItemStatus[] = ['pending_review', 'analysis_failed']

const props = defineProps<{ id: string }>() // URL の :id（router.ts の props: true で渡ってくる）

const item = ref<ItemDetail | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const photoMissing = ref(false) // 写真を読めなかったら true（S3 に届いていない、など）

onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    item.value = await getItem(props.id)
  } catch (e) {
    error.value = toMessage(e)
  } finally {
    loading.value = false
  }
}

function toMessage(e: unknown): string {
  if (!(e instanceof ApiError)) {
    return String(e)
  }
  // 404 は ID の形は正しいが行が無いとき、422 は ID の形（UUID）からして違うとき。どちらも利用者には「無い」と同じ
  if (e.status === 404 || e.status === 422) {
    return 'この拾得物は見つかりません'
  }
  return `${e.status}：${e.message}`
}

/** 比べる4項目。AI の答えも人の値も、この形にそろえてから並べる */
interface Fields {
  category: string | null
  color: string | null
  brand: string | null
  features: string[] | null
}

// AI の答え（ai_result）から4項目を取り出す。中身は保証されていないので、1項目ずつ形を確かめる。
// 形の違う項目は null にして、モデルが項目を抜かしたり形を変えたりしても画面が壊れないようにする
const ai = computed<Fields | null>(() => {
  const result = item.value?.ai_result
  if (result === null || result === undefined) {
    return null
  }
  return {
    category: asText(result.category),
    color: asText(result.color),
    brand: asText(result.brand),
    features: asTextList(result.features),
  }
})

function asText(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function asTextList(value: unknown): string[] | null {
  if (!Array.isArray(value)) {
    return null
  }
  return value.filter((v): v is string => typeof v === 'string')
}

const FIELD_LABELS: Record<keyof Fields, string> = {
  category: '種別',
  color: '色',
  brand: 'ブランド',
  features: '特徴',
}

// 表の行。AI と人の値を同じ形の文字にしてから並べ、違うかどうかも判定しておく
const rows = computed(() => {
  const detail = item.value
  if (detail === null) {
    return []
  }
  return (Object.keys(FIELD_LABELS) as (keyof Fields)[]).map((key) => {
    const aiValue = show(ai.value?.[key] ?? null)
    const humanValue = show(detail[key])
    return {
      key,
      label: FIELD_LABELS[key],
      ai: aiValue,
      human: humanValue,
      // 確定した後だけ比べる。確定前は人の値が全部空なので、比べても意味がない
      differs: detail.confirmed_at !== null && aiValue !== humanValue,
    }
  })
})

const canConfirm = computed(
  () => isAdmin.value && item.value !== null && CONFIRMABLE_STATUSES.includes(item.value.status),
)

// 確定のフォームに最初から入れておく値。AI の答えがあればそれを、無ければ（解析失敗）空欄にする
const initialConfirmation = computed<Confirmation>(() => ({
  category: ai.value?.category ?? '',
  color: ai.value?.color ?? null,
  brand: ai.value?.brand ?? null,
  features: ai.value?.features ?? null,
}))

// 確定できたら、API が返した確定後の詳細で表示を入れ替える（読み直しのリクエストは出さない）。
// 状態が confirmed になるので canConfirm が false になり、フォームは消える
function onConfirmed(detail: ItemDetail): void {
  item.value = detail
  photoMissing.value = false
}

// 表に出す文字にする。配列は「、」でつなぎ、空は「—」にする
function show(value: string | string[] | null): string {
  if (value === null) {
    return '—'
  }
  return Array.isArray(value) ? value.join('、') : value
}
</script>

<template>
  <RouterLink to="/items">← 一覧へ</RouterLink>
  <h2>拾得物の詳細</h2>

  <p v-if="loading">読み込み中…</p>
  <p v-else-if="error" class="error">{{ error }}</p>
  <template v-else-if="item">
    <div class="layout">
      <!-- @error は、画像を読めなかったとき（S3 に写真が無い・署名付きURLの期限切れ）に呼ばれる -->
      <img v-if="!photoMissing" :src="item.photo_url" alt="拾得物の写真" @error="photoMissing = true" />
      <p v-else class="no-photo">写真を読めませんでした（S3 に届いていないか、URL の期限が切れています）</p>

      <dl>
        <dt>状態</dt>
        <dd>{{ STATUS_LABELS[item.status] }}</dd>
        <dt>拾得日時</dt>
        <dd>{{ formatDateTime(item.registered_at) }}</dd>
        <dt>拾得場所</dt>
        <dd>{{ item.location ?? '—' }}</dd>
        <dt>保管期限</dt>
        <dd>{{ item.retention_deadline ?? '（確定すると決まります）' }}</dd>
        <template v-if="item.confirmed_at">
          <dt>確定した日時</dt>
          <dd>{{ formatDateTime(item.confirmed_at) }}</dd>
        </template>
      </dl>
    </div>

    <h3>AI の答えと確定した値</h3>
    <table>
      <thead>
        <tr>
          <th></th>
          <th>AI</th>
          <th>確定</th>
        </tr>
      </thead>
      <tbody>
        <!-- :class="{ differs: … }" は、値が true のときだけ class を付ける書き方。AI と人の値が違う行に色を付ける -->
        <tr v-for="row in rows" :key="row.key" :class="{ differs: row.differs }">
          <th>{{ row.label }}</th>
          <td>{{ row.ai }}</td>
          <td>{{ row.human }}</td>
        </tr>
      </tbody>
    </table>

    <p v-if="item.ai_error" class="error">解析が失敗した理由：{{ item.ai_error }}</p>
    <p v-if="item.ai_model && item.ai_analyzed_at" class="note">
      AI：{{ item.ai_model }}（{{ formatDateTime(item.ai_analyzed_at) }}）
    </p>

    <ConfirmForm v-if="canConfirm" :item-id="item.id" :initial="initialConfirmation" @confirmed="onConfirmed" />
  </template>
</template>

<style scoped>
.layout {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  align-items: flex-start;
}
img {
  max-width: 320px;
  height: auto;
}
.no-photo {
  width: 320px;
  color: #666;
}
dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 16px;
  margin: 0;
}
dt {
  color: #666;
}
dd {
  margin: 0;
}
table {
  border-collapse: collapse;
}
th,
td {
  border-bottom: 1px solid #ddd;
  padding: 6px 12px;
  text-align: left;
}
.differs td {
  background: #fff4d6;
}
.note {
  color: #666;
  font-size: 0.85em;
}
</style>
