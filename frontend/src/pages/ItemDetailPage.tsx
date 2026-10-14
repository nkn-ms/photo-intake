// 拾得物1件の詳細を出す画面。写真と、AI の答えと人が確定した値を横に並べて出す。
// admin が見ていて、確定できる状態なら、下に確定のフォーム（ConfirmForm）を出す
import ArrowBackIcon from '@mui/icons-material/ArrowBack'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import LinearProgress from '@mui/material/LinearProgress'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Table from '@mui/material/Table'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableContainer from '@mui/material/TableContainer'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import Typography from '@mui/material/Typography'
import { useEffect, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'
import { ApiError, getItem, type Confirmation, type ItemDetail, type ItemStatus } from '../api'
import ConfirmForm from '../components/ConfirmForm'
import StatusChip from '../components/StatusChip'
import { formatDateTime } from '../format'
import { useIsAdmin } from '../session'

// 確定できる状態。API の CONFIRMABLE_STATUSES と同じ値だが、ここではフォームを出すかどうかにだけ使う。
// ずれても困るのは「出したフォームが 409 で断られる」ことだけで、確定できない行が確定されることは無い
const CONFIRMABLE_STATUSES: ItemStatus[] = ['pending_review', 'analysis_failed']

export default function ItemDetailPage() {
  // URL の :id（router.tsx の 'items/:id'）。Vue 版の props: true の代わりに、ページの中で読む
  const { id } = useParams<{ id: string }>()
  const [item, setItem] = useState<ItemDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [photoMissing, setPhotoMissing] = useState(false) // 写真を読めなかったら true（S3 に届いていない、など）
  const isAdmin = useIsAdmin()

  // id が変わるたびに読み直す。ignore の役目は一覧のページと同じ（古い返事を捨てる）
  useEffect(() => {
    let ignore = false

    async function load(itemId: string): Promise<void> {
      setLoading(true)
      setError(null)
      setPhotoMissing(false)
      try {
        const loaded = await getItem(itemId)
        if (!ignore) {
          setItem(loaded)
        }
      } catch (e) {
        if (!ignore) {
          setError(toMessage(e))
        }
      } finally {
        if (!ignore) {
          setLoading(false)
        }
      }
    }

    // router.tsx の 'items/:id' からしか来ないので id はいつもあるが、型の上では undefined もありうる
    if (id !== undefined) {
      void load(id)
    }
    return () => {
      ignore = true
    }
  }, [id])

  // 確定できたら、API が返した確定後の詳細で表示を入れ替える（読み直しのリクエストは出さない）。
  // 状態が confirmed になるので、下の「確定できる状態」に当たらなくなり、フォームは消える
  function onConfirmed(detail: ItemDetail): void {
    setItem(detail)
    setPhotoMissing(false)
  }

  // 出すものを、ガード節で上から順に決める
  function body() {
    if (loading) {
      return <LinearProgress />
    }
    if (error) {
      return <Alert severity="error">{error}</Alert>
    }
    if (item === null) {
      return null
    }
    return (
      <Stack spacing={3}>
        {/* 狭い画面では縦に、広い画面では写真と欄を横に並べる */}
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={3} sx={{ alignItems: 'flex-start' }}>
          {/* onError は、画像を読めなかったとき（S3 に写真が無い・署名付きURLの期限切れ）に呼ばれる */}
          {photoMissing ? (
            <Alert severity="warning" sx={{ width: { xs: '100%', sm: 320 } }}>
              写真を読めませんでした（S3 に届いていないか、URL の期限が切れています）
            </Alert>
          ) : (
            <Box
              component="img"
              src={item.photo_url}
              alt="拾得物の写真"
              onError={() => setPhotoMissing(true)}
              sx={{ width: { xs: '100%', sm: 320 }, borderRadius: 2, bgcolor: 'grey.100' }}
            />
          )}

          <Paper variant="outlined" sx={{ p: 2, flexGrow: 1, width: { xs: '100%', sm: 'auto' } }}>
            <Stack spacing={1.5}>
              <Field label="状態">
                <StatusChip status={item.status} />
              </Field>
              <Field label="拾得日時">{formatDateTime(item.registered_at)}</Field>
              <Field label="拾得場所">{item.location ?? '—'}</Field>
              <Field label="保管期限">{item.retention_deadline ?? '（確定すると決まります）'}</Field>
              {item.confirmed_at && <Field label="確定した日時">{formatDateTime(item.confirmed_at)}</Field>}
            </Stack>
          </Paper>
        </Stack>

        <Box>
          <Typography variant="h6" component="h3" sx={{ fontWeight: 700, mb: 1 }}>
            AI の答えと確定した値
          </Typography>
          <TableContainer component={Paper} variant="outlined">
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell sx={{ width: 100 }} />
                  <TableCell>AI</TableCell>
                  <TableCell>確定</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {/* AI と人の値が違う行に色を付ける（AI の答えを上書きせずに残しているので、この差が見える） */}
                {compareRows(item).map((row) => (
                  <TableRow key={row.key} sx={row.differs ? { bgcolor: 'warning.light' } : undefined}>
                    <TableCell component="th" sx={{ color: 'text.secondary' }}>
                      {row.label}
                    </TableCell>
                    <TableCell>{row.ai}</TableCell>
                    <TableCell>{row.human}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          {item.ai_model && item.ai_analyzed_at && (
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
              AI：{item.ai_model}（{formatDateTime(item.ai_analyzed_at)}）
            </Typography>
          )}
        </Box>

        {item.ai_error && <Alert severity="error">解析が失敗した理由：{item.ai_error}</Alert>}

        {/* key：別の拾得物に移ったら、フォームを作り直して入力中の値を捨てる（同じ部品が使い回されないように） */}
        {isAdmin && CONFIRMABLE_STATUSES.includes(item.status) && (
          <ConfirmForm key={item.id} itemId={item.id} initial={initialConfirmation(item)} onConfirmed={onConfirmed} />
        )}
      </Stack>
    )
  }

  return (
    <Stack spacing={2}>
      <Box>
        <Button component={Link} to="/items" startIcon={<ArrowBackIcon />}>
          一覧へ
        </Button>
      </Box>
      <Typography variant="h5" component="h2" sx={{ fontWeight: 700 }}>
        拾得物の詳細
      </Typography>
      {body()}
    </Stack>
  )
}

/** 詳細の欄の1行（左に項目名、右に値） */
function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}>
      <Typography variant="body2" sx={{ color: 'text.secondary', width: 96, flexShrink: 0 }}>
        {label}
      </Typography>
      <Typography variant="body1" component="div">
        {children}
      </Typography>
    </Stack>
  )
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

// ===========================================================================
// AI の答えと人の値を並べる（画面の状態を持たない、ただの関数。Vue 版では computed だった）
// ===========================================================================

/** 比べる4項目。AI の答えも人の値も、この形にそろえてから並べる */
interface Fields {
  category: string | null
  color: string | null
  brand: string | null
  features: string[] | null
}

const FIELD_LABELS: Record<keyof Fields, string> = {
  category: '種別',
  color: '色',
  brand: 'ブランド',
  features: '特徴',
}

/**
 * AI の答え（ai_result）から4項目を取り出す。中身は保証されていないので、1項目ずつ形を確かめる。
 * 形の違う項目は null にして、モデルが項目を抜かしたり形を変えたりしても画面が壊れないようにする。
 */
function aiFields(item: ItemDetail): Fields | null {
  const result = item.ai_result
  if (result === null) {
    return null
  }
  return {
    category: asText(result.category),
    color: asText(result.color),
    brand: asText(result.brand),
    features: asTextList(result.features),
  }
}

// 確定のフォームに最初から入れておく値。AI の答えがあればそれを、無ければ（解析失敗）空欄にする
function initialConfirmation(item: ItemDetail): Confirmation {
  const ai = aiFields(item)
  return {
    category: ai?.category ?? '',
    color: ai?.color ?? null,
    brand: ai?.brand ?? null,
    features: ai?.features ?? null,
  }
}

function asText(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function asTextList(value: unknown): string[] | null {
  if (!Array.isArray(value)) {
    return null
  }
  return value.filter((v): v is string => typeof v === 'string')
}

// 表の行。AI と人の値を同じ形の文字にしてから並べ、違うかどうかも判定しておく
function compareRows(item: ItemDetail) {
  const ai = aiFields(item)
  return (Object.keys(FIELD_LABELS) as (keyof Fields)[]).map((key) => {
    const aiValue = show(ai?.[key] ?? null)
    const humanValue = show(item[key])
    return {
      key,
      label: FIELD_LABELS[key],
      ai: aiValue,
      human: humanValue,
      // 確定した後だけ比べる。確定前は人の値が全部空なので、比べても意味がない
      differs: item.confirmed_at !== null && aiValue !== humanValue,
    }
  })
}

// 表に出す文字にする。配列は「、」でつなぎ、空は「—」にする
function show(value: string | string[] | null): string {
  if (value === null) {
    return '—'
  }
  return Array.isArray(value) ? value.join('、') : value
}
