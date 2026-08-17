<template>
  <div class="skillcfg">
    <p v-if="msg" class="ok-text small">{{ msg }}</p>
    <p v-if="errMsg" class="error small">{{ errMsg }}</p>

    <!-- 默认内置技能说明 -->
    <section class="card builtin">
      <h3>📦 默认内置技能</h3>
      <p class="muted small">以下技能随系统预置、默认集成，无需手动添加即可使用（仅需在「API 技能」中填对应的 Key）。</p>
      <div class="bi-item">
        <div class="bi-name">🗺️ 高德地图 <span class="badge b-pkg">技能包</span></div>
        <div class="bi-func">
          <span class="tag">地点搜索</span>
          <span class="tag">坐标查询</span>
          <span class="tag">驾车路线规划</span>
        </div>
        <div class="bi-usage xsmall mono">高德 故宫 ｜ 高德 北京天安门 坐标 ｜ 高德 北京到上海</div>
        <div class="xsmall muted">Key 在下方「API 技能」中管理（标注「由完整技能包接管」的即高德）。</div>
      </div>
      <div class="bi-item">
        <div class="bi-name">🌤️ 天气查询 <span class="badge b-pkg">技能包</span></div>
        <div class="bi-func">
          <span class="tag">实时天气</span>
          <span class="tag">未来几天预报</span>
          <span class="tag">气温/湿度/风力</span>
        </div>
        <div class="bi-usage xsmall mono">天气 北京 ｜ 上海今天天气 ｜ 广州明天会下雨吗</div>
        <div class="xsmall muted">基于 Open-Meteo 公开数据，免费无需 Key，开箱即用。</div>
      </div>
    </section>

    <!-- 已配置技能列表 -->
    <section class="card">
      <h3>🧩 已配置技能 <button class="ghost sm" @click="load">刷新</button></h3>
      <p v-if="skills.some(s => s.managed_by_package)" class="hint">
        💡 标注「技能包接管」的条目（如高德地图）已由内置完整技能包实现，此处仅用于管理其 API Key；删除或停用请到对应技能包。
      </p>
      <p v-if="loading" class="muted small">加载中…</p>
      <p v-else-if="!skills.length" class="muted small">暂无技能，可在下方新增 API 技能或完整技能包。</p>
      <div v-else class="sk-list">
        <div v-for="s in skills" :key="s.type + ':' + s.name" class="sk-item">
          <span class="badge" :class="s.type === 'api' ? 'b-api' : 'b-pkg'">{{ s.type === 'api' ? 'API' : '技能包' }}</span>
          <div class="sk-main">
            <div class="sk-name">{{ s.name }} <span v-if="!s.enabled" class="muted">(已停用)</span></div>
            <div class="muted xsmall">{{ s.desc || '—' }}</div>
            <div class="kw" v-if="(s.trigger_keywords||[]).length">
              <span v-for="k in s.trigger_keywords" :key="k" class="tag">{{ k }}</span>
            </div>
            <div v-if="s.type==='api' && s.api_url" class="xsmall mono">{{ s.method }} {{ s.api_url }}</div>
            <div v-if="s.managed_by_package" class="managed-note">🔧 由内置完整技能包接管（此处仅管理 API Key）</div>
            <div v-if="s.api_key_masked" class="xsmall mono">Key: {{ s.api_key_masked }}</div>
          </div>
          <div class="sk-actions">
            <template v-if="s.managed_by_package">
              <span class="muted xsmall">Key 由此管理</span>
            </template>
            <template v-else>
              <button v-if="s.type==='api'" class="ghost sm" @click="toggleApi(s)">{{ s.enabled ? '停用' : '启用' }}</button>
              <button class="ghost sm danger" @click="del(s)">删除</button>
            </template>
          </div>
        </div>
      </div>
    </section>

    <!-- 新增 API 技能（配置驱动，无需写代码） -->
    <section class="card">
      <h3>➕ 新增 API 技能</h3>
      <div class="form">
        <label>名称 *
          <input v-model="apiForm.name" placeholder="如 高德地图" />
        </label>
        <label>描述
          <input v-model="apiForm.description" placeholder="一句话说明用途" />
        </label>
        <label>触发词（逗号分隔） *
          <input v-model="apiKwText" placeholder="高德,地图,导航" />
        </label>
        <label>请求地址模板 *
          <input v-model="apiForm.api_url" placeholder="https://restapi.amap.com/v3/...?keywords={query}&key=..." />
        </label>
        <label>API Key（可选，作为 Bearer 注入）
          <input v-model="apiForm.api_key" type="password" placeholder="留空则不带鉴权" />
        </label>
        <label>请求方式
          <select v-model="apiForm.method">
            <option value="GET">GET</option>
            <option value="POST">POST</option>
          </select>
        </label>
        <label class="inline-check">
          <input type="checkbox" v-model="apiForm.enabled" /> 启用
        </label>
      </div>
      <p class="muted xsmall">地址中 <code>{query}</code> 会被替换成「用户消息去掉触发词后的剩余文本」。首次触发示例：「高德地图 北京到上海」。</p>
      <button class="primary" :disabled="busy" @click="saveApi">保存 API 技能</button>
    </section>

    <!-- 新增完整技能包（写代码，热加载） -->
    <section class="card">
      <h3>📦 新增完整技能包</h3>
      <div class="warn">⚠️ 技能包的 <code>handler.py</code> 代码将在本进程直接执行，仅添加自己信任的代码，勿粘贴来源不明的代码。</div>
      <div class="form">
        <label>技能名（字母/数字/下划线） *
          <input v-model="pkg.name" placeholder="如 my_tool" />
        </label>
        <label>描述
          <input v-model="pkg.description" placeholder="一句话说明" />
        </label>
        <label>触发词（逗号分隔）
          <input v-model="pkgKwText" placeholder="工具,tool" />
        </label>
        <label>handler.py 代码 *
          <textarea v-model="pkg.handler_code" rows="12" spellcheck="false" placeholder="在此粘贴 Python 代码"></textarea>
        </label>
      </div>
      <button class="ghost" @click="fillTemplate">填入示例代码</button>
      <button class="primary" :disabled="busy" @click="savePkg">写入并热加载</button>
    </section>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const skills = ref([])
