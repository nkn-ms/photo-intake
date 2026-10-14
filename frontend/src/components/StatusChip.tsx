// 拾得物の状態を、色付きの小さなラベル（Chip）で出す。一覧と詳細の両方で使う
import Chip, { type ChipProps } from '@mui/material/Chip'
import type { ItemStatus } from '../api'
import { STATUS_LABELS } from '../format'

// 状態ごとの色。人の手が要るもの（確認待ち・解析失敗）を目立たせる
const STATUS_COLORS: Record<ItemStatus, ChipProps['color']> = {
  analyzing: 'info',
  analysis_failed: 'error',
  pending_review: 'warning',
  confirmed: 'success',
  matched: 'secondary',
  closed: 'default',
  archived: 'default',
}

export default function StatusChip({ status }: { status: ItemStatus }) {
  return <Chip size="small" label={STATUS_LABELS[status]} color={STATUS_COLORS[status]} />
}
