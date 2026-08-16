<template>
  <div class="config card">
    <h2>⚙️ 系统设置</h2>
    <p v-if="loading" class="muted">加载中…</p>
    <template v-else-if="cfg">
      <div class="grid">
        <div class="kv"><span class="muted">AI 总开关</span><b>{{ cfg.ai_enabled ? '开' : '关' }}</b></div>
        <div class="kv"><span class="muted">AI 生效模型</span><b>{{ cfg.ai_model || '—' }}</b></div>
        <div class="kv"><span class="muted">AI 基础地址</span><b class="break">{{ cfg.ai_base_url }}</b></div>
        <div class="kv"><span class="muted">API Key</span><b>{{ cfg.ai_api_key_masked || '未配置' }}</b></div>
        <div class="kv"><span class="muted">模型库数量</span><b>{{ cfg.ai_models_count }}</b></div>
        <div class="kv"><span class="muted">场景模型</span><b class="break">{{ fmt(cfg.scenario_models) }}</b></div>
        <div class="kv"><span class="muted">飞书</span>
          <b :class="cfg.feishu_enabled ? 'ok-text' : ''">{{ cfg.feishu_enabled ? '已启用' : '未启用' }}</b>
        </div>
        <div class="kv"><span class="muted">飞书 App ID</span><b>{{ cfg.feishu_app_id_masked || '未配置' }}</b></div>
        <div class="kv"><span class="muted">OTP 绑定开放</span><b>{{ cfg.otp_setup_open ? '是（首次绑定）' : '否' }}</b></div>
      </div>
      <p class="muted small">AI / 飞书 的实际配置在服务端环境变量或扫码授权结果中，本页仅展示脱敏概览。</p>
    </template>
    <p v-else class="error">{{ err }}</p>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const cfg = ref(null)
const loading = ref(true)
const err = ref('')

function fmt(o) { return o && Object.keys(o).length ? JSON.stringify(o) : '—' }

onMounted(async () => {
  try { cfg.value = await api.config() } catch (e) { err.value = e.message } finally { loading.value = false }
})
</script>

<style scoped>
.config { max-width: 720px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 24px; margin-top: 8px; }
.kv { display: flex; flex-direction: column; gap: 2px; padding: 8px 0; border-bottom: 1px solid var(--line); }
.kv b { font-weight: 600; }
.break { word-break: break-all; font-size: 13px; }
.small { font-size: 12px; margin-top: 12px; }
</style>
