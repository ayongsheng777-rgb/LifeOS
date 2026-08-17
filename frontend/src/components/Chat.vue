<template>
  <div class="chat card">
    <h2>💬 对话助手</h2>
    <p class="muted">直接问我任何事；试试「添加待办 买菜」「记一笔 早饭 15」「今天有什么新闻」。对话历史会自动恢复。</p>
    <div class="log scroll" ref="logEl">
      <div v-if="messages.length === 0" class="empty">还没有对话，发条消息试试～</div>
      <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
        <div class="who">{{ m.role === 'user' ? '我' : 'LifeOS' }}</div>
        <!-- AI 回复：渲染 Markdown（标题/加粗/列表/链接/图片/表格）；用户输入绝不走 v-html -->
        <div v-if="m.role === 'ai'" class="bubble md" v-html="renderMd(m.text)"></div>
        <div v-else class="bubble" style="white-space: pre-wrap">{{ m.text }}</div>
      </div>
    </div>
    <div class="row">
      <textarea v-model="input" rows="2" placeholder="说点什么…（Enter 发送，Shift+Enter 换行）" @keydown.enter.exact.prevent="send"></textarea>
      <button :disabled="busy || !input.trim()" @click="send">{{ busy ? '思考中…' : '发送' }}</button>
    </div>
    <p v-if="err" class="error">{{ err }}</p>
  </div>
</template>

<script setup>
import { ref, nextTick, onMounted } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { api } from '../api.js'

marked.setOptions({ breaks: true, gfm: true })

// 把 AI 回复的 Markdown 转成安全的 HTML（DOMPurify 兜底 XSS）
function renderMd(txt) {
  if (!txt) return ''
  try {
    const raw = marked.parse(txt)
    return DOMPurify.sanitize(raw, { ADD_ATTR: ['target'] })
  } catch (e) {
    return DOMPurify.sanitize(String(txt))
  }
}

const messages = ref([])
const input = ref('')
const busy = ref(false)
const err = ref('')
const logEl = ref(null)

async function loadHistory() {
  try {
    const r = await api.history()
    if (r.messages && r.messages.length) {
      messages.value = r.messages
      await nextTick()
      logEl.value?.scrollTo({ top: logEl.value.scrollHeight })
    }
  } catch (e) {
    // 历史加载失败不应阻断新对话，静默忽略
  }
}

onMounted(loadHistory)

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

/* ===== Markdown 富文本样式 ===== */
.bubble.md { white-space: normal; line-height: 1.65; text-align: left; max-width: 100%; }
.bubble.md :deep(h1),
.bubble.md :deep(h2),
.bubble.md :deep(h3) { margin: 8px 0 4px; line-height: 1.3; }
.bubble.md :deep(h1) { font-size: 1.15em; }
.bubble.md :deep(h2) { font-size: 1.08em; }
.bubble.md :deep(h3) { font-size: 1.02em; }
.bubble.md :deep(p) { margin: 4px 0; }
.bubble.md :deep(ul),
.bubble.md :deep(ol) { padding-left: 22px; margin: 4px 0; }
.bubble.md :deep(li) { margin: 2px 0; }
.bubble.md :deep(code) {
  background: rgba(127,127,127,0.16); padding: 1px 5px; border-radius: 4px;
  font-size: 0.9em; font-family: ui-monospace, Menlo, Consolas, monospace;
}
.bubble.md :deep(pre) {
  background: rgba(127,127,127,0.14); padding: 10px; border-radius: 8px;
  overflow: auto; margin: 6px 0;
}
.bubble.md :deep(pre code) { background: none; padding: 0; font-size: 0.88em; }
.bubble.md :deep(a) { color: var(--brand); text-decoration: underline; }
.bubble.md :deep(img) { max-width: 100%; border-radius: 8px; margin: 6px 0; display: block; }
.bubble.md :deep(table) { border-collapse: collapse; margin: 6px 0; font-size: 0.92em; }
.bubble.md :deep(th),
.bubble.md :deep(td) { border: 1px solid var(--muted); padding: 3px 8px; }
.bubble.md :deep(blockquote) { border-left: 3px solid var(--muted); margin: 4px 0; padding-left: 10px; color: var(--muted); }
</style>
