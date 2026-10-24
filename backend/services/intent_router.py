"""Function-Calling + Chain-of-Thought intent router.

Replaces the legacy regex + JSON-prompt intent pipeline with a modern
tool-use approach.  The LLM sees each query type as a callable *tool*
and autonomously decides:
  - **which** tool(s) to invoke  → determines query_kind (+ multi-intent)
  - **what arguments** to pass   → extracts company / period / metric slots

When multiple tool_calls are returned in one response, the question is
automatically treated as multi-intent — no separate detection step needed.

Requires an LLM that supports the OpenAI-compatible ``tools`` / ``tool_calls``
interface (DeepSeek-V3, Qwen-plus, GLM-4-plus, etc.).  Configure via the
``intent_llm`` block in config.json; falls back to the main ``llm`` block.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .llm import intent_llm_available, request_llm_with_tools


# ── Tool definitions ─────────────────────────────────────────────────────────

def _build_metric_enum(table_metric_columns: dict[str, dict[str, str]]) -> list[str]:
    """Collect all unique metric column names across tables."""
    seen: set[str] = set()
    out: list[str] = []
    for cols in table_metric_columns.values():
        for col in cols:
            if col not in seen:
                seen.add(col)
                out.append(col)
    return out


def build_tools(
    company_names: list[str],
    table_metric_columns: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Return OpenAI-compatible tool definitions for intent routing."""
    metric_enum = _build_metric_enum(table_metric_columns)
    metric_desc_lines: list[str] = []
    for tbl, cols in table_metric_columns.items():
        for col, label in cols.items():
            metric_desc_lines.append(f'{col} = {label}')
    metric_desc = '\n'.join(metric_desc_lines)

    return [
        {
            'type': 'function',
            'function': {
                'name': 'query_metric',
                'description': (
                    '查询某公司某报告期的一个或多个财务指标（单值查询）。'
                    '适用场景：用户问某公司某个指标的具体数值，如"同仁堂2024年净利润是多少"'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'company': {
                            'type': 'string',
                            'description': '公司简称（必须是已知公司列表中的名称）',
                        },
                        'period': {
                            'type': 'string',
                            'description': '报告期代码，格式为YYYYFY/YYYYHY/YYYYQ1/YYYYQ3/YYYYQ3C，如2024FY、2024Q3。"前三季度"用Q3C（累计），"第三季度"用Q3（单季）。如果用户未明确指定则留空',
                        },
                        'metrics': {
                            'type': 'array',
                            'items': {'type': 'string', 'enum': metric_enum},
                            'description': f'要查询的指标字段名列表。可选值及含义：\n{metric_desc}',
                        },
                    },
                    'required': ['company', 'metrics'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'query_trend',
                'description': (
                    '查看某公司某指标的多期趋势变化（折线图）。'
                    '适用场景：用户问趋势、变化、走势、逐年、近几年、增长情况等，'
                    '如"白云山近三年净利润变化趋势"、"同仁堂营收逐年走势"'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'company': {
                            'type': 'string',
                            'description': '公司简称',
                        },
                        'metrics': {
                            'type': 'array',
                            'items': {'type': 'string', 'enum': metric_enum},
                            'description': '要查看趋势的指标字段名列表',
                        },
                    },
                    'required': ['company', 'metrics'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'query_ranking',
                'description': (
                    '对所有公司按某财务指标排名（柱状图）。'
                    '适用场景：用户问排名、最高、最低、哪家最多、谁最赚钱、Top N、龙头等，'
                    '如"2024年净利润最高的公司"、"哪家公司营收排第一"、"中药行业龙头"'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'metric': {
                            'type': 'string',
                            'enum': metric_enum,
                            'description': '排名所用的指标字段名',
                        },
                        'period': {
                            'type': 'string',
                            'description': '报告期代码（YYYYFY等），用户未指定则留空',
                        },
                        'direction': {
                            'type': 'string',
                            'enum': ['desc', 'asc'],
                            'description': '排序方向：desc=从高到低（默认），asc=从低到高',
                        },
                        'limit': {
                            'type': 'integer',
                            'description': '返回前N名，默认10',
                        },
                    },
                    'required': ['metric'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'query_cash_flow',
                'description': (
                    '查看某公司某报告期的现金流结构（饼图）。'
                    '适用场景：用户问现金流、经营活动现金流、投资活动现金流、筹资活动现金流等，'
                    '如"片仔癀2024年现金流情况"、"同仁堂的经营现金流怎么样"'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'company': {
                            'type': 'string',
                            'description': '公司简称',
                        },
                        'period': {
                            'type': 'string',
                            'description': '报告期代码，用户未指定则留空',
                        },
                    },
                    'required': ['company'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'general_chat',
                'description': (
                    '通用对话：闲聊、自我介绍、帮助说明、无法归类为财务查询的问题。'
                    '适用场景：用户问"你是谁"、"你能做什么"、打招呼、感谢、告别等'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'topic': {
                            'type': 'string',
                            'description': '对话主题简述',
                        },
                    },
                    'required': [],
                },
            },
        },
    ]


# ── CoT system prompt ────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是中药上市公司财务数据库的**意图路由器**。

## 工作流程（Chain-of-Thought）
收到用户问题后，请按以下步骤思考，然后调用对应的工具：

1. **识别子问题数量**：问题中包含几个*独立的*查询意图？
   - "同一指标 + 多家公司"是 1 个意图（调用 1 次工具）
   - "不同指标/不同表"是多个意图（调用多次工具）
   - "排名 + 追问这些公司的其他指标"是 2 个意图

2. **对每个子问题**：判断属于哪种查询类型，提取参数：
   - 具体数值查询 → query_metric
   - 趋势/变化/历年 → query_trend
   - 排名/最高/最低/哪家最多 → query_ranking
   - 现金流结构 → query_cash_flow
   - 闲聊/帮助 → general_chat

3. **提取参数**：
   - company：只填用户明确提到的公司名，不要猜测
   - period：统一转为 YYYYFY/YYYYHY/YYYYQ1/YYYYQ3/YYYYQ3C 格式
     - "前三季度"→ YYYYQ3C（前三季度累计），"第三季度"→ YYYYQ3（仅第三季度单季）
   - metrics/metric：从支持字段列表中选最匹配的字段名

4. **调用工具**：
   - 单意图 → 调用 1 个工具
   - 多意图 → 同时调用多个工具（parallel tool calls）

## 公司别名识别
用户可能使用简称、数字代号或品牌名来指代公司，你需要将其映射为**官方简称**后填入company参数：
- 数字代号："999" → "华润三九", "538" → "云南白药"
- 品牌名："白药" → "云南白药", "三九" → "华润三九", "阿胶" → "东阿阿胶", "片仔" → "片仔癀"
- 简称："同仁" → "同仁堂", "太极" → "太极集团"
- 股票代码："000999" → "华润三九", "600332" → "白云山"

## 时间词解析
- "去年""上一年" → 留空period（由后端根据数据库最新数据自动解析）
- "前年" → 留空period（由后端解析）
- "今年" → 留空period（由后端解析）
- "最新""最近" → 留空period（由后端解析）
- 明确年份如"2024年" → 2024FY

## 指标别名识别
- "收益率""回报率" → roe
- "利润率" → net_profit_margin
- "扣非""扣非利润" → net_profit_excl_non_recurring
- "营收增速""收入增长" → operating_revenue_yoy_growth
- "利润增速" → net_profit_yoy_growth

## 注意事项
- **绝不编造**公司名、字段名或报告期
- 用户没有明确说的参数，就不要填（留空或不传）
- 如果问题模糊但偏向财务查询，优先选财务工具而不是 general_chat
- "近三年""历年""逐年"类问题一律用 query_trend
- "XX和YY的净利润对比"如果是同一指标多公司，用 query_metric；如果问不同类型数据，拆成多个工具调用
"""


# ── Parse tool_calls into structured intents ─────────────────────────────────

_TOOL_KIND_MAP: dict[str, tuple[str, str]] = {
    'query_metric':    ('metric',    'single'),
    'query_trend':     ('metric',    'trend'),
    'query_ranking':   ('ranking',   'ranking'),
    'query_cash_flow': ('cash_flow', 'single'),
    'general_chat':    ('general',   'general'),
}


def _parse_tool_call(tc: dict[str, Any]) -> dict[str, Any]:
    """Parse one tool_call into a structured intent dict."""
    func = tc.get('function', {})
    name = func.get('name', '')
    args_raw = func.get('arguments', '{}')
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {}

    qk, am = _TOOL_KIND_MAP.get(name, ('general', 'general'))

    period = str(args.get('period', '') or '').strip()
    company = str(args.get('company', '') or '').strip()

    # ── 别名后处理：LLM 可能返回非官方名称，尝试解析 ──
    if company:
        try:
            from ..core.context import COMPANY_NAMES, resolve_company_alias
            if company not in COMPANY_NAMES:
                resolved = resolve_company_alias(company)
                if resolved:
                    company = resolved
        except ImportError:
            pass

    # normalize metrics (tool may use 'metrics' list or 'metric' single)
    metrics_raw = args.get('metrics') or args.get('metric')
    if isinstance(metrics_raw, str):
        metrics = [metrics_raw]
    elif isinstance(metrics_raw, list):
        metrics = [str(m) for m in metrics_raw if m]
    else:
        metrics = []

    direction = str(args.get('direction', 'desc')).strip()
    limit = args.get('limit')
    if isinstance(limit, str):
        try:
            limit = int(limit)
        except ValueError:
            limit = None

    return {
        'tool_name': name,
        'query_kind': qk,
        'analysis_mode': am,
        'company': company,
        'period': period,
        'metrics': metrics,
        'direction': direction,
        'limit': limit,
        'description': args.get('topic', ''),
    }


def _fallback_from_content(content: str) -> list[dict[str, Any]]:
    """If the model returns content instead of tool_calls, attempt JSON parse."""
    if not content:
        return []
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        matched = re.search(r'[\[{].*[\]}]', content, re.S)
        if not matched:
            return []
        try:
            obj = json.loads(matched.group(0))
        except json.JSONDecodeError:
            return []
    items = obj if isinstance(obj, list) else [obj]
    intents: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        qk = str(item.get('queryKind') or item.get('query_kind') or 'general').strip()
        am = str(item.get('analysisMode') or item.get('analysis_mode') or 'general').strip()
        intents.append({
            'tool_name': '',
            'query_kind': qk if qk in {'metric', 'cash_flow', 'ranking', 'general'} else 'general',
            'analysis_mode': am if am in {'single', 'trend', 'ranking', 'general'} else 'general',
            'company': str(item.get('company', '')).strip(),
            'period': str(item.get('period', '')).strip(),
            'metrics': [str(item.get('metric', ''))] if item.get('metric') else [],
            'direction': 'desc',
            'limit': None,
            'description': '',
        })
    return intents


# ── Public API ───────────────────────────────────────────────────────────────

def route_intent(
    question: str,
    history: list[dict[str, Any]],
    company_names: list[str],
    table_metric_columns: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Route a user question via function-calling LLM.

    Returns a list of parsed intents (1 for single-intent, >=2 for multi-intent).
    Raises RuntimeError if intent_llm is not configured.
    """
    if not intent_llm_available():
        raise RuntimeError('intent_llm 不可用')

    tools = build_tools(company_names, table_metric_columns)

    # Build user prompt with recent history context
    recent = []
    for item in history[-8:]:
        role = item.get('role', '')
        text = str(item.get('content', ''))[:300]
        if role == 'assistant':
            meta = item.get('meta')
            if isinstance(meta, dict):
                parts = []
                for mk in ('company', 'metric', 'report_period', 'query_kind'):
                    mv = str(meta.get(mk, '')).strip()
                    if mv and mv != 'None':
                        parts.append(f'{mk}={mv}')
                if parts:
                    text = f'{text[:200]} [{", ".join(parts)}]'
            recent.append(f'assistant: {text}')
        elif role == 'user':
            recent.append(f'user: {text}')
    history_text = '\n'.join(recent) if recent else '（无历史）'

    user_prompt = (
        f'最近对话：\n{history_text}\n\n'
        f'当前问题：{question}\n\n'
        f'支持的公司列表：{json.dumps(company_names, ensure_ascii=False)}\n'
    )

    message = request_llm_with_tools(
        _SYSTEM_PROMPT, user_prompt, tools, temperature=0.1,
    )

    # Parse tool_calls
    tool_calls = message.get('tool_calls') or []
    if tool_calls:
        return [_parse_tool_call(tc) for tc in tool_calls]

    # Fallback: some models return content instead of tool_calls
    content = str(message.get('content', ''))
    fallback = _fallback_from_content(content)
    if fallback:
        return fallback

    # Ultimate fallback: general
    return [{'tool_name': '', 'query_kind': 'general', 'analysis_mode': 'general',
             'company': '', 'period': '', 'metrics': [], 'direction': 'desc',
             'limit': None, 'description': ''}]
