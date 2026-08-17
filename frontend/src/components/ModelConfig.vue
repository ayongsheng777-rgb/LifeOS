<template>
  <div class="mc">
    <!-- 当前模型 -->
    <div class="card">
      <div class="row spread">
        <h2>🧩 模型配置</h2>
        <button class="ghost" @click="loadAll">刷新</button>
      </div>
      <p v-if="loading" class="muted">加载中…</p>
      <p v-else-if="models.length === 0" class="empty">尚未配置任何模型。从下方「预设模型库」或「获取模型列表」添加。</p>
      <ul v-else class="list">
        <li v-for="m in models" :key="m.id" class="item" :class="{ active: m.is_active }">
          <div class="grow">
            <b>{{ m.name }}</b>
            <span class="muted small"> · {{ m.id }}</span>
            <div class="break muted small">{{ m.base_url }} · {{ m.model }}</div>
            <div class="small">
              <span :class="m.has_key ? 'ok-text' : 'warn-text'">{{ m.has_key ? '有 Key' : '无 Key' }}</span>
              <span v-if="m.is_active" class="tag ok" style="margin-left:6px">默认</span>
            </div>
          </div>
          <button v-if="!m.is_active" class="ghost" @click="setActive(m.id)">设为默认</button>
          <button class="ghost danger" @click="remove(m.id)">删除</button>
        </li>
      </ul>
    </div>

    <!-- 添加 / 编辑表单 -->
    <div class="card" ref="formCard" style="margin-top:14px">
      <h3 style="margin:0 0 10px">添加 / 修改模型</h3>
      <div class="grid2">
        <input v-model="form.id" placeholder="模型配置ID（如 deepseek / qwen-max）" />
        <input v-model="form.name" placeholder="显示名（可选）" />
        <input class="grow" v-model="form.base_url" placeholder="Base URL（如 https://api.deepseek.com/v1）" />
        <input v-model="form.model" placeholder="模型名（如 deepseek-chat）" />
        <input class="grow" v-model="form.api_key" :type="showKey ? 'text' : 'password'"
               placeholder="API Key（仅本地保存，不回显明文）" />
        <label class="row" style="gap:6px"><input type="checkbox" v-model="showKey" />显示</label>
        <input class="grow" v-model="form.proxy" placeholder="代理（可选，如 http://127.0.0.1:7890）" />
      </div>
      <div class="row" style="margin-top:10px">
        <button :disabled="!canSubmit || busy" @click="submitAdd">{{ busy ? '…' : '保存模型' }}</button>
        <button class="ghost" @click="resetForm">清空</button>
        <span class="muted small" v-if="lastProvider">检测到渠道：{{ lastProvider }}<template v-if="lastPricing"> · 官方参考价 ¥{{ lastPricing.in_per_million }}/¥{{ lastPricing.out_per_million }} 每百万</template></span>
      </div>
      <p v-if="err" class="error">{{ err }}</p>
    </div>

    <!-- 获取模型列表 -->
    <div class="card" style="margin-top:14px">
      <h3 style="margin:0 0 10px">🔍 获取模型列表</h3>
      <div class="grid2">
        <input class="grow" v-model="fetchForm.base_url" placeholder="Base URL" />
        <input v-model="fetchForm.api_key" :type="showFetchKey ? 'text' : 'password'" placeholder="API Key（可选）" />
        <label class="row" style="gap:6px"><input type="checkbox" v-model="showFetchKey" />显示</label>
        <button :disabled="!fetchForm.base_url || fetchBusy" @click="doFetch">{{ fetchBusy ? '获取中…' : '获取' }}</button>
      </div>
      <p v-if="fetchErr" class="error">{{ fetchErr }}</p>
      <p v-else-if="fetched.length" class="muted small">共 {{ fetched.length }} 个模型，点击填入上方表单：</p>
      <div v-if="fetched.length" class="chips">
        <button v-for="f in fetched" :key="f.id" class="chip" @click="pickFetched(f)">{{ f.id }}</button>
      </div>
    </div>

    <!-- 预设模型库 -->
    <div class="card" style="margin-top:14px">
      <h3 style="margin:0 0 10px">📦 预设模型库</h3>
      <div v-for="p in presets" :key="p.key" class="prov">
        <div class="prov-head" @click="toggleProv(p.key)">
          <span class="caret">{{ expanded === p.key ? '▾' : '▸' }}</span>
          <b>{{ p.name }}</b>
          <span class="muted small">{{ p.base_url }}</span>
        </div>
        <div v-if="expanded === p.key" class="prov-body">
          <div v-for="m in p.models" :key="m.id" class="model-row">
            <div class="grow">
              <b>{{ m.name }}</b> <span class="muted small">{{ m.id }}</span>
              <div class="muted small">¥{{ m.in_per_million }} / ¥{{ m.out_per_million }} 每百万（{{ m.note }}）</div>
            </div>
            <button class="ghost" @click="addFromPreset(p, m)">添加</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 测速 -->
    <div class="card" style="margin-top:14px">
      <h3 style="margin:0 0 10px">⚡ 模型测速</h3>
      <div class="row wrap">
        <select v-model="speedId" style="min-width:240px">
          <option value="">选择已配置模型（需有 Key）</option>
          <option v-for="m in models.filter(x=>x.has_key)" :key="m.id" :value="m.id">{{ m.name }}（{{ m.id }}）</option>
        </select>
        <input v-model.number="speedRounds" type="number" min="1" max="10" style="width:90px" />
        <span class="muted small">轮</span>
        <button :disabled="!speedId || speedBusy" @click="runSpeed">{{ speedBusy ? '测速中…' : '开始测速' }}</button>
      </div>
      <p v-if="speedErr" class="error">{{ speedErr }}</p>
      <div v-if="speed" class="speed">
        <div v-if="speed.avg" class="summary">
          <div><span class="muted">平均首字</span><b>{{ speed.avg.ttft_ms }} ms</b></div>
          <div><span class="muted">平均总延迟</span><b>{{ speed.avg.latency_ms }} ms</b></div>
          <div><span class="muted">平均吞吐</span><b>{{ speed.avg.tps }} tok/s</b></div>
          <div><span class="muted">成功</span><b>{{ speed.success }}/{{ speed.total }}</b></div>
        </div>
        <table v-if="speed.rounds && speed.rounds.length" class="tbl">
          <thead><tr><th>#</th><th>首字(ms)</th><th>总延迟(ms)</th><th>输出tok</th><th>tok/s</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in speed.rounds" :key="i">
              <td>{{ i + 1 }}</td>
              <td :class="r.ok ? '' : 'warn-text'">{{ r.ok ? r.ttft_ms : '—' }}</td>
              <td :class="r.ok ? '' : 'warn-text'">{{ r.ok ? r.latency_ms : '—' }}</td>
              <td>{{ r.ok ? r.output_tokens : '—' }}</td>
              <td>{{ r.ok ? r.tps : (r.reason || '失败') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Token 费用参考 -->
    <div class="card" style="margin-top:14px">
      <div class="row spread">
        <h3 style="margin:0">💸 Token 费用参考</h3>
        <button class="ghost" @click="loadPricing">刷新</button>
      </div>
      <p class="muted small">单位：元 / 每百万 token（输入 / 输出分开）。价格为公开价参考值，厂商调价后请以官方为准。</p>
      <div v-if="usage" class="summary" style="margin:8px 0">
        <div><span class="muted">已折算费用</span><b>¥ {{ fmtCost(usage.cost) }}</b></div>
        <div><span class="muted">总 Token</span><b>{{ usage.total_tokens }}</b></div>
        <div><span class="muted">调用</span><b>{{ usage.calls }}</b></div>
        <div><span class="muted">失败</span><b :class="usage.fail?'warn-text':''">{{ usage.fail }}</b></div>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr><th>渠道</th><th>模型</th><th>输入 ¥/M</th><th>输出 ¥/M</th></tr></thead>
          <tbody>
            <tr v-for="(p, i) in pricing" :key="i">
              <td>{{ p.provider }}</td>
              <td>{{ p.model }}</td>
              <td>{{ p.in_per_million }}</td>
              <td>{{ p.out_per_million }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { api } from '../api.js'

const loading = ref(true)
const busy = ref(false)
const err = ref('')
const models = ref([])
const presets = ref([])
const pricing = ref([])
const usage = ref(null)

const form = reactive({ id: '', name: '', base_url: '', model: '', api_key: '', proxy: '' })
const showKey = ref(false)
const formCard = ref(null)
const lastProvider = ref('')
const lastPricing = ref(null)

const fetchForm = reactive({ base_url: '', api_key: '' })
const showFetchKey = ref(false)
const fetched = ref([])
const fetchBusy = ref(false)
const fetchErr = ref('')

const expanded = ref('')
const speedId = ref('')
const speedRounds = ref(3)
const speed = ref(null)
const speedBusy = ref(false)
const speedErr = ref('')

const canSubmit = computed(() => form.id.trim() && form.base_url.trim() && form.model.trim())

async function loadAll() {
  loading.value = true; err.value = ''
  try {
    const [ml, pp, pr] = await Promise.all([
      api.listModels(), api.modelsPresets(), api.modelsPricing(),
    ])
    models.value = ml.models || []
    presets.value = pp.providers || []
    pricing.value = pr.pricing || []
    usage.value = pr.usage || null
  } catch (e) { err.value = e.message } finally { loading.value = false }
}
async function loadPricing() {
  try {
    const pr = await api.modelsPricing()
    pricing.value = pr.pricing || []
    usage.value = pr.usage || null
  } catch (e) { err.value = e.message }
}

function resetForm() {
  form.id = ''; form.name = ''; form.base_url = ''; form.model = ''
  form.api_key = ''; form.proxy = ''; lastProvider.value = ''; lastPricing.value = null
}

function addFromPreset(p, m) {
  form.id = m.id; form.name = m.name; form.base_url = p.base_url
  form.model = m.id; form.api_key = ''; form.proxy = ''
  lastProvider.value = p.key
  lastPricing.value = { in_per_million: m.in_per_million, out_per_million: m.out_per_million }
  formCard.value && formCard.value.scrollIntoView({ behavior: 'smooth' })
}

function pickFetched(f) {
  form.model = f.id; form.id = f.id
  if (!form.base_url) form.base_url = fetchForm.base_url
  if (!form.api_key && fetchForm.api_key) form.api_key = fetchForm.api_key
  formCard.value && formCard.value.scrollIntoView({ behavior: 'smooth' })
}

async function submitAdd() {
  if (!canSubmit.value || busy.value) return
  busy.value = true; err.value = ''
  try {
    await api.addModel({
      id: form.id.trim(),
      name: form.name.trim() || form.id.trim(),
      base_url: form.base_url.trim(), model: form.model.trim(),
      api_key: form.api_key, proxy: form.proxy.trim() || '',
    })
    await loadAll()
    resetForm()
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

async function remove(id) {
  if (!confirm('确认删除模型「' + id + '」？')) return
  try { await api.delModel(id); await loadAll() } catch (e) { err.value = e.message }
}
async function setActive(id) {
  try { await api.setActiveModel(id); await loadAll() } catch (e) { err.value = e.message }
}

async function doFetch() {
  if (!fetchForm.base_url || fetchBusy.value) return
  fetchBusy.value = true; fetchErr.value = ''; fetched.value = []
  try {
    const r = await api.fetchModels(fetchForm.base_url, fetchForm.api_key, '')
    if (!r.ok) { fetchErr.value = r.reason || '获取失败'; return }
    fetched.value = r.models || []
  } catch (e) { fetchErr.value = e.message } finally { fetchBusy.value = false }
}

function toggleProv(key) { expanded.value = expanded.value === key ? '' : key }

async function runSpeed() {
  if (!speedId.value || speedBusy.value) return
  speedBusy.value = true; speedErr.value = ''; speed.value = null
  try {
    const r = await api.speedTest({ id: speedId.value, rounds: speedRounds.value })
    if (!r.ok && !r.rounds) { speedErr.value = r.reason || '测速失败'; return }
    speed.value = r
  } catch (e) { speedErr.value = e.message } finally { speedBusy.value = false }
}

function fmtCost(v) {
  const n = Number(v || 0)
  return n.toFixed(4)
}

onMounted(loadAll)
</script>

<style scoped>
.mc { max-width: 860px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: center; }
.row { display: flex; align-items: center; gap: 8px; }
.row.wrap { flex-wrap: wrap; }
.spread { justify-content: space-between; }
.grow { flex: 1; min-width: 0; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.break { word-break: break-all; }
.ok-text { color: var(--brand); font-weight: 600; }
.warn-text { color: var(--danger); }
.error { color: var(--danger); font-size: 13px; }
.empty { color: var(--muted); padding: 10px 0; }
.list { list-style: none; padding: 0; margin: 10px 0 0; display: flex; flex-direction: column; gap: 8px; }
.item { display: flex; gap: 10px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; }
.item.active { border-color: var(--brand); background: var(--brand-soft); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; max-height: 180px; overflow-y: auto; }
.chip { background: var(--bg); border: 1px solid var(--line); border-radius: 14px; padding: 4px 12px; font-size: 13px; cursor: pointer; }
.chip:hover { border-color: var(--brand); }
.prov { border: 1px solid var(--line); border-radius: 10px; margin-bottom: 8px; overflow: hidden; }
.prov-head { display: flex; align-items: center; gap: 8px; padding: 10px 12px; cursor: pointer; background: var(--bg); }
.caret { width: 14px; color: var(--muted); }
.prov-body { padding: 4px 12px 10px; }
.model-row { display: flex; gap: 10px; align-items: center; padding: 8px 0; border-top: 1px solid var(--line); }
.summary { display: flex; flex-wrap: wrap; gap: 18px; padding: 12px; background: var(--bg); border-radius: 10px; }
.summary b { font-size: 15px; margin-left: 6px; }
.tbl-wrap { max-height: 320px; overflow-y: auto; margin-top: 10px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
.tbl th { position: sticky; top: 0; background: var(--panel); color: var(--muted); font-weight: 600; }
.speed { margin-top: 12px; }
button { background: var(--brand); color: #fff; border: none; border-radius: 8px; padding: 8px 14px; cursor: pointer; }
button:disabled { opacity: .5; cursor: not-allowed; }
.ghost { background: transparent; color: var(--ink); border: 1px solid var(--line); }
.ghost.danger { color: var(--danger); border-color: var(--danger); }
input, select { background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; color: var(--ink); font-size: 14px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }
.tag { font-size: 11px; padding: 1px 8px; border-radius: 10px; }
.tag.ok { background: var(--brand-soft); color: var(--brand); }
h2 { margin: 0; }
</style>
