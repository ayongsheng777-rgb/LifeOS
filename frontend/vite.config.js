import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base 用 '/'，构建产物以 /assets/... 绝对路径引用，由后端 catch-all 托管
export default defineConfig({
  plugins: [vue()],
  base: '/',
  server: {
    port: 5173,
    // 本地开发时把 /api 代理到后端 7208（与 CORS 放行二选一，双保险）
    proxy: {
      '/api': {
        target: 'http://localhost:7208',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
