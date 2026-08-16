<template>
  <div class="chat card">
    <h2>💬 对话助手</h2>
    <p class="muted">直接问我任何事；试试「添加待办 买菜」「记一笔 早饭 15」「今天有什么新闻」。</p>
    <div class="log scroll" ref="logEl">
      <div v-if="messages.length === 0" class="empty">还没有对话，发条消息试试～</div>
      <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
        <div class="who">{{ m.role === 'user' ? '我' : 'LifeOS' }}</div>
        <div class="bubble" style="white-space: pre-wrap">{{ m.text }}</div>
      </div>
    </div>
    <div class="row">
      <textarea v-model="input" rows="2" placeholder="说点什么…" @keyup.ctrl.enter="send"></textarea>
      <button :disabled="busy || !input.trim()" @click="send">{{ busy ? '思考中…' : '发送' }}</button>
    </div>
    <p v-if="err" class="error">{{ err }}</p>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'
import { api } from '../api.js'

const messages = ref([])
const input = ref('')
const busy = ref(false)
const err = ref('')
const logEl = ref(null)

async function send() {
  const text = input.value.trim()
  if (!text || busy.value) return
  err.value = ''
  messages.value.push({ role: 'user', text })
  input.value = ''
  busy.value = true
  await nextTick()
  logEl.value?.scrollTo({ top: logEl.value.scrollHeight })
  try {
    const r = await api.chat(text)
    messages.value.push({ role: 'ai', text: r.reply || '（无回复）' })
  } catch (e) {
    err.value = e.message
    messages.value.push({ role: 'ai', text: '⚠️ ' + e.message })
  } finally {
    busy.value = false
    await nextTick()
    logEl.value?.scrollTo({ top: logEl.value.scrollHeight })
  }
}
</script>

<style scoped>
.chat { display: flex; flex-direction: column; height: calc(100vh - 48px); }
.log { flex: 1; margin: 12px 0; padding: 4px; }
.msg { margin-bottom: 12px; max-width: 80%; }
.msg.user { margin-left: auto; text-align: right; }
.who { font-size: 12px; color: var(--muted); margin-bottom: 2px; }
.bubble { display: inline-block; padding: 9px 12px; border-radius: 10px; background: var(--bg); text-align: left; }
.msg.user .bubble { background: var(--brand); color: #fff; }
textarea { resize: none; }
</style>
