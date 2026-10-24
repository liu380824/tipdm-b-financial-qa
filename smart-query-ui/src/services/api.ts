import type {
  AppMetric,
  ChatMessage,
  DatabaseConnection,
  ImportTask,
  ProvenanceRecord,
  QualityRule,
  QueryColumn,
  QueryRow,
  SystemSettings,
  SystemSettingsPayload,
  TableSchema
} from '../types'

interface DatabaseOverviewResponse {
  metrics: AppMetric[]
  connections: DatabaseConnection[]
  importTasks: ImportTask[]
  qualityRules: QualityRule[]
}

interface DatabaseQueryResponse {
  notice: string
  columns: QueryColumn[]
  rows: QueryRow[]
  truncated: boolean
}

interface Task2ExportRecord {
  id: string
  questionType: string
  question: Array<{ Q: string }>
  sql: string
  chartFormat: string
  answer: Array<{
    Q: string
    A: {
      content: string
      image?: string[]
    }
  }>
}

export interface Task2ExportResponse {
  questionFile: string
  workbookPath: string
  workbookUrl: string
  resultDir: string
  questionCount: number
  records: Task2ExportRecord[]
}

const request = async <T>(url: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {})
    },
    ...init
  })

  if (!response.ok) {
    let message = `请求失败：${response.status}`
    try {
      const data = (await response.json()) as { detail?: string }
      if (data.detail) {
        message = data.detail
      }
    } catch {
      message = await response.text()
    }
    throw new Error(message)
  }

  return response.json() as Promise<T>
}

function compressHistory(messages: ChatMessage[]): Record<string, unknown>[] {
  const result: Record<string, unknown>[] = []
  for (const m of messages) {
    if (m.role !== 'user' && m.role !== 'assistant') continue
    // 被中止的 AI 消息：无实质内容则跳过，有部分内容则截断并标注
    if (m.role === 'assistant' && m.aborted) {
      const text = (m.content || '').trim()
      if (!text || text === '（已停止生成）') continue
      result.push({
        role: 'assistant',
        content: text.slice(0, 200) + '…（回答被用户中止，内容不完整）',
      })
      continue
    }
    // For AI replies: only send a brief summary + structured meta to avoid context pollution
    const contentLimit = m.role === 'assistant' ? 400 : 500
    const rawContent = (m.content || '').trim()
    const briefContent = m.role === 'assistant'
      ? rawContent.split('\n').filter(l => l.trim()).slice(0, 5).join(' ').replace(/\*\*/g, '').slice(0, contentLimit)
      : rawContent.slice(0, contentLimit)
    result.push({
      role: m.role,
      content: briefContent,
      ...(m.meta ? {
        meta: {
          company: m.meta.company,
          metric: m.meta.metric,
          query_kind: m.meta.query_kind,
          analysis_mode: m.meta.analysis_mode,
          report_period: m.meta.report_period,
          intent: m.meta.intent,
        }
      } : {}),
    })
  }
  return result.slice(-16)
}

export const askAssistant = (payload: {
  question: string
  history: ChatMessage[]
  auto_chart: boolean
  enable_context: boolean
  session_id?: string
}) =>
  request<ChatMessage>('/api/assistant/query', {
    method: 'POST',
    body: JSON.stringify({ ...payload, history: compressHistory(payload.history) })
  })

export interface StreamStep {
  label: string
  status: 'running' | 'done' | 'error'
  detail: string
}

