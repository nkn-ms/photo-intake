// 落とし主からの問い合わせの一覧を出す画面。新しい順に50件を出す。
// 問い合わせは LINE の問い合わせ用アカウントで受け付け（backend/line/inquiry.py）、ここでは読むだけ。
// 候補の落とし物を出す・お客さまに連絡する操作はまだ無い（照らし合わせは後の段階）
import RefreshIcon from '@mui/icons-material/Refresh'
import Alert from '@mui/material/Alert'
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
import { useEffect, useState } from 'react'
import { listRequests, type RequestSummary } from '../api'
import { formatDateTime, toErrorMessage } from '../format'

export default function RequestListPage() {
  const [reloadCount, setReloadCount] = useState(0) // 「読み直す」を押した回数。増えると下の useEffect がもう一度動く
  const [requests, setRequests] = useState<RequestSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 画面が出たとき・「読み直す」を押したときに、一覧を読む。
  // ignore：読んでいる途中でもう一度押すと、古いほうの返事が後から届いて上書きしうるので、古い返事を捨てる（ItemListPage と同じ書き方）
  useEffect(() => {
    let ignore = false

    async function load(): Promise<void> {
      setLoading(true)
      setError(null)
      try {
        const loaded = await listRequests()
        if (!ignore) {
          setRequests(loaded)
        }
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
  }, [reloadCount])

  // 一覧の部分に出すものを、ガード節で上から順に決める
  function body() {
    if (error) {
      return <Alert severity="error">{error}</Alert>
    }
    if (loading) {
      return <LinearProgress />
    }
    if (requests.length === 0) {
      return <Alert severity="info">問い合わせはまだありません</Alert>
    }
    return (
      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={{ whiteSpace: 'nowrap' }}>受付番号</TableCell>
              <TableCell sx={{ whiteSpace: 'nowrap' }}>受付日時</TableCell>
              <TableCell>内容</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {requests.map((request) => (
              <TableRow key={request.id} hover>
                <TableCell sx={{ whiteSpace: 'nowrap', fontWeight: 700 }}>{request.receipt}</TableCell>
                <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatDateTime(request.created_at)}</TableCell>
                {/* pre-wrap：お客さまが入れた改行をそのまま出す。anywhere：長い英数字（URL など）も枠の中で折り返す。
                    本文は React が文字として出す（HTML として解釈しない）ので、タグが書かれていても動かない */}
                <TableCell sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                  {request.description}
                </TableCell>
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
        問い合わせ
      </Typography>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
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
