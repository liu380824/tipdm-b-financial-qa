<script setup lang="ts">
defineOptions({ name: 'AssistantView' })
import { ref, computed, nextTick, watch, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Promotion, CopyDocument, EditPen, RefreshRight, Delete } from '@element-plus/icons-vue'
import ChartPanel from '../components/ChartPanel.vue'
import CalcTracePanel from '../components/CalcTracePanel.vue'
import ProvenancePanel from '../components/ProvenancePanel.vue'
import ThinkingMap from '../components/ThinkingMap.vue'
import { promptSuggestions } from '../data/ui'
import { askAssistantStream, fetchSystemSettings, fetchTask2Questions, fetchTask3Questions, isAbortError } from '../services/api'
import type { StreamStep } from '../services/api'
import { useChatStore } from '../store/chat'
import type { Attribution, CalcOperand, CalcTrace, ChatMessage, ProvenanceRecord } from '../types'

const chat = useChatStore()
const messages = computed(() => chat.activeConv?.messages ?? [])

const uid = () => `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`

const draft = ref('')
const busy = ref(false)
const showSql = ref(true)
const showChart = ref(true)
const enableAttribution = ref(true)
const enableContext = ref(true)
const deepThink = ref(true)
const task2QuestionCount = ref(0)
const task3QuestionCount = ref(0)
const streamRef = ref<HTMLElement>()
const textareaRef = ref<HTMLTextAreaElement>()
const abortController = ref<AbortController | null>(null)

const openBatchTask2 = () => {
  window.dispatchEvent(new Event('assistant-open-batch-task2'))
}

const openBatchTask3 = () => {
  window.dispatchEvent(new Event('assistant-open-batch-task3'))
}

const scrollToBottom = () =>
  nextTick(() => {
    if (streamRef.value) streamRef.value.scrollTop = streamRef.value.scrollHeight
  })

watch(() => messages.value.length, scrollToBottom)
watch(() => chat.activeId, () => nextTick(scrollToBottom))

const autoResize = () => {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
}

const send = async (text?: string) => {
  const content = (text ?? draft.value).trim()
  if (!content || busy.value) return
  const conv = chat.activeConv
  if (!conv) return
  if (conv.title === '新对话')
    conv.title = content.slice(0, 20) + (content.length > 20 ? '…' : '')
  conv.messages.push({ id: uid(), role: 'user', content })
  draft.value = ''
  nextTick(autoResize)
  busy.value = true
  thinkingSteps.value = []
  thinkingTexts.value = []
  thinkingStartTime.value = Date.now()
  const controller = new AbortController()
  abortController.value = controller
  const aiMsgId = uid()
  const aiMsg = { id: aiMsgId, role: 'assistant' as const, content: '' }
  conv.messages.push(aiMsg)
  streamingMsgId.value = aiMsgId
  scrollToBottom()
  try {
    const reply = await askAssistantStream(
      {
        question: content,
        history: conv.messages,
        auto_chart: showChart.value,
        enable_context: enableContext.value,
        deep_think: deepThink.value,
        session_id: conv.id
      },
      (step: StreamStep) => {
        const existing = thinkingSteps.value.find(s => s.label === step.label)
        if (existing) {
          existing.status = step.status
          existing.detail = step.detail
        } else {
          thinkingSteps.value.push({ ...step })
        }
        scrollToBottom()
      },
      (chunk: string) => {
        const msg = conv.messages.find(m => m.id === aiMsgId)
        if (msg) msg.content += chunk
        scrollToBottom()
      },
      (chart) => {
        const msg = conv.messages.find(m => m.id === aiMsgId)
        if (msg) msg.chart = chart
        scrollToBottom()
      },
      (tableData) => {
        const msg = conv.messages.find(m => m.id === aiMsgId)
        if (msg) {
          msg.columns = tableData.columns
          msg.rows = tableData.rows
        }
        scrollToBottom()
      },
      controller.signal,
      (text: string) => {
        thinkingTexts.value.push(text)
        scrollToBottom()
      }
    )
    const thinkDuration = ((Date.now() - thinkingStartTime.value) / 1000).toFixed(1)
    const msg = conv.messages.find(m => m.id === aiMsgId)
    if (msg) {
      Object.assign(msg, { ...reply, id: aiMsgId })
      ;(msg as any).thinkingTexts = [...thinkingTexts.value]
      ;(msg as any).thinkingDuration = thinkDuration
    }
    thinkingSteps.value = []
    thinkingTexts.value = []
    streamingMsgId.value = null
  } catch (err) {
    const errDuration = ((Date.now() - thinkingStartTime.value) / 1000).toFixed(1)
    if (isAbortError(err)) {
      const msg = conv.messages.find(m => m.id === aiMsgId)
      if (msg) {
        if (!msg.content) msg.content = '（已停止生成）'
        msg.status = 'done'
        msg.aborted = true
        ;(msg as any).thinkingTexts = [...thinkingTexts.value]
        ;(msg as any).thinkingDuration = errDuration
      }
    } else {
      const msg = conv.messages.find(m => m.id === aiMsgId)
      if (msg) {
        msg.content = `请求失败：${err instanceof Error ? err.message : '未知错误'}`
        ;(msg as any).thinkingTexts = [...thinkingTexts.value]
        ;(msg as any).thinkingDuration = errDuration
      }
      ElMessage.error(err instanceof Error ? err.message : '接口调用失败')
    }
    thinkingSteps.value = []
    thinkingTexts.value = []
    streamingMsgId.value = null
  } finally {
    busy.value = false
    abortController.value = null
  }
}

const stopGeneration = () => {
  if (abortController.value) {
    abortController.value.abort()
    abortController.value = null
  }
}

const handleEnter = (e: KeyboardEvent) => {
  if (e.shiftKey) return
  e.preventDefault()
  send()
}

const showWelcome = computed(() => !messages.value.some(m => m.role === 'user'))

const thinkingSteps = ref<StreamStep[]>([])
const thinkingTexts = ref<string[]>([])
const thinkingStartTime = ref<number>(0)
const streamingMsgId = ref<string | null>(null)
const hoveredMsgId = ref<string | null>(null)

// ── 消息操作（豆包风格） ──
const copyText = (text?: string) => {
  if (!text) return
  navigator.clipboard.writeText(text)
    .then(() => ElMessage({ message: '已复制到剪贴板', type: 'success', duration: 1500 }))
    .catch(() => ElMessage.error('复制失败'))
}
const editUserMsg = (text?: string) => {
  if (!text) return
  draft.value = text
  nextTick(() => { textareaRef.value?.focus(); autoResize() })
}
const deleteMsgPair = (msgId: string) => {
  const conv = chat.activeConv
  if (!conv) return
  const idx = conv.messages.findIndex(m => m.id === msgId)
  if (idx < 0) return
  let start = idx, count = 1
  if (conv.messages[idx].role === 'user' && idx + 1 < conv.messages.length && conv.messages[idx + 1].role === 'assistant') count = 2
  if (conv.messages[idx].role === 'assistant' && idx > 0 && conv.messages[idx - 1].role === 'user') { start = idx - 1; count = 2 }
  conv.messages.splice(start, count)
}
const regenerateAiMsg = (msgId: string) => {
  const conv = chat.activeConv
  if (!conv || busy.value) return
  const idx = conv.messages.findIndex(m => m.id === msgId)
  if (idx <= 0 || conv.messages[idx - 1].role !== 'user') return
  const q = conv.messages[idx - 1].content
  conv.messages.splice(idx - 1, 2)
  send(q)
}

const showAttribution = ref(false)
const activeAttribution = ref<Attribution | null>(null)

const showThinkingMapFull = ref(false)
const openAttribution = (attr: Attribution) => {
  activeAttribution.value = attr
  showAttribution.value = true
}
const closeAttribution = () => {
  showAttribution.value = false
}

