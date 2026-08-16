<template>
  <div class="news card">
    <div class="row spread">
      <h2>📰 已摄入新闻素材</h2>
      <button class="ghost" @click="load">刷新</button>
    </div>
    <p class="muted">来源：飞书里把新闻链接发给我（或粘贴正文），后端自动研判并收录。重启服务后内存态会清空。</p>
    <p v-if="loading" class="muted">加载中…</p>
    <div v-else-if="items.length === 0" class="empty">暂无素材。去飞书发条新闻链接试试。</div>
    <ul v-else class="list">
      <li v-for="(n, i) in items" :key="i" class="item">
        <div class="row spread">
          <span class="muted">{{ n.time }}</span>
          <span v-if="n.score != null" class="tag" :class="n.score >= 55 ? 'ok' : (n.score <= 45 ? 'warn' : 'gray')">
            评分 {{ n.score }}
          </span>
        </div>
        <div class="summary">{{ n.summary }}</div>
        <a v-if="n.url" :href="n.url" target="_blank" rel="noopener" class="link">查看原文 ↗</a>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const items = ref([])
const loading = ref(true)

async function load() {
  loading.value = true
  try {
    const r = await api.news()
    items.value = (r.items || []).slice().reverse()
  } catch (e) { items.value = [] } finally { loading.value = false }
}
onMounted(load)
</script>

<style scoped>
.news { max-width: 760px; }
.list { list-style: none; padding: 0; margin: 12px 0 0; display: flex; flex-direction: column; gap: 10px; }
.item { padding: 12px 14px; border: 1px solid var(--line); border-radius: 10px; }
.summary { margin: 6px 0; white-space: pre-wrap; }
.link { color: var(--brand); font-size: 13px; text-decoration: none; }
</style>
