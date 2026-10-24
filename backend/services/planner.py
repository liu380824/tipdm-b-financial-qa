"""Multi-intent autonomous planner for 智能问数助手.

Pipeline:
  1. Detect whether the question contains multiple independent intents.
  2. LLM decomposes question into an ordered list of sub-tasks (with dependencies).
  3. Execute each sub-task sequentially; later tasks may inherit company list from
     an earlier task's result rows.
  4. Final LLM synthesises all sub-task results into a single coherent reply.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..core.db import create_id, query_rows
from .llm import llm_available, llm_temperature, request_llm_json, request_llm_array
from .visualization import auto_generate_chart_payload

# reuse column/table metadata from assistant
from .assistant import (
    TABLE_METRIC_COLUMNS,
    ALLOWED_TABLES,
    TABLE_LABELS,
    format_period,
    percent_text,
    _recent_history,
    _retrieve_kb_context,
    _normalize_text_list,
)

# ── helpers ───────────────────────────────────────────────────────────────────

_MULTI_INTENT_SIGNALS = re.compile(
    r'(？.*？|哪些.*哪家|同时|分别|另外|以及|此外|还有|并且|同时给出|以及.*情况|顺便|除了.*还|一方面.*另一方面|不仅.*还|既.*又|先.*再|想看|想了解'
    r'|；.*；'               # Chinese semicolons separating multiple tasks
    r'|①|②|③|④|⑤'          # circled number markers
    r'|\d+[.、].*\d+[.、]'  # numbered list markers: 1. ... 2. ...
    r'|统计.*计算|计算.*找出|找出.*生成|统计.*生成|统计.*找出|计算.*生成'  # verb chains
    r')'
)

# Topics that imply distinct sub-queries when co-occurring
_TOPIC_KEYWORDS = [
    r'净利润', r'营业收入|营收|财务收入', r'现金流', r'资产负债|资产负债表|负债',
    r'毛利率', r'利润总额', r'ROE|净资产收益率', r'每股收益|EPS',
    r'研发费用|研发投入|研发支出', r'增长率|增速|同比|环比',
    r'散点图|柱状图|折线图|饼图|图表', r'排名|前\d|TOP',
    r'销售毛利率|销售净利率', r'资产负债率',
]


def _count_topics(question: str) -> int:
    """Count distinct financial topic groups mentioned."""
    return sum(1 for pat in _TOPIC_KEYWORDS if re.search(pat, question))


def _is_multi_intent_rule(question: str) -> bool | None:
    """Rule-based fast path: return True/False if confident, None if uncertain."""
    q = question.strip()
    # 明确多问号 → 大概率多意图
    if q.count('？') >= 2 or q.count('?') >= 2:
        return True
    # ≥2 个不同财务主题关键词 → 大概率多意图
    if _count_topics(q) >= 2:
        return True
    # 很短的问题（<15字）或无连接信号 → 大概率单意图
    if len(q) < 15 or not _MULTI_INTENT_SIGNALS.search(q):
        return False
    # 有连接词但不确定（如"以及"可能是多公司单意图） → 交给 LLM
    return None


def _is_multi_intent_llm(question: str) -> bool:
    """Use LLM to determine if the question contains multiple independent intents."""
    if not llm_available():
        return False
    system_prompt = (
        '你是一个查询意图分类器。判断用户问题是否包含多个**独立的**查询意图。\n'
        '只返回 JSON：{"multi":true/false,"reason":"..."}\n\n'
        '判断标准：\n'
        '- "独立意图"指需要查询**不同表或不同类型指标**的子问题。\n'
        '- "同一指标查多家公司"（如"同仁堂和白云山的净利润"）是**单意图**。\n'
        '- "同一公司查利润和资产负债"是**多意图**（不同表）。\n'
        '- "排名+追问这些公司的其他指标"是**多意图**。\n\n'
        '示例：\n'
        '"同仁堂2024年净利润是多少" → {"multi":false,"reason":"单公司单指标"}\n'
        '"同仁堂以及白云山的净利润" → {"multi":false,"reason":"同一指标多公司"}\n'
        '"白云山的净利润和现金流情况" → {"multi":true,"reason":"净利润和现金流分属不同表"}\n'
        '"2024年净利润最高的公司是哪些？它们的资产负债率呢？" → {"multi":true,"reason":"排名查询+追问其他指标"}\n'
    )
    user_prompt = f'用户问题：{question}'
    try:
        result = request_llm_json(system_prompt, user_prompt, temperature=0.05)
        return bool(result.get('multi', False))
    except Exception:
        return False


def is_multi_intent(question: str) -> bool:
    """Return True when the question likely contains ≥2 independent sub-queries."""
    # LLM 优先，规则兜底
    if llm_available():
        return _is_multi_intent_llm(question)
    rule_result = _is_multi_intent_rule(question)
    return bool(rule_result)


# ── sub-task schema ───────────────────────────────────────────────────────────
#
# {
#   "id": int,
#   "description": str,          # human-readable label
#   "table": str,                 # one of ALLOWED_TABLES
#   "columns": [str, ...],        # column keys
#   "company": str | null | "FROM_TASK_<N>",
#   "period": str | null,         # e.g. "YYYYFY"
#   "analysis_mode": "single" | "trend" | "ranking",
#   "order_by": {"field": str, "direction": "asc"|"desc"} | null,
#   "limit": int | null,
#   "depends_on": [int, ...]      # ids of tasks whose results this task needs
# }

_SYSTEM_DECOMPOSE = (
    '你是财务数据库查询规划器。将用户问题分解为有序子任务列表，每个子任务对应一条 SQL 查询。'
    '请严格使用给定表和字段，不得编造。只返回 JSON。'
)


def _decompose_with_llm(question: str, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ask LLM to break question into sub-task dicts; return [] on failure."""
    if not llm_available():
        return []

    table_desc = '\n'.join(
        f'  {tbl}: {", ".join(f"{c}({lbl})" for c, lbl in cols.items())}'
        for tbl, cols in TABLE_METRIC_COLUMNS.items()
    )
    few_shot = (
        '=== 示例 ===\n'
        '问题: "同仁堂2024年的净利润是多少？同时告诉我它的资产负债情况"\n'
        '输出:\n'
        '[{"id":1,"description":"同仁堂2024年净利润","table":"income_sheet","columns":["net_profit"],'
        '"company":"同仁堂","period":"2024FY","analysis_mode":"single","order_by":null,"limit":null,"depends_on":[]},'
        '{"id":2,"description":"同仁堂2024年资产负债情况","table":"balance_sheet","columns":["total_assets","total_liabilities","asset_liability_ratio"],'
        '"company":"同仁堂","period":"2024FY","analysis_mode":"single","order_by":null,"limit":null,"depends_on":[]}]\n\n'
        '问题: "2024年净利润最高的前5家公司是哪些？这些公司的营业收入分别是多少？"\n'
        '输出:\n'
        '[{"id":1,"description":"2024年净利润排名前5","table":"income_sheet","columns":["net_profit"],'
        '"company":null,"period":"2024FY","analysis_mode":"ranking","order_by":{"field":"net_profit","direction":"desc"},"limit":5,"depends_on":[]},'
        '{"id":2,"description":"上述公司的营业收入","table":"income_sheet","columns":["total_operating_revenue"],'
        '"company":"FROM_TASK_1","period":"2024FY","analysis_mode":"single","order_by":null,"limit":null,"depends_on":[1]}]\n\n'
        '问题: "白云山近三年净利润的变化趋势以及现金流情况"\n'
        '输出:\n'
        '[{"id":1,"description":"白云山近三年净利润趋势","table":"income_sheet","columns":["net_profit","net_profit_yoy_growth"],'
        '"company":"白云山","period":null,"analysis_mode":"trend","order_by":null,"limit":null,"depends_on":[]},'
        '{"id":2,"description":"白云山近三年现金流","table":"cash_flow_sheet","columns":["operating_cf_net_amount","investing_cf_net_amount","financing_cf_net_amount"],'
        '"company":"白云山","period":null,"analysis_mode":"trend","order_by":null,"limit":null,"depends_on":[]}]\n\n'
        '问题: "同仁堂以及白云山2024年的净利润分别是多少"\n'
        '说明: 虽然有"以及"，但只问了一个指标(净利润)，属于单意图多公司，只需一个子任务。\n'
        '输出:\n'
        '[{"id":1,"description":"同仁堂和白云山2024年净利润","table":"income_sheet","columns":["net_profit"],'
        '"company":null,"period":"2024FY","analysis_mode":"single","order_by":null,"limit":null,"depends_on":[]}]\n'
        '=== 示例结束 ===\n\n'
    )
    user_prompt = (
        '请将以下问题拆分为有序子任务列表，返回 JSON 数组。\n\n'
        '**重要**：\n'
        '- 必须提取用户问题中所有独立的子问题，不得遗漏。\n'
        '- "同时""以及""另外""还有"连接的**不同指标或不同类型查询**是独立子问题。\n'
        '- 但"同一指标查多家公司"或"同一公司同一表的多个字段"应合并为一个子任务，不要过度拆分。\n\n'
        '每个子任务格式：\n'
        '{"id":1,"description":"...","table":"...","columns":["..."],'
        '"company":null或公司名或"FROM_TASK_<N>",'
        '"period":null或"YYYYFY"/"YYYYQ3"等,'
        '"analysis_mode":"single|trend|ranking",'
        '"order_by":null或{"field":"...","direction":"asc|desc"},'
        '"limit":null或数字,'
        '"depends_on":[]}\n\n'
        '规则：\n'
        '1. company 只允许已知公司名或 null，若需要沿用前一任务的结果公司列表填写 "FROM_TASK_<id>"。\n'
        '2. table 只允许：income_sheet、balance_sheet、cash_flow_sheet、core_performance_indicators_sheet。\n'
        '3. columns 只允许对应表中存在的字段。\n'
        '4. 子任务按执行顺序排列，depends_on 填写依赖的任务 id 列表。\n'
        '5. 如问题只有一个意图，也输出包含单元素的数组。\n'
        '6. 只返回 JSON 数组，不输出其他内容。\n'
        '7. "哪个公司最高/最低""排名""Top"类问题：analysis_mode 必须设为 "ranking"，'
        'company 设为 null（查全部公司），必须设置 order_by 和 limit（默认 limit=10）。\n\n'
        f'{few_shot}'
        f'支持的表和字段：\n{table_desc}\n\n'
        f'最近对话：{json.dumps(_recent_history(history), ensure_ascii=False)}\n'
        f'用户问题：{question}\n'
        f'(共应该拆解出的子问题数量估计：{max(2, _count_topics(question))}个，请不要遗漏)。\n'
    )
    try:
        tasks = request_llm_array(_SYSTEM_DECOMPOSE, user_prompt, temperature=llm_temperature('planner_decompose_temperature', 0.1))
    except Exception:
        return []
    return [t for t in tasks if isinstance(t, dict) and 'table' in t]


