<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import type { ProvenanceRecord } from '../types'
import { getPdfPageImageUrl } from '../services/api'

const props = defineProps<{
  records: ProvenanceRecord[]
  /** 被点击的角标序号（由父组件传入），自动定位到对应的溯源记录 */
  activeCiteIndex?: number | null
  /** 若从计算溯源面板跳入，显示一个返回按钮 */
  backLabel?: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'back'): void
}>()

const FIELD_LABELS: Record<string, string> = {
  total_operating_revenue: '营业总收入',
  operating_revenue_yoy_growth: '营收同比增长',
  net_profit: '净利润',
  net_profit_10k_yuan: '净利润',
  net_profit_yoy_growth: '净利润同比增长',
  total_profit: '利润总额',
  operating_profit: '营业利润',
  operating_expense_cost_of_sales: '营业成本',
  operating_expense_selling_expenses: '销售费用',
  operating_expense_administrative_expenses: '管理费用',
  operating_expense_financial_expenses: '财务费用',
  operating_expense_rnd_expenses: '研发费用',
  other_income: '其他收益',
  total_operating_expenses: '营业总成本',
  asset_cash_and_cash_equivalents: '货币资金',
  asset_accounts_receivable: '应收账款',
  asset_inventory: '存货',
  asset_total_assets: '总资产',
  liability_total_liabilities: '负债合计',
  asset_liability_ratio: '资产负债率',
  equity_total_equity: '股东权益合计',
  eps: '每股收益',
  net_asset_per_share: '每股净资产',
  roe: '净资产收益率',
  operating_cf_per_share: '每股经营现金流',
  net_profit_excl_non_recurring: '扣非净利润',
  gross_profit_margin: '毛利率',
  net_profit_margin: '净利率',
  net_cash_flow: '净现金流',
  operating_cf_net_amount: '经营活动现金流净额',
  investing_cf_net_amount: '投资活动现金流净额',
  financing_cf_net_amount: '筹资活动现金流净额',
}

const TABLE_LABELS: Record<string, string> = {
  core_performance_indicators_sheet: '核心业绩指标表',
  income_sheet: '利润表',
  balance_sheet: '资产负债表',
  cash_flow_sheet: '现金流量表',
}

const fieldLabel = (f: string) => FIELD_LABELS[f] || f
const tableLabel = (t: string) => TABLE_LABELS[t] || t

/** 按来源表分组，同时给每条记录一个全局序号 */
const grouped = computed(() => {
  const map = new Map<string, (ProvenanceRecord & { _idx: number })[]>()
  let idx = 0
  for (const rec of props.records) {
    const key = rec.table_name
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push({ ...rec, _idx: idx++ })
  }
  return map
})

// ── PDF 预览状态 ──
const previewFile = ref('')
const previewPage = ref(1)
const previewHighlight = ref('')
const previewLoading = ref(false)
const previewError = ref('')
const previewTitle = ref('')

const previewUrl = ref('')

const buildKbHighlight = (rec: ProvenanceRecord): string => {
  const candidates: string[] = []
  const pushCandidate = (value: string, maxLen = 30) => {
    const clean = value.replace(/\s+/g, '').trim()
    if (clean.length < 4) return
    const clipped = clean.slice(0, maxLen)
    if (!candidates.includes(clipped)) candidates.push(clipped)
  }

  // 优先：从 source_text（后端传来的完整证据文本）按标点分句提取长短语
  if (rec.source_text) {
    const text = rec.source_text
    const phrases = text
      .split(/[，。；：、,.!?！？\n\r]/)
      .map(part => part.replace(/\s+/g, '').trim())
      .filter(part => part.length >= 4)
    for (const phrase of phrases.slice(0, 5)) pushCandidate(phrase, 30)
  }

  // 兜底：field_value 和 CJK 连续片段
  if (candidates.length < 2) {
    if (typeof rec.field_value === 'string') pushCandidate(rec.field_value)
    if (rec.source_text) {
      const cjkRuns = rec.source_text.match(/[\u4e00-\u9fff]{4,30}/g) ?? []
      for (const run of cjkRuns.slice(0, 3)) pushCandidate(run)
    }
  }

  return candidates.slice(0, 5).join('|')
}