export const askAssistantStream = async (
  payload: { question: string; history: ChatMessage[]; auto_chart: boolean; enable_context: boolean; deep_think?: boolean; session_id?: string },
  onStep: (step: StreamStep) => void,
  onContentChunk?: (chunk: string) => void,
  onChart?: (chart: ChatMessage['chart']) => void,
  onTable?: (data: { columns: ChatMessage['columns']; rows: ChatMessage['rows'] }) => void,
  signal?: AbortSignal,
  onThinking?: (text: string) => void
): Promise<ChatMessage> => {
  const compressed = { ...payload, history: compressHistory(payload.history) }
  const resp = await fetch('/api/assistant/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(compressed),
    signal
  })
  if (!resp.ok) {
    let msg = `请求失败：${resp.status}`
    try { msg = ((await resp.json()) as { detail?: string }).detail ?? msg } catch { /* noop */ }
    throw new Error(msg)
  }
  const reader = resp.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() ?? ''
    for (const part of parts) {
      const text = part.replace(/^data:\s*/, '').trim()
      if (!text) continue
      const evt = JSON.parse(text) as { type: string; data: unknown }
      if (evt.type === 'done') return evt.data as ChatMessage
      if (evt.type === 'error') throw new Error(String(evt.data))
      if (evt.type === 'step') onStep(evt.data as StreamStep)
      if (evt.type === 'content_chunk' && onContentChunk) onContentChunk(String(evt.data))
      if (evt.type === 'chart' && onChart) onChart(evt.data as ChatMessage['chart'])
      if (evt.type === 'table' && onTable) onTable(evt.data as { columns: ChatMessage['columns']; rows: ChatMessage['rows'] })
      if (evt.type === 'thinking' && onThinking) onThinking(String(evt.data))
    }
  }
  throw new Error('流式响应意外结束')
}

/** 判断是否为用户主动中止导致的错误 */
export const isAbortError = (err: unknown): boolean =>
  err instanceof DOMException && err.name === 'AbortError'

export const fetchSystemSettings = () => request<SystemSettings>('/api/system/settings')

export const saveSystemSettings = (payload: SystemSettingsPayload) =>
  request<SystemSettings>('/api/system/settings', {
    method: 'PUT',
    body: JSON.stringify(payload)
  })

export const fetchDatabaseOverview = () => request<DatabaseOverviewResponse>('/api/database/overview')

export const fetchDatabaseSchemas = () => request<TableSchema[]>('/api/database/schemas')

export const fetchDatabaseQualityRules = () => request<QualityRule[]>('/api/database/quality-rules')

export const runDatabaseQuery = (payload: { sql: string; limit?: number }) =>
  request<DatabaseQueryResponse>('/api/database/query', {
    method: 'POST',
    body: JSON.stringify(payload)
  })

export const fetchTask2Questions = () =>
  request<{ count: number; records: Task2ExportRecord[] }>('/api/task2/questions')

export const exportTask2Results = () =>
  request<Task2ExportResponse>('/api/task2/export', {
    method: 'POST'
  })

export const exportTask2WithFile = async (file: File): Promise<Task2ExportResponse> => {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch('/api/task2/export-with-file', { method: 'POST', body: form })
  if (!response.ok) {
    let message = `请求失败：${response.status}`
    try {
      const data = (await response.json()) as { detail?: string }
      if (data.detail) message = data.detail
    } catch {
      message = await response.text()
    }
    throw new Error(message)
  }
  return response.json() as Promise<Task2ExportResponse>
}

export const getTask2ResultFileUrl = () => '/api/task2/result-file'

// ── Table CRUD ──
interface TableDataResponse {
  total: number
  page: number
  limit: number
  columns: { key: string; label: string }[]
  rows: Record<string, unknown>[]
}

export const fetchTableData = (
  tableName: string, page = 1, limit = 50,
  filters?: { search?: string; stock_code?: string; report_period?: string }
) => {
  const params = new URLSearchParams({ page: String(page), limit: String(limit) })
  if (filters?.search) params.set('search', filters.search)
  if (filters?.stock_code) params.set('stock_code', filters.stock_code)
  if (filters?.report_period) params.set('report_period', filters.report_period)
  return request<TableDataResponse>(`/api/database/table/${tableName}?${params}`)
}

export const insertTableRow = (tableName: string, data: Record<string, unknown>) =>
  request<{ ok: boolean; message: string }>(`/api/database/table/${tableName}`, {
    method: 'POST',
    body: JSON.stringify(data)
  })