# ── rule-based fallback decomposer ───────────────────────────────────────────

_RANKING_PAT = re.compile(r'(top\s*\d+|TOP\s*\d+|最高|最低|排名|排行|排序|最大|最小|前\d+|从高到低|从低到高|所有.*?公司)', re.IGNORECASE)
_REFER_PREV_PAT = re.compile(r'(这些|那些|上述|以上|其中).*(企业|公司)')
_TREND_PAT = re.compile(r'(趋势|近几|变化|走势|历年|多年|过去.*年)')
_YOY_PAT = re.compile(r'(同比|增长|增速|涨幅|变动)')
_IMPLICIT_PREV_COMPANY_PAT = re.compile(r'(哪家企业|哪家公司|哪一家|哪只)')


# Delimiters for splitting multi-intent questions
_SPLIT_PAT = re.compile(r'[？?]|同时|另外|此外|还有|并且')
# Split on 以及 only when followed by a financial noun phrase
_YIJI_PAT = re.compile(r'以及(?=.*?[资产负债现金流利润营收毛利财务])')


def _split_question_parts(question: str) -> list[str]:
    """Split question into sub-question parts on various delimiters."""
    # First split on 以及 if it connects distinct financial topics
    q = _YIJI_PAT.sub('、分割、', question)
    parts = [p.strip() for p in _SPLIT_PAT.split(q) if p.strip()]
    return parts if len(parts) >= 2 else []


