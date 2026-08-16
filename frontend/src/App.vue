<template>
  <Login v-if="!token" @logged-in="onLoggedIn" />
  <div v-else class="layout">
    <aside class="side">
      <div class="brand">LifeOS</div>
      <nav>
        <button class="nav" :class="{ active: tab === 'chat' }" @click="tab = 'chat'">💬 对话</button>
        <button class="nav" :class="{ active: tab === 'todo' }" @click="tab = 'todo'">✅ 待办</button>
        <button class="nav" :class="{ active: tab === 'expense' }" @click="tab = 'expense'">💰 记账</button>
        <button class="nav" :class="{ active: tab === 'news' }" @click="tab = 'news'">📰 资讯</button>
        <button class="nav" :class="{ active: tab === 'feishu' }" @click="tab = 'feishu'">🟢 飞书</button>
        <button class="nav" :class="{ active: tab === 'config' }" @click="tab = 'config'">⚙️ 设置</button>
      </nav>
      <div class="side-foot">
        <button class="ghost" @click="doLogout">退出登录</button>
      </div>
    </aside>
    <main class="main scroll">
      <Chat v-if="tab === 'chat'" />
      <Todo v-else-if="tab === 'todo'" />
      <Expense v-else-if="tab === 'expense'" />
      <News v-else-if="tab === 'news'" />
      <Feishu v-else-if="tab === 'feishu'" />
      <Config v-else-if="tab === 'config'" />
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { getToken, clearToken, api } from './api.js'
import Login from './components/Login.vue'
import Chat from './components/Chat.vue'
import Todo from './components/Todo.vue'
import Expense from './components/Expense.vue'
import News from './components/News.vue'
import Feishu from './components/Feishu.vue'
import Config from './components/Config.vue'

const token = ref(getToken())
const tab = ref('chat')

function onLoggedIn(t) {
  token.value = t
  tab.value = 'chat'
}
function doLogout() {
  api.logout().catch(() => {})
  clearToken()
  token.value = ''
}
function onUnauthorized() {
  token.value = ''
}
onMounted(() => window.addEventListener('lifeos-unauthorized', onUnauthorized))
onUnmounted(() => window.removeEventListener('lifeos-unauthorized', onUnauthorized))
</script>

<style scoped>
.layout { display: flex; height: 100%; }
.side {
  width: 200px; flex-shrink: 0; background: var(--panel);
  border-right: 1px solid var(--line); display: flex; flex-direction: column;
  padding: 16px 12px;
}
.brand { font-size: 20px; font-weight: 700; padding: 6px 10px 18px; letter-spacing: 1px; }
nav { display: flex; flex-direction: column; gap: 6px; flex: 1; }
.nav {
  background: transparent; color: var(--ink); text-align: left;
  border-radius: 8px; padding: 10px 12px; font-size: 14px;
}
.nav:hover { background: var(--bg); opacity: 1; }
.nav.active { background: var(--brand-soft); color: var(--brand); font-weight: 600; }
.side-foot { padding-top: 12px; border-top: 1px solid var(--line); }
.side-foot .ghost { width: 100%; }
.main { flex: 1; padding: 24px; overflow-y: auto; }
</style>
