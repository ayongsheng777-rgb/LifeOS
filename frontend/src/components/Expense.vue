<template>
  <div class="expense">
    <div class="card">
      <h2>💰 记账 / 收支</h2>
      <div class="row wrap">
        <select v-model="form.type" style="width: 120px">
          <option value="expense">支出</option>
          <option value="income">收入</option>
        </select>
        <input v-model="form.amount" type="number" step="0.01" placeholder="金额" style="width: 120px" />
        <input class="grow" v-model="form.category" placeholder="类目（如 早饭/工资）" />
      </div>
      <div class="row wrap" style="margin-top:10px">
        <input v-model="form.note" class="grow" placeholder="备注（可选）" />
        <input v-model="form.happened_at" type="date" style="width: 160px" />
        <button :disabled="!form.amount || busy" @click="add">{{ busy ? '…' : '记一笔' }}</button>
      </div>
      <p v-if="err" class="error">{{ err }}</p>
    </div>

    <div class="card" style="margin-top:14px">
      <div class="row spread">
        <h3 style="margin:0">本月流水</h3>
        <div class="row">
          <input v-model="month" type="month" @change="loadAll" style="width:150px" />
          <button class="ghost" @click="loadAll">刷新</button>
        </div>
      </div>
      <div v-if="summary" class="summary">
        <div><span class="muted">收入</span><b class="ok-text">+{{ summary.income }}</b></div>
        <div><span class="muted">支出</span><b style="color:var(--danger)">-{{ summary.expense }}</b></div>
        <div><span class="muted">结余</span><b>{{ summary.balance }}</b></div>
        <div><span class="muted">笔数</span><b>{{ summary.count }}</b></div>
      </div>
      <p v-if="loading" class="muted">加载中…</p>
      <div v-else-if="items.length === 0" class="empty">本月暂无记录</div>
      <ul v-else class="list">
        <li v-for="e in items" :key="e.id" class="item">
          <span class="tag" :class="e.type === 'income' ? 'ok' : 'warn'">
            {{ e.type === 'income' ? '收入' : '支出' }}
          </span>
          <span class="grow"><b>{{ e.type === 'income' ? '+' : '-' }}{{ e.amount }}</b> · {{ e.category }}</span>
          <span class="muted">{{ e.happened_at }}</span>
          <button class="ghost" @click="remove(e)">删除</button>
        </li>
      </ul>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const form = ref({ type: 'expense', amount: '', category: '', note: '', happened_at: '' })
const items = ref([])
const summary = ref(null)
const month = ref('')
const loading = ref(true)
const busy = ref(false)
const err = ref('')

function thisMonth() {
  const d = new Date()
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
}

async function loadAll() {
  loading.value = true; err.value = ''
  try {
    const m = month.value || undefined
    const [list, sum] = await Promise.all([
      api.listExpense(m),
      api.expenseSummary(m),
    ])
    items.value = (list.items || []).slice().reverse()
    summary.value = sum
  } catch (e) { err.value = e.message } finally { loading.value = false }
}

async function add() {
  if (!form.value.amount || busy.value) return
  busy.value = true; err.value = ''
  try {
    await api.addExpense({
      type: form.value.type,
      amount: parseFloat(form.value.amount),
      category: form.value.category.trim() || (form.value.type === 'income' ? '其他' : '其他'),
      note: form.value.note.trim(),
      happened_at: form.value.happened_at || undefined,
    })
    form.value = { type: 'expense', amount: '', category: '', note: '', happened_at: '' }
    await loadAll()
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

async function remove(e) {
  try {
    await api.delExpense(e.id)
    items.value = items.value.filter((x) => x.id !== e.id)
    await loadAll()
  } catch (err2) { err.value = err2.message }
}

onMounted(() => { month.value = thisMonth(); loadAll() })
</script>

<style scoped>
.expense { max-width: 760px; }
.summary { display: flex; gap: 22px; margin: 14px 0; padding: 12px; background: var(--bg); border-radius: 10px; }
.summary b { font-size: 16px; margin-left: 6px; }
.list { list-style: none; padding: 0; margin: 8px 0 0; display: flex; flex-direction: column; gap: 8px; }
.item { display: flex; gap: 10px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; }
</style>
