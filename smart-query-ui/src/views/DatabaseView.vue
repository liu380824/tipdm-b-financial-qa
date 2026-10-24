<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, Refresh } from '@element-plus/icons-vue'
import { fetchTableData, insertTableRow, deleteTableRow, runDatabaseQuery } from '../services/api'
import DataOverviewPanel from '../components/DataOverviewPanel.vue'
import QualityReportPanel from '../components/QualityReportPanel.vue'
import ImportLogPanel from '../components/ImportLogPanel.vue'

type TopTab = 'overview' | 'tables' | 'quality' | 'import'
const activeTab = ref<TopTab>('overview')

const TABLES = [
  { name: 'core_performance_indicators_sheet', label: '核心业绩指标' },
  { name: 'income_sheet', label: '利润表' },
  { name: 'balance_sheet', label: '资产负债表' },
  { name: 'cash_flow_sheet', label: '现金流量表' },
] as const

type Col = { key: string; label: string }
type TableRow = Record<string, unknown>

const activeTable = ref<string>('core_performance_indicators_sheet')
const columns = ref<Col[]>([])
const rows = ref<TableRow[]>([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(100)
const loading = ref(false)

const showAdd = ref(false)
const addLoading = ref(false)
const formData = ref<Record<string, string>>({})

const searchText = ref('')
const filterCode = ref('')
const filterPeriod = ref('')
const activeFilters = computed(() =>
  !!(searchText.value.trim() || filterCode.value.trim() || filterPeriod.value.trim())
)
const clearFilters = () => {
  searchText.value = ''
  filterCode.value = ''
  filterPeriod.value = ''
  currentPage.value = 1
  loadData()
}

const showQuery = ref(false)
const querySql = ref('')
const queryLoading = ref(false)
const queryNotice = ref('')
const queryColumns = ref<Col[]>([])
const queryRows = ref<TableRow[]>([])
const queryError = ref('')

const executeQuery = async () => {
  const sql = querySql.value.trim()
  if (!sql) { ElMessage.warning('请输入 SQL 查询语句'); return }
  queryLoading.value = true
  queryError.value = ''
  queryNotice.value = ''
  queryColumns.value = []
  queryRows.value = []
  try {
    const res = await runDatabaseQuery({ sql, limit: 200 })
    queryNotice.value = res.notice
    queryColumns.value = res.columns
    queryRows.value = res.rows
  } catch (err) {
    queryError.value = err instanceof Error ? err.message : '查询失败'
  } finally {
    queryLoading.value = false
  }
}

const AUTO_FILL = new Set(['report_year'])
const REQUIRED = new Set(['stock_code', 'report_period'])

const formColumns = computed(() => columns.value.filter(c => !AUTO_FILL.has(c.key)))
const currentTableLabel = computed(() => TABLES.find(t => t.name === activeTable.value)?.label ?? '')

const loadData = async () => {
  loading.value = true
  try {
    const filters = {
      search: searchText.value.trim() || undefined,
      stock_code: filterCode.value.trim() || undefined,
      report_period: filterPeriod.value.trim() || undefined,
    }
    const res = await fetchTableData(activeTable.value, currentPage.value, pageSize.value, filters)
    columns.value = res.columns
    rows.value = res.rows
    total.value = res.total
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '加载失败')
  } finally {
    loading.value = false
  }
}

watch(activeTable, () => {
  currentPage.value = 1
  formData.value = {}
  loadData()
})

watch(activeTab, (val) => {
  if (val === 'tables') loadData()
})

onMounted(() => { /* data loads on demand when switching to tables tab */ })

const NARROW_COLS = new Set(['serial_number', 'stock_code', 'stock_abbr', 'report_period', 'report_year'])
const colWidth = (key: string) => NARROW_COLS.has(key) ? 110 : 160

const handleSizeChange = (size: number) => {
  pageSize.value = size
  currentPage.value = 1
  loadData()
}

const openAdd = () => {
  formData.value = {}
  showAdd.value = true
}

const submitAdd = async () => {
  const sc = (formData.value['stock_code'] ?? '').trim()
  const rp = (formData.value['report_period'] ?? '').trim()
  if (!sc || !rp) {
    ElMessage.warning('股票代码和报告期为必填项')
    return
  }
  addLoading.value = true
  try {
    const data: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(formData.value)) {
      if (v === '' || v == null) continue
      const n = Number(v)
      data[k] = isNaN(n) || v.trim() === '' ? v : n
    }
    await insertTableRow(activeTable.value, data)
    ElMessage.success('数据已保存')
    showAdd.value = false
    currentPage.value = 1
    loadData()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '保存失败')
  } finally {
    addLoading.value = false
  }
}

