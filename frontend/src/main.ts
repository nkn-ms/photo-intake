import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import { configureAuth } from './auth'

// 画面を作る前に、どの Cognito にログインするかを Amplify に教える（1回だけ）
configureAuth()

createApp(App).mount('#app')