const loading = ref(false)
const busy = ref(false)
const msg = ref('')
const errMsg = ref('')

const apiKwText = ref('')
const apiForm = ref({ name: '', description: '', api_url: '', api_key: '', method: 'GET', enabled: true })

const pkgKwText = ref('')
const pkg = ref({ name: '', description: '', handler_code: '' })

const TEMPLATE = `"""自定义技能：在此实现你的逻辑。"""
class SkillHandler:
    def __init__(self, metadata: dict):
        self.metadata = metadata

    async def execute(self, message: str, context: list, user_id: str = None) -> str:
        # message: 用户原始消息; context: 最近若干轮短期记忆
        return "你好，这是我的自定义技能返回：" + message
`

function flash(m, isErr) {
  if (isErr) { errMsg.value = m; msg.value = '' }
  else { msg.value = m; errMsg.value = '' }
  setTimeout(() => { msg.value = ''; errMsg.value = '' }, 3000)
}

async function load() {
  loading.value = true
  try {
    const r = await api.listSkillsMgmt()
    skills.value = r.skills || []
  } catch (e) {
    flash('加载失败：' + e.message, true)
  } finally {
    loading.value = false
  }
}

function parseKw(text) {
  return (text || '').split(',').map(s => s.trim()).filter(Boolean)
}

async function saveApi() {
  busy.value = true
  try {
    const payload = {
      name: apiForm.value.name.trim(),
      description: apiForm.value.description.trim(),
      trigger_keywords: parseKw(apiKwText.value),
      api_url: apiForm.value.api_url.trim(),
      api_key: apiForm.value.api_key,
      method: apiForm.value.method,
      enabled: apiForm.value.enabled,
    }
    await api.upsertApiSkill(payload)
    flash('API 技能已保存')
    apiForm.value = { name: '', description: '', api_url: '', api_key: '', method: 'GET', enabled: true }
    apiKwText.value = ''
    await load()
  } catch (e) {
    flash('保存失败：' + e.message, true)
  } finally {
    busy.value = false
  }
}