def _decompose_rule_based(question: str, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split question by delimiters into sub-queries; infer basic subtask schema for each.

    Works without LLM and provides a sensible fallback for common patterns like:
      "A是哪些？这些企业的B是多少？最大的是哪家？"
      "同时给出...A...以及...B..."
    """
    from .assistant import infer_company, infer_period, METRIC_ALIASES

    # split on question marks and connecting keywords
    parts = _split_question_parts(question)
    if not parts:
        # fallback: split on question marks only
        parts = [p.strip() for p in re.split(r'[？?]', question) if p.strip()]
    if len(parts) < 2:
        return []

    global_company = infer_company(question, history, allow_default=False) or None
    global_period = infer_period(question, history) or None
    subtasks: list[dict[str, Any]] = []
    for i, part in enumerate(parts):
        task_id = i + 1
        compact = re.sub(r'\s+', '', part)

        # infer company & period (rule-based, no LLM)
        company: str | None = infer_company(part, history, allow_default=False) or global_company
        period: str | None = infer_period(part, history) or global_period

        matched_columns: list[tuple[str, str]] = []

        def append_metric(table_name: str, column_name: str) -> None:
            pair = (table_name, column_name)
            if column_name in TABLE_METRIC_COLUMNS.get(table_name, {}) and pair not in matched_columns:
                matched_columns.append(pair)

        for alias, key in METRIC_ALIASES.items():
            if not alias or not key or alias not in compact:
                continue
            for tbl, tbl_cols in TABLE_METRIC_COLUMNS.items():
                if key in tbl_cols:
                    append_metric(tbl, key)
                    break

        if '利润总额' in compact or '利润最高' in compact or ('利润' in compact and '净利润' not in compact and '同比' not in compact):
            append_metric('income_sheet', 'total_profit')
        if '净利润' in compact or '利润同比' in compact or '利润增速' in compact or '利润涨幅' in compact:
            append_metric('income_sheet', 'net_profit')
        if '销售额' in compact or '营收' in compact or '营业收入' in compact or '营业总收入' in compact:
            append_metric('income_sheet', 'total_operating_revenue')
        # cash_flow_sheet
        if '现金流' in compact or '经营活动' in compact or '投资活动' in compact or '筹资活动' in compact or '融资活动' in compact:
            append_metric('cash_flow_sheet', 'operating_cf_net_amount')
            append_metric('cash_flow_sheet', 'investing_cf_net_amount')
            append_metric('cash_flow_sheet', 'financing_cf_net_amount')
        # balance_sheet
        if '资产负债' in compact or '总资产' in compact or '负债' in compact or '股东权益' in compact or '净资产' in compact:
            append_metric('balance_sheet', 'total_assets')
            append_metric('balance_sheet', 'total_liabilities')

        table = matched_columns[0][0] if matched_columns else 'income_sheet'
        base_columns: list[str] = []
        for tbl, key in matched_columns:
            if tbl == table and key not in base_columns:
                base_columns.append(key)
        if not base_columns:
            base_columns = ['net_profit']

        columns = list(base_columns)
        if _YOY_PAT.search(compact):
            yoy_columns: list[str] = []
            if '利润' in compact or '净利润' in compact or 'total_profit' in base_columns or 'net_profit' in base_columns:
                yoy_columns.append('net_profit_yoy_growth')
            if '销售额' in compact or '营收' in compact or '营业收入' in compact or '营业总收入' in compact or 'total_operating_revenue' in base_columns:
                yoy_columns.append('operating_revenue_yoy_growth')
            columns = yoy_columns or ['net_profit_yoy_growth']
            table = 'income_sheet'

        # determine mode
        if _TREND_PAT.search(compact):
            analysis_mode = 'trend'
            order_by = None
            limit = None
        elif _RANKING_PAT.search(compact):
            analysis_mode = 'ranking'
            direction = 'asc' if ('最低' in compact or '最小' in compact) else 'desc'
            order_by = {'field': columns[0], 'direction': direction}
            limit_match = re.search(r'(?:top|TOP|前)\s*(\d+)', part)
            limit = int(limit_match.group(1)) if limit_match else 10
            company = None  # ranking is cross-company
        else:
            analysis_mode = 'single'
            order_by = None
            limit = None

        # if this sub-question refers to "these companies" → inherit from prev task
        depends_on: list[int] = []
        if i > 0 and (
            _REFER_PREV_PAT.search(compact)
            or (_IMPLICIT_PREV_COMPANY_PAT.search(compact) and not global_company)
        ):
            company = f'FROM_TASK_{i}'
            depends_on = [i]

        # validate table
        if table not in ALLOWED_TABLES:
            table = 'income_sheet'
        tbl_cols = TABLE_METRIC_COLUMNS.get(table, {})
        columns = [c for c in columns if c in tbl_cols]
        if not columns:
            columns = ['net_profit_yoy_growth'] if _YOY_PAT.search(compact) and 'net_profit_yoy_growth' in tbl_cols else ['net_profit']

        subtasks.append({
            'id': task_id,
            'description': part[:60],
            'table': table,
            'columns': columns,
            'company': company,
            'period': period,
            'analysis_mode': analysis_mode,
            'order_by': order_by,
            'limit': limit,
            'depends_on': depends_on,
        })

    return subtasks


# ── sub-task execution ────────────────────────────────────────────────────────

def _build_sql_for_task(
    task: dict[str, Any],
    prev_results: dict[int, dict[str, Any]],
) -> tuple[str, tuple, list[dict[str, str]], str]:
    """Compile one sub-task dict → (exec_sql, params, col_spec, display_sql).

    Returns ('', (), [], '') if the task cannot be compiled.
    """
    table = str(task.get('table', '')).strip()
    if table not in ALLOWED_TABLES:
        return '', (), [], ''

    col_labels = TABLE_METRIC_COLUMNS.get(table, {})
    raw_cols = task.get('columns') or []
    if isinstance(raw_cols, str):
        raw_cols = [raw_cols]
    cols = [str(c).strip() for c in raw_cols if str(c).strip() in col_labels]
    if not cols:
        return '', (), [], ''

    analysis_mode = str(task.get('analysis_mode', 'single')).strip()
    order_by_raw = task.get('order_by')
    limit_raw = task.get('limit')
    period = str(task.get('period') or '').strip()

    # resolve company
    company_raw = task.get('company')
    companies: list[str] = []
    if isinstance(company_raw, str) and company_raw.startswith('FROM_TASK_'):
        try:
            dep_id = int(company_raw.split('_')[-1])
            dep_rows = prev_results.get(dep_id, {}).get('rows', [])
            for row in dep_rows:
                for col in ('stock_abbr', 'company', '公司'):
                    val = row.get(col)
                    if val and str(val).strip():
                        companies.append(str(val).strip())
                        break
        except Exception:
            pass
    elif company_raw and isinstance(company_raw, str):
        companies = [company_raw.strip()]

    # build SELECT
    select_clause = 'report_period, TRIM(stock_abbr) AS stock_abbr, ' + ', '.join(cols)

    where_parts: list[str] = []
    params: list[Any] = []

    if companies:
        placeholders = ','.join('?' * len(companies))
        where_parts.append(f'TRIM(stock_abbr) IN ({placeholders})')
        params.extend(companies)
    # Q3C → 数据库中使用 Q3，后续在 Python 层面合并 HY 数据
    _db_period = period.replace('Q3C', 'Q3') if period.endswith('Q3C') else period
    if analysis_mode == 'single' and _db_period:
        where_parts.append('report_period = ?')
        params.append(_db_period)
    elif analysis_mode == 'ranking' and _db_period:
        where_parts.append('report_period = ?')
        params.append(_db_period)
    where_parts.append(f'{cols[0]} IS NOT NULL')
    where_clause = ' AND '.join(where_parts)

    # ORDER BY（TEXT 列需 CAST 为数值以正确排序）
    _non_numeric_cols = {'report_period', 'report_year', 'stock_abbr', 'stock_code'}
    order_clause = ''
    if isinstance(order_by_raw, dict):
        field = str(order_by_raw.get('field', '')).strip()
        direction = str(order_by_raw.get('direction', 'desc')).strip().lower()
        if field in col_labels and direction in ('asc', 'desc'):
            _expr = f'CAST({field} AS REAL)' if field not in _non_numeric_cols else field
            order_clause = f' ORDER BY {_expr} {direction.upper()}'
    elif analysis_mode == 'ranking' and cols:
        _expr = f'CAST({cols[0]} AS REAL)' if cols[0] not in _non_numeric_cols else cols[0]
        order_clause = f' ORDER BY {_expr} DESC'
    elif analysis_mode == 'trend':
        order_clause = ' ORDER BY report_year ASC, report_period ASC'

    # LIMIT
    limit_clause = ''
    if isinstance(limit_raw, (int, float)) and 1 <= int(limit_raw) <= 200:
        limit_clause = f' LIMIT {int(limit_raw)}'
    elif analysis_mode == 'ranking' and not limit_clause:
        limit_clause = ' LIMIT 10'

    exec_sql = f'SELECT {select_clause} FROM {table} WHERE {where_clause}{order_clause}{limit_clause}'
    # display SQL (readable)
    disp_company = (
        f"TRIM(stock_abbr) IN ({','.join(repr(c) for c in companies)})"
        if companies else ''
    )
    disp_parts = ([disp_company] if disp_company else []) + [
        p for p in where_parts[len(companies and [1] or []):] if 'IN' not in p
    ]
    display_sql = (
        f"SELECT {select_clause} FROM {table} WHERE {' AND '.join(disp_parts)}"
        f"{order_clause}{limit_clause};"
    )
    col_spec = (
        [{'key': 'stock_abbr', 'label': '公司'}]
        + [{'key': c, 'label': col_labels.get(c, c)} for c in cols]
    )
    return exec_sql, tuple(params), col_spec, display_sql


def _execute_task(
    task: dict[str, Any],
    prev_results: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Execute one sub-task and return a result dict."""
    exec_sql, params, col_spec, display_sql = _build_sql_for_task(task, prev_results)
    result: dict[str, Any] = {
        'id': task.get('id', 0),
        'description': task.get('description', ''),
        'sql': display_sql,
        'columns': col_spec,
        'rows': [],
        'chart': None,
        'error': None,
    }
    if not exec_sql:
        result['error'] = '无法生成有效 SQL'
        return result
    try:
        rows = query_rows(exec_sql, params)
    except Exception as exc:
        result['error'] = str(exc)
        return result

    # post-process: add rank if ranking mode
    analysis_mode = str(task.get('analysis_mode', 'single'))
    if analysis_mode == 'ranking':
        for i, row in enumerate(rows):
            row['rank'] = i + 1  # type: ignore[index]

    result['rows'] = rows

    # detect data coverage gaps
    requested_limit = task.get('limit')
    if requested_limit is None and analysis_mode == 'ranking':
        requested_limit = 10
    if requested_limit is not None:
        try:
            requested_limit = int(requested_limit)
        except (TypeError, ValueError):
            requested_limit = None
    if requested_limit and len(rows) < requested_limit:
        result['data_coverage_note'] = (
            f'要求{requested_limit}条数据，实际仅返回{len(rows)}条（当前数据库收录范围有限）'
        )
    elif not rows:
        result['data_coverage_note'] = '该查询未返回任何数据'

    result['chart'] = auto_generate_chart_payload(
        str(task.get('description', '')).strip() or str(task.get('table', '')).strip(),
        col_spec,
        rows,
        title=str(task.get('description', '')).strip(),
        analysis_mode=analysis_mode,
        existing_chart=None,
    )

    return result


# ── final synthesis ───────────────────────────────────────────────────────────

def _format_metric_value(column: str, value: Any) -> str:
    if value is None or value == '':
        return '暂无数据'
    if isinstance(value, (int, float)):
        if column.endswith('_growth') or column.endswith('_ratio') or 'margin' in column:
            return percent_text(value)
        return f'{float(value):.2f}'
    return str(value)


def _rule_based_synthesis(task_results: list[dict[str, Any]]) -> dict[str, Any]:
    default_hints = ['查看某企业的详细趋势', '对比不同报告期的数据']
    content_parts: list[str] = []
    insights: list[str] = []

    for result in task_results:
        description = str(result.get('description', '')).strip() or '子任务'
        rows = list(result.get('rows') or [])
        error = str(result.get('error') or '').strip()
        raw_columns = list(result.get('columns') or [])
        metric_columns = [
            column for column in raw_columns
            if str(column.get('key', '')).strip() not in {'rank', 'report_period', 'stock_abbr'}
        ]
        first_metric = metric_columns[0] if metric_columns else {}
        first_key = str(first_metric.get('key', '')).strip()
        first_label = str(first_metric.get('label', first_key)).strip() or '指标'

        if error:
            content_parts.append(f'{description}执行失败：{error}。')
            continue
        if not rows:
            content_parts.append(f'{description}未查询到结果。')
            continue

        first_row = rows[0]
        if 'rank' in first_row:
            top_companies = [str(row.get('stock_abbr', '')).strip() for row in rows[:5] if str(row.get('stock_abbr', '')).strip()]
            if top_companies:
                content_parts.append(f'{description}中，排名靠前的企业包括{"、".join(top_companies)}。')
            top_company = str(first_row.get('stock_abbr', '')).strip()
            if top_company and first_key:
                top_value = _format_metric_value(first_key, first_row.get(first_key))
                content_parts.append(f'其中{top_company}位居首位，{first_label}为{top_value}。')
            insights.append(f'{description}共返回 {len(rows)} 条结果。')
            continue

        if len(rows) == 1:
            row = rows[0]
            company = str(row.get('stock_abbr', '')).strip()
            period = str(row.get('report_period', '')).strip()
            metric_texts = [
                f'{str(column.get("label", column.get("key", ""))).strip()}{_format_metric_value(str(column.get("key", "")).strip(), row.get(str(column.get("key", "")).strip()))}'
                for column in metric_columns[:3]
            ]
            subject = company or '该对象'
            if period:
                content_parts.append(f'{description}中，{subject}在{format_period(period)}的{"，".join(metric_texts)}。')
            else:
                content_parts.append(f'{description}中，{subject}的{"，".join(metric_texts)}。')
            continue

        samples: list[str] = []
        for row in rows[:3]:
            company = str(row.get('stock_abbr', '')).strip()
            if company and first_key:
                samples.append(f'{company}{_format_metric_value(first_key, row.get(first_key))}')
        if samples:
            content_parts.append(f'{description}共返回 {len(rows)} 条记录，{first_label}示例：{"、".join(samples)}。')
        else:
            content_parts.append(f'{description}共返回 {len(rows)} 条记录。')
        if len(metric_columns) > 1:
            insights.append(
                f'{description}同时返回了' + '、'.join(str(column.get('label', column.get('key', ''))).strip() for column in metric_columns[:3]) + '。'
            )

    if not content_parts:
        content_parts = ['已完成多子任务查询，请查看各子任务的数据表与图表。']
    if not insights:
        insights = [f'共完成 {len(task_results)} 个子任务查询。']

    # integrate insights into content as 📌 补充要点
    full_content = ' '.join(content_parts[:6])
    if insights:
        supplement = '\n\n📌 补充要点\n' + '\n'.join(f'{i+1}. {ins}' for i, ins in enumerate(insights[:5]))
        full_content += supplement

    return {
        'content': full_content,
        'insights': [],
        'followUpHints': default_hints,
    }


def _build_task_context(task_results: list[dict[str, Any]]) -> str:
    """Build a human-readable summary of all sub-task results for the LLM prompt."""
    lines: list[str] = []
    for result in task_results:
        desc = str(result.get('description', f'子任务{result.get("id", "")}'))
        rows = list(result.get('rows') or [])
        cols = list(result.get('columns') or [])
        error = str(result.get('error') or '').strip()
        lines.append(f'\n【{desc}】')
        if error:
            lines.append(f'  执行失败：{error}')
            continue
        if not rows:
            lines.append('  未查询到数据')
            continue
        coverage_note = str(result.get('data_coverage_note', '')).strip()
        if coverage_note:
            lines.append(f'  ⚠️ 数据覆盖注意：{coverage_note}')
        metric_cols = [c for c in cols if str(c.get('key', '')).strip() not in {'rank', 'stock_abbr'}]
        for row in rows[:5]:
            parts = []
            company = str(row.get('stock_abbr', '')).strip()
            period = str(row.get('report_period', '')).strip()
            if company:
                parts.append(company)
            if period:
                parts.append(period)
            for col in metric_cols:
                key = str(col.get('key', '')).strip()
                label = str(col.get('label', key)).strip()
                val = row.get(key)
                parts.append(f'{label}={_format_metric_value(key, val)}')
            lines.append('  ' + '，'.join(parts))
    return '\n'.join(lines)


def _synthesise_reply(
    question: str,
    history: list[dict[str, Any]],
    task_results: list[dict[str, Any]],
    kb_context: str,
    all_sqls: list[str] | None = None,
    all_columns: list[dict[str, str]] | None = None,
    all_rows: list[dict[str, Any]] | None = None,
    source_tables: list[str] | None = None,
    emit=None,
    render_mode: str = 'chat',
) -> dict[str, Any]:
    from .llm import llm_available, llm_temperature, request_llm_json, request_llm_text_stream
    from .assistant import _recent_history, _normalize_text_list
    default_follow_up_hints = ['继续追问某个子问题', '查看详细数据趋势']
    if not llm_available():
        return _rule_based_synthesis(task_results)

    task_ctx = _build_task_context(task_results)
    recent = json.dumps(_recent_history(history), ensure_ascii=False)

    is_batch = render_mode == 'batch_export'

    if is_batch:
        system_prompt = (
            '你是一名资深A股财务分析师。用户问了多个子问题，系统已分别查询并获得数据。\n'
            '请基于所有子问题的查询结果，用简洁的纯文本生成一份综合回答，输出将直接写入 Excel 单元格。\n'
            '具体要求：\n'
            '1. 直接给出核心数据和结论，不要使用任何 Markdown 格式（禁止 **加粗**、## 标题、- 列表等）。\n'
            '2. 用连续的自然语句回答，不要分段、不要换行、不要编号列表。\n'
            '3. 必须覆盖每一个子问题的查询结果，不得遗漏。\n'
            '4. 数字以万元/亿元/%表达，保留2位小数。\n'
            '5. 不要插入图表占位符、不要添加参考标注。\n'
            '6. 整体字数控制在80~200字以内，一段话说完。\n'
            '7. 不要加emoji，不要使用 HTML 标签。'
        )
    else:
        system_prompt = (
            '你是一名资深A股财务分析师，擅长从多维数据中提炼洞察并用自然语言表达。\n'
            '用户问了多个独立子问题，系统已分别查询并获得数据。请基于所有子问题的查询结果，生成一份统一的综合分析。\n'
            '写作风格要求：\n'
            '1. 像和用户面对面聊天一样自然地回答，不要使用固定模板或分节框架（禁止使用📊📈🔍💡📌等emoji标题）。\n'
            '2. 必须覆盖每一个子问题的查询结果，不得遗漏任何子任务。\n'
            '3. 先用1~2句话概括核心结论，关键词用 **加粗** 标记。\n'
            '4. 后续根据内容自然设定小标题来组织分析（用 **小标题** 独占一行表示），标题名称根据实际内容自主拟定。\n'
            '5. **加粗** 的使用范围：\n'
            '   - 关键数值及其含义（如"营收同比增长**12.35%**，达到**15.8亿元**"）\n'
            '   - 重要结论或判断（如"整体盈利能力**显著改善**"）\n'
            '   - 关键财务术语或指标名称（如"**毛利率**回升至38.5%"）\n'
            '   - 编号列表中每条的核心要点（如"1. **研发投入加大**：研发费用同比增长25%"）\n'
            '6. 用数据支撑观点，数字以万元/亿元/%表达，保留2位小数。不要逐行罗列数据，把数据融入分析叙述中。\n'
            '7. 当有多个并列要点时，用编号列表自然呈现，每条可展开2~3句。段落之间用空行分隔。\n'
            '8. 不要使用 ## 和 HTML 标签，不要加emoji。整体字数400~700字。\n'
            '9. 当查询结果不足以完整回答用户问题时（如用户要求Top10但数据库仅有少量数据、某些子任务无结果等），'
            '必须在回答中主动、明确地说明数据覆盖范围的局限性，不要假装数据是完整的。'
        )

    # Build {{TASK_N}} marker instructions so charts/tables are placed inline
    task_marker_lines: list[str] = []
    for i, result in enumerate(task_results):
        desc = str(result.get('description', f'子任务{i+1}'))
        parts = []
        if result.get('chart') is not None:
            parts.append('图表')
        if result.get('rows'):
            parts.append('数据表')
        label = '和'.join(parts) if parts else '数据'
        task_marker_lines.append(f'- {{{{TASK_{i+1}}}}} — 「{desc}」的{label}')

    task_marker_block = ''
    if task_marker_lines and not is_batch:
        task_marker_block = (
            '\n\n【图表/表格插入规则】你必须在分析各子任务时，在最合适的位置（通常在对应分析段落之前）'
            '插入以下占位符，前端会替换为真实图表和数据表：\n'
            + '\n'.join(task_marker_lines)
            + '\n每个占位符只出现一次，必须独占一行。\n'
            + '占位符自带标题展示，不要再把子任务描述写成小标题。'
        )

    data_block = (
        f'用户原始问题：{question}\n\n'
        f'各子任务查询结果：\n{task_ctx}\n\n'
        f'最近对话记录：{recent}\n\n'
        f'知识库补充：{kb_context[:500] if kb_context else "无"}\n\n'
        '请综合以上所有子任务的数据，用自然连贯的语言生成综合分析。'
        '不要套用固定段落模板，根据数据内容自主组织结构，突出关键变化和洞察。'
        + task_marker_block
    )

    # --- streaming path: emit content_chunk tokens ---
    if callable(emit):
        stream_user_prompt = (
            '请根据以下信息综合回答用户问题，直接输出纯文本（不要输出 JSON）。\n\n'
            f'{data_block}'
        )
        def _on_chunk(text: str) -> None:
            try:
                emit('content_chunk', text)
            except Exception:
                pass
        try:
            content = request_llm_text_stream(
                system_prompt, stream_user_prompt,
                temperature=llm_temperature('synthesis_temperature', 0.4),
                on_chunk=_on_chunk,
            )
        except Exception:
            return _rule_based_synthesis(task_results)
        if not content:
            return _rule_based_synthesis(task_results)
        return {
            'content': content,
            'insights': [],
            'followUpHints': default_follow_up_hints,
        }

    # --- non-streaming path: JSON mode ---
    user_prompt = (
        '请根据以下信息综合回答用户问题，返回 JSON 对象。\n\n'
        '返回格式：{"content":"...","followUpHints":["..."]}\n\n'
        '字段要求：\n'
        '1. content：综合分析的纯文本内容。\n'
        '2. followUpHints：3条追问建议。\n'
        '3. 仅返回 JSON，不输出其他文字。\n\n'
        f'{data_block}'
    )
    try:
        payload = request_llm_json(system_prompt, user_prompt, temperature=llm_temperature('synthesis_temperature', 0.4))
        content = str(payload.get('content', '')).strip()
        if not content:
            raise ValueError('empty content')
        return {
            'content': content,
            'insights': [],
            'followUpHints': _normalize_text_list(payload.get('followUpHints'), default_follow_up_hints, 3),
        }
    except Exception:
        return _rule_based_synthesis(task_results)


def _one_line_snippet(text: str, limit: int = 72) -> str:
    compact = re.sub(r'\s+', ' ', str(text or '')).strip()
    if len(compact) > limit:
        return compact[:limit] + '…'
    return compact


def _format_node_value(value: Any) -> str:
    if value is None:
        return '缺失'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, int):
        return f'{value:,}'
    if isinstance(value, float):
        text = f'{value:,.2f}' if abs(value) >= 1000 else f'{value:.2f}'
        return text.rstrip('0').rstrip('.')
    return _one_line_snippet(str(value), 24)


