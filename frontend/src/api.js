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
  // 若服务器开启 LIFEOS_SETUP_TOKEN 保护，未带令牌返回 403 SETUP_TOKEN_REQUIRED
  setup: async (setupToken) => {
    const headers = {}
    const t = getToken()
    if (t) headers['Authorization'] = 'Bearer ' + t
    let url = '/api/auth/setup'
    if (setupToken) url += '?token=' + encodeURIComponent(setupToken)
    const resp = await fetch(url, { headers })
    if (resp.status === 403) {
      const data = await resp.json().catch(() => ({}))
      if (data.code === 'SETUP_TOKEN_REQUIRED') {
        const e = new Error('需要初始化令牌')
        e.code = 'SETUP_TOKEN_REQUIRED'
        throw e
      }
      return { setup_open: false }
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
  updateConfig: (payload) => request('POST', '/api/config', payload),
  systemStatus: () => request('GET', '/api/status'),

  // 对话
  chat: (message, user_id = 'me') =>
    request('POST', '/api/agent/chat', { message, user_id }),
  history: () => request('GET', '/api/agent/history'),

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

  // 连接器 / Connector
  connectorStatus: () => request('GET', '/api/connector/status'),
  connectorPush: (channel, target, message) =>
    request('POST', '/api/connector/push', { channel, target, message }),

  // 概览 / Dashboard
  aiUsage: () => request('GET', '/api/ai/usage'),
  aiUsageDaily: (days = 14) =>
    request('GET', '/api/ai/usage/daily' + (qs({ days }) ? '?' + qs({ days }) : '')),
  skillsStats: () => request('GET', '/api/skills/stats'),
  memoryShort: () => request('GET', '/api/memory/short'),
  memoryLong: (limit = 50) =>
    request('GET', '/api/memory/long' + (qs({ limit }) ? '?' + qs({ limit }) : '')),

  // 完美模型配置模块
  modelsPresets: () => request('GET', '/api/models/presets'),
  listModels: () => request('GET', '/api/models'),
  addModel: (payload) => request('POST', '/api/models', payload),
  delModel: (id) => request('DELETE', `/api/models/${encodeURIComponent(id)}`),
  setActiveModel: (id) => request('POST', '/api/models/active', { id }),
  fetchModels: (base_url, api_key, proxy) =>
    request('POST', '/api/models/fetch', { base_url, api_key: api_key || '', proxy: proxy || null }),
  speedTest: (payload) => request('POST', '/api/models/speedtest', payload),
  modelsPricing: () => request('GET', '/api/models/pricing'),

  // 技能管理（设置页）
  listSkillsMgmt: () => request('GET', '/api/skills'),
  listApiSkills: () => (request('GET', '/api/skills/api')),
  upsertApiSkill: (payload) => request('POST', '/api/skills/api', payload),
  delApiSkill: (id) => request('DELETE', `/api/skills/api/${encodeURIComponent(id)}`),
  toggleApiSkill: (id, enabled) => request('POST', `/api/skills/api/${encodeURIComponent(id)}/toggle`, { enabled }),
  createSkillPackage: (payload) => request('POST', '/api/skills/package', payload),
  delSkillPackage: (name) => request('DELETE', `/api/skills/package/${encodeURIComponent(name)}`),
  reloadSkills: () => request('POST', '/api/skills/reload'),
}