const showProvenance = ref(false)
const activeProvenance = ref<ProvenanceRecord[]>([])
const activeCiteIndex = ref<number | null>(null)

const openProvenance = (records: ProvenanceRecord[], citeIdx: number | null = null) => {
  activeProvenance.value = records
  // 先重置再赋值，确保 watcher 在重复点击相同序号时也能触发
  activeCiteIndex.value = null
  nextTick(() => { activeCiteIndex.value = citeIdx })
  showProvenance.value = true
}
const closeProvenance = () => {
  showProvenance.value = false
  activeCiteIndex.value = null
  provenanceBackLabel.value = ''
  pendingCalcTrace.value = null
}

const showCalcTrace = ref(false)
const activeCalcTrace = ref<CalcTrace | null>(null)

const openCalcTrace = (trace: CalcTrace) => {
  showProvenance.value = false
  activeCalcTrace.value = trace
  showCalcTrace.value = true
}

const closeCalcTrace = () => {
  showCalcTrace.value = false
  activeCalcTrace.value = null
}

const provenanceBackLabel = ref('')
const pendingCalcTrace = ref<CalcTrace | null>(null)

const openCalcOperandEvidence = (operand: CalcOperand) => {
  const records = operand.evidences ?? []
  if (!records.length) return
  // 保留 trace 上下文以便返回
  pendingCalcTrace.value = activeCalcTrace.value
  provenanceBackLabel.value = '返回计算溯源'
  showCalcTrace.value = false
  openProvenance(records, 0)
}

const handleProvenanceBack = () => {
  showProvenance.value = false
  provenanceBackLabel.value = ''
  if (pendingCalcTrace.value) {
    activeCalcTrace.value = pendingCalcTrace.value
    showCalcTrace.value = true
    pendingCalcTrace.value = null
  }
}

const findCalcTraceForRecord = (msg: ChatMessage, record: ProvenanceRecord): CalcTrace | null => {
  const calcTraceId = msg.citationRefs?.find(ref => ref.provenanceId === record.id && ref.type === 'calculation')?.calcTraceId
  if (calcTraceId) {
    return msg.calcTraces?.find(trace => trace.id === calcTraceId) ?? null
  }
  return msg.calcTraces?.find(trace => trace.provenanceId === record.id) ?? null
}

/** 轻量 Markdown 渲染：小标题、加粗、编号列表、段落 */
const renderMarkdown = (raw: string): string => {
  if (!raw) return ''
  const escaped = raw
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const lines = escaped.split('\n')
  const html: string[] = []
  let inParagraph = false
  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed) {
      if (inParagraph) { html.push('</p>'); inParagraph = false }
      continue
    }
    const boldOnly = trimmed.match(/^\*\*(.+?)\*\*$/)
    if (boldOnly) {
      if (inParagraph) { html.push('</p>'); inParagraph = false }
      html.push(`<div class="md-heading">${boldOnly[1]}</div>`)
      continue
    }
    // 独占一行的短文本（<=20字，不以常见标点结尾）视为小标题
    if (trimmed.length <= 20 && trimmed.length >= 2
        && !/[。，、；：！？,.;:!?）)》」』]$/.test(trimmed)
        && !/^\d+[.、]/.test(trimmed)
        && !/^[-•]/.test(trimmed)) {
      if (inParagraph) { html.push('</p>'); inParagraph = false }
      const inner = trimmed.replace(/\*\*(.+?)\*\*/g, '$1')
      html.push(`<div class="md-heading">${inner}</div>`)
      continue
    }
    const numMatch = trimmed.match(/^(\d+)[.、]\s*(.*)/)
    if (numMatch) {
      if (inParagraph) { html.push('</p>'); inParagraph = false }
      const content = numMatch[2].replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      html.push(`<p class="md-li"><span class="md-li-num">${numMatch[1]}.</span><span class="md-li-content">${content}</span></p>`)
      continue
    }
    const bulletMatch = trimmed.match(/^[-•]\s*(.*)/)
    if (bulletMatch) {
      if (inParagraph) { html.push('</p>'); inParagraph = false }
      const content = bulletMatch[1].replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      html.push(`<p class="md-li"><span class="md-li-dot">·</span><span class="md-li-content">${content}</span></p>`)
      continue
    }
    const styled = trimmed.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    if (!inParagraph) { html.push('<p>'); inParagraph = true }
    else { html.push('<br>') }
    html.push(styled)
  }
  if (inParagraph) html.push('</p>')
  return html.join('')
}

/** 在已渲染的 HTML 中，基于溯源记录的 field_value 直接查表注入角标 */
const injectCitations = (html: string, records: ProvenanceRecord[]): string => {
  if (!records.length) return html

  const wanMap = new Map<string, number>()
  const yiMap = new Map<string, number>()

  for (let i = 0; i < records.length; i++) {
    const fv = records[i].field_value
    if (fv == null) continue
    const val = parseFloat(String(fv))
    if (Number.isNaN(val)) continue

    const vs = String(val)
    wanMap.set(vs, i)
    wanMap.set(val.toFixed(2), i)
    wanMap.set(val.toFixed(1), i)
    if (val === Math.floor(val)) wanMap.set(String(Math.floor(val)), i)

    const yi = val / 10000
    yiMap.set(String(parseFloat(yi.toFixed(4))), i)
    yiMap.set(yi.toFixed(2), i)
    yiMap.set(yi.toFixed(1), i)
    if (yi === Math.floor(yi)) yiMap.set(String(Math.floor(yi)), i)
  }

  let citeNum = 0
  const combined = /(-?[\d,]+\.?\d*)(万元|亿元|%)/g

  return html.replace(combined, (match, num, unit) => {
    if (citeNum >= 20) return match
    const clean = String(num).replace(/,/g, '')

    let recIdx: number | undefined
    if (unit === '亿元') recIdx = yiMap.get(clean)
    else recIdx = wanMap.get(clean)

    if (recIdx != null) {
      citeNum++
      const rec = records[recIdx]
      const label = rec.extract_method === 'calculated' ? '计算' : 'PDF'
      return `${num}${unit}<sup class="cite-sup" data-rec-idx="${recIdx}">${citeNum} ${label}</sup>`
    }
    return match
  })
}

const sanitizeKbCitationContent = (raw: string): string => {
  const blocks = raw.split(/\n{2,}/)
  const cleaned = blocks.filter(block => {
    const citeCount = (block.match(/\[参考\d+\]/g) ?? []).length
    if (citeCount < 4) return true
    const normalized = block.replace(/\s+/g, '')
    return !/(以上信息|上述内容|来源于|数据来源|参考资料|参考文献|资料来源|研报来源|可参考)/.test(normalized)
  })
  return cleaned.join('\n\n').trim()
}

const renderWithCitations = (raw: string, records: ProvenanceRecord[]): string => {
  let html = renderMarkdown(sanitizeKbCitationContent(raw))
  html = injectCitations(html, records)
  let kbSeq = 0
  const kbSeqMap = new Map<string, number>()
  html = html.replace(/\[参考(\d+)\]/g, (_m, origN) => {
    if (!kbSeqMap.has(origN)) kbSeqMap.set(origN, ++kbSeq)
    const displayN = kbSeqMap.get(origN)!
    return ` <sup class="cite-sup kb-cite" data-kb-idx="${origN}">参${displayN} 研报</sup>`
  })
  return html
}

const stripChartTitle = (option: Record<string, any> | undefined): Record<string, any> | undefined => {
  if (!option) return option
  const { title, ...rest } = option
  return { ...rest, title: { text: '' } }
}

