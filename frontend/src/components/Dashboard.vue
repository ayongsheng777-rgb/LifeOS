<template>
  <div class="dash">
    <div class="head row spread">
      <div>
        <h2>📊 概览</h2>
        <p class="muted">系统运行态势、AI 用量、记忆与技能热度一览。</p>
      </div>
      <div class="row">
        <span class="muted" v-if="lastUpdated">{{ lastUpdated }}</span>
        <button class="ghost" @click="loadAll" :disabled="loading">🔄 刷新</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <!-- 核心指标 -->
    <div class="stats">
      <div class="card stat">
        <div class="stat-num">{{ usage.calls }}</div>
        <div class="muted">AI 调用次数</div>
      </div>
      <div class="card stat">
        <div class="stat-num" :class="successRate >= 99 ? 'ok-text' : (successRate >= 90 ? '' : 'error')">
          {{ successRate }}%
        </div>
        <div class="muted">成功率（{{ usage.ok }}/{{ usage.calls }}）</div>
      </div>
      <div class="card stat">
        <div class="stat-num">{{ fmtNum(usage.total_tokens) }}</div>
        <div class="muted">累计 Token</div>
      </div>
      <div class="card stat">
        <div class="stat-num">${{ usage.cost.toFixed(4) }}</div>
        <div class="muted">累计花费（估算）</div>
      </div>
    </div>

    <div class="grid">
      <!-- AI 用量趋势 -->
      <div class="card">
        <div class="card-title">AI 用量趋势（近 {{ daily.length }} 天）</div>
        <div v-if="daily.length === 0" class="empty">暂无用量数据</div>
        <div v-else class="trend">
          <div class="trend-row" v-for="d in daily" :key="d.date">
            <div class="trend-date">{{ d.date.slice(5) }}</div>
            <div class="trend-bar-wrap">
              <div class="trend-bar" :style="{ width: barWidth(d.calls) }"></div>
            </div>
            <div class="trend-meta">{{ d.calls }} 次 · {{ fmtNum(d.tokens) }} tok</div>
          </div>
        </div>
      </div>

      <!-- 系统健康 -->
      <div class="card">
        <div class="card-title">系统健康</div>
        <div class="kv"><span class="muted">状态</span>
          <span class="tag" :class="health.status === 'ok' ? 'ok' : 'warn'">{{ health.status }}</span>
        </div>
        <div class="kv"><span class="muted">AI 可用</span>
          <span class="tag" :class="health.ai_available ? 'ok' : 'warn'">{{ health.ai_available ? '是' : '否' }}</span>
        </div>
        <div class="kv"><span class="muted">飞书 Bot</span>
          <span class="tag" :class="feishuOnline ? 'ok' : 'gray'">{{ feishuOnline ? '在线' : '离线/未配置' }}</span>
        </div>
        <div class="kv"><span class="muted">依赖</span>
          <span class="row wrap">
            <span class="tag" :class="depClass(health.deps.redis)">{{ 'Redis ' + health.deps.redis }}</span>
            <span class="tag" :class="depClass(health.deps.qdrant)">{{ 'Qdrant ' + health.deps.qdrant }}</span>
            <span class="tag" :class="depClass(health.deps.embedding)">{{ 'Embed ' + health.deps.embedding }}</span>
          </span>
        </div>
      </div>

      <!-- 技能命中 -->
      <div class="card">
        <div class="card-title">技能命中热度</div>
        <div v-if="skills.length === 0" class="empty">暂无技能</div>
        <div v-else>
          <div class="skill-row" v-for="s in skillsSorted" :key="s.name">
            <div class="grow">
              <div class="skill-name">{{ s.name }}</div>
              <div class="muted skill-desc">{{ s.desc || '' }}</div>
            </div>
            <span class="tag" :class="s.hits > 0 ? '' : 'gray'">{{ s.hits }} 次</span>
          </div>
        </div>
      </div>

      <!-- 记忆状态 -->
      <div class="card">
        <div class="card-title">记忆状态</div>
        <div class="kv"><span class="muted">最近意图</span><span>{{ working.last_intent || '—' }}</span></div>
        <div class="kv"><span class="muted">最近技能</span><span>{{ working.last_skill || '—' }}</span></div>
        <div class="kv"><span class="muted">短期记忆</span><span>{{ shortList.length }} 条（7 天）</span></div>
        <div class="kv"><span class="muted">长期记忆</span>
          <span class="tag" :class="longConfigured ? 'ok' : 'gray'">
            {{ longConfigured ? (longList.length + ' 条') : '未配置' }}
          </span>
        </div>
      </div>

      <!-- 连接器 / Connector -->
      <div class="card">
        <div class="card-title">连接器（入站 Webhook / 出站推送）</div>
        <div class="kv"><span class="muted">Webhook 入站</span>
          <span class="tag" :class="conn.webhook_enabled ? 'ok' : 'warn'">
            {{ conn.webhook_enabled ? '已启用' : '未配置令牌' }}
          </span>
        </div>
        <div class="kv"><span class="muted">入站累计</span><span>{{ conn.inbound_count }} 次</span></div>
        <div class="kv"><span class="muted">最近入站</span>
          <span>{{ conn.last_inbound ? (conn.last_inbound.type + ' @' + new Date(conn.last_inbound.at * 1000).toLocaleTimeString('zh-CN')) : '—' }}</span>
        </div>
        <div class="kv"><span class="muted">飞书推送</span>
          <span class="tag" :class="conn.feishu_push ? 'ok' : 'gray'">{{ conn.feishu_push ? '可用' : '未配置' }}</span>
        </div>
        <p class="muted conn-hint">外部系统向 <code>POST /api/connector/webhook</code> 推送 JSON（带 <code>X-LifeOS-Webhook-Token</code> 头），按 <code>type</code> 路由 todo / memory / chat / raw。</p>
        <button class="ghost" :disabled="pushing || !conn.feishu_push" @click="testPush">📤 发送测试推送（飞书）</button>
        <p v-if="pushMsg" class="muted">{{ pushMsg }}</p>
      </div>
    </div>

    <!-- 最近记忆 -->
    <div class="card">
      <div class="card-title">最近对话 / 记忆</div>
      <div v-if="shortList.length === 0 && longList.length === 0" class="empty">暂无记忆</div>
      <div class="mem-list">
        <div class="mem" v-for="(m, i) in shortList.slice().reverse()" :key="'s' + i">
          <span class="tag" :class="m.role === 'user' ? '' : 'gray'">{{ m.role === 'user' ? '我' : 'AI' }}</span>
          <span class="mem-text">{{ m.content }}</span>
        </div>
        <div class="mem long" v-for="item in longList.slice(0, 8)" :key="'l' + item.id">
          <span class="tag ok">长期</span>
          <span class="mem-text">{{ longText(item) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { api } from '../api.js'

const loading = ref(false)
const error = ref('')
const lastUpdated = ref('')

const usage = ref({ calls: 0, ok: 0, fail: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, cost: 0, per_model: {} })
const daily = ref([])
const skills = ref([])
const working = ref({})
const shortList = ref([])
const longConfigured = ref(false)
const longList = ref([])
const health = ref({ status: '—', ai_available: false, deps: {}, feishu: {} })
const conn = ref({ webhook_enabled: false, inbound_count: 0, last_inbound: null, feishu_push: false })
const pushing = ref(false)
const pushMsg = ref('')

const successRate = computed(() => {
  const c = usage.value.calls || 0
  if (!c) return 0
  return Math.round((usage.value.ok / c) * 100)
})
const skillsSorted = computed(() =>
  [...skills.value].sort((a, b) => (b.hits || 0) - (a.hits || 0))
)
const feishuOnline = computed(() => !!health.value.feishu?.bot_online)
const maxCalls = computed(() => Math.max(1, ...daily.value.map((d) => d.calls || 0)))

function fmtNum(n) { return (n || 0).toLocaleString('en-US') }
function depClass(v) { return v === 'configured' ? 'ok' : 'gray' }
function barWidth(calls) { return Math.max(6, Math.round((calls / maxCalls.value) * 100)) + '%' }
function longText(item) {
  const p = item?.payload || {}
  return p.text || JSON.stringify(p).slice(0, 120)
}

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const [u, d, sk, ms, ml, h, c] = await Promise.all([
      api.aiUsage().catch(() => null),
      api.aiUsageDaily(14).catch(() => null),
      api.skillsStats().catch(() => null),
      api.memoryShort().catch(() => null),
      api.memoryLong(50).catch(() => null),
      api.health().catch(() => null),
      api.connectorStatus().catch(() => null),
    ])
    if (u) usage.value = { ...usage.value, ...(u || {}) }
    if (d?.daily) daily.value = d.daily
    if (sk) {
      skills.value = sk.skills || []
      working.value = { last_intent: sk.last_intent, last_skill: sk.last_skill }
    }
    if (ms) {
      working.value = { ...working.value, ...(ms.working || {}) }
      shortList.value = ms.short || []
    }
    if (ml) {
      longConfigured.value = !!ml.configured
      longList.value = ml.items || []
    }
    if (h) health.value = { status: h.status, ai_available: h.ai_available, deps: h.dependencies || {}, feishu: h.feishu || {} }
    if (c) conn.value = { webhook_enabled: !!c.webhook_enabled, inbound_count: c.inbound_count || 0, last_inbound: c.last_inbound || null, feishu_push: !!c.feishu_push }
    lastUpdated.value = '更新于 ' + new Date().toLocaleTimeString('zh-CN')
  } catch (e) {
    error.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function testPush() {
  if (!conn.value.feishu_push) return
  pushing.value = true
  pushMsg.value = ''
  try {
    const r = await api.connectorPush('feishu', 'admin', 'LifeOS 连接器测试推送 ✅')
    pushMsg.value = r.ok
      ? ('已推送，成功 ' + (r.sent || 0) + ' 条')
      : ('推送失败：' + (r.error || '未知原因'))
  } catch (e) {
    pushMsg.value = '推送异常：' + (e.message || e)
  } finally {
    pushing.value = false
    const c = await api.connectorStatus().catch(() => null)
    if (c) conn.value = { webhook_enabled: !!c.webhook_enabled, inbound_count: c.inbound_count || 0, last_inbound: c.last_inbound || null, feishu_push: !!c.feishu_push }
  }
}

loadAll()
</script>

<style scoped>
.dash { display: flex; flex-direction: column; gap: 16px; }
.head h2 { margin: 0; }
.head p { margin: 4px 0 0; }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat { text-align: center; }
.stat-num { font-size: 26px; font-weight: 700; margin-bottom: 4px; }
.grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.card-title { font-weight: 600; margin-bottom: 12px; }
.kv { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--line); }
.kv:last-child { border-bottom: none; }

/* 趋势条 */
.trend { display: flex; flex-direction: column; gap: 8px; }
.trend-row { display: flex; align-items: center; gap: 10px; }
.trend-date { width: 42px; font-size: 12px; color: var(--muted); }
.trend-bar-wrap { flex: 1; background: var(--bg); border-radius: 6px; overflow: hidden; height: 14px; }
.trend-bar { height: 100%; background: var(--brand); border-radius: 6px; transition: width 0.3s ease; }
.trend-meta { width: 130px; text-align: right; font-size: 12px; color: var(--muted); }

/* 技能 */
.skill-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--line); }
.skill-row:last-child { border-bottom: none; }
.skill-name { font-weight: 500; }
.skill-desc { font-size: 12px; }

/* 记忆 */
.mem-list { display: flex; flex-direction: column; gap: 8px; max-height: 320px; overflow-y: auto; }
.mem { display: flex; gap: 8px; align-items: flex-start; }
.mem-text { font-size: 13px; white-space: pre-wrap; word-break: break-word; }

@media (max-width: 760px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .grid { grid-template-columns: 1fr; }
}
</style>
