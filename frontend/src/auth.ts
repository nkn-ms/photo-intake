/**
 * ログイン（Cognito）。Amplify のライブラリを薄く包む。
 *
 * 画面のほかの場所は Amplify を直接 import せず、ここの関数だけを使う。
 * ログインの方法を替えるとき（Amplify をやめる、など）に、直すのがこのファイルだけで済むように。
 *
 * トークンの保存（ブラウザの localStorage）と、期限が近づいたときの取り直し（リフレッシュトークン）は
 * Amplify がやる。このファイルでは書かない。
 */
import { Amplify } from 'aws-amplify'
import { fetchAuthSession, signIn, signOut } from 'aws-amplify/auth'

/** アプリの起動時に1回だけ呼ぶ（main.ts）。どの User Pool の、どのアプリクライアントにログインするかを教える。 */
export function configureAuth(): void {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID,
        userPoolClientId: import.meta.env.VITE_COGNITO_CLIENT_ID,
      },
    },
  })
}

/**
 * メールアドレスとパスワードでログインする。失敗したら例外（パスワード違いなど）。
 *
 * 方式は SRP（パスワードそのものを送らずに「知っている」ことを証明する）。Amplify の既定。
 * make token の USER_PASSWORD_AUTH（パスワードをそのまま送る）とは違う。
 */
export async function logIn(email: string, password: string): Promise<void> {
  const result = await signIn({ username: email, password })
  if (!result.isSignedIn) {
    // ログインの続きの手続きが要る場合（初回のパスワード変更・MFA など）。
    // make user はパスワードを確定済みで作り、MFA も使っていないので、今は起きない
    throw new Error(`ログインの続きの手続きが必要です（${result.nextStep.signInStep}）`)
  }
}

/** ログアウトする。ブラウザに保存したトークンを消す。 */
export async function logOut(): Promise<void> {
  await signOut()
}

/**
 * 今のアクセストークン。ログインしていなければ null。
 *
 * 期限が近ければ、Amplify がリフレッシュトークンで取り直してから返す。
 * API に送るのはアクセストークン（ID トークンは API の auth.py が 401 で弾く）。
 */
export async function getAccessToken(): Promise<string | null> {
  const session = await fetchAuthSession()
  return session.tokens?.accessToken.toString() ?? null
}