/** 点击角标时打开溯源面板（分屏），定位到对应的溯源记录或知识库PDF */
const handleCiteClick = (e: Event, msg: ChatMessage) => {
  const target = e.target as HTMLElement
  if (!target.classList.contains('cite-sup')) return

  // 知识库引用角标
  if (target.classList.contains('kb-cite') && msg.references?.length) {
    const kbIdx = parseInt(target.dataset.kbIdx || '1', 10) - 1
    const ref = msg.references[kbIdx]
    if (!ref) return
    const hlText = ref.highlight_text || ref.text || ''
    const pseudoRec: ProvenanceRecord = {
      id: 90000 + kbIdx,
      table_name: 'kb',
      stock_code: '',
      report_period: '',
      field_name: ref.doc_name || '研报引用',
      field_value: ref.highlight || hlText.slice(0, 24) || null,
      source_file: ref.paper_path || '',
      source_page: ref.page_no ?? 1,
      source_table_name: ref.doc_type || '研报',
      source_text: hlText,
      confidence: ref.score ?? null,
      extract_method: 'kb',
    }
    openProvenance([pseudoRec], 0)
    return
  }

  // 数据溯源角标
  if (msg.provenance?.length) {
    const recIdx = parseInt(target.dataset.recIdx || '0', 10)
    const rec = msg.provenance[recIdx]
    if (!rec) return
    const trace = findCalcTraceForRecord(msg, rec)
    if (trace) {
      openCalcTrace(trace)
      return
    }
    openProvenance(msg.provenance, recIdx)
  }
}

interface ContentSegment {
  type: 'text' | 'chart' | 'table' | 'task'
  text?: string
  taskIndex?: number
  isFirstText?: boolean
}

/** 将内容按 {{CHART}} / {{TABLE}} / {{TASK_N}} 占位符拆分为有序段落
 *  streaming=true 时仅在标记已出现时才渲染，保证严格从上到下顺序
 *  taskResults 传入时启用多意图 TASK_N 占位符解析 */
const parseContentSegments = (content: string, hasChart: boolean, hasTable: boolean, streaming = false, taskResults?: any[]): ContentSegment[] => {
  if (!content) return []
  const segments: ContentSegment[] = []
  const pattern = /\{\{(CHART|TABLE|TASK_(\d+))\}\}/g
  let lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = pattern.exec(content)) !== null) {
    const before = content.slice(lastIndex, match.index).trim()
    if (before) segments.push({ type: 'text', text: before })
    const tag = match[1]
    if (tag === 'CHART' && hasChart) segments.push({ type: 'chart' })
    else if (tag === 'TABLE' && hasTable) segments.push({ type: 'table' })
    else if (tag.startsWith('TASK_') && taskResults) {
      const idx = parseInt(match[2]) - 1
      if (idx >= 0 && idx < taskResults.length) {
        segments.push({ type: 'task', taskIndex: idx })
      }
    }
    lastIndex = pattern.lastIndex
  }
  const tail = content.slice(lastIndex).trim()
  if (tail) segments.push({ type: 'text', text: tail })
  if (!streaming) {
    // 单意图 fallback：LLM 没输出标记时追加到末尾
    const hasChartSeg = segments.some(s => s.type === 'chart')
    const hasTableSeg = segments.some(s => s.type === 'table')
    if (hasChart && !hasChartSeg) segments.push({ type: 'chart' })
    if (hasTable && !hasTableSeg) segments.push({ type: 'table' })
    // 多意图 fallback：LLM 没输出 TASK_N 标记的子任务追加到末尾
    if (taskResults) {
      const referenced = new Set(segments.filter(s => s.type === 'task').map(s => s.taskIndex))
      for (let i = 0; i < taskResults.length; i++) {
        if (!referenced.has(i)) segments.push({ type: 'task', taskIndex: i })
      }
    }
  }
  const firstTextIndex = segments.findIndex(s => s.type === 'text')
  if (firstTextIndex >= 0) segments[firstTextIndex].isFirstText = true
  return segments
}

onMounted(async () => {
  try {
    const settings = await fetchSystemSettings()
    showChart.value = settings.assistant.default_auto_chart
    enableContext.value = settings.assistant.default_enable_context
  } catch {
  }
  try {
    const data = await fetchTask2Questions()
    task2QuestionCount.value = data.count
  } catch {
  }
  try {
    const data3 = await fetchTask3Questions()
    task3QuestionCount.value = data3.count
  } catch {
  }
})

</script>