const buildPreviewHighlight = (rec: ProvenanceRecord): string => {
  if (rec.extract_method === 'kb') return buildKbHighlight(rec)
  // calculated 记录：field_value 是万元制，PDF 原文是元。从 source_table_name（row_label）里提取原始数字
  if (rec.extract_method === 'calculated' && rec.source_table_name) {
    const numMatch = rec.source_table_name.match(/[\-\d,]+\.\d{2}/)
    if (numMatch) return numMatch[0].replace(/,/g, '')
  }
  const val = rec.field_value != null ? String(rec.field_value).trim() : ''
  return val || FIELD_LABELS[rec.field_name] || ''
}

/** 通过 fetch 加载 PDF 页面图片，读取 X-Actual-Page 头自动跳转到正确页 */
let _loadSeq = 0
const loadPreviewImage = async () => {
  if (!previewFile.value) { previewUrl.value = ''; return }
  // 先释放旧图片，避免 revoke 触发 <img> error 干扰新加载
  const oldUrl = previewUrl.value
  previewUrl.value = ''
  previewError.value = ''
  if (oldUrl) URL.revokeObjectURL(oldUrl)
  const seq = ++_loadSeq
  const url = getPdfPageImageUrl(previewFile.value, previewPage.value, previewHighlight.value, 200)
  try {
    const resp = await fetch(url)
    if (seq !== _loadSeq) return  // 被更新的请求取代，丢弃旧结果
    if (!resp.ok) { previewLoading.value = false; previewError.value = `PDF 加载失败 (${resp.status})`; return }
    const actualPage = resp.headers.get('X-Actual-Page')
    if (actualPage) {
      const ap = parseInt(actualPage, 10)
      if (!isNaN(ap) && ap !== previewPage.value) {
        previewPage.value = ap
      }
    }
    const blob = await resp.blob()
    if (seq !== _loadSeq) return
    previewError.value = ''
    previewUrl.value = URL.createObjectURL(blob)
    previewLoading.value = false
  } catch (err) {
    if (seq !== _loadSeq) return
    previewLoading.value = false
    previewError.value = 'PDF 页面渲染失败'
    console.error('[ProvenancePanel] loadPreviewImage error:', err)
  }
}

const activeRecordId = ref<number | null>(null)

const openPreview = (rec: ProvenanceRecord) => {
  if (!rec.source_file) return
  const page = rec.source_page || 1  // KB 记录可能无 page，默认第1页
  // 重复点击同一条记录时不重新加载，避免卡在"渲染中"
  if (activeRecordId.value === rec.id) return
  activeRecordId.value = rec.id
  previewFile.value = rec.source_file
  previewPage.value = page
  previewHighlight.value = buildPreviewHighlight(rec)
  previewError.value = ''
  previewLoading.value = true
  // 提取文件名作为标题
  const parts = rec.source_file.replace(/\\/g, '/').split('/')
  previewTitle.value = parts[parts.length - 1] || rec.source_file
  // 通过 fetch 加载（支持自动跳页）
  loadPreviewImage()
}

const onImgLoad = () => { /* loading handled by loadPreviewImage */ }
const onImgError = () => {
  // loadPreviewImage 已处理所有错误状态；此处仅用于 img 标签的 @error 回调
  // 空 URL 或 revoked blob URL 触发的 error 直接忽略
}

const prevPage = () => {
  if (previewPage.value > 1) {
    previewPage.value--
    previewLoading.value = true
    previewHighlight.value = '' // 手动翻页不高亮
    loadPreviewImage()
  }
}
const nextPage = () => {
  previewPage.value++
  previewLoading.value = true
  previewHighlight.value = '' // 手动翻页不高亮
  loadPreviewImage()
}

// 当外部传入的 activeCiteIndex 变化时，自动打开第一条记录
watch(() => props.activeCiteIndex, (idx) => {
  if (idx != null && props.records.length) {
    // 点击第 N 个角标 → 直接打开第 N 条（或第一条）溯源记录
    const target = props.records[Math.min(idx, props.records.length - 1)]
    if (target) openPreview(target)
  }
}, { immediate: true })
</script>

