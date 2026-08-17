<template>
  <div class="backup">
    <h2>💾 数据备份与还原</h2>
    <p class="muted small">自动备份 PostgreSQL / Redis / Qdrant / 配置目录到本地磁盘与多台 NAS，可手动触发、在线改配置、随时还原。</p>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-if="err" class="error">{{ err }}</p>

    <template v-if="!loading && !err">
      <!-- 配置卡片 -->
      <section class="card">
        <div class="head">
          <h3>⚙️ 备份配置</h3>
          <button v-if="!editing" class="primary" @click="startEdit">编辑配置</button>
        </div>

        <div v-if="!editing" class="kv-grid">
          <div><div class="k">备份目标</div><div class="v">
            <div v-for="t in cfg.backup_targets" :key="t.path" class="target-mini">
              <span class="tag" :class="t.enabled ? 'ok' : 'gray'">{{ t.enabled ? '启用' : '停用' }}</span>
              <span class="tag" :class="methodCls(t.method)">{{ methodLabel(t.method) }}</span>
              <span class="path">{{ t.path }}</span>
            </div>
            <span v-if="!cfg.backup_targets || !cfg.backup_targets.length" class="muted">未配置</span>
          </div></div>
          <div><div class="k">保留天数</div><div class="v">{{ cfg.backup_retention_days }} 天</div></div>
          <div><div class="k">定时备份</div><div class="v">每日 {{ String(cfg.backup_schedule_hour).padStart(2, '0') }}:00</div></div>
          <div><div class="k">定时调度</div><div class="v">
            <span class="tag" :class="status.scheduler_running ? 'ok' : 'gray'">
              {{ status.scheduler_running ? '运行中' : '未运行' }}
            </span>
          </div></div>
        </div>

        <div v-else class="form">
          <div class="targets-edit">
            <div class="row spread targets-head">
              <span class="small muted">备份目标（本地磁盘 / NAS：SMB · SFTP · FTP · WebDAV）</span>
              <span class="add-btns">
                <button class="ghost small" @click="addTarget('local')">＋ 本地</button>
                <button class="ghost small" @click="addTarget('smb')">＋ SMB</button>
                <button class="ghost small" @click="addTarget('sftp')">＋ SFTP</button>
                <button class="ghost small" @click="addTarget('ftp')">＋ FTP</button>
                <button class="ghost small" @click="addTarget('webdav')">＋ WebDAV</button>
              </span>
            </div>
            <div v-for="(t, i) in formTargets" :key="i" class="target-card">
              <div class="row spread tcard-head">
                <select v-model="t.method" class="ttype" @change="onMethodChange(t)">
                  <option value="local">本地磁盘</option>
                  <option value="smb">SMB/CIFS</option>
                  <option value="sftp">SFTP</option>
                  <option value="ftp">FTP</option>
                  <option value="webdav">WebDAV</option>
                </select>
                <label class="enb"><input type="checkbox" v-model="t.enabled" /> 启用</label>
                <button class="del" @click="removeTarget(i)" title="删除该目标">✕</button>
              </div>
              <div class="tcard-body">
                <template v-if="t.method === 'local'">
                  <label>本地路径
                    <input v-model="t.path" placeholder="如 F:\\LifeOS_BAK（绝对路径）" />
                  </label>
                </template>
                <template v-else-if="t.method === 'smb'">
                  <div class="grid3">
                    <label>主机(IP)<input v-model="t.host" placeholder="192.168.1.100" /></label>
                    <label>共享名<input v-model="t.share" placeholder="LifeOS" /></label>
                    <label>远程目录<input v-model="t.directory" placeholder="backup" /></label>
                  </div>
                  <div class="grid2">
                    <label>用户名<input v-model="t.username" placeholder="可选（留空匿名）" /></label>
                    <label>密码<input type="password" v-model="t.password" placeholder="留空=无/不修改" autocomplete="new-password" /></label>
                  </div>
                </template>
                <template v-else-if="t.method === 'sftp' || t.method === 'ftp'">
                  <div class="grid3">
                    <label>主机(IP)<input v-model="t.host" placeholder="192.168.1.100" /></label>
                    <label>端口<input type="number" v-model.number="t.port" :placeholder="t.method === 'sftp' ? '22' : '21'" /></label>
                    <label>远程目录<input v-model="t.directory" placeholder="/lifeos" /></label>
                  </div>
                  <div class="grid2">
                    <label>用户名<input v-model="t.username" placeholder="必填" /></label>
                    <label>密码<input type="password" v-model="t.password" placeholder="必填" autocomplete="new-password" /></label>
                  </div>
                </template>
                <template v-else-if="t.method === 'webdav'">
                  <label class="row enb" style="margin-bottom:6px;">
                    <input type="checkbox" v-model="t.https" /> 使用 HTTPS（否则 HTTP）
                  </label>
                  <div class="grid3">
                    <label>主机(IP)<input v-model="t.host" placeholder="nas.example.com" /></label>
                    <label>端口<input type="number" v-model.number="t.port" :placeholder="t.https ? '443' : '80'" /></label>
                    <label>远程目录<input v-model="t.directory" placeholder="/lifeos" /></label>
                  </div>
                  <div class="grid2">
                    <label>用户名<input v-model="t.username" placeholder="可选" /></label>
                    <label>密码<input type="password" v-model="t.password" placeholder="可选" autocomplete="new-password" /></label>
                  </div>
                </template>
              </div>
            </div>
            <p v-if="!formTargets.length" class="warn-text small">至少需保留一个目标。</p>
          </div>
          <div class="two">
            <label>保留天数（1-365）
              <input type="number" min="1" max="365" v-model.number="form.retention" />
            </label>
            <label>定时小时（0-23）
              <input type="number" min="0" max="23" v-model.number="form.schedule" />
            </label>
          </div>
          <div class="row spread">
            <span v-if="saveMsg" class="small" :class="saveErr ? 'error' : 'ok-text'">{{ saveMsg }}</span>
            <span v-else></span>
            <div class="row">
              <button class="ghost" :disabled="saving" @click="editing = false">取消</button>
              <button class="primary" :disabled="saving" @click="saveConfig">保存</button>
            </div>
          </div>
        </div>
      </section>

      <!-- 手动备份 + 实时日志 -->
      <section class="card">
        <div class="head">
          <h3>🚀 手动备份</h3>
          <button class="ok" :disabled="busy || !hasEnabledTargets" @click="runBackup">
            {{ busy && !restoring ? '备份中…' : '立即备份' }}
          </button>
        </div>
        <p v-if="!hasEnabledTargets" class="warn-text small">未配置任何启用的备份目标，无法备份（请先编辑配置）。</p>
        <p v-if="runMsg" class="small" :class="runErr ? 'error' : 'ok-text'">{{ runMsg }}</p>
        <div class="logbox" ref="logbox">
          <div v-for="l in logs" :key="l.id" class="logline" :class="lvlCls(l.level)">
            <span class="ts">{{ l.ts }}</span>
            <span class="lv">{{ l.level }}</span>
            <span class="msg">{{ l.msg }}</span>
          </div>
          <div v-if="!logs.length" class="muted small">点击「立即备份」或下方「确认还原」后，这里会实时显示进度。</div>
        </div>
      </section>

      <!-- 上次备份状态 -->
      <section class="card">
        <h3>📦 上次备份状态</h3>
        <div v-if="!status.targets || !status.targets.length" class="muted small">暂无备份记录。</div>
        <div v-for="t in status.targets" :key="t.target" class="target">
          <div class="row spread">
            <b>{{ t.target }} <span class="tag" :class="methodCls(t.method)">{{ methodLabel(t.method) }}</span></b>
            <span class="small muted">{{ t.last_backup || '尚无备份' }}</span>
          </div>
          <div v-if="t.results" class="comps">
            <span v-for="(v, k) in t.results" :key="k" class="tag" :class="compCls(v.status)" :title="compTitle(k, v)">
              {{ compLabel(k) }}: {{ v.status }}
            </span>
          </div>
        </div>
      </section>

      <!-- 还原面板 -->
      <section class="card restore">
        <h3>♻️ 还原备份</h3>
        <p class="muted small">从某个备份目标的历史时间点，把指定组件还原回线上。注意：还原会覆盖当前线上数据，请先确认。</p>

        <div class="row restore-ctrl">
          <label class="sel">目标
            <select v-model="restoreTarget" @change="onTargetChange">
              <option v-for="t in enabledTargets" :key="t.path" :value="t.path">{{ t.path }}</option>
            </select>
          </label>
          <button class="ghost" :disabled="!restoreTarget || pointsLoading" @click="loadPoints">
            {{ pointsLoading ? '加载中…' : '加载时间点' }}
          </button>
        </div>
        <p v-if="pointsErr" class="error small">{{ pointsErr }}</p>

        <div v-if="points.length" class="points">
          <div v-for="p in points" :key="p.timestamp" class="point" :class="{ active: selTimestamp === p.timestamp }">
            <label class="point-main">
              <input type="radio" name="point" :value="p.timestamp" v-model="selTimestamp" />
              <span class="ts">{{ p.timestamp }}</span>
            </label>
            <div class="comps">
              <span v-for="(v, k) in p.components" :key="k" class="tag" :class="compCls(v.status)" :title="compTitle(k, v)">
                {{ compLabel(k) }}: {{ v.status }}
              </span>
              <span v-if="!p.components || !Object.keys(p.components).length" class="muted small">无组件信息</span>
            </div>
          </div>
        </div>
        <p v-else-if="pointsLoaded" class="muted small">该目标下暂无备份时间点。</p>

        <div v-if="selTimestamp" class="restore-pick">
          <div class="small muted" style="margin-bottom:6px;">选择要还原的组件（仅该时间点已备份的可用）：</div>
          <div class="comps">
            <label v-for="c in componentKeys" :key="c" class="comp-chk" :class="{ disabled: !compAvail(c) }">
              <input type="checkbox" v-model="selComps[c]" :disabled="!compAvail(c)" />
              {{ compLabel(c) }}
              <span class="muted small" v-if="!compAvail(c)">（该点无备份）</span>
            </label>
          </div>
          <button class="danger" :disabled="busy || !hasSelComp" @click="askRestore">还原选中</button>
          <p v-if="!hasSelComp" class="warn-text small" style="margin-top:6px;">请至少勾选一个可用组件。</p>
        </div>
      </section>
    </template>

    <!-- 二次确认弹窗（危险操作） -->
    <div v-if="confirmOpen" class="overlay" @click.self="confirmOpen = false">
      <div class="modal">
        <h3 class="danger-text">⚠️ 确认还原（不可撤销）</h3>
        <p>即将把以下组件从备份点 <b>{{ selTimestamp }}</b>（目标 <b>{{ restoreTarget }}</b>）还原回线上：</p>
        <ul>
          <li v-for="c in selectedComps" :key="c">▸ {{ compLabel(c) }}</li>
        </ul>
        <p class="warn-text small">此操作会覆盖当前线上数据。请确认已无其他正在进行的重要写入。配置目录还原会保留当前面板/调度设置。</p>
        <div class="row spread">
          <button class="ghost" :disabled="busy" @click="confirmOpen = false">取消</button>
          <button class="danger" :disabled="busy" @click="confirmRestore">
            {{ busy && restoring ? '还原中…' : '确认还原' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api.js'

const cfg = ref({ backup_targets: [], backup_retention_days: 7, backup_schedule_hour: 3 })
const status = ref({ targets: [], backup_targets: [], scheduler_running: false })
const loading = ref(true)
const err = ref('')

const editing = ref(false)
const saving = ref(false)
const saveMsg = ref('')
const saveErr = ref(false)
const formTargets = ref([])
const form = ref({ retention: 7, schedule: 3 })

const busy = ref(false)
const restoring = ref(false)
const runMsg = ref('')
const runErr = ref(false)
const logs = ref([])
const lastId = ref(0)
const logbox = ref(null)
let pollTimer = null
let pollBusy = false

const restoreTarget = ref('')
const points = ref([])
const pointsLoaded = ref(false)
const pointsLoading = ref(false)
const pointsErr = ref('')
const selTimestamp = ref('')
const selComps = ref({ postgres: false, redis: false, qdrant: false, data: false })
const confirmOpen = ref(false)

const componentKeys = ['postgres', 'redis', 'qdrant', 'data']

const hasEnabledTargets = computed(() => (cfg.value.backup_targets || []).some(t => t.enabled && t.path))
const enabledTargets = computed(() => (cfg.value.backup_targets || []).filter(t => t.enabled && t.path))
const selectedComps = computed(() => componentKeys.filter(c => selComps.value[c]))
const hasSelComp = computed(() => selectedComps.value.length > 0)

async function load() {
  loading.value = true
  try {
    const [c, s] = await Promise.all([api.backupConfig(), api.backupStatus()])
    cfg.value = c
    status.value = s
    if (!restoreTarget.value && enabledTargets.value.length) restoreTarget.value = enabledTargets.value[0].path
  } catch (e) {
    err.value = e.message
  } finally {
    loading.value = false
  }
}

async function loadStatus() {
  try { status.value = await api.backupStatus() } catch (_) {}
}

function startEdit() {
  formTargets.value = (cfg.value.backup_targets || []).map(t => ({
    method: t.method || (String(t.path).startsWith('\\\\') ? 'smb' : 'local'),
    host: t.host || '',
    port: t.port || 0,
    share: t.share || '',
    directory: t.directory || '',
    username: t.username || '',
    password: t.password || '',
    https: !!t.https,
    enabled: !!t.enabled,
    path: t.path || '',
  }))
  form.value = { retention: cfg.value.backup_retention_days, schedule: cfg.value.backup_schedule_hour }
  saveMsg.value = ''
  saveErr.value = false
  editing.value = true
}

function addTarget(method) {
  const t = { method, host: '', port: 0, share: '', directory: '', username: '', password: '', https: true, enabled: true, path: '' }
  if (method === 'sftp') t.port = 22
  else if (method === 'ftp') t.port = 21
  else if (method === 'webdav') t.port = 443
  formTargets.value.push(t)
}
function onMethodChange(t) {
  if (t.port) return
  if (t.method === 'sftp') t.port = 22
  else if (t.method === 'ftp') t.port = 21
  else if (t.method === 'webdav') t.port = t.https ? 443 : 80
}
function removeTarget(i) {
  formTargets.value.splice(i, 1)
}

async function saveConfig() {
  saving.value = true
  saveMsg.value = ''
  saveErr.value = false
  try {
    const r = await api.updateBackupConfig({
      backup_targets: formTargets.value,
      backup_retention_days: form.value.retention,
      backup_schedule_hour: form.value.schedule,
    })
    cfg.value = { ...cfg.value, ...r }
    saveMsg.value = '配置已保存 ✓'
    saveErr.value = false
    editing.value = false
    // 同步还原面板可选目标
    if (enabledTargets.value.length && !enabledTargets.value.some(t => t.path === restoreTarget.value))
      restoreTarget.value = enabledTargets.value[0].path
    await loadStatus()
  } catch (e) {
    saveMsg.value = '保存失败：' + e.message
    saveErr.value = true
  } finally {
    saving.value = false
  }
}

function scrollLog() {
  if (logbox.value) logbox.value.scrollTop = logbox.value.scrollHeight
}

async function pollOnce() {
  if (pollBusy) return
  pollBusy = true
  try {
    const r = await api.backupLog(lastId.value)
    if (r.logs && r.logs.length) {
      logs.value.push(...r.logs)
      lastId.value = r.last_id
      scrollLog()
    }
  } catch (_) {}
  finally { pollBusy = false }
}

function startPoll() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = setInterval(pollOnce, 800)
}
function stopPoll() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
}

