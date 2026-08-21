import { defineStore } from 'pinia'
import { getToken, setToken, clearToken } from '../api.js'

// 鉴权状态集中管理（O-5：引入 Pinia 做全局状态）。
// token 仍以 localStorage 为真相源（api.js 负责 401 清 token + 广播事件），
// 这里只做响应式包装，避免每个组件各自读 localStorage。
export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: getToken(),
  }),
  getters: {
    isLoggedIn: (state) => !!state.token,
  },
  actions: {
    login(t) {
      setToken(t)
      this.token = t
    },
    logout() {
      clearToken()
      this.token = ''
    },
  },
})
