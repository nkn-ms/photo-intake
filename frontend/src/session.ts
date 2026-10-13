/**
 * ログインしている人（GET /me の結果）を、どのページからでも読めるようにする。
 *
 * App がログインを確かめ、/me を読んでから <MeContext value={me}> で配る。
 * ログインしていない間はページを出さない（App がログインのフォームを出す）ので、
 * ページの中で useMe() を呼べば、必ずログインしている人が返る。
 */
import { createContext, useContext } from 'react'
import type { Me } from './api'

// 配る値の入れ物。Provider の外で読んだときだけ null になる
export const MeContext = createContext<Me | null>(null)

/** ログインしている人を返す。MeContext の外（App の配る範囲の外）で呼ぶと、書き間違いとして例外にする */
export function useMe(): Me {
  const me = useContext(MeContext)
  if (me === null) {
    throw new Error('useMe は MeContext の内側（ログインした後のページ）で呼んでください')
  }
  return me
}

/** ログインしている人が admin のグループに入っていれば true。確定のフォームを出すかどうかに使う（守るのは API の 403） */
export function useIsAdmin(): boolean {
  return useMe().groups.includes('admin')
}