<template>
  <div class="sp-panel">
    <!-- 顶栏 -->
    <div class="sp-header">
      <div class="sp-header-left">
        <button v-if="props.backLabel" class="sp-back-btn" @click="emit('back')">
          ← {{ props.backLabel }}
        </button>
        <span class="sp-icon">📄</span>
        <span class="sp-title">数据溯源</span>
      </div>
      <button class="sp-close" @click="emit('close')">✕</button>
    </div>

    <!-- PDF 预览区 -->
    <div v-if="previewFile" class="sp-pdf-area">
      <div class="sp-pdf-toolbar">
        <span class="sp-pdf-name" :title="previewTitle">{{ previewTitle }}</span>
        <div class="sp-pdf-nav">
          <button class="sp-nav-btn" @click="prevPage" :disabled="previewPage <= 1">◀</button>
          <span class="sp-page-num">第 {{ previewPage }} 页</span>
          <button class="sp-nav-btn" @click="nextPage">▶</button>
        </div>
      </div>
      <div class="sp-pdf-scroll">
        <div v-if="previewLoading" class="sp-pdf-loading">
          <span class="sp-spin" /><span>渲染中…</span>
        </div>
        <div v-if="previewError" class="sp-pdf-error">{{ previewError }}</div>
        <img
          v-show="!previewError && previewUrl"
          :src="previewUrl"
          class="sp-pdf-img"
          @load="onImgLoad"
          @error="onImgError"
          alt="PDF 页面预览"
        />
      </div>
    </div>

    <!-- 未选中时：溯源列表 -->
    <div v-else class="sp-list-area">
      <template v-for="[tbl, recs] in grouped" :key="tbl">
        <div class="sp-group-title">{{ tableLabel(tbl) }}</div>
        <div
          v-for="rec in recs"
          :key="rec.id"
          :class="['sp-card', { active: activeRecordId === rec.id }]"
          @click="openPreview(rec)"
        >
          <div class="sp-card-top">
            <span v-if="rec.extract_method === 'kb'" class="sp-field-badge sp-kb-badge">{{ rec.field_name }}</span>
            <span v-else class="sp-field-badge">{{ fieldLabel(rec.field_name) }}</span>
            <span v-if="rec.extract_method === 'calculated'" class="sp-page-tag sp-calc-tag">计算</span>
            <span v-else-if="rec.extract_method === 'kb'" class="sp-page-tag sp-kb-tag">研报 · 第 {{ rec.source_page || 1 }} 页</span>
            <span v-else-if="rec.source_page" class="sp-page-tag">第 {{ rec.source_page }} 页</span>
            <span v-if="rec.confidence" class="sp-conf">{{ (rec.confidence * 100).toFixed(0) }}%</span>
          </div>
          <div v-if="rec.extract_method === 'calculated'" class="sp-snippet sp-formula">公式：{{ rec.source_text }}</div>
          <div v-else-if="rec.source_text" class="sp-snippet">{{ rec.source_text }}</div>
          <div class="sp-card-foot">
            <span class="sp-method">{{ rec.extract_method === 'regex' ? '正则提取' : rec.extract_method === 'llm' ? 'LLM提取' : rec.extract_method === 'calculated' ? '公式计算' : rec.extract_method === 'kb' ? '研报知识库' : rec.extract_method }}</span>
            <span v-if="rec.extract_method !== 'calculated'" class="sp-view-link">点击查看原文 →</span>
          </div>
        </div>
      </template>
    </div>

    <!-- 底部溯源索引（PDF预览时显示，方便切换） -->
    <div v-if="previewFile" class="sp-index-bar">
      <div class="sp-index-scroll">
        <template v-for="[tbl, recs] in grouped" :key="tbl">
          <button
            v-for="rec in recs"
            :key="rec.id"
            :class="['sp-index-chip', { active: activeRecordId === rec.id }]"
            @click="openPreview(rec)"
            :title="fieldLabel(rec.field_name) + (rec.source_page ? ` · 第${rec.source_page}页` : '')"
          >{{ fieldLabel(rec.field_name) }}</button>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sp-panel {
  display: flex; flex-direction: column; height: 100%;
  background: #fff; border-left: 1px solid #e5e7eb;
}

