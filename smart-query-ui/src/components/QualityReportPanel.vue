<script setup lang="ts">
import { ref, onMounted, computed, watch, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import * as echarts from 'echarts'
import { fetchQualityReport, generateQualityReport, fetchQualityReportHistory, getQualityReportChartUrl } from '../services/api'
import type { QualityReport, QualityReportHistoryItem } from '../services/api'

const report = ref<QualityReport | null>(null)
const loading = ref(false)
const generating = ref(false)
const error = ref('')

const historyList = ref<QualityReportHistoryItem[]>([])
const historyLoading = ref(false)
const expandedReport = ref<string | null>(null)

const scoreChartRef = ref<HTMLDivElement>()
const fillRateChartRef = ref<HTMLDivElement>()
const methodChartRef = ref<HTMLDivElement>()
const issueChartRef = ref<HTMLDivElement>()

let scoreChart: echarts.ECharts | null = null
let fillRateChart: echarts.ECharts | null = null
let methodChart: echarts.ECharts | null = null
let issueChart: echarts.ECharts | null = null

const gradeColor = computed(() => {
  if (!report.value) return 'var(--text-tertiary)'
  const s = report.value.score_panel.overall_score
  if (s >= 90) return 'var(--success)'
  if (s >= 80) return 'var(--accent)'
  if (s >= 70) return 'var(--warning)'
  return 'var(--danger)'
})

const scorePct = computed(() => {
  if (!report.value) return 0
  return report.value.score_panel.overall_score
})

const pct = (v: number | null | undefined) => {
  if (v == null) return 'N/A'
  return (v * 100).toFixed(2) + '%'
}

const num = (v: number | null | undefined, d = 2) => {
  if (v == null) return 'N/A'
  return v.toFixed(d)
}

const loadReport = async () => {
  loading.value = true
  error.value = ''
  try {
    report.value = await fetchQualityReport()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
  if (report.value) {
    await nextTick()
    renderCharts()
  }
}

const doGenerate = async () => {
  generating.value = true
  try {
    const res = await generateQualityReport()
    ElMessage.success(`可视化报告已导出至：${res.report_dir}`)
    loadHistory()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '生成失败')
  } finally {
    generating.value = false
  }
}

const loadHistory = async () => {
  historyLoading.value = true
  try {
    historyList.value = await fetchQualityReportHistory()
  } catch { /* ignore */ }
  finally { historyLoading.value = false }
}

const toggleReport = (name: string) => {
  expandedReport.value = expandedReport.value === name ? null : name
}

const formatTs = (ts: string) => {
  return ts.replace(/[_T]/g, ' ').replace(/-/g, '/').slice(0, 16)
}

const CHART_COLORS = ['#2563eb', '#7c3aed', '#0d9488', '#d97706', '#dc2626']

function renderCharts() {
  if (!report.value) return

  // Score panel bar chart
  if (scoreChartRef.value) {
    scoreChart?.dispose()
    scoreChart = echarts.init(scoreChartRef.value)
    const comps = report.value.score_panel.components
    scoreChart.setOption({
      grid: { left: 120, right: 60, top: 16, bottom: 16 },
      xAxis: { type: 'value', max: 100, show: false },
      yAxis: {
        type: 'category',
        data: comps.map(c => c.name).reverse(),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: '#52525b', fontSize: 13 },
      },
      series: [{
        type: 'bar',
        data: comps.map((c, i) => ({
          value: c.score,
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
              { offset: 0, color: CHART_COLORS[i] || CHART_COLORS[0] },
              { offset: 1, color: CHART_COLORS[(i + 1) % CHART_COLORS.length] },
            ]),
            borderRadius: [0, 6, 6, 0],
          },
        })).reverse(),
        barWidth: 20,
        label: { show: true, position: 'right', formatter: '{c}', color: '#1d1d1f', fontWeight: 600, fontSize: 13 },
      }],
      animation: true,
      animationDuration: 800,
    })
  }

  // Table fill rate chart
  if (fillRateChartRef.value) {
    fillRateChart?.dispose()
    fillRateChart = echarts.init(fillRateChartRef.value)
    const tables = report.value.completeness.table_summary
    fillRateChart.setOption({
      grid: { left: 120, right: 60, top: 16, bottom: 16 },
      xAxis: { type: 'value', max: 100, show: false },
      yAxis: {
        type: 'category',
        data: tables.map(t => t.table_label).reverse(),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: '#52525b', fontSize: 13 },
      },
      series: [{
        type: 'bar',
        data: tables.map((t, i) => ({
          value: +((t.cell_fill_rate ?? 0) * 100).toFixed(2),
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
              { offset: 0, color: '#059669' },
              { offset: 1, color: '#0d9488' },
            ]),
            borderRadius: [0, 6, 6, 0],
          },
        })).reverse(),
        barWidth: 20,
        label: { show: true, position: 'right', formatter: (p: any) => p.value + '%', color: '#1d1d1f', fontWeight: 600, fontSize: 13 },
      }],
      animation: true,
      animationDuration: 800,
    })
  }

  // Extract method pie chart
  if (methodChartRef.value) {
    methodChart?.dispose()
    methodChart = echarts.init(methodChartRef.value)
    const methods = report.value.provenance.method_breakdown
    methodChart.setOption({
      tooltip: { trigger: 'item', formatter: '{b}: {d}%' },
      series: [{
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['50%', '50%'],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
        label: { show: true, formatter: '{b}\n{d}%', fontSize: 12, color: '#52525b' },
        data: methods.map((m, i) => ({
          name: m.extract_method,
          value: m.record_count,
          itemStyle: { color: CHART_COLORS[i % CHART_COLORS.length] },
        })),
      }],
      animation: true,
      animationDuration: 800,
    })
  }

  // Issue breakdown chart
  if (issueChartRef.value) {
    issueChart?.dispose()
    issueChart = echarts.init(issueChartRef.value)
    const issues = report.value.issues.by_type.slice(0, 6)
    issueChart.setOption({
      grid: { left: 140, right: 60, top: 16, bottom: 16 },
      xAxis: { type: 'value', show: false },
      yAxis: {
        type: 'category',
        data: issues.map(i => i.issue_type).reverse(),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: '#52525b', fontSize: 12 },
      },
      series: [{
        type: 'bar',
        data: issues.map((item, idx) => ({
          value: item.issue_count,
          itemStyle: {
            color: item.issue_level === 'error' ? '#dc2626' : '#d97706',
            borderRadius: [0, 6, 6, 0],
          },
        })).reverse(),
        barWidth: 18,
        label: { show: true, position: 'right', formatter: '{c}', color: '#1d1d1f', fontWeight: 600, fontSize: 12 },
      }],
      animation: true,
      animationDuration: 800,
    })
  }
}

