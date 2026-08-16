<template>
  <div class="feishu card">
    <h2>🟢 飞书接入</h2>
    <p class="muted">扫码把 LifeOS 接入你的飞书：在飞书里发消息会触发 AI 回复，发新闻链接会自动研判收录。</p>

    <div class="status">
      <span class="tag" :class="status.cred_configured ? 'ok' : 'gray'">
        凭据{{ status.cred_configured ? '已配置' : '未配置' }}
      </span>
      <span class="tag" :class="status.bot_online ? 'ok' : 'gray'">
        Bot{{ status.bot_online ? '在线' : '离线' }}
      </span>
      <span class="tag gray">素材数 {{ status.news_elements }}</span>
    </div>

    <div class="row wrap" style="margin-top:12px">
      <button @click="startAuth" :disabled="busy">扫码授权（设备流）</button>
      <button class="ghost" @click="botStart" :disabled="busy">启动/重连 Bot</button>
      <button class="ghost danger" @click="disconnect" :disabled="busy">解绑</button>
    </div>
    <p v-if="err" class="error">{{ err }}</p>

    <div v-if="qrUri" class="qr-box">
      <p>用 <b>飞书 App</b> 扫码，在官方授权页点击允许：</p>
      <canvas ref="canvasEl"></canvas>
      <p class="muted small">等待授权中…（{{ pollText }}）</p>
    </div>
    <div v-else-if="status.bot_online" class="ok-text">✅ 飞书已连接，去飞书发条消息试试。</div>
    <div v-else class="empty">尚未连接飞书。</div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import QRCode from 'qrcode'
import { api } from '../api.js'

const status = ref({ cred_configured: false, bot_online: false, news_elements: 0 })
const qrUri = ref('')
const pollToken = ref('')
const pollText = ref('')
const busy = ref(false)
const err = ref('')
const canvasEl = ref(null)
let timer = null

async function refreshStatus() {
  try { status.value = await api.feishuStatusInfo() } catch (_) {}
}

async function startAuth() {
  err.value = ''; busy.value = true
  try {
    const r = await api.feishuQrcode()
    qrUri.value = r.scan_url
    pollToken.value = r.poll_token
    await nextTick()
    if (canvasEl.value) QRCode.toCanvas(canvasEl.value, r.scan_url, { width: 220, margin: 1 })
    startPoll()
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

function startPoll() {
  let tries = 0
  clearInterval(timer)
  timer = setInterval(async () => {
    tries++
    pollText.value = `已等待 ${tries * 2}s`
    try {
      const r = await api.feishuStatusPoll(pollToken.value)
      if (r.status === 'success') {
        clearInterval(timer); qrUri.value = ''
        await refreshStatus()
        err.value = ''
      } else if (r.status === 'expired' || r.status === 'denied') {
        clearInterval(timer); qrUri.value = ''
        err.value = r.status === 'expired' ? '授权超时，请重试' : '已拒绝授权'
      }
    } catch (e) {
      clearInterval(timer); qrUri.value = ''
      err.value = e.message
    }
  }, 2000)
}

async function botStart() {
  busy.value = true; err.value = ''
  try { await api.feishuBotStart(); await refreshStatus() }
  catch (e) { err.value = e.message } finally { busy.value = false }
}

async function disconnect() {
  busy.value = true; err.value = ''
  try { await api.feishuDisconnect(); await refreshStatus() }
  catch (e) { err.value = e.message } finally { busy.value = false }
}

onMounted(refreshStatus)
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.feishu { max-width: 640px; }
.status { display: flex; gap: 8px; margin-top: 10px; }
.qr-box { margin-top: 16px; text-align: center; }
.qr-box canvas { margin: 10px auto; display: block; border: 1px solid var(--line); border-radius: 10px; }
.small { font-size: 12px; }
</style>