<template>
  <div :class="['chat-view', { 'split-active': showProvenance }]">
    <header class="topbar">
      <div class="topbar-left">
        <span class="topbar-title">智能对话</span>
      </div>
      <div class="topbar-actions">
        <div v-if="busy" class="thinking-bar">
          <span class="t-dot" /><span class="t-dot" /><span class="t-dot" />
        </div>
        <div class="batch-shortcuts">
          <button class="batch-shortcut batch-shortcut--primary" @click="openBatchTask2">
            <span class="batch-shortcut-icon">⚡</span>
            <span class="batch-shortcut-label">批量回答</span>
            <span class="batch-shortcut-badge">{{ task2QuestionCount || '—' }}</span>
          </button>
          <button class="batch-shortcut" @click="openBatchTask3">
            <span class="batch-shortcut-icon">🧠</span>
            <span class="batch-shortcut-label">增强版</span>
            <span class="batch-shortcut-badge">{{ task3QuestionCount || '—' }}</span>
          </button>
        </div>
      </div>
    </header>

    <div class="chat-split">
    <div class="chat-main">
    <div class="stream" ref="streamRef">
      <div v-if="showWelcome" class="welcome">
        <div class="welcome-icon">💬</div>
        <div class="welcome-title">有什么可以帮你？</div>
        <div class="welcome-sub">直接提问，或从示例开始</div>
        <div class="suggestions">
          <button
            v-for="p in promptSuggestions"
            :key="p.label"
            class="suggestion"
            @click="send(p.value)"
          >{{ p.label }}</button>
        </div>
      </div>

      <template v-for="msg in (showWelcome ? [] : messages)" :key="msg.id">
        <div v-if="msg.role === 'user'" class="msg-row msg-user" @mouseenter="hoveredMsgId = msg.id" @mouseleave="hoveredMsgId = null">
          <div class="user-msg-col">
            <div class="user-bubble">{{ msg.content }}</div>
            <div class="msg-toolbar user-toolbar" :class="{ visible: hoveredMsgId === msg.id }">
              <button class="tb-btn" @click="copyText(msg.content)" title="复制">
                <el-icon :size="14"><CopyDocument /></el-icon>
              </button>
              <button class="tb-btn" @click="editUserMsg(msg.content)" title="编辑">
                <el-icon :size="14"><EditPen /></el-icon>
              </button>
              <button class="tb-btn tb-btn-danger" @click="deleteMsgPair(msg.id)" title="删除">
                <el-icon :size="14"><Delete /></el-icon>
              </button>
            </div>
          </div>
          <div class="avatar av-user">你</div>
        </div>

        <div v-else class="msg-row msg-ai" @mouseenter="hoveredMsgId = msg.id" @mouseleave="hoveredMsgId = null">
          <div class="avatar av-ai">AI</div>
          <div class="ai-body">
            <!-- 思考过程：流式时展开显示自然语言推理，完成后折叠 -->
            <div v-if="streamingMsgId === msg.id && thinkingTexts.length" class="think-block think-open">
              <div class="think-header">
                <span class="think-spinner"><span class="ts-spin" /></span>
                <span class="think-label">思考中…</span>
              </div>
              <div class="think-body">
                <div v-for="(t, ti) in thinkingTexts" :key="ti" class="think-line">
                  <span class="think-bullet">•</span>
                  <span class="think-text">{{ t }}</span>
                </div>
              </div>
            </div>
            <details v-else-if="(msg as any).thinkingTexts?.length" class="think-block think-done">
              <summary class="think-header">
                <span class="think-check">✓</span>
                <span class="think-label">已深度思考 ({{ (msg as any).thinkingDuration }}s)</span>
              </summary>
              <div class="think-body">
                <div v-for="(t, ti) in (msg as any).thinkingTexts" :key="ti" class="think-line">
                  <span class="think-bullet">•</span>
                  <span class="think-text">{{ t }}</span>
                </div>
              </div>
            </details>

            <!-- 流式输出中：逐段显示内容（文字 / 图表 / 表格穿插） -->
            <template v-if="streamingMsgId === msg.id">
              <template v-if="msg.content">
                <template v-for="(seg, si) in parseContentSegments(msg.content, !!(msg.chart && showChart), !!msg.rows?.length, true)" :key="si">
                  <div v-if="seg.type === 'text'" class="ai-text ai-rich streaming-text" v-html="renderMarkdown(seg.text!)" />
                  <ChartPanel v-else-if="seg.type === 'chart' && msg.chart" :option="msg.chart.option" height="300px" />
                  <el-table v-else-if="seg.type === 'table' && msg.rows?.length" :data="msg.rows" border size="small" class="result-tbl">
                    <el-table-column v-for="col in msg.columns" :key="col.key" :prop="col.key" :label="col.label" min-width="100" />
                  </el-table>
                </template>
                <span class="cursor-blink">▍</span>
              </template>
              <template v-else>
                <div class="loading-dots"><span /><span /><span /></div>
              </template>
            </template>

            <!-- 完成后：完整渲染 -->
            <template v-else>
            <!-- 智能意图澄清交互卡片 -->
            <div v-if="msg.status === 'clarify'" class="clarify-card">
              <div class="clarify-card-header">
                <span class="clarify-icon">🤔</span>
                <span class="clarify-badge">需要补充信息</span>
              </div>
              <div class="clarify-question">{{ msg.content }}</div>
              <div v-if="msg.clarifyOptions?.length" class="clarify-options">
                <button
                  v-for="opt in msg.clarifyOptions"
                  :key="opt"
                  class="clarify-opt-btn"
                  @click="send(opt)"
                >{{ opt }}</button>
              </div>
              <div class="clarify-free-input">
                <input
                  type="text"
                  class="clarify-input"
                  placeholder="或者直接输入您的回答…"
                  @keydown.enter="(e) => { const v = (e.target as HTMLInputElement).value.trim(); if(v){ send(v); (e.target as HTMLInputElement).value = '' } }"
                />
                <button
                  class="clarify-send-btn"
                  @click="(e) => { const inp = (e.currentTarget as HTMLElement).previousElementSibling as HTMLInputElement; const v = inp.value.trim(); if(v){ send(v); inp.value = '' } }"
                >发送</button>
              </div>
            </div>

            <!-- 普通消息：段落式布局（文字 / 图表 / 表格穿插） -->
            <template v-if="!msg.taskResults?.length">
              <template v-for="(seg, si) in parseContentSegments(msg.content, !!(msg.chart && showChart), !!msg.rows?.length)" :key="si">
                <div v-if="seg.type === 'text'" class="ai-text ai-rich" v-html="renderWithCitations(seg.text!, msg.provenance ?? [])" @click="(e: MouseEvent) => handleCiteClick(e, msg)" />
                <ChartPanel v-else-if="seg.type === 'chart' && msg.chart" :option="msg.chart.option" height="300px" />
                <el-table v-else-if="seg.type === 'table' && msg.rows?.length" :data="msg.rows" border size="small" class="result-tbl">
                  <el-table-column v-for="col in msg.columns" :key="col.key" :prop="col.key" :label="col.label" min-width="100" />
                </el-table>
              </template>
            </template>

            <!-- 单意图专用：顶层 SQL / 来源表 -->
            <details v-if="msg.sql && showSql && !msg.taskResults?.length" class="sql-wrap">
              <summary>查看 SQL</summary>
              <pre class="sql-code">{{ msg.sql }}</pre>
            </details>

            <div v-if="msg.sourceTables?.length && !msg.taskResults?.length" class="src-tags">
              <el-tag v-for="s in msg.sourceTables" :key="s" size="small" effect="plain">{{ s }}</el-tag>
            </div>

            <!-- 多意图：图表/表格穿插在分析文字中间（通过 {{TASK_N}} 占位符定位） -->
            <template v-if="msg.taskResults?.length">
              <template v-for="(seg, si) in parseContentSegments(msg.content, false, false, false, msg.taskResults)" :key="'mi-' + si">
                <div v-if="seg.type === 'text'" class="ai-text ai-rich" v-html="renderWithCitations(seg.text!, msg.provenance ?? [])" @click="(e: MouseEvent) => handleCiteClick(e, msg)" />
                <template v-else-if="seg.type === 'task' && seg.taskIndex != null && msg.taskResults[seg.taskIndex]">
                  <div class="mi-section-title">{{ msg.taskResults[seg.taskIndex].description }}</div>
                  <div v-if="msg.taskResults[seg.taskIndex].error" class="mi-section-err">{{ msg.taskResults[seg.taskIndex].error }}</div>
                  <ChartPanel v-if="msg.taskResults[seg.taskIndex].chart && showChart" :option="stripChartTitle(msg.taskResults[seg.taskIndex].chart?.option)" height="280px" />
                  <el-table v-if="msg.taskResults[seg.taskIndex].rows?.length" :data="msg.taskResults[seg.taskIndex].rows" border size="small" class="result-tbl">
                    <el-table-column
                      v-for="col in msg.taskResults[seg.taskIndex].columns"
                      :key="col.key"
                      :prop="col.key"
                      :label="col.label"
                      min-width="90"
                    />
                  </el-table>
                  <details v-if="msg.taskResults[seg.taskIndex].sql" class="subtask-sql-wrap">
                    <summary>查看 SQL</summary>
                    <pre class="sql-code">{{ msg.taskResults[seg.taskIndex].sql }}</pre>
                  </details>
                </template>
              </template>
            </template>

            <div v-if="msg.followUpHints?.length" class="follow-ups">
              <button v-for="h in msg.followUpHints" :key="h" class="follow-chip" @click="send(h)">{{ h }}</button>
            </div>

            <div class="trace-buttons">
              <button v-if="msg.attribution && enableAttribution" class="attr-trigger" @click="openAttribution(msg.attribution!)">
                🔗 归因分析 · 完整生成链路追溯
                <span class="attr-badge">{{ msg.attribution.steps.length }} 步</span>
              </button>
            </div>

            <details v-if="msg.references?.length" class="refs-wrap">
              <summary class="refs-summary">📚 知识库参考（{{ msg.references.length }} 条）</summary>
              <div class="refs-list">
                <div v-for="(ref, i) in msg.references" :key="i" class="ref-card">
                  <div class="ref-header">
                    <span class="ref-idx">#{{ i + 1 }}</span>
                    <span class="ref-name">{{ ref.doc_name }}</span>
                    <span class="ref-type">{{ ref.doc_type }}</span>
                    <span class="ref-score">相关度 {{ ref.score?.toFixed(3) }}</span>
                  </div>
                  <div class="ref-text">{{ ref.text }}</div>
                </div>
              </div>
            </details>
            </template>

            <!-- AI 消息底部操作栏（豆包风格） -->
            <div v-if="streamingMsgId !== msg.id" class="msg-toolbar ai-toolbar" :class="{ visible: hoveredMsgId === msg.id }">
              <button class="tb-btn" @click="copyText(msg.content)" title="复制">
                <el-icon :size="14"><CopyDocument /></el-icon>
              </button>
              <button class="tb-btn" @click="regenerateAiMsg(msg.id)" title="重新生成">
                <el-icon :size="14"><RefreshRight /></el-icon>
              </button>
              <button class="tb-btn tb-btn-danger" @click="deleteMsgPair(msg.id)" title="删除">
                <el-icon :size="14"><Delete /></el-icon>
              </button>
            </div>
          </div>
        </div>
      </template>

      <!-- thinking steps now shown inside AI message bubble -->

    </div>

    <!-- 归因分析右侧抽屉 -->
    <Teleport to="body">
      <div v-if="showAttribution" class="attr-overlay" @click.self="closeAttribution">
        <div class="attr-panel">
          <div class="attr-header">
            <div class="attr-header-left">
              <span class="attr-icon">🔗</span>
              <span class="attr-title">归因分析</span>
              <span class="attr-sub">完整生成链路追溯</span>
              <span class="attr-count">{{ activeAttribution?.steps.length }} 步</span>
            </div>
            <button class="attr-close" @click="closeAttribution">✕</button>
          </div>

          <div class="attr-body">

            <!-- ① 思维导图（推理链路） -->
            <section v-if="activeAttribution?.thinking_map" class="attr-section">
              <div class="attr-section-title">🧠 推理链路</div>
              <ThinkingMap
                :root="activeAttribution.thinking_map"
                @fullscreen="showThinkingMapFull = true"
              />
            </section>

            <!-- ② 概要指标条 -->
            <section v-if="activeAttribution?.data_summary" class="attr-section">
              <div class="attr-metric-bar">
                <span class="attr-metric"><b>{{ activeAttribution.data_summary.table_labels?.join(' · ') || '—' }}</b></span>
                <span class="attr-metric-sep">|</span>
                <span class="attr-metric">{{ activeAttribution.data_summary.row_count }} 行</span>
                <span class="attr-metric-sep">|</span>
                <span class="attr-metric">{{ activeAttribution.data_summary.columns?.length ?? 0 }} 字段</span>
                <span class="attr-metric-sep">|</span>
                <span class="attr-metric">{{ activeAttribution.kb_sources?.length ?? 0 }} 篇研报</span>
              </div>
            </section>

            <!-- ③ SQL 查询语句（可折叠） -->
            <details v-if="activeAttribution?.sql" class="attr-section attr-sql-details">
              <summary class="attr-section-title attr-sql-summary">🗄️ SQL 查询语句</summary>
              <pre class="attr-sql">{{ activeAttribution.sql }}</pre>
            </details>

            <!-- ④ 知识库引用来源 -->
            <details v-if="activeAttribution?.kb_sources?.length" class="attr-section attr-sql-details">
              <summary class="attr-section-title attr-sql-summary">📚 知识库引用（{{ activeAttribution.kb_sources.length }}）</summary>
              <div class="attr-kb-list">
                <div v-for="(src, i) in activeAttribution.kb_sources" :key="i" class="attr-kb-card">
                  <div class="attr-kb-header">
                    <span class="attr-kb-idx">#{{ i + 1 }}</span>
                    <span class="attr-kb-name">{{ src.doc_name }}</span>
                    <span class="attr-kb-type">{{ src.doc_type }}</span>
                  </div>
                  <div class="attr-kb-text">{{ src.text?.slice(0, 100) }}…</div>
                </div>
              </div>
            </details>

          </div>

          <!-- 思维导图全屏弹窗 -->
          <Teleport to="body">
            <div v-if="showThinkingMapFull && activeAttribution?.thinking_map" class="tm-fullscreen-overlay" @click.self="showThinkingMapFull = false">
              <div class="tm-fullscreen-panel">
                <div class="tm-fullscreen-header">
                  <span>🧠 推理链路（全屏）</span>
                  <button class="attr-close" @click="showThinkingMapFull = false">✕</button>
                </div>
                <ThinkingMap
                  :root="activeAttribution.thinking_map"
                  @fullscreen="showThinkingMapFull = false"
                  style="height: calc(100vh - 56px);"
                />
              </div>
            </div>
          </Teleport>
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <CalcTracePanel
        v-if="showCalcTrace"
        :trace="activeCalcTrace"
        @close="closeCalcTrace"
        @openEvidence="openCalcOperandEvidence"
      />
    </Teleport>

    <div class="composer">
      <div class="composer-box">
        <textarea
          ref="textareaRef"
          v-model="draft"
          class="c-input"
          placeholder="输入问题，例如：某公司最近年报净利润是多少？"
          @keydown.enter.exact="handleEnter"
          @input="autoResize"
        />
        <button v-if="busy" class="stop-btn" @click="stopGeneration()" title="停止生成">
          <span class="stop-icon" />
        </button>
        <button v-else class="send-btn" :disabled="!draft.trim()" @click="send()">
          <el-icon><Promotion /></el-icon>
        </button>
      </div>
      <div class="composer-footer">
        <div class="toggle-group">
          <label class="opt-toggle">
            <input type="checkbox" v-model="showSql" />
            <span>SQL</span>
          </label>
          <label class="opt-toggle">
            <input type="checkbox" v-model="showChart" />
            <span>生成图表</span>
          </label>
          <label class="opt-toggle">
            <input type="checkbox" v-model="enableAttribution" />
            <span>归因分析</span>
          </label>
          <label class="opt-toggle">
            <input type="checkbox" v-model="enableContext" />
            <span>上下文</span>
          </label>
          <label class="deep-think-toggle" :class="{ active: deepThink }">
            <span class="dt-track" @click="deepThink = !deepThink">
              <span class="dt-thumb" />
            </span>
            <span class="dt-label">深度思考</span>
          </label>
        </div>
        <span class="hint-text">Enter 发送 · Shift+Enter 换行</span>
      </div>
    </div>
    </div><!-- /chat-main -->

    <!-- 右侧溯源面板（分屏） -->
    <div v-if="showProvenance" class="prov-side">
      <ProvenancePanel
        :records="activeProvenance"
        :active-cite-index="activeCiteIndex"
        :back-label="provenanceBackLabel"
        @close="closeProvenance"
        @back="handleProvenanceBack"
      />
    </div>
    </div><!-- /chat-split -->
  </div>