const handleResize = () => {
  scoreChart?.resize()
  fillRateChart?.resize()
  methodChart?.resize()
  issueChart?.resize()
}

onMounted(() => {
  loadReport()
  loadHistory()
  window.addEventListener('resize', handleResize)
})
</script>

<template>
  <div class="qr-panel" v-loading="loading">
    <!-- Error state -->
    <div v-if="error && !loading" class="qr-error">
      <span>{{ error }}</span>
      <button class="qr-retry-btn" @click="loadReport">重试</button>
    </div>

    <template v-if="report && !loading">
      <!-- Header toolbar -->
      <div class="qr-toolbar">
        <div class="qr-toolbar-left">
          <span class="qr-gen-time">分析时间：{{ report.generated_at }}</span>
        </div>
        <div class="qr-toolbar-right">
          <button class="qr-gen-btn" :disabled="generating" @click="doGenerate">
            {{ generating ? '生成中…' : '导出可视化报告' }}
          </button>
          <button class="qr-refresh-btn" :class="{ spinning: loading }" :disabled="loading" @click="loadReport">
            <el-icon><Refresh /></el-icon>
          </button>
        </div>
      </div>

      <!-- Score Hero -->
      <div class="qr-hero">
        <div class="qr-score-card">
          <div class="qr-score-ring-wrap">
            <svg class="qr-score-ring" viewBox="0 0 120 120">
              <circle cx="60" cy="60" r="52" fill="none" stroke="#e5e5ea" stroke-width="10" />
              <circle
                cx="60" cy="60" r="52" fill="none"
                :stroke="gradeColor"
                stroke-width="10"
                stroke-linecap="round"
                :stroke-dasharray="`${scorePct * 3.267} 326.7`"
                transform="rotate(-90 60 60)"
                style="transition: stroke-dasharray 0.8s ease"
              />
            </svg>
            <div class="qr-score-text">
              <span class="qr-score-num">{{ num(report.score_panel.overall_score) }}</span>
              <span class="qr-score-grade" :style="{ color: gradeColor }">{{ report.score_panel.grade }}</span>
            </div>
          </div>
          <div class="qr-score-note">{{ report.score_panel.note }}</div>
        </div>

        <div class="qr-overview-text">
          <div class="qr-overview-title">{{ report.interpretation.overview }}</div>
          <div class="qr-chips">
            <span class="qr-chip">公司 {{ report.overview.company_count }}</span>
            <span class="qr-chip">报告期 {{ report.overview.report_group_count }}</span>
            <span class="qr-chip qr-chip--accent">总分 {{ num(report.score_panel.overall_score) }}</span>
            <span class="qr-chip" :class="report.score_panel.overall_score >= 80 ? 'qr-chip--success' : 'qr-chip--warn'">
              {{ report.score_panel.grade }}
            </span>
          </div>
        </div>
      </div>

      <!-- Stat cards -->
      <div class="qr-stats">
        <div class="qr-stat" v-for="comp in report.score_panel.components" :key="comp.name">
          <div class="qr-stat-label">{{ comp.name }}</div>
          <div class="qr-stat-value">{{ num(comp.score) }}</div>
          <div class="qr-stat-bar">
            <div class="qr-stat-bar-fill" :style="{ width: comp.score + '%' }" />
          </div>
          <div class="qr-stat-sub">权重 {{ (comp.weight * 100).toFixed(0) }}% · 贡献 {{ num(comp.contribution) }}</div>
        </div>
        <div class="qr-stat">
          <div class="qr-stat-label">Regex 占比</div>
          <div class="qr-stat-value">{{ pct(report.provenance.method_breakdown.find(m => m.extract_method === 'regex')?.rate ?? null) }}</div>
          <div class="qr-stat-bar">
            <div class="qr-stat-bar-fill qr-stat-bar-fill--teal"
              :style="{ width: ((report.provenance.method_breakdown.find(m => m.extract_method === 'regex')?.rate ?? 0) * 100) + '%' }" />
          </div>
          <div class="qr-stat-sub">
            Warning {{ report.issues.summary.warning_count }} / Error {{ report.issues.summary.error_count }}
          </div>
        </div>
      </div>

      <!-- Charts 2x2 grid -->
      <div class="qr-chart-grid">
        <div class="qr-chart-card">
          <div class="qr-chart-title">评分维度</div>
          <div ref="scoreChartRef" class="qr-chart-body" />
        </div>
        <div class="qr-chart-card">
          <div class="qr-chart-title">各表填充率</div>
          <div ref="fillRateChartRef" class="qr-chart-body" />
        </div>
        <div class="qr-chart-card">
          <div class="qr-chart-title">提取方式占比</div>
          <div ref="methodChartRef" class="qr-chart-body" />
        </div>
        <div class="qr-chart-card">
          <div class="qr-chart-title">问题类型分布</div>
          <div ref="issueChartRef" class="qr-chart-body" />
        </div>
      </div>

      <!-- Interpretation -->
      <div class="qr-interp-grid">
        <div class="qr-interp-card qr-interp-card--success">
          <div class="qr-interp-header">
            <span class="qr-interp-icon">&#9650;</span>
            优势
          </div>
          <ul class="qr-interp-list">
            <li v-for="(item, i) in report.interpretation.strengths" :key="i">{{ item }}</li>
          </ul>
        </div>
        <div class="qr-interp-card qr-interp-card--warn">
          <div class="qr-interp-header">
            <span class="qr-interp-icon">&#9888;</span>
            风险
          </div>
          <ul class="qr-interp-list">
            <li v-for="(item, i) in report.interpretation.risks" :key="i">{{ item }}</li>
          </ul>
        </div>
        <div class="qr-interp-card qr-interp-card--accent">
          <div class="qr-interp-header">
            <span class="qr-interp-icon">&#10148;</span>
            优先动作
          </div>
          <ul class="qr-interp-list">
            <li v-for="(item, i) in report.interpretation.actions" :key="i">{{ item }}</li>
          </ul>
        </div>
      </div>

      <!-- History reports -->
      <div class="qr-history" v-if="historyList.length">
        <div class="qr-history-title">历史报告</div>
        <div v-for="item in historyList" :key="item.name" class="qr-history-item">
          <div class="qr-history-row" @click="toggleReport(item.name)">
            <span class="qr-history-arrow" :class="{ 'qr-history-arrow--open': expandedReport === item.name }">▶</span>
            <span class="qr-history-ts">{{ formatTs(item.timestamp) }}</span>
            <span class="qr-history-charts">{{ item.charts.length }} 张图表</span>
          </div>
          <div v-if="expandedReport === item.name && item.charts.length" class="qr-history-gallery">
            <div v-for="chart in item.charts" :key="chart" class="qr-history-chart">
              <div class="qr-history-chart-label">{{ chart.replace('.svg', '') }}</div>
              <img :src="getQualityReportChartUrl(item.name, chart)" :alt="chart" class="qr-history-chart-img" />
            </div>
          </div>
          <div v-if="expandedReport === item.name && item.charts.length === 0" class="qr-history-empty">暂无图表</div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.qr-panel {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* ── Error ── */
.qr-error {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  border-radius: var(--radius-lg);
  color: var(--danger);
  font-size: 13px;
}
.qr-retry-btn {
  padding: 6px 14px;
  border: 1px solid var(--danger-border);
  background: #fff;
  color: var(--danger);
  border-radius: var(--radius-md);
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}
.qr-retry-btn:hover { background: var(--danger-bg); }

/* ── Toolbar ── */
.qr-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}
.qr-toolbar-left { display: flex; align-items: center; gap: 10px; }
.qr-toolbar-right { display: flex; align-items: center; gap: 8px; }
.qr-gen-time {
  font-size: 12px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}