async function toggleApi(s) {
  busy.value = true
  try {
    await api.toggleApiSkill(s.name, !s.enabled)
    await load()
  } catch (e) {
    flash('操作失败：' + e.message, true)
  } finally {
    busy.value = false
  }
}

async function del(s) {
  busy.value = true
  try {
    if (s.type === 'api') await api.delApiSkill(s.name)
    else await api.delSkillPackage(s.name)
    flash('已删除：' + s.name)
    await load()
  } catch (e) {
    flash('删除失败：' + e.message, true)
  } finally {
    busy.value = false
  }
}

function fillTemplate() {
  pkg.value.handler_code = TEMPLATE
}

async function savePkg() {
  busy.value = true
  try {
    const payload = {
      name: pkg.value.name.trim(),
      description: pkg.value.description.trim(),
      trigger_keywords: parseKw(pkgKwText.value),
      handler_code: pkg.value.handler_code,
    }
    await api.createSkillPackage(payload)
    flash('技能包已写入并热加载')
    pkg.value = { name: '', description: '', handler_code: '' }
    pkgKwText.value = ''
    await load()
  } catch (e) {
    flash('写入失败：' + e.message, true)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.skillcfg { display: flex; flex-direction: column; gap: 16px; }
.skillcfg h3 { margin: 0 0 12px; font-size: 15px; display: flex; align-items: center; gap: 8px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }
.card.builtin { background: linear-gradient(180deg, #f3f8ff 0%, var(--panel) 60%); border-color: #cfe2fb; }
.bi-item { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.bi-name { font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px; }
.bi-func { display: flex; flex-wrap: wrap; gap: 4px; }
.bi-usage { background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; margin-top: 2px; }
.sk-list { display: flex; flex-direction: column; gap: 10px; }
.sk-item { display: flex; gap: 12px; align-items: flex-start; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.sk-main { flex: 1; min-width: 0; }
.sk-name { font-weight: 600; }
.badge { font-size: 11px; padding: 3px 8px; border-radius: 999px; flex-shrink: 0; }
.badge.b-api { background: #e7f0ff; color: #2b6cb0; border: 1px solid #c3d8f5; }
.badge.b-pkg { background: #f0e9ff; color: #6b46c1; border: 1px solid #d9c8f5; }
.sk-actions { display: flex; flex-direction: column; gap: 6px; flex-shrink: 0; }
.kw { margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px; }
.tag { font-size: 11px; background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 1px 6px; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; word-break: break-all; }
.form { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.form label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.form label:nth-child(3), .form label:nth-child(4), .form label:nth-child(7), .form textarea { grid-column: 1 / -1; }
.form input, .form select, .form textarea {
  padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--ink); font-size: 13px;
}
.form textarea { font-family: ui-monospace, Menlo, Consolas, monospace; resize: vertical; }
.inline-check { flex-direction: row !important; align-items: center; gap: 6px !important; }
.warn { background: #fff8e6; border: 1px solid #f0d98a; color: #8a6d1a; border-radius: 8px; padding: 8px 10px; font-size: 12px; margin-bottom: 12px; }
.hint { background: #eef6ff; border: 1px solid #cfe2fb; color: #2b6cb0; border-radius: 8px; padding: 8px 10px; font-size: 12px; margin: 0 0 12px; }
.managed-note { color: #6b46c1; font-size: 11px; margin-top: 4px; }
.small { font-size: 12px; } .xsmall { font-size: 11px; } .muted { color: var(--muted); }
button.primary { background: var(--brand); color: #fff; border: none; border-radius: 8px; padding: 8px 14px; font-size: 13px; cursor: pointer; margin-top: 10px; }
button.primary:disabled { opacity: .5; cursor: not-allowed; }
button.ghost { background: transparent; border: 1px solid var(--line); border-radius: 8px; padding: 7px 12px; font-size: 13px; cursor: pointer; margin-top: 8px; }
button.ghost.sm { padding: 4px 10px; font-size: 12px; margin: 0; }
button.ghost.danger { color: #c0392b; border-color: #f0c6c6; }
.ok-text { color: #1a7f37; } .error { color: #c0392b; }
</style>
