// 统一 API 封装：自动带 Bearer，401 清 token 并广播未授权事件。
const TOKEN_KEY = 'lifeos_token'

export function getToken() { return localStorage.getItem(TOKEN_KEY) || '' }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t) }
export function clearToken() { localStorage.removeItem(TOKEN_KEY) }

async function request(method, path, body) {
  const headers = {}
  const token = getToken()
  if (token) headers['Authorization'] = 'Bearer ' + token
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let resp
  try {
    resp = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (e) {
    throw new Error('网络错误：' + e.message)
  }
  if (resp.status === 401) {
    clearToken()
    window.dispatchEvent(new Event('lifeos-unauthorized'))
    throw new Error('登录已失效，请重新登录')
  }
  let data = {}
  try { data = await resp.json() } catch (_) { /* ignore */ }
  if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status))
  return data
}

const qs = (obj) => Object.entries(obj || {})
  .filter(([, v]) => v !== undefined && v !== null && v !== '')
  .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v))
  .join('&')

export const api = {
  // 鉴权
  // /api/auth/setup 在「已绑定」时返回 403 {setup_open:false}，属正常态，不抛错
  setup: async () => {
    const headers = {}
    const t = getToken()
    if (t) headers['Authorization'] = 'Bearer ' + t
    const resp = await fetch('/api/auth/setup', { headers })
    if (resp.status === 403) {
      return await resp.json().catch(() => ({ setup_open: false }))
    }
    if (resp.status === 401) {
      clearToken()
      window.dispatchEvent(new Event('lifeos-unauthorized'))
      throw new Error('请先登录')
    }
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status))
    return data
  },
  login: (otp) => request('POST', '/api/auth/login', { otp }),
  logout: () => request('POST', '/api/auth/logout'),
  health: () => request('GET', '/api/health'),
  config: () => request('GET', '/api/config'),

  // 对话
  chat: (message, user_id = 'me') =>
    request('POST', '/api/agent/chat', { message, user_id }),

  // 待办
  listTodos: () => request('GET', '/api/todos'),
  addTodo: (title, priority, due) =>
    request('POST', '/api/todos', { title, priority: priority || null, due: due || null }),
  doneTodo: (id) => request('POST', `/api/todos/${id}/done`),
  delTodo: (id) => request('DELETE', `/api/todos/${id}`),

  // 收支
  listExpense: (month) => request('GET', '/api/expense' + (qs({ month }) ? '?' + qs({ month }) : '')),
  addExpense: (payload) => request('POST', '/api/expense', payload),
  delExpense: (id) => request('DELETE', `/api/expense/${id}`),
  expenseSummary: (month) =>
    request('GET', '/api/expense/summary' + (qs({ month }) ? '?' + qs({ month }) : '')),

  // 新闻
  news: () => request('GET', '/api/feishu/news'),

  // 飞书
  feishuQrcode: () => request('POST', '/api/feishu/qrcode'),
  feishuStatusPoll: (token) => request('GET', '/api/feishu/qrcode/status?token=' + encodeURIComponent(token)),
  feishuStatusInfo: () => request('GET', '/api/feishu/status'),
  feishuDisconnect: () => request('POST', '/api/feishu/disconnect'),
  feishuBotStart: () => request('POST', '/api/feishu/bot-start'),
}
