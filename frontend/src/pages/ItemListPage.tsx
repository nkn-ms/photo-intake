// 拾得物の一覧を出す画面。新しい順に50件を出し、状態でしぼり込める。
// 長く analyzing のままの行には印を付ける（片付ける操作はまだ無く、見せるだけ）
import RefreshIcon from '@mui/icons-material/Refresh'
import Alert from '@mui/material/Alert'
import Button from '@mui/material/Button'
import LinearProgress from '@mui/material/LinearProgress'
import MenuItem from '@mui/material/MenuItem'
import MuiLink from '@mui/material/Link'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Table from '@mui/material/Table'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableContainer from '@mui/material/TableContainer'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { listItems, type ItemStatus, type ItemSummary } from '../api'
import StatusChip from '../components/StatusChip'
import { formatDateTime, STATUS_LABELS, toErrorMessage } from '../format'

// worker は失敗しても、最後の判定まで最大で約18分かかる（6分×3回）。
// 30分を超えて analyzing のままなら、やり直し中ではなく止まっている（写真が S3 に届かなかった、など）
const STALE_ANALYZING_MS = 30 * 60 * 1000

export default function ItemListPage() {
  const [status, setStatus] = useState<ItemStatus | ''>('') // しぼり込む状態。空の文字列は「すべての状態」を表す
  const [reloadCount, setReloadCount] = useState(0) // 「読み直す」を押した回数。増えると下の useEffect がもう一度動く
  const [items, setItems] = useState<ItemSummary[]>([])
  const [loadedAt, setLoadedAt] = useState(() => Date.now()) // 一覧を読んだ時刻。「何分 analyzing のままか」はこの時刻を基準に測る
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 画面が出たとき・状態を選び直したとき・「読み直す」を押したときに、一覧を読む。
  // ignore：読んでいる途中で状態を選び直すと、古いほうの返事が後から届いて上書きしうる。
  // 次の読み込みが始まるとき（後片付け）に ignore を true にして、古い返事を捨てる（React の公式ドキュメントの書き方）
  useEffect(() => {
    let ignore = false

    async function load(): Promise<void> {
      setLoading(true)
      setError(null)
      try {
        const loaded = await listItems(status === '' ? undefined : status)
        if (ignore) {
          return
        }
        setItems(loaded)
        setLoadedAt(Date.now())
      } catch (e) {
        if (!ignore) {
          setError(toErrorMessage(e))
        }
      } finally {
        if (!ignore) {
          setLoading(false)
        }
      }
    }

    void load()
    return () => {
      ignore = true
    }
  }, [status, reloadCount])

  // 行を作った時刻（created_at）で測る。拾得日時（registered_at）は人が過去の日時を入れられるので使わない
  function isStale(item: ItemSummary): boolean {
    return item.status === 'analyzing' && loadedAt - Date.parse(item.created_at) > STALE_ANALYZING_MS
  }

  // 一覧の部分に出すものを、ガード節で上から順に決める
  function body() {
    if (error) {
      return <Alert severity="error">{error}</Alert>
    }
    if (loading) {
      return <LinearProgress />
    }
    if (items.length === 0) {
      return <Alert severity="info">該当する拾得物はありません</Alert>
    }
    return (
      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>拾得日時</TableCell>
              <TableCell>状態</TableCell>
              {/* AI の答えと人が確定した値は混ぜずに、2列に分けて出す */}
              <TableCell>種別（確定）</TableCell>
              <TableCell>種別（AI）</TableCell>
              <TableCell>拾得場所</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {items.map((item) => (
              <TableRow key={item.id} hover>
                <TableCell>
                  {/* component={Link}：MUI のリンクの見た目のまま、中身を React Router のリンクにする */}
                  <MuiLink component={Link} to={`/items/${item.id}`}>
                    {formatDateTime(item.registered_at)}
                  </MuiLink>
                </TableCell>
                <TableCell>
                  <StatusChip status={item.status} />
                  {isStale(item) && (
                    <Typography variant="caption" component="div" sx={{ color: 'error.main', mt: 0.5 }}>
                      30分以上止まっています
                    </Typography>
                  )}
                </TableCell>
                <TableCell>{item.category ?? '—'}</TableCell>
                <TableCell>{item.ai_category ?? '—'}</TableCell>
                <TableCell>{item.location ?? '—'}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    )
  }

  return (
    <Stack spacing={2}>
      <Typography variant="h5" component="h2" sx={{ fontWeight: 700 }}>
        一覧
      </Typography>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        {/* select：TextField を選ぶ欄として使う。中の MenuItem が選択肢 */}
        <TextField
          select
          size="small"
          label="状態"
          value={status}
          onChange={(e) => setStatus(e.target.value as ItemStatus | '')}
          sx={{ minWidth: 180 }}
        >
          <MenuItem value="">すべての状態</MenuItem>
          {/* Object.entries：{ analyzing: '解析中', … } を [['analyzing', '解析中'], …] の並びにする */}
          {Object.entries(STATUS_LABELS).map(([key, label]) => (
            <MenuItem key={key} value={key}>
              {label}
            </MenuItem>
          ))}
        </TextField>
        <Button
          startIcon={<RefreshIcon />}
          disabled={loading}
          onClick={() => setReloadCount((count) => count + 1)}
        >
          読み直す
        </Button>
      </Stack>
      {body()}
    </Stack>
  )
}