const confirmDelete = async (row: TableRow) => {
  const sc = String(row['stock_code'] ?? '')
  const rp = String(row['report_period'] ?? '')
  try {
    await ElMessageBox.confirm(
      `确定删除 ${sc} ${rp} 的记录吗？此操作不可撤销。`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger' }
    )
    await deleteTableRow(activeTable.value, sc, rp)
    ElMessage.success('已删除')
    loadData()
  } catch (err) {
    if ((err as string) !== 'cancel') {
      ElMessage.error(err instanceof Error ? err.message : '删除失败')
    }
  }
}


</script>

<template>
  <div class="db">
    <!-- ══ Top-Level Tab Bar (SettingsView style) ══ -->
    <nav class="db-tabs">
      <button :class="['db-tab', { active: activeTab === 'overview' }]" @click="activeTab = 'overview'">
        <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z"/></svg>
        数据总览
      </button>
      <button :class="['db-tab', { active: activeTab === 'tables' }]" @click="activeTab = 'tables'">
        <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125"/></svg>
        数据表管理
      </button>
      <button :class="['db-tab', { active: activeTab === 'quality' }]" @click="activeTab = 'quality'">
        <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z"/></svg>
        数据质量
      </button>
      <button :class="['db-tab', { active: activeTab === 'import' }]" @click="activeTab = 'import'">
        <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m6.75 12H9.75m11.25-10.5H15M2.25 6.75c0 2.278 3.694 4.125 8.25 4.125s8.25-1.847 8.25-4.125M2.25 6.75L12 2.25l9.75 4.5M2.25 6.75v10.5c0 2.278 3.694 4.125 8.25 4.125s8.25-1.847 8.25-4.125V6.75"/></svg>
        导入记录
      </button>
    </nav>

    <!-- ══ Content Area ══ -->
    <div class="db-content">

      <!-- Tab: 数据总览 -->
      <DataOverviewPanel v-if="activeTab === 'overview'" />

      <!-- Tab: 数据表管理 -->
      <div v-show="activeTab === 'tables'" class="db-tables-panel">
        <!-- Sub-tabs for tables -->
        <div class="db-sub-tabs">
          <button
            v-for="t in TABLES"
            :key="t.name"
            :class="['db-sub-tab', { active: activeTable === t.name }]"
            @click="activeTable = t.name"
          >{{ t.label }}</button>
        </div>

        <!-- Toolbar -->
        <div class="table-toolbar">
          <button class="add-btn" @click="openAdd">
            <el-icon><Plus /></el-icon>
            添加数据
          </button>
          <button class="query-toggle-btn" :class="{ 'query-toggle-btn--active': showQuery }" @click="showQuery = !showQuery">
            SQL 查询
          </button>
          <div class="toolbar-search">
            <svg class="toolbar-search-icon" width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
            <input
              v-model="searchText"
              class="toolbar-search-input"
              placeholder="搜索股票代码或简称…"
              @keydown.enter="() => { currentPage = 1; loadData() }"
            />
            <button v-if="searchText.trim()" class="toolbar-search-clear" @click="clearFilters">&times;</button>
          </div>
          <span class="table-meta">{{ currentTableLabel }} · {{ total }} 条</span>
          <button class="refresh-btn" :class="{ spinning: loading }" :disabled="loading" @click="loadData">
            <el-icon><Refresh /></el-icon>
          </button>
        </div>

        <!-- SQL Query Panel -->
        <div v-if="showQuery" class="query-panel">
          <div class="query-input-row">
            <textarea
              v-model="querySql"
              class="query-textarea"
              rows="3"
              placeholder="输入 SQL 查询语句，例如：SELECT stock_abbr, report_period, total_profit FROM income_sheet ORDER BY total_profit DESC LIMIT 10"
              @keydown.ctrl.enter="executeQuery"
            />
            <button class="query-run-btn" :disabled="queryLoading" @click="executeQuery">
              {{ queryLoading ? '执行中…' : '▶ 执行' }}
            </button>
          </div>
          <div v-if="queryError" class="query-error">{{ queryError }}</div>
          <div v-if="queryNotice" class="query-notice">{{ queryNotice }}</div>
          <div v-if="queryColumns.length" class="query-result-wrap">
            <el-table :data="queryRows" border size="small" max-height="280" style="width:100%" empty-text="无结果">
              <el-table-column
                v-for="col in queryColumns"
                :key="col.key"
                :prop="col.key"
                :label="col.label"
                min-width="120"
                show-overflow-tooltip
              />
            </el-table>
          </div>
        </div>

        <!-- Data Table -->
        <div class="table-wrap" v-loading="loading">
          <el-table
            :data="rows"
            border
            size="small"
            style="width: 100%"
            height="100%"
            empty-text="暂无数据"
          >
            <el-table-column
              v-for="col in columns"
              :key="col.key"
              :prop="col.key"
              :label="col.label"
              :min-width="colWidth(col.key)"
              show-overflow-tooltip
              resizable
            />
            <el-table-column label="操作" width="72" fixed="right">
              <template #default="{ row }">
                <button class="del-row-btn" title="删除此行" @click="confirmDelete(row)">
                  <el-icon><Delete /></el-icon>
                </button>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <!-- Pagination -->
        <div class="pagination-bar">
          <el-pagination
            v-model:current-page="currentPage"
            v-model:page-size="pageSize"
            :total="total"
            :page-sizes="[50, 100, 200]"
            layout="total, sizes, prev, pager, next, jumper"
            small
            @current-change="loadData"
            @size-change="handleSizeChange"
          />
        </div>
      </div>

      <!-- Tab: 数据质量 -->
      <QualityReportPanel v-if="activeTab === 'quality'" />

      <!-- Tab: 导入记录 -->
      <ImportLogPanel v-if="activeTab === 'import'" />
    </div>

    <!-- ── Add Dialog ── -->
    <el-dialog
      v-model="showAdd"
      :title="`添加数据 — ${currentTableLabel}`"
      width="700px"
      align-center
    >
      <div class="form-grid">
        <div v-for="col in formColumns" :key="col.key" class="form-item">
          <label class="form-label">
            {{ col.label }}
            <span v-if="REQUIRED.has(col.key)" class="req-star">*</span>
          </label>
          <input
            v-model="formData[col.key]"
            class="form-input"
            :class="{ 'form-input--req': REQUIRED.has(col.key) }"
            :placeholder="REQUIRED.has(col.key) ? '必填' : '可选'"
          />
        </div>
      </div>
      <template #footer>
        <button class="dlg-cancel" @click="showAdd = false">取消</button>
        <button class="dlg-confirm" :disabled="addLoading" @click="submitAdd">
          {{ addLoading ? '保存中…' : '确认保存' }}
        </button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* ═══════════════════════════════════════════════
   DatabaseView — SettingsView-style Layout
   ═══════════════════════════════════════════════ */