</template>

<style scoped>
/* ═══════════════════════════════════════════════
   AssistantView — Intelligent Conversation Console
   ═══════════════════════════════════════════════ */

.chat-view {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  overflow: hidden;
  font-family: var(--font-body);
}

/* ── Split-screen layout ── */
.chat-split {
  flex: 1; min-height: 0; display: flex; overflow: hidden;
}
.chat-main {
  flex: 1; min-width: 0; display: flex; flex-direction: column;
  transition: flex 0.3s ease;
}
.prov-side {
  width: 50%; max-width: 600px; min-width: 340px;
  flex-shrink: 0;
  animation: provSlideIn 0.28s ease;
}
@keyframes provSlideIn {
  from { width: 0; opacity: 0; }
  to   { width: 50%; opacity: 1; }
}
.topbar {
  height: 52px;
  padding: 0 28px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border-primary);
  background: var(--bg-secondary);
  flex-shrink: 0;
}
.topbar-left {
  display: flex;
  align-items: center;
  min-width: 0;
}
.topbar-title {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
}
.topbar-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.thinking-bar { display: flex; gap: 4px; align-items: center; }
.batch-shortcuts {
  display: flex;
  align-items: center;
  gap: 8px;
}
.batch-shortcut {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 34px;
  padding: 0 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-primary);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s ease;
}
.batch-shortcut:hover {
  border-color: var(--accent-border);
  color: var(--accent);
  background: var(--accent-bg);
}
.batch-shortcut--primary {
  background: var(--accent-bg);
  color: var(--accent);
  border-color: var(--accent-border);
}
.batch-shortcut-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  font-size: 13px;
}
.batch-shortcut-label {
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.batch-shortcut-badge {
  min-width: 20px;
  height: 20px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--accent-bg);
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.t-dot {
  display: block; width: 5px; height: 5px; border-radius: 50%;
  background: var(--accent); animation: tdot 0.9s ease infinite;
}
.t-dot:nth-child(2) { animation-delay: 0.16s; }
.t-dot:nth-child(3) { animation-delay: 0.32s; }
@keyframes tdot {
  0%, 80%, 100% { transform: scale(0.65); opacity: 0.35; }
  40% { transform: scale(1); opacity: 1; }
}
@media (max-width: 960px) {
  .topbar {
    height: auto;
    min-height: 52px;
    padding: 10px 20px;
    align-items: flex-start;
    gap: 10px;
  }
  .topbar-actions {
    flex-wrap: wrap;
    justify-content: flex-end;
  }
}
.stream {
  flex: 1; overflow-y: auto; padding: 28px 0 16px;
  display: flex; flex-direction: column; gap: 24px; min-height: 0;
}
.split-active .msg-row { padding: 0 16px; }
.split-active .ai-text { font-size: 13px; }
.split-active .result-tbl { font-size: 11px; }
.stream::-webkit-scrollbar { width: 4px; }
.stream::-webkit-scrollbar-thumb { background: var(--border-primary); border-radius: 4px; }
.welcome {
  flex: 1; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 10px; padding: 48px 24px; text-align: center;
}
.welcome-icon { font-size: 40px; }
.welcome-title {
  font-family: var(--font-display);
  font-size: 20px;
  font-weight: 700;
  color: var(--text-primary);
}
.welcome-sub { font-size: 13px; color: var(--text-tertiary); }
.suggestions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 10px; }
.suggestion {
  padding: 8px 16px; border-radius: 20px; border: 1px solid var(--border-primary);
  background: var(--bg-tertiary); font-size: 13px; color: var(--text-secondary); cursor: pointer; transition: all 0.15s;
}
.suggestion:hover { border-color: var(--accent-border); color: var(--accent); background: var(--accent-bg); }
.msg-row {
  display: flex; gap: 12px; padding: 0 28px;
  max-width: 860px; width: 100%; margin: 0 auto; box-sizing: border-box;
}
.msg-user { flex-direction: row-reverse; }
.avatar {
  width: 32px; height: 32px; border-radius: var(--radius-md); flex-shrink: 0;
  display: grid; place-items: center; font-size: 11px; font-weight: 700;
}
.av-ai { background: var(--accent); color: #fff; }
.av-user { background: var(--bg-tertiary); color: var(--text-secondary); border: 1px solid var(--border-primary); }
.user-bubble {
  width: fit-content; max-width: 100%; padding: 10px 16px;
  border-radius: 16px 16px 4px 16px;
  background: var(--accent);
  color: #fff; font-size: 14px; line-height: 1.65; white-space: pre-wrap;
}
.ai-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 10px; padding-top: 4px; }
.clarify-tag {
  display: inline-block; padding: 2px 8px; border-radius: 5px;
  background: var(--warning-bg); color: var(--warning); font-size: 11px; font-weight: 600; align-self: flex-start;
}
.ai-text { font-size: 14px; line-height: 1.8; color: var(--text-primary); white-space: pre-wrap; }
.ai-rich { white-space: normal; }
.ai-rich :deep(p) { margin: 0 0 8px 0; }
.ai-rich :deep(p:last-child) { margin-bottom: 0; }
.ai-rich :deep(strong) { color: var(--text-primary); font-weight: 600; }
.ai-rich :deep(.md-li) {
  display: flex; gap: 6px; padding-left: 4px;
  margin-bottom: 4px; line-height: 1.75;
}
.ai-rich :deep(.md-li-num) {
  flex-shrink: 0; font-weight: 600; color: var(--accent);
  min-width: 20px;
}
.ai-rich :deep(.md-li-dot) {
  flex-shrink: 0; font-weight: 700; color: var(--accent);
  min-width: 14px; text-align: center;
}
.ai-rich :deep(.md-li-content) { flex: 1; min-width: 0; }
.ai-rich :deep(.md-heading) {
  font-family: var(--font-display);
  font-size: 18px; font-weight: 800; color: var(--text-primary);
  margin: 24px 0 10px 0; padding: 0;
  line-height: 1.4; letter-spacing: 0.01em;
}
.ai-rich :deep(.md-heading:first-child) { margin-top: 6px; }

