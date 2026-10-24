<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchImportLogs, fetchImportIssues } from '../services/api'
import type { ImportLogRow, ImportIssueRow } from '../services/api'

const logLoading = ref(false)
const logRows = ref<ImportLogRow[]>([])
const logTotal = ref(0)
const logPage = ref(1)

const issueLoading = ref(false)
const issueRows = ref<ImportIssueRow[]>([])
const issueTotal = ref(0)
const issuePage = ref(1)

const selectedJobId = ref<string | undefined>(undefined)
const viewMode = ref<'jobs' | 'issues'>('jobs')

const loadLogs = async () => {
  logLoading.value = true
  try {
    const res = await fetchImportLogs(logPage.value, 15)
    logRows.value = res.rows
    logTotal.value = res.total
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '加载失败')
  } finally {
    logLoading.value = false
  }
}

const loadIssues = async () => {
  issueLoading.value = true
  try {
    const res = await fetchImportIssues(issuePage.value, 50, selectedJobId.value)
    issueRows.value = res.rows
    issueTotal.value = res.total
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '加载失败')
  } finally {
    issueLoading.value = false
  }
}

const viewJobIssues = (jobId: string) => {
  selectedJobId.value = jobId
  issuePage.value = 1
  viewMode.value = 'issues'
  loadIssues()
}

const backToJobs = () => {
  viewMode.value = 'jobs'
  selectedJobId.value = undefined
}

const viewAllIssues = () => {
  selectedJobId.value = undefined
  issuePage.value = 1
  viewMode.value = 'issues'
  loadIssues()
}

const levelClass = (level: string) => {
  if (level === 'error') return 'il-tag--red'
  if (level === 'warning') return 'il-tag--amber'
  return ''
}

onMounted(() => {
  loadLogs()
})
</script>

