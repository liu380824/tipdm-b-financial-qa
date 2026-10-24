<script setup lang="ts">
import { ref, onMounted, computed, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { fetchDbOverview, fetchDbSchemas, databaseInit, databaseBatchImport, fetchBuildStatus } from '../services/api'
import type { DbOverview, DbSchema } from '../services/api'

const overview = ref<DbOverview | null>(null)
const schemas = ref<DbSchema[]>([])
const loading = ref(false)

const totalRows = computed(() => schemas.value.reduce((s, t) => s + t.rows, 0))

const ICON_COLORS = ['ov-card-icon--blue', 'ov-card-icon--violet', 'ov-card-icon--teal', 'ov-card-icon--amber']

const loadData = async () => {
  loading.value = true
  try {
    const [ov, sc] = await Promise.all([fetchDbOverview(), fetchDbSchemas()])
    overview.value = ov
    schemas.value = sc
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadData)

// ── Build actions ──
const buildLoading = ref<Record<string, boolean>>({ init: false, import: false })
const buildStatus = ref<Record<string, { status: string; message: string }>>({ init: { status: '', message: '' }, import: { status: '', message: '' } })
let pollTimers: Record<string, ReturnType<typeof setInterval>> = {}

const pollTask = (key: string, taskId: string) => {
  if (pollTimers[key]) clearInterval(pollTimers[key])
  pollTimers[key] = setInterval(async () => {
    try {
      const s = await fetchBuildStatus(taskId)
      buildStatus.value[key] = { status: s.status, message: s.message }
      if (s.status !== 'running') {
        clearInterval(pollTimers[key])
        delete pollTimers[key]
        buildLoading.value[key] = false
        if (s.status === 'done') {
          ElMessage.success(s.message)
          loadData()
        } else if (s.status === 'error') {
          ElMessage.error(s.message)
        }
      }
    } catch { /* ignore */ }
  }, 3000)
}

const doInit = async () => {
  try {
    await ElMessageBox.confirm('这将清空并重建数据库表结构，已有数据会被删除。确定继续？', '初始化数据库', { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' })
  } catch { return }
  buildLoading.value.init = true
  buildStatus.value.init = { status: 'running', message: '正在初始化…' }
  try {
    const res = await databaseInit()
    buildStatus.value.init = { status: 'done', message: res.message }
    ElMessage.success(res.message)
    loadData()
  } catch (err) {
    buildStatus.value.init = { status: 'error', message: err instanceof Error ? err.message : '失败' }
    ElMessage.error(buildStatus.value.init.message)
  } finally {
    buildLoading.value.init = false
  }
}

const doImport = async () => {
  buildLoading.value.import = true
  buildStatus.value.import = { status: 'running', message: '正在启动…' }
  try {
    const res = await databaseBatchImport()
    if (!res.ok) { ElMessage.warning(res.message); buildLoading.value.import = false; return }
    buildStatus.value.import = { status: 'running', message: res.message }
    pollTask('import', res.task_id)
  } catch (err) {
    buildStatus.value.import = { status: 'error', message: err instanceof Error ? err.message : '失败' }
    ElMessage.error(buildStatus.value.import.message)
    buildLoading.value.import = false
  }
}

onUnmounted(() => {
  Object.values(pollTimers).forEach(clearInterval)
})
</script>

<template>
  <div class="ov" v-loading="loading">
    <template v-if="overview">
      <!-- Stat cards row -->
      <div class="ov-stats">
        <div v-for="(m, i) in overview.metrics" :key="m.title" class="ov-card">
          <div class="ov-card-icon" :class="ICON_COLORS[i % 4]">
            <svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8">
              <template v-if="i === 0"><path stroke-linecap="round" stroke-linejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375"/></template>
              <template v-else-if="i === 1"><path stroke-linecap="round" stroke-linejoin="round" d="M3.375 19.5h17.25m-17.25 0a1.125 1.125 0 01-1.125-1.125M3.375 19.5h7.5c.621 0 1.125-.504 1.125-1.125m-9.75 0V5.625m0 12.75v-1.5c0-.621.504-1.125 1.125-1.125"/></template>
              <template v-else-if="i === 2"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 21h16.5M4.5 3h15M5.25 3v18m13.5-18v18M9 6.75h1.5m-1.5 3h1.5m-1.5 3h1.5m3-6H15m-1.5 3H15m-1.5 3H15M9 21v-3.375c0-.621.504-1.125 1.125-1.125h3.75c.621 0 1.125.504 1.125 1.125V21"/></template>
              <template v-else><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z"/></template>
            </svg>
          </div>
          <div class="ov-card-body">
            <div class="ov-card-value">{{ m.value }}</div>
            <div class="ov-card-label">{{ m.title }}</div>
          </div>
        </div>
      </div>

      <!-- Table breakdown from schemas -->
      <div class="ov-section" v-if="schemas.length">
        <div class="ov-section-head">
          <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M3.375 19.5h17.25m-17.25 0a1.125 1.125 0 01-1.125-1.125M3.375 19.5h7.5c.621 0 1.125-.504 1.125-1.125m-9.75 0V5.625"/></svg>
          <span>数据表概况</span>
          <div class="ov-section-actions">
            <span v-if="buildStatus.init.message || buildStatus.import.message" class="ov-action-status" :class="'ov-action-status--' + (buildStatus.import.status || buildStatus.init.status)">
              {{ buildStatus.import.message || buildStatus.init.message }}
            </span>
            <button class="ov-action ov-action--warn" :disabled="buildLoading.init" @click="doInit">初始化</button>
            <button class="ov-action" :disabled="buildLoading.import" @click="doImport">{{ buildLoading.import ? '导入中…' : '导入财报' }}</button>
          </div>
        </div>
        <div class="ov-table-grid">
          <div v-for="tbl in schemas" :key="tbl.name" class="ov-table-item">
            <div class="ov-table-item-head">
              <span class="ov-table-item-name">{{ tbl.chineseName }}</span>
              <span class="ov-table-item-badge">{{ tbl.fields.length }} 列</span>
            </div>
            <div class="ov-table-item-value">{{ tbl.rows.toLocaleString() }}</div>
            <div class="ov-table-item-bar">
              <div class="ov-table-item-bar-fill" :style="{ width: (totalRows ? tbl.rows / totalRows * 100 : 0) + '%' }" />
            </div>
            <div class="ov-table-item-pct">占比 {{ totalRows ? (tbl.rows / totalRows * 100).toFixed(1) : 0 }}%</div>
          </div>
        </div>
      </div>

      <!-- Two-column: Connections + Import Tasks -->
      <div class="ov-grid-2">
        <div class="ov-section">
          <div class="ov-section-head">
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m9.86-2.056a4.5 4.5 0 00-6.364-6.364L4.5 8.25"/></svg>
            <span>数据连接</span>
          </div>
          <div class="ov-kv-list">
            <div v-for="c in overview.connections" :key="c.name" class="ov-kv">
              <span class="ov-kv-k">{{ c.name }} <span class="ov-kv-type">{{ c.type }}</span></span>
              <span class="ov-kv-v" :class="c.status === '运行中' ? 'ov-kv-v--green' : c.status === '已就绪' ? 'ov-kv-v--green' : 'ov-kv-v--amber'">{{ c.status }}</span>
            </div>
          </div>
        </div>

        <div class="ov-section">
          <div class="ov-section-head">
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            <span>导入任务状态</span>
          </div>
          <div class="ov-kv-list">
            <div v-for="t in overview.importTasks" :key="t.name" class="ov-kv">
              <div class="ov-kv-k">
                {{ t.name }}
                <div class="ov-kv-detail">{{ t.detail }}</div>
              </div>
              <span class="ov-tag" :class="t.status === '已完成' ? 'ov-tag--green' : t.status === '执行中' ? 'ov-tag--blue' : 'ov-tag--gray'">{{ t.status }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Quality rules -->
      <div class="ov-section" v-if="overview.qualityRules.length">
        <div class="ov-section-head">
          <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          <span>数据质量规则</span>
        </div>
        <div class="ov-rules-grid">
          <div v-for="(r, i) in overview.qualityRules" :key="i" class="ov-rule-item">
            <span class="ov-rule-num" :class="r.status === '通过' ? 'ov-rule-num--ok' : 'ov-rule-num--warn'">{{ i + 1 }}</span>
            <div class="ov-rule-body">
              <div class="ov-rule-title">{{ r.name }}</div>
              <div class="ov-rule-desc">范围: {{ r.scope }} · 上次检查: {{ r.lastCheck }}</div>
            </div>
            <span class="ov-tag" :class="r.status === '通过' ? 'ov-tag--green' : 'ov-tag--gray'">{{ r.status }}</span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ov {
  padding: 28px 36px 36px;
  overflow-y: auto;
  flex: 1;
  animation: ov-fadeIn 0.25s ease both;
}
@keyframes ov-fadeIn { from { opacity: 0; } to { opacity: 1; } }

/* ── Stat Cards ── */
.ov-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 28px;
}
.ov-card {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 20px;
  background: #fff;
  border: 1px solid #e5e5ea;
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.ov-card-icon {
  width: 44px; height: 44px;
  border-radius: 10px;
  display: grid; place-items: center;
  flex-shrink: 0;
}
.ov-card-icon--blue { background: #eff6ff; color: #2563eb; }
.ov-card-icon--violet { background: #f5f3ff; color: #7c3aed; }
.ov-card-icon--teal { background: #f0fdfa; color: #0d9488; }
.ov-card-icon--amber { background: #fffbeb; color: #d97706; }
.ov-card-body { min-width: 0; }
.ov-card-value {
  font-size: 22px; font-weight: 700; color: #1d1d1f;
  font-family: var(--font-display, 'Inter', sans-serif);
  line-height: 1.2;
}
.ov-card-label {
  font-size: 12px; color: #a1a1aa; margin-top: 2px; font-weight: 500;
}

/* ── Sections ── */
.ov-section {
  background: #fff;
  border: 1px solid #e5e5ea;
  border-radius: 12px;
  padding: 20px 24px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  margin-bottom: 20px;
}
.ov-section-head {
  display: flex; align-items: center; gap: 8px;
  font-size: 15px; font-weight: 700; color: #1d1d1f;
  margin-bottom: 16px;
}
.ov-section-head svg { color: #2563eb; flex-shrink: 0; }

/* ── Table grid ── */
.ov-table-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
}
.ov-table-item {
  padding: 14px 16px;
  background: #fafafa;
  border: 1px solid #f0f0f3;
  border-radius: 10px;
}
.ov-table-item-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 8px;
}
.ov-table-item-name { font-size: 13px; font-weight: 600; color: #52525b; }
.ov-table-item-badge {
  font-size: 11px; padding: 2px 8px; border-radius: 999px;
  background: #eff6ff; color: #2563eb; font-weight: 600;
}
.ov-table-item-value {
  font-size: 20px; font-weight: 700; color: #1d1d1f;
  font-family: var(--font-display, 'Inter', sans-serif);
  margin-bottom: 8px;
}
.ov-table-item-bar {
  height: 5px; border-radius: 999px; background: #e5e5ea; overflow: hidden;
  margin-bottom: 6px;
}
.ov-table-item-bar-fill {
  height: 100%; border-radius: 999px;
  background: linear-gradient(90deg, #2563eb, #7c3aed);
  transition: width 0.6s ease;
}
.ov-table-item-pct { font-size: 11px; color: #a1a1aa; }

/* ── Two col ── */
.ov-grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}
.ov-grid-2 .ov-section { margin-bottom: 0; }

/* ── KV list ── */
.ov-kv-list { display: flex; flex-direction: column; gap: 10px; }
.ov-kv {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 12px; background: #fafafa; border-radius: 8px;
}
.ov-kv-k { font-size: 13px; color: #52525b; font-weight: 500; }
.ov-kv-v { font-size: 13px; color: #1d1d1f; font-weight: 600; }
.ov-kv-v--green { color: #059669; }
.ov-kv-v--amber { color: #d97706; }
.ov-kv-v--red { color: #dc2626; }
.ov-kv-v--mono { font-family: var(--font-mono, monospace); font-size: 11px; color: #a1a1aa; font-weight: 500; }
.ov-kv-type {
  font-size: 11px; color: #a1a1aa; font-weight: 400; margin-left: 6px;
}
.ov-kv-detail {
  font-size: 11px; color: #a1a1aa; margin-top: 2px; line-height: 1.4;
}
.ov-empty { font-size: 13px; color: #a1a1aa; padding: 12px 0; }

/* ── Tags ── */
.ov-tag {
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: 11px; font-weight: 600; flex-shrink: 0;
}
.ov-tag--green { background: #f0fdf4; color: #059669; }
.ov-tag--blue { background: #eff6ff; color: #2563eb; }
.ov-tag--gray { background: #f5f5f5; color: #a1a1aa; }

/* ── Quality Rules ── */
.ov-rules-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
}
.ov-rule-item {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 10px 14px; background: #fafafa; border-radius: 8px;
}
.ov-rule-num {
  width: 22px; height: 22px; border-radius: 6px;
  background: #eff6ff; color: #2563eb;
  font-size: 11px; font-weight: 700;
  display: grid; place-items: center; flex-shrink: 0;
}
.ov-rule-body { flex: 1; min-width: 0; }
.ov-rule-num--ok { background: #f0fdf4; color: #059669; }
.ov-rule-num--warn { background: #fffbeb; color: #d97706; }
.ov-rule-title { font-size: 13px; font-weight: 600; color: #1d1d1f; }
.ov-rule-desc { font-size: 11px; color: #a1a1aa; margin-top: 2px; }

/* ── Section Actions ── */
.ov-section-actions {
  margin-left: auto;
  display: flex; align-items: center; gap: 8px;
}
.ov-action {
  padding: 4px 12px; border-radius: 6px; border: 1px solid #e5e5ea;
  background: #fff; color: #71717a;
  font-size: 12px; font-weight: 500; cursor: pointer;
  transition: all 0.15s ease; white-space: nowrap;
}
.ov-action:hover:not(:disabled) { border-color: #2563eb; color: #2563eb; background: #eff6ff; }
.ov-action:disabled { opacity: 0.5; cursor: default; }
.ov-action--warn:hover:not(:disabled) { border-color: #d97706; color: #d97706; background: #fffbeb; }
.ov-action-status {
  font-size: 11px; padding: 3px 8px; border-radius: 6px;
}
.ov-action-status--running { background: #eff6ff; color: #2563eb; }
.ov-action-status--done { background: #f0fdf4; color: #059669; }
.ov-action-status--error { background: #fef2f2; color: #dc2626; }

@media (max-width: 1100px) {
  .ov-stats, .ov-table-grid { grid-template-columns: repeat(2, 1fr); }
  .ov-grid-2 { grid-template-columns: 1fr; }
  .ov-build-actions { grid-template-columns: 1fr; }
}
</style>