def _describe_task_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '没有可直接引用的结果样本。'
    sample = rows[0] if isinstance(rows[0], dict) else {}
    company = str(sample.get('stock_name') or sample.get('company_name') or '').strip()
    period = str(sample.get('report_period') or sample.get('报告期') or '').strip()
    numeric_pairs = [
        (str(k), v) for k, v in sample.items()
        if isinstance(v, (int, float)) and k not in {'id'}
    ][:2]
    parts: list[str] = []
    if company:
        parts.append(f'样本主体是 {company}')
    if period:
        parts.append(f'时间点为 {period}')
    if numeric_pairs:
        parts.append('示例读数包括 ' + '；'.join(f'{k}={_format_node_value(v)}' for k, v in numeric_pairs))
    return ('；'.join(parts) + '。') if parts else '返回结果可作为该子任务的直接证据。'


def _describe_reference_detail(ref: dict[str, Any]) -> str:
    doc_name = _one_line_snippet(str(ref.get('doc_name', '参考资料')), 28) or '参考资料'
    doc_type = str(ref.get('doc_type', '研报')).strip() or '研报'
    snippet = _one_line_snippet(str(ref.get('text', '') or ''), 90)
    if snippet:
        return f'这份{doc_type}《{doc_name}》补充了与多意图问题相关的证据片段：{snippet}'
    return f'这份{doc_type}《{doc_name}》用于补充背景解释与辅助判断。'


