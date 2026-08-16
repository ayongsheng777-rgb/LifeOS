<template>
  <div class="login-wrap">
    <div class="login card">
      <h1>LifeOS</h1>
      <p class="muted">个人智能控制台 · 请先登录</p>

      <div v-if="loading" class="empty">加载中…</div>

      <template v-else>
        <!-- 首次绑定：展示密钥二维码 -->
        <div v-if="setupOpen" class="bind">
          <p>首次使用，请用 <b>Google 验证器 / 1Password / Authy</b> 扫码绑定：</p>
          <div class="qr-box">
            <img v-if="qr" :src="qr" class="qr-img" alt="OTP 绑定二维码" />
            <div v-else class="empty">无法生成二维码</div>
          </div>
          <div class="secret-box">
            <span class="muted">密钥（手动输入）：</span>
            <code>{{ secret }}</code>
            <button class="ghost" @click="copySecret">复制</button>
          </div>
          <p class="muted small">绑定后此页面消失，以后只需输入 6 位动态码。</p>
        </div>

        <!-- 日常登录：仅 6 位码 -->
        <div class="form">
          <label>动态验证码（6 位）</label>
          <input
            v-model="otp"
            inputmode="numeric"
            maxlength="6"
            placeholder="请输入验证器上的 6 位码"
            @keyup.enter="submit"
          />
          <button class="grow" :disabled="otp.length !== 6 || busy" @click="submit">
            {{ busy ? '验证中…' : '登录' }}
          </button>
          <p v-if="err" class="error">{{ err }}</p>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import QRCode from 'qrcode'
import { api, setToken } from '../api.js'

const emit = defineEmits(['logged-in'])
const loading = ref(true)
const setupOpen = ref(false)
const qr = ref('')
const secret = ref('')
const otp = ref('')
const busy = ref(false)
const err = ref('')

onMounted(async () => {
  try {
    const info = await api.setup()
    setupOpen.value = !!info.setup_open
    if (info.setup_open) {
      secret.value = info.secret || ''
      // 优先用后端已生成的 SVG 二维码（已 URL 编码，跨浏览器稳定）
      // 兜底：用前端 qrcode 库把 otpauth_uri 编码成 PNG
      try {
        if (info.qr_data_uri) {
          qr.value = info.qr_data_uri
        } else if (info.otpauth_uri) {
          qr.value = await QRCode.toDataURL(info.otpauth_uri, { width: 200, margin: 1 })
        }
      } catch (e) {
        qr.value = ''
      }
    }
  } catch (e) {
    err.value = e.message
  } finally {
    loading.value = false
  }
})

async function submit() {
  err.value = ''
  if (otp.value.length !== 6) { err.value = '请输入 6 位动态码'; return }
  busy.value = true
  try {
    const r = await api.login(otp.value)
    setToken(r.token)
    emit('logged-in', r.token)
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = false
  }
}

function copySecret() {
  navigator.clipboard?.writeText(secret.value)
}
</script>

<style scoped>
.login-wrap { height: 100%; display: flex; align-items: center; justify-content: center; }
.login { width: 380px; text-align: center; }
.login h1 { margin: 0 0 4px; font-size: 26px; letter-spacing: 1px; }
.form { display: flex; flex-direction: column; gap: 10px; text-align: left; margin-top: 6px; }
.form label { font-size: 13px; color: var(--muted); }
.qr-box { display: flex; justify-content: center; padding: 10px 0; }
.qr-img { width: 200px; height: 200px; background: #fff; border-radius: 8px; padding: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.12); }
.secret-box { display: flex; gap: 8px; align-items: center; justify-content: center; flex-wrap: wrap; font-size: 13px; }
.secret-box code { background: var(--bg); padding: 4px 8px; border-radius: 6px; word-break: break-all; }
.small { font-size: 12px; }
</style>
