import type { AppMetric, ChatMessage, PipelineStep, QuickPrompt } from '../types'

const createId = () => `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`

export const assistantMetrics: AppMetric[] = [
  { title: '任务二流程', value: '自然语言 → SQL → 图表 → 结论', subtitle: '覆盖单轮查询、多轮追问、意图澄清' },
  { title: '当前模型', value: 'glm-4-flash', subtitle: '后续可替换为你的真实推理服务' },
  { title: '结构化底座', value: '4 张核心财务表', subtitle: 'income / cash_flow / balance / core_indicators' },
  { title: '输出产物', value: 'JSON + result_2.xlsx', subtitle: '界面预留了提交结果映射能力' }
]

export const promptSuggestions: QuickPrompt[] = [
  { label: '利润总额单轮查询', value: '某公司最近一期利润总额是多少？' },
  { label: '利润趋势分析', value: '某公司近几年的利润总额变化趋势是什么样的？' },
  { label: 'Top10 统计', value: '最近一个完整年度利润最高的前十家企业是哪些？' },
  { label: '现金流分析', value: '某公司最近一期经营活动现金流如何？' }
]

export const pipelineSteps: PipelineStep[] = [
  { title: '意图识别', description: '抽取公司、指标、时间范围与图表偏好' },
  { title: 'SQL 生成', description: '将自然语言映射到结构化财报 SQL' },
  { title: '结果检索', description: '查询 SQLite / MySQL 财务数据库并格式化' },
  { title: '图表生成', description: '根据结果自动选择折线图、柱状图或表格' },
  { title: '结论输出', description: '产出中文分析结论并保留提交所需结构' }
]

export const defaultSqlDraft = `SELECT stock_abbr, report_period, total_profit
FROM income_sheet
ORDER BY total_profit DESC
LIMIT 10;`

export const createWelcomeMessage = (): ChatMessage => ({
  id: createId(),
  role: 'assistant',
  content:
    '你好，我是上市公司财报智能问数助手。你可以直接问我利润总额、营收趋势、现金流情况，也可以连续追问报告期，我会自动补全 SQL、结果表格和图表结论。',
  followUpHints: ['某公司最近一期利润总额是多少？', '某公司近几年的利润总额变化趋势是什么样的？'],
  status: 'done'
})