def _build_multi_intent_thinking_map(
    question: str,
    subtasks: list[dict[str, Any]],
    task_results: list[dict[str, Any]],
    references: list[dict[str, Any]],
    content: str,
) -> dict[str, Any]:
    task_nodes = []
    for idx, (task, result) in enumerate(zip(subtasks, task_results), start=1):
        desc = str(task.get('description', f'子任务 {idx}')).strip() or f'子任务 {idx}'
        table = TABLE_LABELS.get(str(task.get('table', '')).strip(), str(task.get('table', '')).strip())
        rows = list(result.get('rows') or [])
        error = str(result.get('error') or '').strip()
        detail = f'围绕「{desc}」在 {table} 中返回 {len(rows)} 条数据。{_describe_task_rows(rows)}'
        node_type = 'data'
        if error:
            detail = f'围绕「{desc}」尝试在 {table} 中执行查询时失败：{error}。因此这个子任务暂时不能为最终结论提供有效证据。'
            node_type = 'process'
        elif not rows:
            detail = f'围绕「{desc}」在 {table} 中没有查到匹配记录，说明当前条件下这一分支暂时没有直接数据证据。'
            node_type = 'process'
        task_nodes.append({
            'id': f'task_{idx}',
            'label': _one_line_snippet(desc, 30),
            'detail': detail,
            'type': node_type,
            'edge_label': f'子任务{idx}',
            'children': [],
        })

    kb_children = [
        {
            'id': f'kb_{i}',
            'label': _one_line_snippet(str(ref.get('doc_name', '参考资料')), 26),
            'detail': _describe_reference_detail(ref),
            'type': 'source',
            'edge_label': str(ref.get('doc_type', '研报')) or '研报',
            'children': [],
        }
        for i, ref in enumerate(references[:3])
    ]

    children = [
        {
            'id': 'decompose',
            'label': '意图拆解',
            'detail': f'先把原问题拆成 {len(subtasks)} 个可独立执行的子任务，分别取数或检索后再统一汇总，避免把多个目标混在一次查询里。',
            'type': 'process',
            'edge_label': '拆解',
            'children': task_nodes,
        }
    ]
    if references:
        children.append({
            'id': 'kb_search',
            'label': '知识参考检索',
            'detail': f'在结构化子任务之外，额外补充 {len(references)} 条知识证据，用来解释行业背景、政策口径或文本信息，帮助把多个子任务结果串联成完整结论。',
            'type': 'knowledge',
            'edge_label': '增强',
            'children': kb_children,
        })
    children.append({
        'id': 'synthesis',
        'label': '综合分析',
        'detail': '把各子任务结果放到同一个结论框架中，对齐主体、时间口径和指标含义，再生成统一答案，避免不同分支之间互相冲突。',
        'type': 'synthesis',
        'edge_label': '推理',
        'children': [
            {
                'id': 'output',
                'label': '最终回答',
                'detail': f'最终回答会统一整理 {len(subtasks)} 个子任务的结果，先直接回答问题，再补充最关键的对比和依据。摘要：{_one_line_snippet(content, 110) or "生成最终回答。"}',
                'type': 'result',
                'edge_label': '输出',
                'children': [],
            }
        ],
    })
    return {
        'id': 'root',
        'label': question[:40] + ('…' if len(question) > 40 else ''),
        'detail': f'这个问题同时包含 {len(subtasks)} 个分析目标，需要分别执行后再合并答案，才能避免遗漏其中某一条子需求。',
        'type': 'question',
        'children': children,
    }