/* ── Citation superscripts ── */
.ai-rich :deep(.cite-sup) {
  display: inline-flex; align-items: center; gap: 2px;
  font-family: var(--font-mono);
  font-size: 9px; font-weight: 700; color: var(--danger);
  cursor: pointer; vertical-align: super; line-height: 1;
  padding: 1px 5px; margin-left: 2px;
  background: var(--danger-bg); border: 1px solid var(--danger-border); border-radius: var(--radius-sm);
  transition: all 0.14s; user-select: none;
  font-style: normal;
}
.ai-rich :deep(.cite-sup:hover) {
  background: var(--danger); color: #fff; border-color: var(--danger);
  transform: scale(1.1);
}
.ai-rich :deep(.cite-sup.kb-cite) {
  color: #0369a1; background: #e0f2fe; border-color: #7dd3fc;
}
.ai-rich :deep(.cite-sup.kb-cite:hover) {
  background: #0369a1; color: #fff; border-color: #0369a1;
}
.kb-fallback-line {
  font-size: 12px; color: var(--text-tertiary); margin-top: 6px; padding: 6px 10px;
  background: var(--accent-bg); border-radius: var(--radius-md); border: 1px solid var(--accent-border);
}
/* ── Smart Clarify Card ── */
.clarify-card {
  border: 1px solid var(--accent-border); border-radius: var(--radius-lg); padding: 16px 18px;
  background: var(--accent-bg);
  display: flex; flex-direction: column; gap: 12px;
}
.clarify-card-header { display: flex; align-items: center; gap: 8px; }
.clarify-icon { font-size: 18px; }
.clarify-badge {
  font-size: 11px; font-weight: 700; color: var(--accent);
  background: var(--bg-secondary); border-radius: 20px; padding: 2px 10px;
}
.clarify-question { font-size: 14px; font-weight: 500; color: var(--text-primary); line-height: 1.7; }
.clarify-options { display: flex; flex-wrap: wrap; gap: 8px; }
.clarify-opt-btn {
  padding: 6px 14px; font-size: 13px; border-radius: 20px; cursor: pointer;
  border: 1px solid var(--accent-border); background: var(--bg-secondary); color: var(--accent); font-weight: 500;
  transition: all 0.15s;
}
.clarify-opt-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
.clarify-free-input {
  display: flex; gap: 8px; align-items: center;
  border-top: 1px solid var(--border-primary); padding-top: 10px;
}
.clarify-input {
  flex: 1; border: 1px solid var(--border-primary); border-radius: var(--radius-md); padding: 7px 12px;
  font-size: 13px; outline: none; background: var(--bg-secondary); color: var(--text-primary);
}
.clarify-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.08); }
.clarify-input::placeholder { color: var(--text-tertiary); }
.clarify-send-btn {
  padding: 7px 16px; background: var(--accent); color: #fff; border: none;
  border-radius: var(--radius-md); font-size: 13px; font-weight: 600; cursor: pointer;
  transition: all 0.15s;
}
.clarify-send-btn:hover { background: var(--accent-hover); }

/* ── Multi-intent results (natural flow) ── */
.mi-section-title {
  font-family: var(--font-display);
  font-size: 14px; font-weight: 700; color: var(--text-primary);
  padding: 6px 0 2px; margin-top: 10px;
  border-bottom: 2px solid var(--accent-border);
}
.mi-section-err { font-size: 12px; color: var(--danger); margin: 4px 0; }
.subtask-sql-wrap { font-size: 12px; margin: 4px 0; }
.subtask-sql-wrap summary { cursor: pointer; color: var(--accent); user-select: none; padding: 6px 0; }