async function runBackup() {
  if (busy.value) return
  if (!hasEnabledTargets.value) {
    runMsg.value = '请先配置启用的备份目标'
    runErr.value = true
    return
  }
  busy.value = true
  restoring.value = false
  runErr.value = false
  runMsg.value = '备份进行中，详见下方日志…'
  logs.value = []
  lastId.value = 0
  startPoll()
  try {
    await api.backupRun()
    runMsg.value = '✅ 备份已完成'
    runErr.value = false
  } catch (e) {
    runMsg.value = '备份失败：' + e.message
    runErr.value = true
  } finally {
    await pollOnce()
    stopPoll()
    await loadStatus()
    busy.value = false
  }
}

// ===== 还原 =====
async function loadPoints() {
  if (!restoreTarget.value) return
  pointsLoading.value = true
  pointsErr.value = ''
  selTimestamp.value = ''
  points.value = []
  pointsLoaded.value = false
  try {
    const r = await api.backupPoints(restoreTarget.value)
    points.value = r.points || []
    pointsLoaded.value = true
  } catch (e) {
    pointsErr.value = e.message
  } finally {
    pointsLoading.value = false
  }
}

function onTargetChange() {
  points.value = []
  pointsLoaded.value = false
  selTimestamp.value = ''
  componentKeys.forEach(c => (selComps.value[c] = false))
}