/* Header */
.sp-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid #f0f0f0; flex-shrink: 0;
}
.sp-header-left { display: flex; align-items: center; gap: 6px; }
.sp-icon { font-size: 16px; }
.sp-title { font-size: 14px; font-weight: 700; color: #111; }
.sp-close {
  width: 26px; height: 26px; border-radius: 50%; border: 1px solid #e5e7eb;
  background: #f9f9fb; color: #666; font-size: 13px; cursor: pointer;
  display: grid; place-items: center; transition: all 0.14s;
}
.sp-close:hover { background: #fee2e2; border-color: #fca5a5; color: #ef4444; }
.sp-back-btn {
  padding: 3px 10px; border-radius: 6px; border: 1px solid #c7d2fe;
  background: #eef2ff; color: #4338ca; font-size: 11px; font-weight: 600;
  cursor: pointer; transition: all 0.14s; white-space: nowrap;
}
.sp-back-btn:hover { background: #e0e7ff; border-color: #818cf8; }

/* PDF area */
.sp-pdf-area { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.sp-pdf-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 14px; border-bottom: 1px solid #f0f0f0; flex-shrink: 0;
  background: #f8fafc;
}
.sp-pdf-name {
  font-size: 12px; color: #6366f1; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 200px;
}
.sp-pdf-nav { display: flex; align-items: center; gap: 8px; }
.sp-nav-btn {
  padding: 3px 10px; border-radius: 5px; border: 1px solid #d1d5db;
  background: #fff; color: #374151; font-size: 11px; cursor: pointer;
  transition: all 0.12s;
}
.sp-nav-btn:hover:not(:disabled) { background: #eef2ff; border-color: #6366f1; color: #6366f1; }
.sp-nav-btn:disabled { opacity: 0.35; cursor: default; }
.sp-page-num { font-size: 12px; font-weight: 600; color: #374151; }

.sp-pdf-scroll { flex: 1; overflow: auto; padding: 8px; background: #f1f5f9; position: relative; }
.sp-pdf-img { width: 100%; height: auto; border-radius: 4px; box-shadow: 0 1px 8px rgba(0,0,0,0.06); }
.sp-pdf-loading {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  gap: 6px; font-size: 12px; color: #6366f1; background: rgba(255,255,255,0.85);
}
.sp-spin {
  width: 14px; height: 14px; border-radius: 50%;
  border: 2px solid #c7d2fe; border-top-color: #6366f1;
  animation: spSpin 0.7s linear infinite; display: inline-block;
}
@keyframes spSpin { to { transform: rotate(360deg); } }
.sp-pdf-error { font-size: 12px; color: #ef4444; text-align: center; padding: 20px; }

/* List area */
.sp-list-area {
  flex: 1; overflow-y: auto; padding: 12px 14px;
  display: flex; flex-direction: column; gap: 6px;
}
.sp-list-area::-webkit-scrollbar { width: 4px; }
.sp-list-area::-webkit-scrollbar-thumb { background: #e5e7eb; border-radius: 4px; }

.sp-group-title {
  font-size: 11px; font-weight: 700; color: #6366f1;
  padding: 6px 0 3px; margin-top: 2px;
  border-bottom: 1px solid #eef2ff;
}
.sp-card {
  border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px 10px;
  cursor: pointer; transition: all 0.14s; background: #fafafa;
  display: flex; flex-direction: column; gap: 4px;
}
.sp-card:hover { border-color: #a5b4fc; background: #f5f3ff; }
.sp-card.active { border-color: #6366f1; background: #eef2ff; }

.sp-card-top { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
.sp-field-badge {
  font-size: 11px; font-weight: 600; color: #4338ca;
  background: #e0e7ff; padding: 1px 7px; border-radius: 5px;
}
.sp-page-tag {
  font-size: 10px; color: #059669; background: #d1fae5;
  padding: 1px 6px; border-radius: 4px; font-weight: 600;
}
.sp-calc-tag { background: #fef3c7; color: #b45309; }
.sp-kb-badge { color: #0369a1; background: #e0f2fe; }
.sp-kb-tag { color: #0369a1; background: #e0f2fe; }
.sp-conf { font-size: 10px; color: #6366f1; font-weight: 600; margin-left: auto; }
.sp-snippet {
  font-size: 11px; color: #4b5563; line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden;
}
.sp-formula { color: #b45309; font-weight: 500; }
.sp-card-foot { display: flex; justify-content: space-between; align-items: center; }
.sp-method { font-size: 10px; color: #9ca3af; }
.sp-view-link { font-size: 10px; color: #6366f1; font-weight: 500; }

/* Bottom index bar */
.sp-index-bar {
  border-top: 1px solid #f0f0f0; padding: 8px 12px; flex-shrink: 0; background: #fafafa;
}
.sp-index-scroll {
  display: flex; gap: 6px; overflow-x: auto; padding-bottom: 2px;
}
.sp-index-scroll::-webkit-scrollbar { height: 3px; }
.sp-index-scroll::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 3px; }
.sp-index-chip {
  flex-shrink: 0; padding: 3px 10px; border-radius: 12px;
  border: 1px solid #e5e7eb; background: #fff; color: #4b5563;
  font-size: 11px; cursor: pointer; transition: all 0.12s; white-space: nowrap;
}
.sp-index-chip:hover { border-color: #a5b4fc; color: #4338ca; background: #f5f3ff; }
.sp-index-chip.active { border-color: #6366f1; background: #eef2ff; color: #4338ca; font-weight: 600; }
</style>
