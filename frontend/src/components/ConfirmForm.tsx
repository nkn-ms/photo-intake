// 拾得物の内容を確定するフォーム。詳細ページが、admin のときだけ出す。
// 最初は AI の答えを入れておき、人が写真と見比べて直してから確定する。
// フォームを隠すのは画面の都合にすぎず、本当に止めるのは API（admin でなければ 403、確定できない状態なら 409）
import Alert from '@mui/material/Alert'
import Button from '@mui/material/Button'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useState, type FormEvent } from 'react'
import { confirmItem, type Confirmation, type ItemDetail } from '../api'
import { toErrorMessage } from '../format'

interface Props {
  itemId: string
  initial: Confirmation // 入力欄に最初から入れておく値
  onConfirmed: (detail: ItemDetail) => void // 確定できたら、確定後の詳細を親（詳細ページ）に渡す。親はそれで表示を入れ替える
}

export default function ConfirmForm({ itemId, initial, onConfirmed }: Props) {
  // useState に渡した値は、最初の1回だけ使われる（親の initial が後で変わっても、入力中の値は上書きしない）
  const [category, setCategory] = useState(initial.category)
  const [color, setColor] = useState(initial.color ?? '')
  const [brand, setBrand] = useState(initial.brand ?? '')
  const [features, setFeatures] = useState((initial.features ?? []).join('\n')) // 特徴は1行に1つ
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault()
    const confirmation = toConfirmation(category, color, brand, features)
    // 空白だけの種別は、input の required では止まらない。API に送れば 422 になるが、その前にここで止める
    if (confirmation.category === '') {
      setError('種別を入れてください')
      return
    }

    setSubmitting(true)
    setError(null)
    try {
      onConfirmed(await confirmItem(itemId, confirmation))
    } catch (err) {
      // 409 の文（「ほかの人が先に状態を変えました。読み直してください」など）は API が日本語で返す
      setError(toErrorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Paper variant="outlined" sx={{ p: { xs: 2, sm: 3 } }}>
      <Stack component="form" spacing={2} onSubmit={onSubmit}>
        <Typography variant="h6" component="h3" sx={{ fontWeight: 700 }}>
          内容を確定する
        </Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          AI の答えを入れてあります。写真と見比べて、直してから確定してください。
        </Typography>
        <TextField label="種別" required value={category} onChange={(e) => setCategory(e.target.value)} />
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
          <TextField label="色" fullWidth value={color} onChange={(e) => setColor(e.target.value)} />
          <TextField
            label="ブランド"
            fullWidth
            placeholder="分からなければ空のまま"
            value={brand}
            onChange={(e) => setBrand(e.target.value)}
          />
        </Stack>
        <TextField
          label="特徴（1行に1つ）"
          multiline
          minRows={3}
          value={features}
          onChange={(e) => setFeatures(e.target.value)}
        />
        <Button type="submit" variant="contained" loading={submitting} sx={{ alignSelf: 'flex-start' }}>
          確定する
        </Button>
        {error && <Alert severity="error">{error}</Alert>}
      </Stack>
    </Paper>
  )
}

// 入力欄の文字を、API に送る形にする。空欄は null（「分からない」）にして、空の文字列を保存しない
function toConfirmation(category: string, color: string, brand: string, features: string): Confirmation {
  const lines = features
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
  return {
    category: category.trim(),
    color: emptyToNull(color),
    brand: emptyToNull(brand),
    features: lines.length > 0 ? lines : null,
  }
}

function emptyToNull(text: string): string | null {
  const trimmed = text.trim()
  return trimmed === '' ? null : trimmed
}