function compAvail(c) {
  const p = points.value.find(x => x.timestamp === selTimestamp.value)
  if (!p || !p.components) return false
  return p.components[c] && p.components[c].status === 'ok'
}

function askRestore() {
  if (!selTimestamp.value || !hasSelComp.value) return
  confirmOpen.value = true
}

async function confirmRestore() {
  if (busy.value || !selTimestamp.value || !hasSelComp.value) return
  confirmOpen.value = false
  busy.value = true
  restoring.value = true
  runErr.value = false
  runMsg.value = '还原进行中，详见下方日志…'
  logs.value = []
  lastId.value = 0
  startPoll()
  try {
    const summary = await api.backupRestore({
      target: restoreTarget.value,
      timestamp: selTimestamp.value,
      components: selectedComps.value,
    })
    const fails = (summary.results || {})
    const failed = Object.entries(fails).filter(([, v]) => v.status !== 'ok').map(([k]) => compLabel(k))
    if (failed.length) {
      runMsg.value = '⚠️ 还原完成，但部分组件失败：' + failed.join('、')
      runErr.value = true
    } else {
      runMsg.value = '✅ 还原完成'
      runErr.value = false
    }
  } catch (e) {
    runMsg.value = '还原失败：' + e.message
    runErr.value = true
  } finally {
    await pollOnce()
    stopPoll()
    await loadPoints()
    await loadStatus()
    busy.value = false
    restoring.value = false
  }
}

