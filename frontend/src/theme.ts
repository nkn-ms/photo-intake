// 画面全体の見た目（色・文字）。MUI の createTheme で作り、main.tsx の ThemeProvider で全部の部品に配る
import { createTheme } from '@mui/material/styles'

export const theme = createTheme({
  palette: {
    primary: { main: '#1e6b52' }, // 落ち着いた緑（拾得物の窓口の業務画面。派手にしない）
    background: { default: '#f5f7f6' },
  },
  typography: {
    // MUI の既定は Roboto（英字向けで、別に読み込みが要る）。日本語が読みやすい、端末に入っている書体を使う
    fontFamily: 'system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", "Yu Gothic UI", sans-serif',
  },
  shape: { borderRadius: 8 },
})