.qr-gen-btn {
  padding: 7px 16px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-primary);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.qr-gen-btn:hover { border-color: var(--accent-border); color: var(--accent); }
.qr-gen-btn:disabled { opacity: 0.5; cursor: default; }
.qr-refresh-btn {
  width: 32px;
  height: 32px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-primary);
  background: var(--bg-secondary);
  color: var(--text-tertiary);
  cursor: pointer;
  display: grid;
  place-items: center;
  transition: all 0.15s ease;
}
.qr-refresh-btn:hover { background: var(--bg-tertiary); color: var(--text-secondary); }
.spinning { animation: qr-spin 0.8s linear infinite; }
@keyframes qr-spin { to { transform: rotate(360deg); } }

/* ── Hero ── */
.qr-hero {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 24px;
  align-items: center;
  background: var(--bg-secondary);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-xl);
  padding: 24px 28px;
  box-shadow: var(--shadow-sm);
}
.qr-score-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  min-width: 160px;
}
.qr-score-ring-wrap {
  position: relative;
  width: 120px;
  height: 120px;
}
.qr-score-ring { width: 100%; height: 100%; }
.qr-score-text {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}
.qr-score-num {
  font-size: 28px;
  font-weight: 700;
  color: var(--text-primary);
  font-family: var(--font-display);
  line-height: 1;
}
.qr-score-grade {
  font-size: 14px;
  font-weight: 600;
  margin-top: 4px;
}
.qr-score-note {
  font-size: 11px;
  color: var(--text-tertiary);
  text-align: center;
  max-width: 160px;
}
.qr-overview-text {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.qr-overview-title {
  font-size: 15px;
  color: var(--text-primary);
  line-height: 1.7;
  font-weight: 500;
}
.qr-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.qr-chip {
  padding: 5px 12px;
  border-radius: 999px;
  background: var(--bg-inset);
  border: 1px solid var(--border-secondary);
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 500;
}
.qr-chip--accent { background: var(--accent-bg); color: var(--accent-text); border-color: var(--accent-border); }
.qr-chip--success { background: var(--success-bg); color: var(--success); border-color: var(--success-border); }
.qr-chip--warn { background: var(--warning-bg); color: var(--warning); border-color: var(--warning-border); }

/* ── Stats ── */
.qr-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.qr-stat {
  background: var(--bg-secondary);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-lg);
  padding: 16px 18px;
  box-shadow: var(--shadow-xs);
}
.qr-stat-label {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-bottom: 6px;
  font-weight: 500;
}
.qr-stat-value {
  font-size: 24px;
  font-weight: 700;
  color: var(--text-primary);
  font-family: var(--font-display);
}
.qr-stat-bar {
  height: 6px;
  border-radius: 999px;
  background: var(--bg-inset);
  margin: 10px 0 8px;
  overflow: hidden;
}
.qr-stat-bar-fill {
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--accent), var(--violet));
  transition: width 0.8s ease;
}
.qr-stat-bar-fill--teal {
  background: linear-gradient(90deg, var(--teal), var(--success));
}
.qr-stat-sub {
  font-size: 11px;
  color: var(--text-tertiary);
}

