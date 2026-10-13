<script setup lang="ts">
// 拾得物の一覧を出す画面。新しい順に50件を出し、状態でしぼり込める。
// 長く analyzing のままの行には印を付ける（片付ける操作はまだ無く、見せるだけ）
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ApiError, listItems, type ItemStatus, type ItemSummary } from '../api'
import { formatDateTime, STATUS_LABELS } from '../format'

// worker は失敗しても、最後の判定まで最大で約18分かかる（6分×3回）。
// 30分を超えて analyzing のままなら、やり直し中ではなく止まっている（写真が S3 に届かなかった、など）
const STALE_ANALYZING_MS = 30 * 60 * 1000

const status = ref<ItemStatus | ''>('') // しぼり込む状態。空の文字列は「すべての状態」を表す
const items = ref<ItemSummary[]>([])
const loadedAt = ref(Date.now()) // 一覧を読んだ時刻。「何分 analyzing のままか」はこの時刻を基準に測る
const loading = ref(false)
const error = ref<string | null>(null)

onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    items.value = await listItems(status.value === '' ? undefined : status.value)
    loadedAt.value = Date.now()
  } catch (e) {
    error.value = e instanceof ApiError ? `${e.status}：${e.message}` : String(e)
  } finally {
    loading.value = false
  }
}

// 行を作った時刻（created_at）で測る。拾得日時（registered_at）は人が過去の日時を入れられるので使わない
function isStale(item: ItemSummary): boolean {
  return item.status === 'analyzing' && loadedAt.value - Date.parse(item.created_at) > STALE_ANALYZING_MS
}
</script>

<template>
  <h2>一覧</h2>

  <div class="toolbar">
    <!-- 状態を選び直したとき（@change）に、一覧を読み直す -->
    <select v-model="status" @change="load">
      <option value="">すべての状態</option>
      <option v-for="(label, key) in STATUS_LABELS" :key="key" :value="key">{{ label }}</option>
    </select>
    <button :disabled="loading" @click="load">読み直す</button>
  </div>

  <p v-if="error" class="error">{{ error }}</p>
  <p v-else-if="loading">読み込み中…</p>
  <p v-else-if="items.length === 0">該当する拾得物はありません</p>
  <table v-else>
    <thead>
      <tr>
        <th>拾得日時</th>
        <th>状態</th>
        <!-- AI の答えと人が確定した値は混ぜずに、2列に分けて出す -->
        <th>種別（確定）</th>
        <th>種別（AI）</th>
        <th>拾得場所</th>
      </tr>
    </thead>
    <tbody>
      <!-- :key は行を見分ける値。並びが変わっても、Vue が同じ行を使い回せる（React の key と同じ） -->
      <tr v-for="item in items" :key="item.id">
        <td>
          <RouterLink :to="`/items/${item.id}`">{{ formatDateTime(item.registered_at) }}</RouterLink>
        </td>
        <td>
          {{ STATUS_LABELS[item.status] }}
          <span v-if="isStale(item)" class="stale">30分以上止まっています</span>
        </td>
        <td>{{ item.category ?? '—' }}</td>
        <td>{{ item.ai_category ?? '—' }}</td>
        <td>{{ item.location ?? '—' }}</td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
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
.stale {
  display: block;
  color: #b00;
  font-size: 0.85em;
}
</style>