function lvlCls(l) {
  if (l === 'ERROR' || l === 'CRITICAL') return 'err'
  if (l === 'WARNING') return 'warn'
  return ''
}
function compCls(s) {
  if (s === 'ok') return 'ok'
  if (s === 'fail') return 'bad'
  return 'gray'
}
function compLabel(k) {
  return ({ postgres: 'PostgreSQL', redis: 'Redis', qdrant: 'Qdrant', data: '配置目录', pruned: '清理' })[k] || k
}
function methodLabel(m) {
  return ({ local: '本地', smb: 'SMB', sftp: 'SFTP', ftp: 'FTP', webdav: 'WebDAV' })[m] || m
}
function methodCls(m) {
  return ({ local: 'local', smb: 'nas', sftp: 'sftp', ftp: 'ftp', webdav: 'webdav' })[m] || 'local'
}
function compTitle(k, v) {
  if (v.status === 'ok' && v.size != null) return `${compLabel(k)}：${(v.size / 1e6).toFixed(2)} MB`
  if (v.status === 'fail') return v.error || '失败'
  if (v.status === 'skip') return '跳过'
  return ''
}

onMounted(load)
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.backup { max-width: 820px; }
.backup h2 { margin-bottom: 8px; }
section.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
section.card h3 { margin: 0 0 12px; font-size: 15px; }
.head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.head h3 { margin: 0; }
.kv-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 24px; }
.kv-grid .k { font-size: 12px; color: var(--muted); margin-bottom: 2px; }
.kv-grid .v { font-weight: 600; word-break: break-all; }
.target-mini { display: flex; align-items: center; gap: 8px; padding: 2px 0; }
.target-mini .path { word-break: break-all; }
.tag.nas { background: #e8f0fe; color: #1a73e8; }
.tag.local { background: #e6f4ea; color: #1e8e3e; }
.tag.sftp { background: #f3e8fd; color: #8430ce; }
.tag.ftp { background: #fde7e9; color: #c5221f; }
.tag.webdav { background: #e6f9fb; color: #00897b; }
.ttype { padding: 6px 8px; border-radius: 6px; border: 1px solid var(--line); background: var(--panel); color: var(--text); }
.add-btns { display: flex; gap: 6px; }
.form { display: flex; flex-direction: column; gap: 12px; }
.form label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.form .two { display: flex; gap: 12px; }
.form .two label { flex: 1; }
.row { display: flex; align-items: center; gap: 10px; }
.spread { justify-content: space-between; }

.targets-edit { border: 1px solid var(--line); border-radius: 8px; padding: 10px; }
.targets-head { margin-bottom: 8px; }
.target-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.target-row input[type="text"], .target-row input:not([type="checkbox"]) { flex: 1; }
.enb { display: flex; align-items: center; gap: 4px; font-size: 12px; white-space: nowrap; }
.del { background: transparent; border: 1px solid var(--line); color: var(--danger); border-radius: 6px; cursor: pointer; padding: 2px 8px; }
.del:hover { background: var(--danger); color: #fff; }

.target-card { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin-bottom: 10px; background: var(--panel2, var(--panel)); }
.tcard-head { gap: 10px; margin-bottom: 8px; }
.tcard-body { display: flex; flex-direction: column; gap: 8px; }
.tcard-body label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.tcard-body label input { width: 100%; }
.grid2 { display: flex; gap: 8px; }
.grid2 label { flex: 1; }
.grid3 { display: flex; gap: 8px; }
.grid3 label { flex: 1; }

.logbox { background: #0f1115; color: #d6dde6; border-radius: 8px; padding: 12px; height: 240px; overflow-y: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.logline { padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
.logline .ts { color: #6b7785; margin-right: 8px; }
.logline .lv { color: #7aa2f7; margin-right: 8px; }
.logline.err .lv { color: #ff6b6b; }
.logline.err .msg { color: #ff9b9b; }
.logline.warn .lv { color: #e6a23c; }
.logline.warn .msg { color: #f0c674; }
.target { border-top: 1px solid var(--line); padding: 10px 0; }
.target:first-of-type { border-top: none; }
.comps { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.tag.bad { background: #fdeaea; color: var(--danger); }
.small { font-size: 12px; }
.warn-text { color: var(--warn); }

.restore-ctrl { gap: 12px; margin-bottom: 10px; }
.sel { display: flex; flex-direction: column; gap: 4px; font-size: 13px; flex: 1; }
.sel select { padding: 6px 8px; border-radius: 6px; border: 1px solid var(--line); background: var(--panel); color: var(--text); }
.points { display: flex; flex-direction: column; gap: 8px; max-height: 280px; overflow-y: auto; }
.point { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
.point.active { border-color: var(--brand); box-shadow: 0 0 0 1px var(--brand); }
.point-main { display: flex; align-items: center; gap: 8px; font-weight: 600; }
.restore-pick { margin-top: 10px; border-top: 1px solid var(--line); padding-top: 10px; }
.comp-chk { display: flex; align-items: center; gap: 6px; padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px; }
.comp-chk.disabled { opacity: .5; }
.danger { background: var(--danger); color: #fff; border: none; border-radius: 8px; padding: 8px 14px; cursor: pointer; font-weight: 600; }
.danger:disabled { opacity: .5; cursor: not-allowed; }
.danger-text { color: var(--danger); }

.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 20px; max-width: 460px; width: 90%; }
.modal ul { margin: 8px 0; padding-left: 18px; }
.modal p { margin: 6px 0; }
</style>