.db {
  --db-font: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --db-bg: #f5f5f7;
  --db-card: #ffffff;
  --db-border: #e5e5ea;
  --db-border-light: #f0f0f3;
  --db-text-1: #1d1d1f;
  --db-text-2: #52525b;
  --db-text-3: #a1a1aa;
  --db-accent: #2563eb;
  --db-accent-hover: #1d4ed8;
  --db-accent-bg: #eff6ff;
  --db-radius: 14px;
  --db-radius-sm: 8px;
  --db-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.03);

  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 0;
  display: flex;
  flex-direction: column;
  background: var(--db-bg);
  font-family: var(--db-font);
  color: var(--db-text-2);
}

/* ══ Top-Level Tabs ══ */
.db-tabs {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 0;
  padding: 0 36px;
  background: var(--db-card);
  border-bottom: 1px solid var(--db-border);
}
.db-tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 16px 22px;
  border: none;
  background: none;
  font-family: var(--db-font);
  font-size: 14px;
  font-weight: 600;
  color: var(--db-text-3);
  cursor: pointer;
  transition: color 0.2s;
  white-space: nowrap;
}
.db-tab:hover { color: var(--db-text-2); }
.db-tab.active { color: var(--db-accent); }
.db-tab::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: 22px;
  right: 22px;
  height: 2px;
  border-radius: 2px 2px 0 0;
  background: transparent;
  transition: background 0.2s;
}
.db-tab.active::after { background: var(--db-accent); }
.db-tab svg {
  flex-shrink: 0;
  opacity: 0.55;
  transition: opacity 0.2s;
}
.db-tab.active svg { opacity: 1; }