/* ── Thinking block ── */
.think-block {
  border-left: 3px solid var(--border-primary); margin-bottom: 14px;
  padding-left: 12px;
}
.think-block.think-open { border-left-color: var(--accent); }
.think-block.think-done { border-left-color: var(--border-secondary); }
.think-header {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 0; cursor: pointer; user-select: none;
}
.think-spinner {
  width: 14px; height: 14px; display: grid; place-items: center; flex-shrink: 0;
}
.think-check {
  width: 16px; height: 16px; border-radius: 50%; flex-shrink: 0;
  background: var(--bg-tertiary); color: var(--text-tertiary); font-size: 10px; font-weight: 700;
  display: grid; place-items: center;
}
.think-label { font-size: 13px; font-weight: 600; color: var(--text-tertiary); }
.think-open .think-label { color: var(--accent); }
.think-body {
  padding: 4px 0 4px; display: flex; flex-direction: column; gap: 4px;
}
.think-line {
  display: flex; align-items: flex-start; gap: 6px; line-height: 1.65;
}
.think-bullet {
  color: var(--border-primary); font-size: 14px; flex-shrink: 0; margin-top: 1px;
}
.think-open .think-bullet { color: var(--accent); }
.think-text { font-size: 13px; color: var(--text-tertiary); flex: 1; }
.think-open .think-text { color: var(--text-secondary); }
.ts-spin {
  width: 10px; height: 10px; border-radius: 50%;
  border: 2px solid var(--accent-border); border-top-color: var(--accent);
  animation: ts-rotate 0.7s linear infinite; display: block;
}
@keyframes ts-rotate { to { transform: rotate(360deg); } }
details.think-done > summary { list-style: none; }
details.think-done > summary::-webkit-details-marker { display: none; }
details.think-done > summary .think-label::after { content: ' ▾'; font-size: 10px; margin-left: 2px; }
details.think-done:not([open]) > summary .think-label::after { content: ' ▸'; }

.sql-wrap { font-size: 12px; }
.sql-wrap summary { cursor: pointer; color: var(--accent); user-select: none; margin-bottom: 6px; }
.sql-code {
  background: #0f172a; color: #e2e8f0; padding: 12px 16px;
  border-radius: var(--radius-md); font-family: var(--font-mono); font-size: 12px; overflow-x: auto; white-space: pre-wrap; margin: 0;
}
.result-tbl { border-radius: var(--radius-md); overflow: hidden; animation: chartFadeIn 0.4s ease-out; }
.src-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.follow-ups { display: flex; flex-wrap: wrap; gap: 6px; }
.follow-chip {
  padding: 6px 13px; border-radius: 14px; border: 1px solid var(--border-primary);
  background: var(--bg-tertiary); font-size: 12px; color: var(--text-secondary); cursor: pointer; transition: all 0.14s;
}
.follow-chip:hover { border-color: var(--accent-border); color: var(--accent); background: var(--accent-bg); }

/* ── Message toolbar ── */
.user-msg-col {
  display: flex; flex-direction: column; align-items: flex-end; gap: 4px;
  width: fit-content; max-width: min(78%, 680px);
}
.msg-toolbar {
  display: flex; align-items: center; gap: 2px;
  opacity: 0; transition: opacity 0.18s;
  pointer-events: none;
  height: 26px;
}
.msg-toolbar.visible { opacity: 1; pointer-events: auto; }
.user-toolbar { justify-content: flex-end; }
.ai-toolbar { justify-content: flex-start; }
.tb-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 28px; height: 28px; border-radius: var(--radius-sm);
  border: none; background: transparent; color: var(--text-tertiary);
  cursor: pointer; transition: all 0.15s; flex-shrink: 0;
}
.tb-btn:hover { background: var(--bg-tertiary); color: var(--text-secondary); }
.tb-btn-danger:hover { background: var(--danger-bg); color: var(--danger); }

.loading-dots { display: flex; gap: 5px; align-items: center; padding: 6px 0; }
.loading-dots span {
  display: block; width: 7px; height: 7px; background: var(--border-primary);
  border-radius: 50%; animation: tdot 0.9s ease infinite;
}
.loading-dots span:nth-child(2) { animation-delay: 0.16s; }
.loading-dots span:nth-child(3) { animation-delay: 0.32s; }

/* ── Composer ── */
.composer {
  border-top: 1px solid var(--border-primary); padding: 14px 28px 16px; flex-shrink: 0;
  display: flex; flex-direction: column; gap: 7px; align-items: center;
  background: var(--bg-secondary);
}
.composer-box {
  display: flex; align-items: flex-end; gap: 10px;
  border: 1px solid var(--border-primary); border-radius: var(--radius-lg); padding: 10px 12px;
  background: var(--bg-tertiary); transition: all 0.15s; max-width: 760px; width: 100%;
}
.composer-box:focus-within {
  border-color: var(--accent); background: var(--bg-secondary);
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.08);
}
.c-input {
  flex: 1; border: none; outline: none; background: transparent;
  font-size: 14px; resize: none; min-height: 22px; max-height: 160px;
  line-height: 1.6; color: var(--text-primary); font-family: var(--font-body); overflow-y: auto;
}
.c-input::placeholder { color: var(--text-tertiary); }
.send-btn {
  width: 34px; height: 34px; border-radius: var(--radius-md); border: none; cursor: pointer;
  flex-shrink: 0; background: var(--accent); color: #fff;
  display: grid; place-items: center; font-size: 15px;
  transition: all 0.15s ease;
}
.send-btn:hover:not(:disabled) { background: var(--accent-hover); transform: scale(1.05); }
.send-btn:disabled { background: var(--bg-inset); color: var(--text-tertiary); cursor: default; }
.stop-btn {
  width: 34px; height: 34px; border-radius: var(--radius-md); border: none; cursor: pointer;
  flex-shrink: 0; background: var(--danger); color: #fff;
  display: grid; place-items: center;
  transition: all 0.15s ease;
  animation: stopPulse 1.5s ease-in-out infinite;
}
.stop-btn:hover { background: #dc2626; transform: scale(1.05); }
.stop-icon {
  display: block; width: 12px; height: 12px;
  background: #fff; border-radius: 2px;
}
@keyframes stopPulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(239,68,68,0.4); }
  50% { box-shadow: 0 0 0 6px rgba(239,68,68,0); }
}
.composer-footer {
  display: flex; align-items: center; justify-content: space-between;
  max-width: 760px; width: 100%;
}
.toggle-group { display: flex; gap: 12px; }
.opt-toggle { display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--text-tertiary); cursor: pointer; user-select: none; }
.opt-toggle input { cursor: pointer; accent-color: var(--accent); }
.deep-think-toggle {
  display: flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; margin-left: 4px;
}
.dt-track {
  width: 32px; height: 17px; border-radius: 9px;
  background: var(--border-primary); position: relative; transition: background 0.25s;
  display: inline-block; flex-shrink: 0;
}
.deep-think-toggle.active .dt-track { background: var(--accent); }
.dt-thumb {
  position: absolute; top: 2px; left: 2px;
  width: 13px; height: 13px; border-radius: 50%;
  background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.12);
  transition: transform 0.25s;
}
.deep-think-toggle.active .dt-thumb { transform: translateX(15px); }
.dt-label { font-size: 12px; color: var(--text-tertiary); white-space: nowrap; }
.deep-think-toggle.active .dt-label { color: var(--accent); font-weight: 600; }
.hint-text { font-size: 11px; color: var(--text-tertiary); font-family: var(--font-mono); }