<template>
  <div class="il">
    <!-- Sub-tabs -->
    <div class="il-sub-tabs">
      <button :class="['il-sub-tab', { active: viewMode === 'jobs' }]" @click="backToJobs">
        导入任务列表
      </button>
      <button :class="['il-sub-tab', { active: viewMode === 'issues' }]" @click="viewAllIssues">
        质量问题明细
      </button>
    </div>

    <!-- Jobs view -->
    <div v-if="viewMode === 'jobs'" class="il-panel" v-loading="logLoading">
      <div v-if="logRows.length === 0 && !logLoading" class="il-empty">暂无导入任务记录</div>
      <div v-else class="il-table-wrap">
        <table class="il-table">
          <thead>
            <tr>
              <th>任务ID</th>
              <th>开始时间</th>
              <th>结束时间</th>
              <th>报告总数</th>
              <th>成功数</th>
              <th>警告数</th>
              <th>错误数</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in logRows" :key="row.job_id">
              <td><code class="il-mono">{{ row.job_id }}</code></td>
              <td>{{ row.started_at }}</td>
              <td>{{ row.finished_at }}</td>
              <td>{{ row.total_reports }}</td>
              <td class="il-text--green">{{ row.success_reports }}</td>
              <td :class="row.warning_count > 0 ? 'il-text--amber' : ''">{{ row.warning_count }}</td>
              <td :class="row.error_count > 0 ? 'il-text--red' : ''">{{ row.error_count }}</td>
              <td>
                <button class="il-link-btn" @click="viewJobIssues(row.job_id)">查看问题</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="logTotal > 15" class="il-pager">
        <el-pagination
          v-model:current-page="logPage"
          :total="logTotal"
          :page-size="15"
          layout="total, prev, pager, next"
          small
          @current-change="loadLogs"
        />
      </div>
    </div>

    <!-- Issues view -->
    <div v-if="viewMode === 'issues'" class="il-panel" v-loading="issueLoading">
      <div class="il-issue-header">
        <span v-if="selectedJobId" class="il-scope-tag">
          任务: <code>{{ selectedJobId }}</code>
          <button class="il-scope-clear" @click="() => { selectedJobId = undefined; issuePage = 1; loadIssues() }">×</button>
        </span>
        <span v-else class="il-scope-tag il-scope-tag--all">全部问题</span>
        <span class="il-issue-count">共 {{ issueTotal }} 条</span>
      </div>

      <div v-if="issueRows.length === 0 && !issueLoading" class="il-empty">暂无质量问题记录</div>
      <div v-else class="il-table-wrap">
        <table class="il-table il-table--compact">
          <thead>
            <tr>
              <th>级别</th>
              <th>类型</th>
              <th>表名</th>
              <th>字段</th>
              <th>股票代码</th>
              <th>报告期</th>
              <th>问题描述</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in issueRows" :key="row.id">
              <td><span class="il-tag" :class="levelClass(row.issue_level)">{{ row.issue_level }}</span></td>
              <td>{{ row.issue_type }}</td>
              <td>{{ row.table_name }}</td>
              <td>{{ row.field_name }}</td>
              <td><code class="il-mono">{{ row.stock_code }}</code></td>
              <td>{{ row.report_period }}</td>
              <td class="il-msg-cell">{{ row.message }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="issueTotal > 50" class="il-pager">
        <el-pagination
          v-model:current-page="issuePage"
          :total="issueTotal"
          :page-size="50"
          layout="total, prev, pager, next"
          small
          @current-change="loadIssues"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.il {
  flex: 1; display: flex; flex-direction: column;
  padding: 0 36px 36px;
  overflow: hidden;
  animation: il-fadeIn 0.25s ease both;
}
@keyframes il-fadeIn { from { opacity: 0; } to { opacity: 1; } }

/* ── Sub-tabs ── */
.il-sub-tabs {
  display: flex; gap: 0;
  border-bottom: 1px solid #f0f0f3;
  margin-bottom: 20px; flex-shrink: 0;
}
.il-sub-tab {
  position: relative; padding: 10px 20px 12px;
  border: none; background: none;
  font-size: 14px; font-weight: 600;
  color: #a1a1aa; cursor: pointer;
  transition: color 0.2s; white-space: nowrap;
}
.il-sub-tab:hover { color: #52525b; }
.il-sub-tab.active { color: #2563eb; }
.il-sub-tab::after {
  content: ''; position: absolute; bottom: -1px;
  left: 20px; right: 20px; height: 2px;
  border-radius: 2px 2px 0 0;
  background: transparent; transition: background 0.2s;
}
.il-sub-tab.active::after { background: #2563eb; }

/* ── Panel ── */
.il-panel {
  flex: 1; display: flex; flex-direction: column;
  overflow: hidden; min-height: 0;
}
.il-empty { font-size: 13px; color: #a1a1aa; padding: 32px 0; text-align: center; }

/* ── Table ── */
.il-table-wrap {
  flex: 1; overflow: auto;
  border: 1px solid #e5e5ea; border-radius: 10px;
}
.il-table {
  width: 100%; border-collapse: collapse;
  font-size: 13px; color: #1d1d1f;
}
.il-table th {
  text-align: left; padding: 10px 14px;
  background: #fafafa; border-bottom: 1px solid #e5e5ea;
  font-weight: 600; color: #52525b; font-size: 12px;
  white-space: nowrap; position: sticky; top: 0; z-index: 1;
}
.il-table td {
  padding: 9px 14px; border-bottom: 1px solid #f0f0f3;
  white-space: nowrap;
}
.il-table--compact td { font-size: 12px; padding: 7px 12px; }
.il-table tbody tr:hover { background: #fafafa; }
.il-mono { font-family: var(--font-mono, monospace); font-size: 11px; color: #52525b; }
.il-text--green { color: #059669; font-weight: 600; }
.il-text--amber { color: #d97706; font-weight: 600; }
.il-text--red { color: #dc2626; font-weight: 600; }

/* ── Tags ── */
.il-tag {
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: 11px; font-weight: 600;
  background: #f0f0f3; color: #52525b;
}
.il-tag--amber { background: #fffbeb; color: #d97706; }
.il-tag--red { background: #fef2f2; color: #dc2626; }

/* ── Link btn ── */
.il-link-btn {
  padding: 4px 12px; border-radius: 6px;
  border: 1px solid #e5e5ea; background: #fff;
  font-size: 12px; color: #2563eb; font-weight: 600;
  cursor: pointer; transition: all 0.15s;
}
.il-link-btn:hover { background: #eff6ff; border-color: #2563eb; }

/* ── Issue header ── */
.il-issue-header {
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 14px; flex-shrink: 0;
}
.il-scope-tag {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 12px; border-radius: 999px;
  background: #eff6ff; color: #2563eb; font-size: 12px; font-weight: 600;
}
.il-scope-tag--all { background: #f0f0f3; color: #52525b; }
.il-scope-tag code { font-family: var(--font-mono, monospace); font-size: 11px; }
.il-scope-clear {
  width: 16px; height: 16px; border-radius: 50%;
  border: none; background: rgba(37,99,235,0.12);
  color: #2563eb; font-size: 12px; cursor: pointer;
  display: grid; place-items: center;
}
.il-scope-clear:hover { background: rgba(37,99,235,0.2); }
.il-issue-count { font-size: 12px; color: #a1a1aa; }
.il-msg-cell { white-space: normal; max-width: 360px; word-break: break-all; }

/* ── Pager ── */
.il-pager {
  display: flex; justify-content: flex-end;
  padding: 10px 0 0; flex-shrink: 0;
}

@media (max-width: 900px) {
  .il { padding: 0 20px 20px; }
}
</style>