export const deleteTableRow = (tableName: string, stockCode: string, reportPeriod: string) =>
  request<{ ok: boolean; message: string }>(
    `/api/database/table/${tableName}?stock_code=${encodeURIComponent(stockCode)}&report_period=${encodeURIComponent(reportPeriod)}`,
    { method: 'DELETE' }
  )

// ── Knowledge Base ──
export interface KbDocument {
  id: number
  doc_type: string
  doc_name: string
  doc_path: string
  stock_code: string | null
  stock_name: string | null
  report_date: string | null
  index_started_at: string | null
  total_chunks: number
  build_seconds: number | null
  indexed_at: string | null
  status: 'pending' | 'indexing' | 'done' | 'error'
}

export interface KbChunk {
  chunk_id: number
  chunk_idx: number
  source_title: string
  score: number
  text: string
  doc_name: string
  doc_path: string
  doc_type: string
  stock_name: string | null
}

export const fetchKbDocuments = () => request<KbDocument[]>('/api/knowledge/documents')

export const ingestAttachment5 = () =>
  request<{ triggered: number; documents: { id: number; doc_name: string; status: string }[] }>(
    '/api/knowledge/ingest', { method: 'POST' }
  )

export const reindexKb = () =>
  request<{ triggered: number; documents: { id: number; doc_name: string; status: string }[] }>(
    '/api/knowledge/reindex', { method: 'POST' }
  )

export const uploadKbPdf = async (file: File, docType = '用户上传'): Promise<KbDocument> => {
  const form = new FormData()
  form.append('file', file)
  form.append('doc_type', docType)
  const resp = await fetch('/api/knowledge/upload', { method: 'POST', body: form })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText })) as { detail?: string }
    throw new Error(err.detail ?? '上传失败')
  }
  return resp.json() as Promise<KbDocument>
}

export const deleteKbDocument = (id: number) =>
  request<{ ok: boolean }>(`/api/knowledge/documents/${id}`, { method: 'DELETE' })

export interface KbChunkDetail {
  id: number
  chunk_idx: number
  text: string
  parent_text: string | null
  page_no: number | null
  chunk_type: string | null
  section_title: string | null
}

export const fetchKbDocumentChunks = (docId: number) =>
  request<KbChunkDetail[]>(`/api/knowledge/documents/${docId}/chunks`)

export const getKbDocumentFileUrl = (docId: number) =>
  `/api/knowledge/documents/${docId}/file`

export const searchKb = (q: string, topK = 5) =>
  request<KbChunk[]>(`/api/knowledge/search?q=${encodeURIComponent(q)}&top_k=${topK}`)

// ── Task 3 ──
export const fetchTask3Questions = () =>
  request<{ count: number; file: string | null }>('/api/task3/questions')

export const exportTask3Results = () =>
  request<{ questionCount: number; workbookUrl: string }>('/api/task3/export', { method: 'POST' })

export const exportTask3WithFile = async (file: File): Promise<{ questionCount: number }> => {
  const form = new FormData()
  form.append('file', file)
  const resp = await fetch('/api/task3/export', { method: 'POST', body: form })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText })) as { detail?: string }
    throw new Error(err.detail ?? '任务三批量处理失败')
  }
  return resp.json() as Promise<{ questionCount: number }>
}

// ── Database Overview ──
export interface DbOverviewMetric {
  title: string; value: string; subtitle: string
}
export interface DbConnection {
  name: string; type: string; database: string; status: string; lastSync: string
}
export interface DbImportTask {
  name: string; status: string; detail: string
}
export interface DbQualityRule {
  name: string; scope: string; status: string; lastCheck: string
}
export interface DbOverview {
  metrics: DbOverviewMetric[]
  connections: DbConnection[]
  importTasks: DbImportTask[]
  qualityRules: DbQualityRule[]
}
export interface DbSchema {
  name: string; chineseName: string; rows: number
  fields: Array<{ name: string; type: string; description: string }>
  sampleRows: Record<string, unknown>[]
}

export const fetchDbOverview = () =>
  request<DbOverview>('/api/database/overview')

export const fetchDbSchemas = () =>
  request<DbSchema[]>('/api/database/schemas')

