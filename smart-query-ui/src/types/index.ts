export type MessageRole = 'user' | 'assistant'

export interface QueryColumn {
  key: string
  label: string
}

export interface QueryRow {
  [key: string]: string | number
}

export interface ChartPayload {
  title: string
  typeLabel: string
  option: Record<string, any>
  spec?: Record<string, any>
  reason?: string
}

export interface MessageMeta {
  intent?: string
  company?: string
  metric?: string
  required?: string
  query_kind?: string
  analysis_mode?: string
  report_period?: string
}

export interface KbReference {
  paper_path: string
  text: string
  highlight_text?: string
  doc_name: string
  doc_type: string
  score?: number
  page_no?: number | null
  highlight?: string
}

export interface AttributionStep {
  label: string
  status: 'done' | 'error' | 'pending'
  detail: string
}

export interface DataSummary {
  tables: string[]
  row_count: number
  columns: string[]
  table_labels: string[]
}

export interface ThinkingMapNode {
  id: string
  label: string
  detail?: string
  type: 'question' | 'process' | 'data' | 'knowledge' | 'source' | 'synthesis' | 'result'
  edge_label?: string
  children: ThinkingMapNode[]
}

export interface Attribution {
  steps: AttributionStep[]
  sql: string
  kb_sources: KbReference[]
  data_summary?: DataSummary
  thinking_map?: ThinkingMapNode
}

export interface ProvenanceRecord {
  id: number
  table_name: string
  stock_code: string
  report_period: string
  field_name: string
  field_value: string | number | null
  source_file: string
  source_page: number | null
  source_table_name: string | null
  source_text: string | null
  confidence: number | null
  extract_method: string | null
}

export interface CitationRef {
  provenanceId: number
  type: 'evidence' | 'calculation'
  calcTraceId?: string
  label?: string
}

export interface CalcOperand {
  label: string
  fieldName?: string
  value: string | number | null
  displayValue: string
  reportPeriod?: string
  evidenceIds: number[]
  evidences?: ProvenanceRecord[]
}

export interface CalcStep {
  id: string
  title: string
  expression: string
  result: string
  operands: CalcOperand[]
}

export interface CalcTrace {
  id: string
  provenanceId: number
  fieldName: string
  title: string
  traceType: 'formula' | 'ratio' | 'growth' | 'derived' | 'source_value'
  finalValue: string
  formula?: string
  summary?: string
  reportPeriod?: string
  evidenceIds: number[]
  steps: CalcStep[]
}

export interface TaskResult {
  id: number
  description: string
  sql?: string
  columns?: QueryColumn[]
  rows?: QueryRow[]
  chart?: ChartPayload
  error?: string | null
}

export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  sql?: string
  chart?: ChartPayload
  columns?: QueryColumn[]
  rows?: QueryRow[]
  followUpHints?: string[]
  sourceTables?: string[]
  insights?: string[]
  references?: KbReference[]
  taskResults?: TaskResult[]
  attribution?: Attribution
  provenance?: ProvenanceRecord[]
  calcTraces?: CalcTrace[]
  citationRefs?: CitationRef[]
  status?: 'clarify' | 'done'
  meta?: MessageMeta
  clarifyOptions?: string[]
  clarifyMissing?: string
  aborted?: boolean
}

export interface QuickPrompt {
  label: string
  value: string
}

export interface PipelineStep {
  title: string
  description: string
}

export interface AppMetric {
  title: string
  value: string
  subtitle: string
}

export interface DatabaseConnection {
  name: string
  type: string
  database: string
  status: '运行中' | '待接入' | '离线'
  lastSync: string
}

export interface ImportTask {
  name: string
  status: '已完成' | '执行中' | '待处理'
  detail: string
}

export interface TableField {
  name: string
  type: string
  description: string
}

export interface TableSchema {
  name: string
  chineseName: string
  rows: number
  fields: TableField[]
  sampleRows: QueryRow[]
}

export interface QualityRule {
  name: string
  scope: string
  status: '通过' | '关注' | '待执行'
  lastCheck: string
}

export interface KnowledgeBaseSettings {
  index_concurrency: number
  embed_batch_size: number
  embed_max_chars: number
  child_chunk_min: number
  child_chunk_max: number
  parent_chunk_max: number
  chunk_overlap: number
  ocr_dpi: number
  ocr_workers: number
  bm25_weight: number
  vector_weight: number
  rrf_k: number
  rerank_candidates: number
  top_k: number
}

export interface SystemSettings {
  database: {
    path: string
  }
  reports_dir: string
  research_reports_dir: string
  parallel: {
    max_workers: number
  }
  knowledge_base: KnowledgeBaseSettings
  assistant: {
    enable_multi_intent: boolean
    kb_top_k: number
    default_auto_chart: boolean
    default_enable_context: boolean
  }
  llm: {
    base_url: string
    model: string
    has_api_key: boolean
    api_key_masked: string
    request_timeout: number
    intent_temperature: number
    query_plan_temperature: number
    analysis_temperature: number
    general_reply_temperature: number
    planner_decompose_temperature: number
    synthesis_temperature: number
    ref_extract_temperature: number
    available?: boolean
  }
  extract_llm: {
    base_url: string
    model: string
    has_api_key: boolean
    api_key_masked: string
    request_timeout: number
  }
  intent_llm: {
    base_url: string
    model: string
    has_api_key: boolean
    api_key_masked: string
    request_timeout: number
  }
  embedding: {
    base_url: string
    model: string
    reranker_model: string
    has_api_key: boolean
    api_key_masked: string
  }
}

export interface SystemSettingsPayload {
  database: {
    path: string
  }
  reports_dir: string
  research_reports_dir: string
  parallel: {
    max_workers: number
  }
  knowledge_base?: Partial<KnowledgeBaseSettings>
  assistant: {
    enable_multi_intent: boolean
    kb_top_k: number
    default_auto_chart: boolean
    default_enable_context: boolean
  }
  llm: {
    base_url: string
    model: string
    api_key?: string
    clear_api_key?: boolean
    request_timeout: number
    intent_temperature: number
    query_plan_temperature: number
    analysis_temperature: number
    general_reply_temperature: number
    planner_decompose_temperature: number
    synthesis_temperature: number
    ref_extract_temperature: number
  }
  extract_llm: {
    base_url: string
    model: string
    api_key?: string
    clear_api_key?: boolean
    request_timeout: number
  }
  intent_llm: {
    base_url: string
    model: string
    api_key?: string
    clear_api_key?: boolean
    request_timeout: number
  }
  embedding: {
    base_url: string
    model: string
    reranker_model: string
    api_key?: string
    clear_api_key?: boolean
  }
}
