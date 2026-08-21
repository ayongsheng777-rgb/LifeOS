<template>
  <Login v-if="!auth.isLoggedIn" @logged-in="onLoggedIn" />
  <div v-else class="layout">
    <aside class="side">
      <div class="brand">LifeOS</div>
      <nav>
        <button class="nav" :class="{ active: $route.name === 'dashboard' }" @click="go('dashboard')">📊 概览</button>
        <button class="nav" :class="{ active: $route.name === 'chat' }" @click="go('chat')">💬 对话</button>
        <button class="nav" :class="{ active: $route.name === 'todo' }" @click="go('todo')">✅ 待办</button>
        <button class="nav" :class="{ active: $route.name === 'expense' }" @click="go('expense')">💰 记账</button>
        <button class="nav" :class="{ active: $route.name === 'news' }" @click="go('news')">📰 资讯</button>
        <button class="nav" :class="{ active: $route.name === 'feishu' }" @click="go('feishu')">🟢 飞书</button>
        <button class="nav" :class="{ active: $route.name === 'models' }" @click="go('models')">🧩 模型</button>
        <button class="nav" :class="{ active: $route.name === 'backup' }" @click="go('backup')">💾 备份</button>
        <button class="nav" :class="{ active: $route.name === 'config' }" @click="go('config')">⚙️ 设置</button>
      </nav>
      <div class="side-foot">
        <button class="ghost" @click="doLogout">退出登录</button>
      </div>
    </aside>
    <main class="main scroll">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth.js'
import { api } from './api.js'
import Login from './components/Login.vue'

const auth = useAuthStore()
const router = useRouter()

function go(name) {
  if (router.currentRoute.value.name !== name) router.push({ name })
}
function onLoggedIn(t) {
  auth.login(t)
}
function doLogout() {
  api.logout().catch(() => {})
  auth.logout()
  router.replace({ name: 'chat' })
}
function onUnauthorized() {
  auth.logout()
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
