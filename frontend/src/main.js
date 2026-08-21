import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './style.css'

// O-5：挂载 Pinia（全局状态）与 Vue Router（前端路由分层）
createApp(App).use(createPinia()).use(router).mount('#app')
