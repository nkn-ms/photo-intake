import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import { configureAuth } from './auth'
import { router } from './router'

// 画面を作る前に、どの Cognito にログインするかを Amplify に教える（1回だけ）
configureAuth()

// app.use(router)：アプリにルーターを組み込む（RouterLink・RouterView が使えるようになる）
createApp(App).use(router).mount('#app')