/* ══ Content Area ══ */
.db-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

/* ══ Tables Panel (full height flex child) ══ */
.db-tables-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--db-card);
}

/* ── Sub-Tabs (table selector) ── */
.db-sub-tabs {
  display: flex;
  align-items: center;
  gap: 0;
  padding: 0 28px;
  border-bottom: 1px solid var(--db-border-light);
  flex-shrink: 0;
}
.db-sub-tab {
  position: relative;
  padding: 10px 20px 12px;
  border: none;
  background: none;
  font-family: var(--db-font);
  font-size: 13px;
  font-weight: 600;
  color: var(--db-text-3);
  cursor: pointer;
  transition: color 0.2s;
  white-space: nowrap;
}
.db-sub-tab:hover { color: var(--db-text-2); }
.db-sub-tab.active { color: var(--db-accent); }
.db-sub-tab::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: 20px;
  right: 20px;
  height: 2px;
  border-radius: 2px 2px 0 0;
  background: transparent;
  transition: background 0.2s;
}
.db-sub-tab.active::after { background: var(--db-accent); }

/* ── Toolbar ── */
.table-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 28px;
  border-bottom: 1px solid var(--db-border-light);
  flex-shrink: 0;
}
.table-meta {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--db-text-3);
  margin-left: auto;
  margin-right: 4px;
}
.add-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 16px;
  border-radius: var(--db-radius-sm);
  border: none;
  background: var(--db-accent);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;
}
.add-btn:hover { background: var(--db-accent-hover); }
.refresh-btn {
  width: 32px;
  height: 32px;
  border-radius: var(--db-radius-sm);
  border: 1px solid var(--db-border);
  background: var(--db-card);
  color: var(--db-text-3);
  cursor: pointer;
  display: grid;
  place-items: center;
  transition: all 0.15s ease;
}
.refresh-btn:hover { background: #fafafa; color: var(--db-text-2); }
.refresh-btn:disabled { opacity: 0.4; cursor: default; }
.spinning { animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Toolbar Search ── */
.toolbar-search {
  position: relative;
  display: flex;
  align-items: center;
  margin-left: 4px;
}
.toolbar-search-icon {
  position: absolute;
  left: 9px;
  color: var(--db-text-3);
  pointer-events: none;
}
.toolbar-search-input {
  padding: 7px 28px 7px 30px;
  border: 1px solid var(--db-border);
  border-radius: var(--db-radius-sm);
  font-size: 13px;
  outline: none;
  background: var(--db-card);
  color: var(--db-text-1);
  width: 220px;
  transition: border-color 0.15s ease, width 0.2s ease;
}
.toolbar-search-input:focus {
  border-color: var(--db-accent);
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.08);
  width: 280px;
}
.toolbar-search-input::placeholder { color: var(--db-text-3); }
.toolbar-search-clear {
  position: absolute;
  right: 4px;
  width: 20px; height: 20px;
  border: none; background: none;
  color: var(--db-text-3);
  font-size: 16px; line-height: 1;
  cursor: pointer;
  border-radius: 50%;
  display: grid; place-items: center;
}
.toolbar-search-clear:hover { background: #f0f0f3; color: var(--db-text-2); }

/* ── SQL Query Panel ── */
.query-toggle-btn {
  display: flex; align-items: center; gap: 5px;
  padding: 7px 14px; border-radius: var(--db-radius-sm);
  border: 1px solid var(--db-border); background: var(--db-card);
  color: var(--db-text-2); font-size: 13px; cursor: pointer;
  transition: all 0.15s ease; font-family: var(--db-font);
}
.query-toggle-btn:hover { border-color: var(--db-accent); color: var(--db-accent); }
.query-toggle-btn--active { border-color: var(--db-accent); color: var(--db-accent); background: var(--db-accent-bg); font-weight: 600; }

.query-panel {
  padding: 12px 28px; background: #fafafa;
  border-bottom: 1px solid var(--db-border-light);
  display: flex; flex-direction: column; gap: 10px;
  flex-shrink: 0;
}
.query-input-row { display: flex; gap: 10px; align-items: flex-start; }
.query-textarea {
  flex: 1; padding: 10px 12px; border: 1px solid var(--db-border);
  border-radius: var(--db-radius-sm); font-size: 13px; font-family: var(--font-mono, monospace);
  resize: vertical; outline: none; background: var(--db-card); color: var(--db-text-1);
  line-height: 1.5; min-height: 60px;
  transition: border-color 0.15s ease;
}
.query-textarea:focus { border-color: var(--db-accent); box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.08); }
.query-textarea::placeholder { color: var(--db-text-3); }
.query-run-btn {
  padding: 10px 20px; border-radius: var(--db-radius-sm); border: none;
  background: var(--db-accent); color: #fff; font-size: 13px; font-weight: 600;
  cursor: pointer; white-space: nowrap; transition: all 0.15s ease;
  align-self: stretch; min-height: 40px;
}
.query-run-btn:hover:not(:disabled) { background: var(--db-accent-hover); }
.query-run-btn:disabled { opacity: 0.5; cursor: default; }
.query-error {
  padding: 8px 12px; background: #fef2f2; border: 1px solid #fecaca;
  border-radius: var(--db-radius-sm); color: #dc2626; font-size: 13px;
}
.query-notice {
  padding: 8px 12px; background: #f0fdf4; border: 1px solid #bbf7d0;
  border-radius: var(--db-radius-sm); color: #059669; font-size: 13px;
}
.query-result-wrap {
  border-radius: var(--db-radius-sm); overflow: hidden;
  border: 1px solid var(--db-border);
}

/* ── Table ── */
.table-wrap {
  flex: 1;
  overflow: hidden;
  padding: 0 28px;
  padding-top: 10px;
}
.del-row-btn {
  width: 26px;
  height: 26px;
  border-radius: 6px;
  border: 1px solid #fecaca;
  background: #fef2f2;
  color: #dc2626;
  cursor: pointer;
  display: grid;
  place-items: center;
  font-size: 12px;
  transition: all 0.12s ease;
}
.del-row-btn:hover { background: #fecaca; }

/* ── Pagination ── */
.pagination-bar {
  padding: 8px 28px;
  border-top: 1px solid var(--db-border-light);
  display: flex;
  justify-content: flex-end;
  flex-shrink: 0;
}

/* ── Add Dialog Form ── */
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px 20px;
  max-height: 65vh;
  overflow-y: auto;
  padding: 4px 2px;
}
.form-item {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.form-label {
  font-size: 12px;
  color: var(--db-text-2);
  font-weight: 500;
}
.req-star { color: #dc2626; margin-left: 2px; }
.form-input {
  padding: 8px 10px;
  border: 1px solid var(--db-border);
  border-radius: var(--db-radius-sm);
  font-size: 13px;
  outline: none;
  transition: border-color 0.15s ease;
  background: #fafafa;
  color: var(--db-text-1);
}
.form-input:focus { border-color: var(--db-accent); background: var(--db-card); }
.form-input--req { border-color: rgba(37, 99, 235, 0.3); }
.form-input--req:focus { border-color: var(--db-accent); }
.dlg-cancel {
  padding: 8px 20px;
  border-radius: var(--db-radius-sm);
  border: 1px solid var(--db-border);
  background: var(--db-card);
  color: var(--db-text-2);
  font-size: 13px;
  cursor: pointer;
  margin-right: 10px;
  transition: all 0.15s ease;
}
.dlg-cancel:hover { background: #fafafa; }
.dlg-confirm {
  padding: 8px 22px;
  border-radius: var(--db-radius-sm);
  border: none;
  background: var(--db-accent);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;
}
.dlg-confirm:hover:not(:disabled) { background: var(--db-accent-hover); }
.dlg-confirm:disabled { opacity: 0.5; cursor: default; }

/* ══ Animations ══ */
@keyframes db-fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

/* ══ Responsive ══ */
@media (max-width: 900px) {
  .db-tabs { padding: 0 20px; overflow-x: auto; }
  .db-tab { padding: 14px 16px; font-size: 13px; }
  .db-sub-tabs { padding: 0 20px; }
  .table-toolbar { padding: 10px 20px; }
  .query-panel { padding: 12px 20px; }
  .table-wrap { padding: 0 20px; padding-top: 10px; }
  .pagination-bar { padding: 8px 20px; }
}
@media (max-width: 600px) {
  .db-tabs { padding: 0 12px; }
  .db-tab { padding: 12px 10px; font-size: 12px; gap: 4px; }
  .db-sub-tab { padding: 8px 14px; font-size: 12px; }
}
</style>
