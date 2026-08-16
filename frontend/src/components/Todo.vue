<template>
  <div class="todo card">
    <h2>✅ 待办 / 任务</h2>
    <div class="row">
      <input class="grow" v-model="title" placeholder="添加待办，如：明天 10 点开会" @keyup.enter="add" />
      <input v-model="due" placeholder="截止(可选)" style="width: 150px" />
      <button :disabled="!title.trim() || busy" @click="add">{{ busy ? '…' : '添加' }}</button>
    </div>
    <p v-if="err" class="error">{{ err }}</p>
    <p v-if="loading" class="muted">加载中…</p>
    <div v-else-if="items.length === 0" class="empty">暂无待办，享受清静 🍃</div>
    <ul v-else class="list">
      <li v-for="t in sorted" :key="t.id" class="item" :class="{ done: t.done }">
        <label class="row grow">
          <input type="checkbox" :checked="t.done" @change="toggle(t)" style="width:auto" />
          <span class="grow">{{ t.title }}</span>
          <span v-if="t.due" class="tag gray">截止 {{ t.due }}</span>
        </label>
        <button class="ghost" @click="remove(t)">删除</button>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api.js'

const items = ref([])
const title = ref('')
const due = ref('')
const loading = ref(true)
const busy = ref(false)
const err = ref('')

const sorted = computed(() =>
  [...items.value].sort((a, b) => (a.done === b.done ? 0 : a.done ? 1 : -1))
)

async function load() {
  loading.value = true
  try {
    const r = await api.listTodos()
    items.value = r.items || []
  } catch (e) { err.value = e.message } finally { loading.value = false }
}

async function add() {
  const t = title.value.trim()
  if (!t || busy.value) return
  busy.value = true; err.value = ''
  try {
    const r = await api.addTodo(t, null, due.value.trim() || null)
    items.value.unshift(r)
    title.value = ''; due.value = ''
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

async function toggle(t) {
  try {
    if (!t.done) { const r = await api.doneTodo(t.id); Object.assign(t, r) }
    else { t.done = false } // 仅前端撤回勾选（后端不支持反勾，刷新恢复）
  } catch (e) { err.value = e.message }
}

async function remove(t) {
  try {
    await api.delTodo(t.id)
    items.value = items.value.filter((x) => x.id !== t.id)
  } catch (e) { err.value = e.message }
}

onMounted(load)
</script>

<style scoped>
.todo { max-width: 720px; }
.list { list-style: none; padding: 0; margin: 14px 0 0; display: flex; flex-direction: column; gap: 8px; }
.item { display: flex; gap: 10px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; }
.item.done .grow span { text-decoration: line-through; color: var(--muted); }
.item label { gap: 10px; }
</style>