/* ── Charts ── */
.qr-chart-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.qr-chart-card {
  background: var(--bg-secondary);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-lg);
  padding: 18px;
  box-shadow: var(--shadow-xs);
}
.qr-chart-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 12px;
}
.qr-chart-body {
  width: 100%;
  height: 200px;
}

/* ── Interpretation ── */
.qr-interp-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}
.qr-interp-card {
  background: var(--bg-secondary);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-lg);
  padding: 18px;
  box-shadow: var(--shadow-xs);
}
.qr-interp-card--success { border-left: 3px solid var(--success); }
.qr-interp-card--warn { border-left: 3px solid var(--warning); }
.qr-interp-card--accent { border-left: 3px solid var(--accent); }
.qr-interp-header {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.qr-interp-card--success .qr-interp-icon { color: var(--success); }
.qr-interp-card--warn .qr-interp-icon { color: var(--warning); }
.qr-interp-card--accent .qr-interp-icon { color: var(--accent); }
.qr-interp-list {
  margin: 0;
  padding-left: 16px;
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.8;
}
.qr-interp-list li {
  margin-bottom: 6px;
}

/* ── History ── */
.qr-history {
  background: var(--bg-secondary);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-lg);
  padding: 18px;
  box-shadow: var(--shadow-xs);
}
.qr-history-title {
  font-size: 14px; font-weight: 600; color: var(--text-primary); margin-bottom: 12px;
}
.qr-history-item {
  border-bottom: 1px solid var(--border-secondary);
}
.qr-history-item:last-child { border-bottom: none; }
.qr-history-row {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 4px; cursor: pointer; transition: background 0.12s;
}
.qr-history-row:hover { background: var(--bg-tertiary); border-radius: var(--radius-sm); }
.qr-history-arrow {
  font-size: 10px; color: var(--text-tertiary); transition: transform 0.15s;
}
.qr-history-arrow--open { transform: rotate(90deg); }
.qr-history-ts {
  font-size: 13px; color: var(--text-primary); font-family: var(--font-mono); font-weight: 500;
}
.qr-history-charts {
  font-size: 11px; color: var(--text-tertiary); margin-left: auto;
}
.qr-history-gallery {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px;
  padding: 8px 4px 14px;
}
.qr-history-chart {
  border: 1px solid var(--border-secondary); border-radius: var(--radius-md);
  overflow: hidden; background: #fff;
}
.qr-history-chart-label {
  padding: 6px 10px; font-size: 11px; font-weight: 600;
  color: var(--text-tertiary); background: var(--bg-tertiary); border-bottom: 1px solid var(--border-secondary);
}
.qr-history-chart-img {
  width: 100%; height: auto; display: block; padding: 8px;
}
.qr-history-empty {
  padding: 12px 4px; font-size: 12px; color: var(--text-tertiary);
}

/* ── Responsive ── */
@media (max-width: 1100px) {
  .qr-hero { grid-template-columns: 1fr; }
  .qr-stats { grid-template-columns: repeat(2, 1fr); }
  .qr-chart-grid { grid-template-columns: 1fr; }
  .qr-interp-grid { grid-template-columns: 1fr; }
}
</style>
