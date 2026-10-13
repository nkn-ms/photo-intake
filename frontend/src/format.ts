/**
 * API から届いた値を、画面に出す文字に直す処理。一覧と詳細の両方で使う。
 */
import type { ItemStatus } from './api'

// 画面に出す状態の名前。Record<ItemStatus, string> なので、状態が増えたのにここを足し忘れると型チェックで止まる
export const STATUS_LABELS: Record<ItemStatus, string> = {
  analyzing: '解析中',
  analysis_failed: '解析失敗',
  pending_review: '確認待ち',
  confirmed: '確定',
  matched: '照合済み',
  closed: '終了',
  archived: '保管終了',
}

// API の日時は UTC の文字列で届く。日本時間の「2026/10/13 10:23」の形に直して出す
export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString('ja-JP', {
    timeZone: 'Asia/Tokyo',
    dateStyle: 'short',
    timeStyle: 'short',
  })
}
