import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from '../components/Dashboard.vue'
import Chat from '../components/Chat.vue'
import Todo from '../components/Todo.vue'
import Expense from '../components/Expense.vue'
import News from '../components/News.vue'
import Feishu from '../components/Feishu.vue'
import ModelConfig from '../components/ModelConfig.vue'
import Backup from '../components/Backup.vue'
import Config from '../components/Config.vue'
import { getToken } from '../api.js'

// 路由表：将原先 App.vue 里的 tab 切换提升为真正的 URL 路由，
// 组件本身不变（仍为 components/ 下的现有组件），仅由 router-view 渲染。
const routes = [
  { path: '/', redirect: '/chat' },
  { path: '/dashboard', name: 'dashboard', component: Dashboard, meta: { title: '概览' } },
  { path: '/chat', name: 'chat', component: Chat, meta: { title: '对话' } },
  { path: '/todo', name: 'todo', component: Todo, meta: { title: '待办' } },
  { path: '/expense', name: 'expense', component: Expense, meta: { title: '记账' } },
  { path: '/news', name: 'news', component: News, meta: { title: '资讯' } },
  { path: '/feishu', name: 'feishu', component: Feishu, meta: { title: '飞书' } },
  { path: '/models', name: 'models', component: ModelConfig, meta: { title: '模型' } },
  { path: '/backup', name: 'backup', component: Backup, meta: { title: '备份' } },
  { path: '/config', name: 'config', component: Config, meta: { title: '设置' } },
  // 未知路径兜底到对话页
  { path: '/:pathMatch(.*)*', redirect: '/chat' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 全局前置守卫：未登录则退回登录态（由 App.vue 渲染 Login）。
// 这里只做兜底——真正的登录门禁在 App.vue 的 token 判断。
router.beforeEach((to) => {
  const token = getToken()
  if (!token && to.path !== '/login') {
    // token 缺失：交给 App.vue 的登录门禁渲染 Login
    return true
  }
  return true
})

export default router
