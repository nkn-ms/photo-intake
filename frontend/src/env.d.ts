// .env.local（make web-env が書き出す）の値の型。
// これを書くと、import.meta.env.VITE_〜 の書き間違いを TypeScript が指摘してくれる。
interface ImportMetaEnv {
  readonly VITE_COGNITO_USER_POOL_ID: string
  readonly VITE_COGNITO_CLIENT_ID: string
  readonly VITE_API_URL: string // 末尾は / （例：https://xxx.execute-api.ap-northeast-1.amazonaws.com/）
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
