<template>
  <div class="config">
    <h2>⚙️ 系统设置</h2>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-else-if="err" class="error">{{ err }}</p>

    <template v-else-if="cfg">
      <p v-if="msg" class="ok-text small">{{ msg }}</p>

      <!-- AI 能力 -->
      <section class="card">
        <h3>🤖 AI 能力</h3>
        <div class="row">
          <div>
            <div class="kv-title">AI 总开关</div>
            <div class="muted small">关闭后所有 AI 对话 / 分析停止工作</div>
          </div>
          <label class="switch">
            <input type="checkbox" v-model="aiEnabled" @change="saveAi" :disabled="saving" />
            <span class="slider"></span>
          </label>
        </div>

        <div class="row">
          <div>
            <div class="kv-title">当前生效模型</div>
            <div class="muted small">
              已配置 {{ cfg.ai_models_count }} 个 ·
              <b v-if="cfg.ai_active_name">{{ cfg.ai_active_name }}</b>
              <b v-else class="muted">未选择</b>
            </div>
          </div>
          <button class="primary" @click="goto('models')">🧩 管理模型</button>
        </div>

        <div class="row col">
          <div class="kv-title">长期记忆模型（embedding）</div>
          <div class="inline">
            <input v-model="embeddingModel" list="emb-models" placeholder="如 text-embedding-3-small" />
            <datalist id="emb-models">
              <option v-for="m in modelOptions" :key="m.id" :value="m.name">{{ m.id }}</option>
            </datalist>
            <button class="primary" :disabled="saving || embeddingModel === cfg.embedding_model"
                    @click="saveEmbedding">保存</button>
          </div>
          <div class="muted small">留空则不启用长期记忆；可填已配置模型名或自定义 OpenAI 兼容 embedding。</div>
        </div>
      </section>

      <!-- 飞书 -->
      <section class="card">
        <h3>🟢 飞书</h3>
        <div class="row">
          <div>
            <div class="kv-title">飞书启用</div>
            <div class="muted small">
              App ID：{{ cfg.feishu_app_id_masked || '未配置（去「飞书」页扫码授权）' }}
            </div>
          </div>
          <label class="switch">
            <input type="checkbox" v-model="feishuEnabled" @change="saveFeishu" :disabled="saving" />
            <span class="slider"></span>
          </label>
        </div>
      </section>

      <!-- 系统状态 -->
      <section class="card" v-if="status">
        <h3>📡 系统状态</h3>
        <div class="badges">
          <span class="badge" :class="badgeCls(status.dependencies?.redis)">Redis {{ status.dependencies?.redis }}</span>
          <span class="badge" :class="badgeCls(status.dependencies?.qdrant)">Qdrant {{ status.dependencies?.qdrant }}</span>
          <span class="badge" :class="badgeCls(status.dependencies?.embedding)">Embedding {{ status.dependencies?.embedding }}</span>
          <span class="badge" :class="badgeCls(status.connector?.webhook)">Webhook {{ status.connector?.webhook }}</span>
          <span class="badge" :class="badgeCls(status.feishu)">飞书 Bot {{ status.feishu }}</span>
        </div>
        <div class="muted small">AI 可用性：<b :class="status.ai_available ? 'ok-text' : ''">{{ status.ai_available ? '可用' : '不可用' }}</b></div>
      </section>

      <!-- 安全 -->
      <section class="card">
        <h3>🔐 安全</h3>
        <div class="row">
          <div class="kv-title">OTP 绑定开放</div>
          <b :class="cfg.otp_setup_open ? 'warn-text' : 'ok-text'">
            {{ cfg.otp_setup_open ? '是（首次绑定，建议尽快绑定后关闭）' : '否（已绑定）' }}
          </b>
        </div>
      </section>

      <!-- 技能管理 -->
      <SkillConfig />
    </template>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'
import SkillConfig from './SkillConfig.vue'

const router = useRouter()

const cfg = ref(null)
const status = ref(null)
const modelOptions = ref([])
const loading = ref(true)
const saving = ref(false)
const err = ref('')
const msg = ref('')

const aiEnabled = ref(false)
const embeddingModel = ref('')
const feishuEnabled = ref(false)

function badgeCls(s) {
  if (s === 'configured' || s === true || s === 'ok') return 'good'
  if (s === 'not_configured' || s === false) return 'bad'
  return 'neutral'
}

function goto(tab) { router.push('/' + tab) }

async function load() {
  loading.value = true
  try {
    const [c, st, ml] = await Promise.all([
      api.config(),
      api.systemStatus().catch(() => null),
      api.listModels().catch(() => ({ models: [] })),
    ])
    cfg.value = c
    status.value = st
    modelOptions.value = (ml.models || []).map(m => ({ id: m.id, name: m.name }))
    aiEnabled.value = !!c.ai_enabled
    embeddingModel.value = c.embedding_model || ''
    feishuEnabled.value = !!c.feishu_enabled
  } catch (e) {
    err.value = e.message
  } finally {
    loading.value = false
  }
}

async function saveAi() {
  await save({ ai_enabled: aiEnabled.value }, 'AI 总开关已更新')
}
async function saveFeishu() {
  await save({ feishu_enabled: feishuEnabled.value }, '飞书启用状态已更新')
}
async function saveEmbedding() {
  await save({ embedding_model: embeddingModel.value.trim() }, '长期记忆模型已保存')
}

async function save(patch, okText) {
  saving.value = true
  msg.value = ''
  try {
    const r = await api.updateConfig(patch)
    Object.assign(cfg.value, r)
    if ('embedding_model' in r) embeddingModel.value = r.embedding_model || ''
    msg.value = okText
    setTimeout(() => (msg.value = ''), 2500)
  } catch (e) {
    msg.value = '保存失败：' + e.message
    // 回滚本地开关
    if ('ai_enabled' in patch) aiEnabled.value = cfg.value.ai_enabled
    if ('feishu_enabled' in patch) feishuEnabled.value = cfg.value.feishu_enabled
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.config { max-width: 760px; }
.config h2 { margin-bottom: 12px; }
section.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
section.card h3 { margin: 0 0 12px; font-size: 15px; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 8px 0; }
.row.col { flex-direction: column; align-items: stretch; gap: 8px; }
.kv-title { font-weight: 600; }
.inline { display: flex; gap: 8px; }
.inline input { flex: 1; }
.small { font-size: 12px; }
.badges { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.badge { font-size: 12px; padding: 4px 10px; border-radius: 999px; border: 1px solid var(--line); }
.badge.good { background: #e7f7ec; color: #1a7f37; border-color: #b7e4c7; }
.badge.bad { background: #fdeaea; color: #c0392b; border-color: #f5c6c6; }
.badge.neutral { background: var(--bg); color: var(--muted); }

/* 开关 */
.switch { position: relative; display: inline-block; width: 46px; height: 26px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; inset: 0; background: var(--line); border-radius: 999px; transition: .2s; }
.slider::before { content: ""; position: absolute; height: 20px; width: 20px; left: 3px; top: 3px; background: #fff; border-radius: 50%; transition: .2s; }
.switch input:checked + .slider { background: var(--brand); }
.switch input:checked + .slider::before { transform: translateX(20px); }
.switch input:disabled + .slider { opacity: .5; }

button.primary { background: var(--brand); color: #fff; border: none; border-radius: 8px; padding: 8px 14px; font-size: 13px; cursor: pointer; }
button.primary:disabled { opacity: .5; cursor: not-allowed; }
.ok-text { color: #1a7f37; }
.warn-text { color: #b7791f; }
</style>
