// 拾得物の登録。写真を選ぶ（スマホならカメラが開く）→ 縮小・EXIF の削除 → POST /items → 署名付きURLに PUT
import PhotoCameraIcon from '@mui/icons-material/PhotoCamera'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useEffect, useState, type ChangeEvent, type FormEvent } from 'react'
import { createItem, uploadPhoto } from '../api'
import { toErrorMessage } from '../format'
import { shrinkPhoto } from '../image'

/** 選んで縮小した写真。送るもの・画面に出すもの・縮小の前後の大きさを1つにまとめて持つ */
interface Photo {
  blob: Blob // 縮小・EXIF を消した後の写真（送るのはこちら）
  previewUrl: string // blob を画面に出すための URL（createObjectURL で作る）
  beforeKB: number
  afterKB: number
}

export default function RegisterForm() {
  const [photo, setPhoto] = useState<Photo | null>(null)
  const [location, setLocation] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [registeredId, setRegisteredId] = useState<string | null>(null) // 登録できた行の ID

  // createObjectURL で作った URL は、使い終わったら revokeObjectURL で返す（メモリのため）。
  // 写真を選び直したとき・登録し終えたとき・ページを離れたときに、前の URL をここでまとめて返す
  // （useEffect が返す関数は、次に動く前と、部品が消えるときに呼ばれる）
  useEffect(() => {
    if (photo === null) {
      return
    }
    return () => URL.revokeObjectURL(photo.previewUrl)
  }, [photo])

  /** 写真が選ばれたら、その場で縮小して、縮小後の写真を見せる（送る前に、何が送られるかが見える）。 */
  async function onFileChange(e: ChangeEvent<HTMLInputElement>): Promise<void> {
    const file = e.target.files?.[0]
    setPhoto(null)
    setError(null)
    setRegisteredId(null)
    if (file === undefined) {
      return
    }
    try {
      const blob = await shrinkPhoto(file)
      setPhoto({
        blob,
        previewUrl: URL.createObjectURL(blob),
        beforeKB: toKB(file.size),
        afterKB: toKB(blob.size),
      })
    } catch (err) {
      setError(`写真を読み込めませんでした（${err instanceof Error ? err.message : String(err)}）`)
    }
  }

  /** 登録する。行を作ってから（API）、写真を置く（S3）。順番は API の create_item のコメントのとおり。 */
  async function onSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault()
    // await の後では e.currentTarget が null になるので、フォームは先に取っておく（最後に選んだファイルの欄を空にするため）
    const form = e.currentTarget
    if (photo === null) {
      setError('写真を選んでください')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const created = await createItem({
        content_type: 'image/jpeg', // image.ts はいつも JPEG で書き出す
        location: location === '' ? null : location,
      })
      await uploadPhoto(created, photo.blob)
      setRegisteredId(created.item_id)
      setPhoto(null)
      setLocation('')
      form.reset() // ファイルの欄は React の state とつないでいないので、フォームの機能で空にする
    } catch (err) {
      setError(toErrorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Paper variant="outlined" sx={{ p: { xs: 2, sm: 4 } }}>
      <Stack component="form" spacing={2} onSubmit={onSubmit}>
        <Typography variant="h5" component="h2" sx={{ fontWeight: 700 }}>
          拾得物を登録する
        </Typography>
        {/* component="label"：ボタンを押すと、中の見えない input（ファイルを選ぶ欄）が開く。
            accept="image/*"：画像だけ選べる。capture="environment"：スマホでは背面カメラが開く */}
        <Button variant="outlined" size="large" component="label" startIcon={<PhotoCameraIcon />}>
          {photo ? '写真を選び直す' : '写真を撮る・選ぶ'}
          <input type="file" accept="image/*" capture="environment" hidden onChange={onFileChange} />
        </Button>
        {photo && (
          <Box>
            <Box
              component="img"
              src={photo.previewUrl}
              alt="送る写真"
              sx={{ width: '100%', maxHeight: 280, objectFit: 'contain', borderRadius: 1, bgcolor: 'grey.100' }}
            />
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
              {photo.beforeKB} KB → {photo.afterKB} KB（縮小し、撮影場所などの情報を消しました）
            </Typography>
          </Box>
        )}
        <TextField
          label="拾得場所（任意）"
          placeholder="例：2階 東口のベンチ"
          value={location}
          onChange={(e) => setLocation(e.target.value)}
        />
        <Button type="submit" variant="contained" size="large" disabled={photo === null} loading={submitting}>
          登録する
        </Button>
        {registeredId && <Alert severity="success">登録しました（解析中）。ID：{registeredId}</Alert>}
        {error && <Alert severity="error">{error}</Alert>}
      </Stack>
    </Paper>
  )
}

function toKB(bytes: number): number {
  return Math.round(bytes / 1024)
}
