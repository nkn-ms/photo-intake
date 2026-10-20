// 構成図のページ。ログインしなくても見られる（router.tsx で public にしている）。
// 図は docs/architecture.drawio（draw.io で描いた図）を make diagram で PNG に書き出したもの。
// README.md の構成図も同じ PNG なので、画面と README で図がずれない
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { ReactNode } from 'react'
// 画像を import すると、Vite はビルドしたときの画像の URL（ファイル名に中身から作った文字が入る）を返す
import diagramUrl from '../architecture.png'

export default function ArchitecturePage() {
  return (
    <Stack spacing={3}>
      <Typography variant="h5" component="h2" sx={{ fontWeight: 700 }}>
        構成
      </Typography>
      <Paper variant="outlined" sx={{ p: 2 }}>
        {/* 図は横に長く、スマホでは文字が小さくなるので、押すと元の大きさの画像を新しいタブで開けるようにする */}
        <Link href={diagramUrl} target="_blank" rel="noopener">
          <Box
            component="img"
            src={diagramUrl}
            alt="構成図。画面・API・解析・LINE・知らせの写真の流れを、AWS のアイコンで描いたもの"
            sx={{ display: 'block', width: '100%', height: 'auto' }}
          />
        </Link>
        <Typography variant="body2" sx={{ mt: 1, color: 'text.secondary' }}>
          矢印の色：灰＝画面から・緑＝LINE から登録・青＝確定したら知らせる・赤＝失敗したとき。図を押すと大きく開く
        </Typography>
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