// ── Import Logs ──
export interface ImportLogRow {
  job_id: string; started_at: string; finished_at: string
  total_reports: number; success_reports: number
  warning_count: number; error_count: number
}
export interface ImportIssueRow {
  id: number; job_id: string; table_name: string; field_name: string
  stock_code: string; report_period: string
  issue_type: string; issue_level: string; message: string; created_at: string
}

export const fetchImportLogs = (page = 1, limit = 20) =>
  request<{ total: number; rows: ImportLogRow[] }>(`/api/database/import-logs?page=${page}&limit=${limit}`)

export const fetchImportIssues = (page = 1, limit = 50, jobId?: string) => {
  let url = `/api/database/import-issues?page=${page}&limit=${limit}`
  if (jobId) url += `&job_id=${encodeURIComponent(jobId)}`
  return request<{ total: number; rows: ImportIssueRow[] }>(url)
}

// ── Build Actions ──
export const databaseInit = () =>
  request<{ ok: boolean; message: string }>('/api/database/init', { method: 'POST' })

export const databaseBatchImport = () =>
  request<{ ok: boolean; message: string; task_id: string }>('/api/database/batch-import', { method: 'POST' })

export const fetchBuildStatus = (taskId: string) =>
  request<{ status: string; message: string; started?: string; finished?: string }>(`/api/build-status/${taskId}`)

// ── Quality Report ──
export interface QualityScoreComponent {
  name: string
  score: number
  weight: number
  contribution: number
  metrics: Record<string, number | null>
}

export interface QualityReport {
  generated_at: string
  db_path: string
  overview: {
    company_count: number
    report_group_count: number
    table_rows: Record<string, number>
    latest_job: {
      job_id: string
      started_at: string
      finished_at: string
      total_reports: number
      success_reports: number
      warning_count: number
      error_count: number
    } | null
  }
  score_panel: {
    overall_score: number
    grade: string
    components: QualityScoreComponent[]
    note: string
  }
  completeness: {
    overall: { overall_cell_fill_rate: number | null; total_filled_cells: number; total_possible_cells: number }
    table_summary: Array<{
      table_name: string; table_label: string; row_count: number
      cell_fill_rate: number | null; key_complete_rate: number | null
    }>
  }
  provenance: {
    summary: { traceability_rate: number | null; page_traceability_rate: number | null; avg_confidence: number | null }
    method_breakdown: Array<{ extract_method: string; record_count: number; rate: number | null }>
  }
  issues: {
    summary: { scope: string; total_issues: number; warning_count: number; error_count: number }
    by_type: Array<{ issue_type: string; issue_level: string; issue_count: number }>
    by_table: Array<{ table_name: string; issue_count: number }>
  }
  interpretation: {
    overview: string
    strengths: string[]
    risks: string[]
    actions: string[]
    weakest_component: string
  }
}

export const fetchQualityReport = () =>
  request<QualityReport>('/api/database/quality-report')

export const generateQualityReport = () =>
  request<{ ok: boolean; report_dir: string; html: string }>('/api/database/quality-report/generate', { method: 'POST' })

export interface QualityReportHistoryItem {
  name: string
  timestamp: string
  has_html: boolean
  charts: string[]
}

export const fetchQualityReportHistory = () =>
  request<QualityReportHistoryItem[]>('/api/database/quality-reports')

export const getQualityReportChartUrl = (reportName: string, chartName: string) =>
  `/api/database/quality-report/${encodeURIComponent(reportName)}/chart/${encodeURIComponent(chartName)}`

export const getTask3ResultFileUrl = () => '/api/task3/result-file'

export const getPdfPageImageUrl = (
  file: string,
  page: number,
  highlight?: string,
  dpi = 200
): string => {
  const params = new URLSearchParams({ file, page: String(page), dpi: String(dpi) })
  if (highlight) params.append('highlight', highlight)
  return `/api/pdf/page-image?${params}`
}

export const fetchPdfInfo = (file: string) =>
  request<{ file: string; page_count: number }>(`/api/pdf/info?file=${encodeURIComponent(file)}`)