# ── public entry point ────────────────────────────────────────────────────────

def _fc_intents_to_subtasks(fc_intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert function-calling intent dicts into planner subtask format.

    This lets the planner skip a separate LLM decomposition call when the
    intent router already produced structured tool_calls.
    """
    subtasks: list[dict[str, Any]] = []
    for i, intent in enumerate(fc_intents):
        task_id = i + 1
        qk = intent.get('query_kind', 'metric')
        am = intent.get('analysis_mode', 'single')
        company = intent.get('company', '') or None
        period = intent.get('period', '') or None
        metrics = intent.get('metrics', [])
        direction = intent.get('direction', 'desc')
        limit = intent.get('limit')

        # determine table from first metric
        table = 'income_sheet'
        if metrics:
            for tbl, tbl_cols in TABLE_METRIC_COLUMNS.items():
                if metrics[0] in tbl_cols:
                    table = tbl
                    break

        # cash_flow tool always maps to cash_flow_sheet
        if qk == 'cash_flow':
            table = 'cash_flow_sheet'
            if not metrics:
                metrics = ['operating_cf_net_amount', 'investing_cf_net_amount', 'financing_cf_net_amount']

        # validate columns against table
        valid_cols = TABLE_METRIC_COLUMNS.get(table, {})
        columns = [m for m in metrics if m in valid_cols]
        if not columns and qk != 'general':
            columns = list(valid_cols.keys())[:3]

        # ranking: company=null, need order_by
        order_by = None
        if am == 'ranking':
            company = None
            order_by = {'field': columns[0] if columns else 'net_profit', 'direction': direction}
            if limit is None:
                limit = 10

        # build description
        tool_name = intent.get('tool_name', '')
        col_labels = TABLE_METRIC_COLUMNS.get(table, {})
        metric_desc = '、'.join(col_labels.get(c, c).split('（')[0] for c in columns[:3])
        if company and metric_desc:
            desc = f'{company}的{metric_desc}'
        elif metric_desc:
            desc = f'{metric_desc}排名' if am == 'ranking' else metric_desc
        else:
            desc = tool_name or f'子任务{task_id}'

        subtasks.append({
            'id': task_id,
            'description': desc,
            'table': table,
            'columns': columns,
            'company': company,
            'period': period,
            'analysis_mode': am if am != 'general' else 'single',
            'order_by': order_by,
            'limit': limit,
            'depends_on': [],
        })
    return subtasks


def build_multi_intent_reply(question: str, history: list[dict[str, Any]], emit=None, deep_think: bool = True, render_mode: str = 'chat', fc_intents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Full multi-intent planning pipeline → unified reply dict."""
    def _emit(label: str, status: str, detail: str = '') -> None:
        if callable(emit):
            try:
                emit('step', {'label': label, 'status': status, 'detail': detail})
            except Exception:
                pass

    def _think(text: str) -> None:
        if not deep_think:
            return
        if callable(emit):
            try:
                emit('thinking', text)
            except Exception:
                pass

    # Step 1: decompose
    # Priority: fc_intents (from function-calling) → LLM decompose → rule-based
    subtasks: list[dict[str, Any]] = []
    if fc_intents and len(fc_intents) >= 2:
        _emit('意图拆解', 'running', '使用 function-calling 结果构建子任务…')
        subtasks = _fc_intents_to_subtasks(fc_intents)

    # KB retrieval (run in parallel with LLM decompose if needed)
    from concurrent.futures import ThreadPoolExecutor as _MiTPE
    _emit('知识库检索', 'running', '正在从研报知识库中检索相关文献…')
    if subtasks:
        # fc_intents already gave us subtasks, just do KB in parallel
        kb_context, references = _retrieve_kb_context(question)
    else:
        # Need LLM decompose — run both in parallel
        _emit('意图拆解', 'running', '正在使用 LLM 将问题拆分为多个独立子任务…')
        with _MiTPE(max_workers=2) as _mi_pool:
            _kb_fut = _mi_pool.submit(_retrieve_kb_context, question)
            _decompose_fut = _mi_pool.submit(_decompose_with_llm, question, history)
            kb_context, references = _kb_fut.result()
            subtasks = _decompose_fut.result()
    _emit('知识库检索', 'done', f'检索完成，命中 {len(references)} 条文献片段')

    if not subtasks:
        _emit('意图拆解', 'running', 'LLM 拆解失败，切换规则方法…')
        subtasks = _decompose_rule_based(question, history)
    if not subtasks:
        raise ValueError('multi-intent decomposition returned no subtasks')
    descs = '、'.join(str(t.get('description', '')) for t in subtasks[:4])
    _think(f'这个问题需要从 {len(subtasks)} 个角度来分析：{descs}。')
    _emit('意图拆解', 'done', f'拆解完成，共识别出 {len(subtasks)} 个子问题')

    # ── Step 1.5: pre-execution clarification check ───────────────────────────
    import re as _re
    _EXPLICIT_PERIOD_RE = _re.compile(
        r'(20\d{2}|第[一二三四]季度|[Qq][1-4]|上半年|下半年|年报|半年报|季报|今年|去年|最新|最近|近[几\d])'
    )
    has_explicit_period = bool(_EXPLICIT_PERIOD_RE.search(question))

    def _is_null(val: Any) -> bool:
        return not val or str(val).strip().lower() in ('null', 'none', '')

    # check if the original question mentions any specific company name (支持别名)
    from ..core.context import COMPANY_NAMES as _PLAN_COMPANY_NAMES, resolve_company_alias as _plan_resolve_alias
    _question_has_company = bool(_plan_resolve_alias(question)) if _PLAN_COMPANY_NAMES else False
    _AGGREGATE_KW = _re.compile(
        r'(统计|数量|多少家|多少个|哪些公司|所有公司|全部公司|所有企业|每家|各公司|各企业|筛选|过滤|找出.*公司|公司数量|排名|前\d|行业)'
    )

    def _company_missing(t: dict) -> bool:
        """True only when the subtask genuinely needs a company but doesn't have one."""
        company_val = str(t.get('company') or '').strip()
        # ranking queries search all companies — no specific company needed
        if str(t.get('analysis_mode', '')).lower() == 'ranking':
            return False
        # depends on a previous task's result (FROM_TASK_<N>)
        if company_val.upper().startswith('FROM_TASK'):
            return False
        # has depends_on — likely inherits company from an earlier task
        if t.get('depends_on'):
            return False
        if not _is_null(company_val):
            return False
        # question never mentioned a specific company → all-company query
        if not _question_has_company:
            return False
        # subtask description contains aggregate/filter keywords
        desc = str(t.get('description', '')).strip()
        if _AGGREGATE_KW.search(desc) or _AGGREGATE_KW.search(question):
            return False
        return True

    missing_companies = [t for t in subtasks if _company_missing(t)]
    missing_periods = [
        t for t in subtasks
        if str(t.get('analysis_mode', 'single')) == 'single'
        and str(t.get('analysis_mode', '')).lower() != 'ranking'
        and _is_null(t.get('period'))
        and not has_explicit_period
    ]
    missing_columns = [
        t for t in subtasks
        if not t.get('columns') or (isinstance(t.get('columns'), list) and len(t['columns']) == 0)
    ]

    if missing_companies or missing_periods or missing_columns:
        from .assistant import _build_smart_clarify_reply, available_periods, format_period
        missing_parts: list[str] = []
        options: list[str] = []
        understood: dict[str, str] = {'子问题数': str(len(subtasks))}

        if missing_companies:
            task_descs = '、'.join(str(t.get('description', '')) for t in missing_companies[:3])
            missing_parts.append(f'公司名称（涉及：{task_descs}）')

        if missing_periods:
            task_descs = '、'.join(str(t.get('description', '')) for t in missing_periods[:3])
            missing_parts.append(f'报告期（涉及：{task_descs}）')
            known_companies = [
                str(t.get('company', '')) for t in subtasks
                if not _is_null(t.get('company'))
            ]
            if known_companies:
                raw_periods = available_periods(
                    str(missing_periods[0].get('table', 'income_sheet')),
                    known_companies[0]
                )[-5:]
                options += [format_period(p) for p in raw_periods] if raw_periods else ['最近年报', '最近半年报', '最近季报']
            else:
                options += ['最近年报', '最近半年报', '最近季报']

        if missing_columns:
            task_descs = '、'.join(str(t.get('description', '')) for t in missing_columns[:3])
            missing_parts.append(f'财务指标（涉及：{task_descs}）')
            known_company = next(
                (str(t.get('company', '')) for t in subtasks if not _is_null(t.get('company'))), ''
            )
            prefix = known_company or '该公司'
            period_hint = next(
                (format_period(str(t.get('period', ''))) for t in subtasks if not _is_null(t.get('period'))), ''
            )
            period_str = f'{period_hint}的' if period_hint else ''
            options += [
                f'{prefix}{period_str}净利润',
                f'{prefix}{period_str}营业收入',
                f'{prefix}{period_str}现金流结构',
                f'{prefix}{period_str}资产负债情况',
            ]

        # Build understood context from non-missing fields
        known_cos = list({str(t.get('company', '')) for t in subtasks if not _is_null(t.get('company'))})
        known_ps = list({format_period(str(t.get('period', ''))) for t in subtasks if not _is_null(t.get('period'))})
        if known_cos:
            understood['公司'] = '、'.join(known_cos[:3])
        if known_ps:
            understood['报告期'] = '、'.join(known_ps[:3])

        return _build_smart_clarify_reply(
            question, history,
            understood=understood,
            missing='、'.join(missing_parts),
            options=list(dict.fromkeys(options))[:8],
            source_tables=list({str(t.get('table', 'income_sheet')) for t in subtasks}),
            meta={'query_kind': 'multi_intent', 'subtask_count': len(subtasks)}
        )
    # ─────────────────────────────────────────────────────────────────────────

    # Step 2: execute subtasks — independent ones in parallel, dependent ones sequentially
    prev_results: dict[int, dict[str, Any]] = {}
    task_results: list[dict[str, Any]] = [None] * len(subtasks)  # type: ignore[list-item]
    all_sqls: list[str] = []
    all_charts: list[dict[str, Any]] = []
    all_columns: list[dict[str, str]] = []
    all_rows: list[dict[str, Any]] = []
    source_tables: list[str] = []

    dep_ids: set[int] = set()
    for t in subtasks:
        dep_ids.update(t.get('depends_on') or [])
    independent = [(i, t) for i, t in enumerate(subtasks) if not (t.get('depends_on') or [])]
    dependent = [(i, t) for i, t in enumerate(subtasks) if (t.get('depends_on') or [])]

    def _record_result(i: int, task: dict, result: dict) -> None:
        task_id = int(task.get('id', len(prev_results) + 1))
        prev_results[task_id] = result
        task_results[i] = result
        row_count = len(result.get('rows') or [])
        desc = str(task.get('description', f'子任务 {i + 1}'))
        if result.get('error'):
            _emit(f'子任务 {i + 1}', 'error', f'{desc} 失败：{result["error"]}')
        else:
            _emit(f'子任务 {i + 1}', 'done', f'{desc} 完成，返回 {row_count} 条数据')
            if row_count > 0:
                _think(f'「{desc}」已获取 {row_count} 条数据，结果可用。')

    # Phase A: run independent subtasks in parallel
    if len(independent) > 1:
        with _MiTPE(max_workers=min(len(independent), 4)) as _task_pool:
            futs = {}
            for i, task in independent:
                desc = str(task.get('description', f'子任务 {i + 1}'))
                tbl_name = TABLE_LABELS.get(str(task.get('table', '')), str(task.get('table', '')))
                _emit(f'子任务 {i + 1}', 'running', f'正在查询：{desc}（{tbl_name}）…')
                futs[_task_pool.submit(_execute_task, task, prev_results)] = (i, task)
            for fut in _as_completed(futs):
                i, task = futs[fut]
                try:
                    _record_result(i, task, fut.result())
                except Exception as exc:
                    _record_result(i, task, {'id': task.get('id', 0), 'description': task.get('description', ''), 'sql': '', 'columns': [], 'rows': [], 'chart': None, 'error': str(exc)})
    else:
        for i, task in independent:
            desc = str(task.get('description', f'子任务 {i + 1}'))
            tbl_name = TABLE_LABELS.get(str(task.get('table', '')), str(task.get('table', '')))
            _emit(f'子任务 {i + 1}', 'running', f'正在查询：{desc}（{tbl_name}）…')
            _record_result(i, task, _execute_task(task, prev_results))

    # Phase B: run dependent subtasks sequentially (they need prev_results)
    for i, task in dependent:
        desc = str(task.get('description', f'子任务 {i + 1}'))
        tbl_name = TABLE_LABELS.get(str(task.get('table', '')), str(task.get('table', '')))
        _emit(f'子任务 {i + 1}', 'running', f'正在查询：{desc}（{tbl_name}）…')
        _record_result(i, task, _execute_task(task, prev_results))

    # Collect aggregated data in original order
    for i, result in enumerate(task_results):
        if result is None:
            continue
        if result['sql']:
            all_sqls.append(result['sql'])
        if result['chart']:
            all_charts.append(result['chart'])
        if result['columns']:
            all_columns = result['columns']
        if result['rows']:
            all_rows = result['rows']
        tbl = str(subtasks[i].get('table', ''))
        if tbl and tbl not in source_tables:
            source_tables.append(tbl)

    # Step 3: synthesise
    success_count = sum(1 for r in task_results if not r.get('error'))
    _think(f'各维度数据齐备（{success_count}/{len(subtasks)}），接下来综合对比、得出结论。')
    _emit('综合分析', 'running', f'正在综合 {len(subtasks)} 个子任务结果，生成统一分析报告…')
    synthesis = _synthesise_reply(
        question, history, task_results, kb_context,
        all_sqls=all_sqls,
        all_columns=all_columns,
        all_rows=all_rows,
        source_tables=source_tables,
        emit=emit,
        render_mode=render_mode,
    )
    _emit('综合分析', 'done', f'综合报告生成完成，共 {len(synthesis["content"])} 字')

    # ── Step 3.5: collect provenance for all subtask companies/periods ────────
    from config import CONFIG as _MI_CONFIG
    from .assistant import _build_calc_traces as _mi_build_calc_traces
    provenance_list: list[dict[str, Any]] = []
    calc_traces: list[dict[str, Any]] = []
    citation_refs: list[dict[str, Any]] = []
    _stock_map = _MI_CONFIG.get('stock_abbr_to_code', {})
    try:
        # gather (stock_code, period) pairs from all subtask rows
        _prov_pairs: dict[str, set[str]] = {}  # stock_code -> set of periods
        for i, result in enumerate(task_results):
            print(f'[PROV-DEBUG] subtask {i}: company={subtasks[i].get("company")}, period={subtasks[i].get("period")}, rows={len(result.get("rows") or [])}, first_row_keys={list((result.get("rows") or [{}])[0].keys()) if result.get("rows") else "EMPTY"}')
            if result is None or result.get('error'):
                continue
            task_company = str(subtasks[i].get('company') or '').strip()
            task_period = str(subtasks[i].get('period') or '').strip()
            task_sc = _stock_map.get(task_company, '')
            rows_for_prov = result.get('rows') or []
            # from task definition
            if task_sc:
                _prov_pairs.setdefault(task_sc, set())
                if task_period:
                    _prov_pairs[task_sc].add(task_period)
            # from actual rows
            for row in rows_for_prov:
                abbr = str(row.get('stock_abbr', '')).strip()
                sc = _stock_map.get(abbr, '')
                if not sc:
                    continue
                rp = str(row.get('report_period', '')).strip()
                _prov_pairs.setdefault(sc, set())
                if rp:
                    _prov_pairs[sc].add(rp)
        # query provenance for each stock_code
        _select_cols = (
            'SELECT id, stock_code, report_period, table_name, field_name, '
            'field_value, source_pdf AS source_file, page_no AS source_page, '
            'row_label AS source_table_name, raw_text AS source_text, '
            'confidence, extract_method FROM field_provenance'
        )
        _seen_prov: dict[tuple[str, str, str], dict[str, Any]] = {}
        for sc, periods in _prov_pairs.items():
            if periods:
                _ph = ','.join(['?'] * len(periods))
                _prov_sql = f'{_select_cols} WHERE stock_code = ? AND report_period IN ({_ph}) ORDER BY page_no ASC'
                _prov_rows = query_rows(_prov_sql, tuple([sc] + sorted(periods)))
            else:
                _prov_sql = f'{_select_cols} WHERE stock_code = ? ORDER BY page_no ASC LIMIT 200'
                _prov_rows = query_rows(_prov_sql, (sc,))
            for pr in _prov_rows:
                _key = (pr.get('stock_code', ''), pr.get('report_period', ''), pr.get('field_name', ''))
                _existing = _seen_prov.get(_key)
                if _existing is None or (pr.get('confidence', 0) or 0) >= (_existing.get('confidence', 0) or 0):
                    _seen_prov[_key] = pr
        provenance_list = list(_seen_prov.values())
        print(f'[PROV-DEBUG] _prov_pairs keys={list(_prov_pairs.keys())}, total provenance={len(provenance_list)}')
        # build calc traces from all collected provenance
        if provenance_list:
            _first_sc = next(iter(_prov_pairs), '')
            calc_traces, citation_refs = _mi_build_calc_traces(provenance_list, _first_sc)
    except Exception:
        import traceback
        traceback.print_exc()

    # build plan summary for meta
    plan_summary = [
        {'id': t.get('id'), 'description': t.get('description'), 'table': t.get('table')}
        for t in subtasks
    ]
    total_rows = sum(len(r.get('rows') or []) for r in task_results)
    thinking_map = _build_multi_intent_thinking_map(question, subtasks, task_results, references, synthesis['content'])

    return {
        'id': create_id(),
        'role': 'assistant',
        'content': synthesis['content'],
        'sql': '\n\n'.join(all_sqls),
        'chart': all_charts[0] if all_charts else None,
        'charts': all_charts,            # all charts for multi-display
        'columns': all_columns,
        'rows': all_rows,
        'taskResults': [                 # per-subtask breakdown
            {
                'id': r['id'],
                'description': r['description'],
                'sql': r['sql'],
                'columns': r['columns'],
                'rows': r['rows'],
                'chart': r['chart'],
                'error': r['error'],
            }
            for r in task_results
        ],
        'sourceTables': source_tables,
        'insights': synthesis['insights'],
        'followUpHints': synthesis['followUpHints'],
        'references': references,
        'provenance': provenance_list,
        'calcTraces': calc_traces,
        'citationRefs': citation_refs,
        'attribution': {
            'steps': [
                {'label': '知识检索', 'status': 'done', 'detail': f'检索到 {len(references)} 条相关知识'},
                {'label': 'SQL 生成', 'status': 'done', 'detail': f'共 {len(subtasks)} 个子任务，涉及 {len(source_tables)} 张表'},
                {'label': '回答生成', 'status': 'done', 'detail': f'生成内容共 {len(synthesis["content"])} 字'},
                {'label': '归因分析', 'status': 'done', 'detail': f'含 {len(provenance_list)} 条数据溯源 + {len(references)} 条知识库引用'},
            ],
            'sql': '\n\n'.join(all_sqls),
            'kb_sources': references,
            'data_summary': {
                'tables': source_tables,
                'table_labels': [TABLE_LABELS.get(t, t) for t in source_tables],
                'row_count': total_rows,
                'columns': [str(c.get('label', c.get('key', ''))).strip() for c in all_columns],
            },
            'thinking_map': thinking_map,
        },
        'meta': {
            'query_kind': 'multi_intent',
            'analysis_mode': 'multi',
            'plan': plan_summary,
        },
        'status': 'done',
    }
