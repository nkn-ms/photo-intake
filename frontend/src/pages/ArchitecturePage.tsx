// 構成図のページ。ログインしなくても見られる（router.tsx で public にしている）。
// 図は architecture.mmd（Mermaid の文字）から描く。README.md の構成図も同じ文字なので、画面と README で図がずれない
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import LinearProgress from '@mui/material/LinearProgress'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useTheme } from '@mui/material/styles'
import { useEffect, useState, type ReactNode } from 'react'
// ?raw：ファイルの中身を、そのまま1つの文字列として読み込む（Vite の機能）
import diagram from '../architecture.mmd?raw'

// mermaid.render に渡す ID は、描くたびに違うものにする。
// 開発中は StrictMode が useEffect を2回動かすので、同じ ID だと描いている途中の2つがぶつかる
let renderCount = 0

export default function ArchitecturePage() {
  const [svg, setSvg] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fontFamily = useTheme().typography.fontFamily

  // ページが出たときに1回、図を描く。ignore の役目は一覧のページと同じ（ページを離れた後の結果を捨てる）
  useEffect(() => {
    let ignore = false

    async function draw(): Promise<void> {
      try {
        // import()：mermaid（数百 KB）を、このページを開いたときに初めて読む。
        // 先頭で import すると、構成図を見ない人（登録だけする人）まで最初に読むことになる
        const { default: mermaid } = await import('mermaid')
        mermaid.initialize({
          startOnLoad: false, // ページの中の .mermaid を自動で探して描く機能。React では自分で render を呼ぶので切る
          theme: 'base', // 色を themeVariables で決める土台のテーマ
          fontFamily,
          themeVariables: {
            primaryColor: '#e8f1ed', // 箱の塗り（theme.ts の緑を薄くした色）
            primaryBorderColor: '#1e6b52', // 箱の線（theme.ts の緑）
            primaryTextColor: '#1a1a1a',
            lineColor: '#5f6b66', // 矢印
            clusterBkg: '#f9fbfa', // 枠（subgraph）の塗り
            clusterBorder: '#b9c8c1',
          },
        })
        renderCount += 1
        const result = await mermaid.render(`architecture-${renderCount}`, diagram)
        if (!ignore) {
          setSvg(result.svg)
        }
      } catch (e) {
        if (!ignore) {
          setError(`図を描けませんでした（${e instanceof Error ? e.message : String(e)}）`)
        }
      }
    }

    void draw()
    return () => {
      ignore = true
    }
  }, [fontFamily])

  // 図の部分に出すものを、ガード節で上から順に決める
  function figure() {
    if (error) {
      return <Alert severity="error">{error}</Alert>
    }
    if (svg === null) {
      return <LinearProgress />
    }
    // dangerouslySetInnerHTML：文字列の HTML（ここでは SVG）をそのまま入れる。
    // 人が入れた文字を入れると XSS の穴になるが、ここに入るのは architecture.mmd（自分たちのファイル）から作った SVG だけ。
    // mermaid も既定（securityLevel: 'strict'）で、図の中の文字からスクリプトになるものを取り除いている
    return <Box dangerouslySetInnerHTML={{ __html: svg }} sx={{ '& svg': { display: 'block', mx: 'auto' } }} />
  }

  return (
    <Stack spacing={3}>
      <Typography variant="h5" component="h2" sx={{ fontWeight: 700 }}>
        構成
      </Typography>
      <Paper variant="outlined" sx={{ p: 2 }}>
        {figure()}
      </Paper>

      <Section title="この構成の要点">
        <li>
          <b>VPC の中は Aurora だけ。</b>Lambda は VPC の外に置き、Data API（HTTPS で SQL を送る仕組み）で DB を使う。
          VPC の外に出る道（NAT Gateway・月 $45 前後）が要らない
        </li>
        <li>
          <b>写真は API を通らない。</b>API は署名付きURL（期限付きで S3 に置く許可）を返すだけで、ブラウザが S3 に直接置く
        </li>
        <li>
          <b>解析は非同期。</b>S3 → SQS → worker。一時的な失敗は3回までやり直し、それでも駄目なら DLQ
          に移り、アラームがメールで知らせる。やり直しても変わらない失敗（読めない形式など）は、その場で「解析失敗」にする。
          SQS は同じ通知を2回届けることがあるので、worker は DynamoDB の記録で二重実行を止める（@idempotent_function）
        </li>
        <li>
          <b>AI の答えを上書きしない。</b>AI の答えと人が確定した値を別の列に持つ。後から AI の正しさを測れる
        </li>
      </Section>

      <Section title="今は代わりのものを使っているところ">
        <li>
          <b>画面の置き場所：Cloudflare Workers。</b>本来は CloudFront + S3。AWS アカウントの確認が済むまで CloudFront
          を作れなかったための代わり（確認は済み、CloudFront は作成済み。画面を S3 に置けば切り替えられる）
        </li>
        <li>
          <b>AI：Claude API（Anthropic）を直接。</b>本来は Bedrock（コードは analyzer = bedrock で切り替える。Bedrock はアカウントの制限が解けるのを待っている）。写真が AWS
          の外に出るので、本番では Bedrock にする
        </li>
      </Section>

      <Section title="まだ作っていないもの">
        <li>落とし主の問い合わせ（自由文）から、近い拾得物を探す（pgvector のベクトル類似検索）</li>
        <li>
          <b>LINE から拾得物を登録する。</b>スタッフが LINE で写真を送ると、拾得物として登録される。写真を S3
          に置けば、その後は画面から登録したときと同じ流れ（SQS → worker → AI）に乗る
        </li>
        <li>
          <b>社内の LINE グループに知らせる。</b>AI の解析が終わったら、落とし物の知らせ用のグループ（例：「〇〇社
          落とし物情報」）に「休憩室に鍵の忘れ物がありました」のように届ける（何の忘れ物かは解析が終わるまで分からないので、そこで送る）
        </li>
        <li>（優先度を下げている）落とし主の問い合わせに合う拾得物が見つかったら、落とし主にも LINE で知らせる</li>
        <li>GitHub Actions（PR に Terraform の plan を出し、merge で apply する）</li>
      </Section>
    </Stack>
  )
}

/** 見出しと箇条書きの1まとまり */
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Box>
      <Typography variant="h6" component="h3" sx={{ fontWeight: 700, mb: 1 }}>
        {title}
      </Typography>
      <Box component="ul" sx={{ m: 0, pl: 3, '& li': { mb: 1, lineHeight: 1.8 } }}>
        {children}
      </Box>
    </Box>
  )
}