/* ── Multi-intent subtasks ── */
.subtasks-wrap { margin-top: 2px; }
.subtasks-summary {
  cursor: pointer; font-size: 12px; color: var(--accent);
  user-select: none; list-style: none; padding: 4px 0;
}
.subtasks-summary::-webkit-details-marker { display: none; }
.subtask-list { display: flex; flex-direction: column; gap: 10px; margin-top: 8px; }
.subtask-card {
  border: 1px solid var(--accent-border); border-radius: var(--radius-md);
  background: var(--accent-bg); padding: 10px 12px;
  display: flex; flex-direction: column; gap: 7px;
}
.subtask-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.subtask-idx {
  font-family: var(--font-mono);
  font-size: 11px; font-weight: 700; color: var(--accent);
  background: var(--bg-secondary); padding: 1px 7px; border-radius: var(--radius-xs); flex-shrink: 0;
}
.subtask-desc { font-size: 12px; font-weight: 600; color: var(--text-primary); flex: 1; }
.subtask-err { font-size: 11px; color: var(--danger); }
.subtask-sql-wrap { font-size: 11px; }
.subtask-sql-wrap summary { cursor: pointer; color: var(--accent); user-select: none; margin-bottom: 4px; }
.subtask-tbl { border-radius: var(--radius-sm); overflow: hidden; }

/* ── Trace buttons container ── */
.trace-buttons { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 2px; }

/* ── Attribution trigger button ── */
.attr-trigger {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 12px; border-radius: var(--radius-md); border: 1px solid var(--accent-border);
  background: var(--accent-bg); color: var(--accent); font-size: 12px; cursor: pointer;
  transition: all 0.15s;
}
.attr-trigger:hover { background: var(--bg-secondary); border-color: var(--accent); }
.attr-badge {
  background: var(--accent); color: #fff; font-family: var(--font-mono); font-size: 10px; font-weight: 700;
  padding: 1px 6px; border-radius: var(--radius-xs);
}


/* ── Attribution side panel ── */
.attr-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.25); z-index: 1000;
  display: flex; justify-content: flex-end;
}
.attr-panel {
  width: 440px; height: 100vh; background: var(--bg-secondary);
  box-shadow: -4px 0 24px rgba(0,0,0,0.1);
  display: flex; flex-direction: column; animation: slideIn 0.22s ease;
}
@keyframes slideIn {
  from { transform: translateX(440px); }
  to { transform: translateX(0); }
}
.attr-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px 12px; border-bottom: 1px solid var(--border-primary); flex-shrink: 0;
}
.attr-header-left { display: flex; align-items: center; gap: 8px; }
.attr-icon { font-size: 16px; }
.attr-title { font-family: var(--font-display); font-size: 14px; font-weight: 700; color: var(--text-primary); }
.attr-sub { font-size: 11px; color: var(--text-tertiary); }
.attr-count {
  background: var(--accent); color: #fff; font-family: var(--font-mono); font-size: 10px; font-weight: 700;
  padding: 2px 8px; border-radius: var(--radius-xs);
}
.attr-close {
  width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--border-primary);
  background: var(--bg-tertiary); color: var(--text-tertiary); font-size: 14px; cursor: pointer;
  display: grid; place-items: center; transition: all 0.14s;
}
.attr-close:hover { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger); }
.attr-body { flex: 1; overflow-y: auto; padding: 16px 20px 24px; display: flex; flex-direction: column; gap: 20px; }
.attr-section { display: flex; flex-direction: column; gap: 10px; }
.attr-section-title { font-size: 11px; font-weight: 700; color: var(--accent); text-transform: uppercase; letter-spacing: 0.05em; }

/* Metric bar */
.attr-metric-bar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 12px; background: var(--bg-tertiary); border: 1px solid var(--border-secondary); border-radius: var(--radius-md);
}
.attr-metric { font-size: 12px; color: var(--text-secondary); }
.attr-metric b { color: var(--accent); }
.attr-metric-sep { color: var(--border-primary); font-size: 11px; }

/* SQL / KB details */
.attr-sql-details { border: 1px solid var(--border-primary); border-radius: var(--radius-lg); padding: 0; }
.attr-sql-summary {
  cursor: pointer; padding: 10px 14px; margin: 0; list-style: none;
  user-select: none;
}
.attr-sql-summary::-webkit-details-marker { display: none; }
.attr-sql-details[open] > .attr-sql-summary { border-bottom: 1px solid var(--border-primary); }

/* SQL block */
.attr-sql {
  background: #0f172a; color: #e2e8f0; padding: 14px 16px;
  border-radius: var(--radius-md); font-family: var(--font-mono); font-size: 12px; overflow-x: auto; white-space: pre-wrap;
  margin: 0; line-height: 1.6;
}

/* KB sources */
.attr-no-kb {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; color: var(--text-tertiary); padding: 12px; background: var(--bg-tertiary);
  border: 1px dashed var(--border-primary); border-radius: var(--radius-md);
}
.attr-no-kb-icon { font-size: 16px; }
.attr-kb-list { display: flex; flex-direction: column; gap: 10px; }
.attr-kb-card {
  border: 1px solid var(--accent-border); border-radius: var(--radius-lg); background: var(--accent-bg); padding: 12px;
}
.attr-kb-header { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
.attr-kb-idx {
  font-family: var(--font-mono);
  font-size: 11px; font-weight: 700; color: var(--accent);
  background: var(--bg-secondary); padding: 1px 6px; border-radius: var(--radius-xs);
}
.attr-kb-name { font-size: 12px; font-weight: 600; color: var(--text-primary); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.attr-kb-type {
  font-size: 11px; color: #7c3aed; background: #ede9fe;
  padding: 1px 7px; border-radius: var(--radius-xs); flex-shrink: 0;
}
.attr-kb-score {
  display: flex; align-items: center; gap: 5px;
  font-family: var(--font-mono);
  font-size: 11px; color: var(--accent); font-weight: 600; flex-shrink: 0;
}
.attr-score-bar-wrap {
  width: 40px; height: 4px; background: var(--accent-border); border-radius: 3px; overflow: hidden;
}
.attr-score-bar {
  display: inline-block; height: 4px; background: var(--accent); border-radius: 3px;
}
.attr-kb-text { font-size: 12px; line-height: 1.75; color: var(--text-secondary); }

/* ── References ── */
.refs-wrap { margin-top: 2px; }
.refs-summary {
  cursor: pointer; font-size: 12px; color: var(--accent);
  user-select: none; list-style: none; padding: 4px 0;
}
.refs-summary::-webkit-details-marker { display: none; }
.refs-list { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
.ref-card {
  padding: 10px 12px; border-radius: var(--radius-md);
  border: 1px solid var(--accent-border); background: var(--accent-bg);
}
.ref-header {
  display: flex; align-items: center; gap: 7px; margin-bottom: 6px; flex-wrap: wrap;
}
.ref-idx {
  font-family: var(--font-mono);
  font-size: 11px; font-weight: 700; color: var(--accent);
  background: var(--bg-secondary); padding: 1px 6px; border-radius: var(--radius-xs);
}
.ref-name { font-size: 12px; font-weight: 600; color: var(--text-primary); flex: 1; }
.ref-type {
  font-size: 11px; color: #7c3aed; background: #ede9fe;
  padding: 1px 7px; border-radius: var(--radius-xs);
}
.ref-score { font-family: var(--font-mono); font-size: 11px; color: var(--text-tertiary); margin-left: auto; }
.ref-text {
  font-size: 12px; line-height: 1.7; color: var(--text-secondary);
  display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3;
  -webkit-box-orient: vertical; overflow: hidden;
}
.streaming-text {
  white-space: pre-wrap;
}
.cursor-blink {
  display: inline;
  animation: blink 0.8s step-end infinite;
  color: var(--accent);
  font-weight: 700;
}
@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}

/* ── ThinkingMap fullscreen overlay ── */
.tm-fullscreen-overlay {
  position: fixed; inset: 0; z-index: 9999;
  background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center;
}
.tm-fullscreen-panel {
  width: 92vw; height: 90vh; background: var(--bg-secondary); border-radius: var(--radius-xl);
  overflow: hidden; display: flex; flex-direction: column;
  box-shadow: 0 8px 40px rgba(0,0,0,0.15);
}
.tm-fullscreen-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 20px; border-bottom: 1px solid var(--border-primary);
  font-family: var(--font-display); font-size: 14px; font-weight: 700; color: var(--text-primary);
}
</style>
