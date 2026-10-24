# -*- coding: utf-8 -*-
"""
LLM 驱动的结构化查询计划器
─────────────────────────
当规则引擎无法处理复杂查询时（多期筛选、跨表关联、派生计算等），
用 LLM 解析用户意图并输出结构化 JSON，再由代码安全地生成参数化 SQL。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .llm import request_llm_json, llm_available, llm_temperature
from ..core.db import open_connection, query_rows

_log = logging.getLogger(__name__)

# ─── 白名单 ──────────────────────────────────────────────────────────────
ALLOWED_TABLES = {
    'core_performance_indicators_sheet',
    'balance_sheet',
    'income_sheet',
    'cash_flow_sheet',
}

# 从数据库动态获取每张表的真实列名（缓存）
_COLUMN_WHITELIST: dict[str, set[str]] | None = None


def _get_column_whitelist() -> dict[str, set[str]]:
    global _COLUMN_WHITELIST
    if _COLUMN_WHITELIST is not None:
        return _COLUMN_WHITELIST
    _COLUMN_WHITELIST = {}
    with open_connection() as conn:
        for tbl in ALLOWED_TABLES:
            try:
                cur = conn.execute(f"PRAGMA table_info({tbl})")
                _COLUMN_WHITELIST[tbl] = {row[1] for row in cur.fetchall()}
            except Exception:
                _COLUMN_WHITELIST[tbl] = set()
    return _COLUMN_WHITELIST


def _validate_column(table: str, col: str) -> bool:
    whitelist = _get_column_whitelist()
    return col in whitelist.get(table, set())

# ─── Schema Prompt ────────────────────────────────────────────────────────

SCHEMA_PROMPT = """你是一个金融数据查询助手。数据库中有以下表和字段：

## 表1: core_performance_indicators_sheet (核心业绩指标)
字段: stock_code(股票代码), stock_abbr(公司简称), report_period(报告期), report_year(报告年份),
  total_operating_revenue(营业总收入,万元), net_profit_10k_yuan(归母净利润,万元),
  eps(每股收益,元), net_asset_per_share(每股净资产,元), roe(加权平均净资产收益率,%),
  roe_weighted_excl_non_recurring(加权平均净资产收益率扣非,%),
  operating_revenue_yoy_growth(营业收入同比增长率,%), net_profit_yoy_growth(净利润同比增长率,%),
  net_profit_excl_non_recurring(扣非净利润,万元),
  operating_revenue_qoq_growth(营业收入环比增长率,%), net_profit_qoq_growth(净利润环比增长率,%),
  gross_profit_margin(毛利率,%), net_profit_margin(净利率,%)

## 表2: balance_sheet (资产负债表)
字段: stock_code, stock_abbr, report_period, report_year,
  asset_inventory(存货,万元), asset_total_assets(总资产,万元),
  asset_accounts_receivable(应收账款,万元), asset_cash_and_cash_equivalents(货币资金,万元),
  asset_trading_financial_assets(交易性金融资产,万元), asset_construction_in_progress(在建工程,万元),
  liability_total_liabilities(总负债,万元), liability_accounts_payable(应付账款,万元),
  liability_contract_liabilities(合同负债,万元), liability_short_term_loans(短期借款,万元),
  liability_advance_from_customers(预收账款,万元),
  equity_total_equity(所有者权益,万元), equity_unappropriated_profit(未分配利润,万元),
  asset_liability_ratio(资产负债率,%),
  asset_total_assets_yoy_growth(总资产同比增长率,%), liability_total_liabilities_yoy_growth(总负债同比增长率,%)

## 表3: income_sheet (利润表)
字段: stock_code, stock_abbr, report_period, report_year,
  total_operating_revenue(营业总收入,万元), operating_profit(营业利润,万元),
  total_profit(利润总额,万元), net_profit(净利润,万元),
  operating_expense_cost_of_sales(营业成本,万元),
  operating_expense_selling_expenses(销售费用,万元),
  operating_expense_administrative_expenses(管理费用,万元),
  operating_expense_financial_expenses(财务费用,万元),
  operating_expense_rnd_expenses(研发费用,万元),
  operating_expense_taxes_and_surcharges(税金及附加,万元),
  total_operating_expenses(营业总成本,万元),
  other_income(其他收益,万元), asset_impairment_loss(资产减值损失,万元),
  credit_impairment_loss(信用减值损失,万元)

## 表4: cash_flow_sheet (现金流量表)
字段: stock_code, stock_abbr, report_period, report_year,
  operating_cf_net_amount(经营活动现金流量净额,万元),
  operating_cf_ratio_of_net_cf(经营现金流占净现金流比例,% 注意:=经营现金流÷net_cash_flow,不是÷净利润),
  operating_cf_cash_from_sales(销售商品收到的现金,万元),
  investing_cf_net_amount(投资活动现金流量净额,万元),
  investing_cf_ratio_of_net_cf(投资现金流占净现金流比例,%),
  investing_cf_cash_for_investments(取得投资收到的现金,万元),
  investing_cf_cash_from_investment_recovery(收回投资收到的现金,万元),
  financing_cf_net_amount(筹资活动现金流量净额,万元),
  financing_cf_ratio_of_net_cf(筹资现金流占净现金流比例,%),
  financing_cf_cash_from_borrowing(取得借款收到的现金,万元),
  financing_cf_cash_for_debt_repayment(偿还债务支付的现金,万元),
  net_cash_flow(现金及现金等价物净增加额,万元)

## report_period 格式：第一季度=2025Q1, 半年度=2025HY, 第三季度=2025Q3, 年度=2025FY
## 注意：所有金额单位为万元，百分比字段存储的是数值（如10.5表示10.5%）
## 语义映射：用户说"利润"通常指净利润(net_profit)，不是利润总额(total_profit)；用户说"销售额/营收"指营业总收入(total_operating_revenue)。排序/Top N 时优先使用 net_profit。
## 【命名规范——层级标注】用户问题中"A-B"格式（如"营业总支出-研发费用""资产-应收账款"）表示B是A的子项，即取B字段的值，而非A减去B。例如"营业总支出-研发费用"指的是operating_expense_rnd_expenses（研发费用），"资产-应收账款"指的是asset_accounts_receivable（应收账款）。请勿将"-"理解为减法运算。

## 【极其重要——Q3数据口径】
## income_sheet 和 cash_flow_sheet 中，Q3 报告期的金额字段存储的是【第三季度单季值】，不是前三季度累计值！
## balance_sheet 中，Q3 报告期的金额字段存储的是【期末余额】（时点存量值）。
## 因此，当需要计算 balance_sheet 字段（如应收账款）占 income_sheet 字段（如营业总收入）的比例时，
## 如果报告期为 Q3，分母必须使用前三季度累计收入 = HY值 + Q3值（即同一公司 HY 报告期的值 + Q3 报告期的值相加）。
## 同理，Q1 是单季度值，HY 是上半年累计值，FY 是全年累计值。
## 【重要】只有跨表计算（balance_sheet字段 / income_sheet字段）才需要 HY+Q3 累加！
## 同一张表内的比率计算（如 income_sheet 的 net_profit / total_operating_revenue = 销售净利率）直接用 Q3 单季值即可，无需累加。
## 示例：应收账款占营收比 = balance_sheet.asset_accounts_receivable(Q3期末余额) / (income_sheet.total_operating_revenue(HY) + income_sheet.total_operating_revenue(Q3)) * 100
## 示例：销售净利率 = income_sheet.net_profit(Q3) / income_sheet.total_operating_revenue(Q3) * 100  ← 同表，直接用Q3值

请根据用户问题，输出一个 JSON 查询计划。严格只输出 JSON，不要其他内容。
格式如下（严格JSON，不要添加注释，不要在字段名后面加单位或说明）：
(注意：stock_code、stock_abbr、report_period 会自动添加到SELECT，columns中只需填写业务字段如"net_profit"，不要重复添加它们)
{
  "tables": [{"name": "表名", "columns": ["字段名"]}],
  "periods": ["2025Q3"],
  "filters": [{"column": "字段", "op": ">|<|=|>=|<=|!=|LIKE", "value": "值", "scope": "each_period|any_period|simple"}],
  "group_by": null | ["stock_code"],
  "having": null | "SQL HAVING子句内容",
  "order_by": null | {"column": "字段", "direction": "DESC|ASC"},
  "limit": null | 数字,
  "derived_columns": null | [{"name": "列名", "formula": "col1 / col2", "label": "显示名"}],
  "company_filter": null | {"stock_code": "000538"} | {"stock_abbr": "云南白药"}
}

规则：
0. 【跨表比较——极其重要】如果需要比较的两个字段分布在不同表中（如 balance_sheet 的总资产同比增长率 vs core_performance_indicators_sheet 的营收同比增长率），tables 必须列出两张表，并用 alias 区分！filter 的 value 可以引用另一个表的列（格式"别名.列名"）。
   示例："总资产同比增长率超过营收同比增长率" →
   tables: [{"name":"balance_sheet","columns":["asset_total_assets_yoy_growth"],"alias":"t1"}, {"name":"core_performance_indicators_sheet","columns":["operating_revenue_yoy_growth"],"alias":"t2"}],
   filters: [{"column":"asset_total_assets_yoy_growth","op":">","value":"t2.operating_revenue_yoy_growth","scope":"simple"}]
1. periods 必须是具体的报告期代码列表。数据库中可用的所有报告期为：["2022FY","2023Q1","2023HY","2023Q3","2023FY","2024Q1","2024HY","2024Q3","2024FY","2025Q1","2025HY","2025Q3"]。从中选取用户所需的报告期。
   - "2022年到2025年第三季度"= 上述全部12个报告期
   - "2022-2025年每年第三季度"= ["2022Q3","2023Q3","2024Q3","2025Q3"]（注意：2022年只有FY，无Q3数据，实际只有后三个）
   - "最近4个季度" = 从2025Q3往前数4个: ["2024Q3","2024FY","2025Q1","2025HY","2025Q3"]
2. 如果问题涉及多个表，tables 需要列出所有涉及的表
3. 如果需要"每期都满足"的条件，用 scope="each_period"。
   示例1：用户问"寻找所有报告期扣非净利润均超过5000万元的公司"→ periods列出所有报告期，filters=[{"column":"net_profit_excl_non_recurring","op":">","value":"5000","scope":"each_period"}]
   示例2：用户问"营业总收入连续保持正增长的公司"→ "正增长"="同比增长率>0" → filters=[{"column":"operating_revenue_yoy_growth","op":">","value":"0","scope":"each_period"}]
   示例3：用户问"连续亏损的公司"→ "亏损"="净利润<0" → filters=[{"column":"net_profit","op":"<","value":"0","scope":"each_period"}]
   关键词映射：正增长/持续增长→yoy_growth>0，负增长→yoy_growth<0，亏损→net_profit<0，盈利→net_profit>0
4. derived_columns 的 formula 只能用四则运算和列名
5. company_filter 用于限定单一公司。如果用户没有明确提到某个具体公司名称或股票代码，company_filter 必须为 null，不要自行假设或编造公司
6. 如果用户问的是整体/行业/全部公司的数据（如"xx行业的财务数据"），不要设置 company_filter
"""

# ─── 核心函数 ─────────────────────────────────────────────────────────────


ALL_AVAILABLE_PERIODS = [
    '2022FY', '2023Q1', '2023HY', '2023Q3', '2023FY',
    '2024Q1', '2024HY', '2024Q3', '2024FY',
    '2025Q1', '2025HY', '2025Q3',
]

_PERIOD_SORT = {p: i for i, p in enumerate(ALL_AVAILABLE_PERIODS)}


def _expand_periods_from_question(question: str, llm_periods: list[str]) -> list[str]:
    """当问题包含年份范围（如"2022-2025年第三季度"）且含有"连续""均""每期"等关键词时，
    将 periods 扩展为该范围内的全部可用报告期。"""
    # 匹配 "2022-2025年" 或 "2022到2025年" 或 "2022至2025年" 格式
    m = re.search(r'(20\d{2})\s*[-–—到至~]\s*(20\d{2})\s*年', question)
    if not m:
        return llm_periods
    # 只在问题含"连续""均""每期""都"等全量筛选关键词时才扩展
    if not re.search(r'连续|均[超大高低]|每期|每个报告期|都[超大高低满]|全部.*满足', question):
        return llm_periods
    start_year = int(m.group(1))
    end_year = int(m.group(2))
    # 判断结束报告期：如果提到"第三季度/Q3"，上界为 end_year+Q3
    end_suffix = 'Q3' if re.search(r'第三季度|三季度|Q3', question) else 'FY'
    end_period = f'{end_year}{end_suffix}'
    expanded = [p for p in ALL_AVAILABLE_PERIODS
                if int(p[:4]) >= start_year and _PERIOD_SORT.get(p, 999) <= _PERIOD_SORT.get(end_period, 999)]
    if len(expanded) > len(llm_periods):
        _log.info('[LLM-Planner] 扩展 periods: %s → %s', llm_periods, expanded)
        return expanded
    return llm_periods


def _auto_add_filter_from_question(question: str, plan: dict[str, Any]) -> dict[str, Any]:
    """如果 LLM 没有生成 filter 但问题暗含筛选条件（正增长/负增长/亏损/盈利），自动补充。"""
    filters = plan.get('filters') or []
    if filters:
        return plan  # LLM 已经生成了 filter，不干预

    tables = plan.get('tables', [])
    cols = []
    for t in tables:
        cols.extend(t.get('columns', []))

    # 检测隐含筛选条件
    _added = []
    # "正增长/持续增长/连续增长" + 营业/收入/营收 → operating_revenue_yoy_growth > 0
    if re.search(r'(正增长|持续增长|连续.*增长|保持.*增长)', question):
        if re.search(r'(营业|营收|收入|总收入)', question):
            _col = 'operating_revenue_yoy_growth'
            if _col in cols:
                _added.append({'column': _col, 'op': '>', 'value': '0', 'scope': 'each_period'})
        elif re.search(r'(净利润|利润)', question):
            _col = 'net_profit_yoy_growth'
            if _col in cols:
                _added.append({'column': _col, 'op': '>', 'value': '0', 'scope': 'each_period'})
    # "负增长" → yoy_growth < 0
    if re.search(r'负增长|持续下滑|连续下降', question):
        if re.search(r'(营业|营收|收入)', question):
            _col = 'operating_revenue_yoy_growth'
            if _col in cols:
                _added.append({'column': _col, 'op': '<', 'value': '0', 'scope': 'each_period'})
    # "亏损" → net_profit < 0
    if re.search(r'连续.*亏损|持续.*亏损', question):
        for c in cols:
            if 'net_profit' in c and 'yoy' not in c and 'excl' not in c:
                _added.append({'column': c, 'op': '<', 'value': '0', 'scope': 'each_period'})
                break
    # "盈利" → net_profit > 0
    if re.search(r'连续.*盈利|持续.*盈利', question):
        for c in cols:
            if 'net_profit' in c and 'yoy' not in c and 'excl' not in c:
                _added.append({'column': c, 'op': '>', 'value': '0', 'scope': 'each_period'})
                break
    if _added:
        plan['filters'] = _added
        _log.info('[LLM-Planner] 自动补充 filters: %s', _added)
    return plan


def get_query_plan(question: str) -> dict[str, Any] | None:
    """调用 LLM 将用户问题解析为结构化查询计划。失败返回 None。"""
    if not llm_available():
        return None
    _temps = [
        llm_temperature('query_plan_temperature', 0.1),
        0.05,  # retry with lower temperature
        0.01,  # last resort with near-zero temperature
    ]
    for _attempt, _temp in enumerate(_temps, 1):
        try:
            plan = request_llm_json(
                SCHEMA_PROMPT, question,
                temperature=_temp,
                max_tokens=2048,
            )
            if not plan.get('tables'):
                return None
            # Guard: strip company_filter if the question doesn't mention any company
            cf = plan.get('company_filter')
            if cf:
                _cf_val = cf.get('stock_abbr', '') or cf.get('stock_code', '')
                if _cf_val and _cf_val not in question:
                    _log.info('[LLM-Planner] 问题未提及公司"%s"，移除 company_filter', _cf_val)
                    plan['company_filter'] = None
            # ── 年份范围扩展：检测"YYYY-YYYY年XXX"并扩展 periods ──
            plan['periods'] = _expand_periods_from_question(question, plan.get('periods', []))
            # ── 自动补充 filter：检测"正增长/负增长/亏损/盈利"等关键词 ──
            plan = _auto_add_filter_from_question(question, plan)
            return plan
        except Exception as e:
            _log.warning('[LLM-Planner] 解析失败(尝试%d): %s', _attempt, e)
            if _attempt >= len(_temps):
                return None


def execute_query_plan(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], str, list[str]]:
    """
    将查询计划转为参数化 SQL 并执行。
    返回: (rows, display_sql, source_tables)
    如果有 each_period scope 条件，在 Python 侧做多期过滤。
    """
    tables = plan.get('tables', [])
    periods = plan.get('periods', [])
    filters = plan.get('filters') or []
    order = plan.get('order_by')
    limit = plan.get('limit')
    derived = plan.get('derived_columns') or []
    group_by = plan.get('group_by')
    having = plan.get('having')
    company = plan.get('company_filter') or {}

    # ── 校验 ──
    if not tables:
        raise ValueError('查询计划缺少 tables')
    source_tables = []
    for t in tables:
        tname = t['name']
        if tname not in ALLOWED_TABLES:
            raise ValueError(f'非法表名: {tname}')
        source_tables.append(tname)
        _valid_cols = []
        _raw_cols = t.get('columns', [])
        # Expand wildcard: ["*"] or ["table.*"] → all real columns from that table
        if any(c.strip() in ('*', f'{tname}.*') for c in _raw_cols):
            _whitelist = _get_column_whitelist().get(tname, set())
            _raw_cols = sorted(_whitelist - {'stock_code', 'stock_abbr', 'report_period', 'report_year'})
        # Fix LLM combining multiple columns into one string: "col1, col2" → ["col1", "col2"]
        _expanded_cols: list[str] = []
        for _rc in _raw_cols:
            if ',' in _rc:
                _expanded_cols.extend(part.strip() for part in _rc.split(',') if part.strip())
            else:
                _expanded_cols.append(_rc.strip())
        _raw_cols = _expanded_cols
        for col in _raw_cols:
            if col in ('stock_code', 'stock_abbr', 'report_period', 'report_year'):
                _valid_cols.append(col)
            elif _validate_column(tname, col):
                _valid_cols.append(col)
            else:
                _log.warning('[LLM-Planner] 跳过非法列名: %s.%s', tname, col)
        t['columns'] = _valid_cols

    params: list[Any] = []

    # ── 清洗 filter scope 值（LLM 可能在 scope 中夹杂额外文本）──
    _valid_scopes = {'each_period', 'any_period', 'simple'}
    for f in filters:
        _scope = str(f.get('scope', 'simple')).strip()
        # 取逗号前的第一个token，如 "each_period, stock_code" → "each_period"
        _scope = _scope.split(',')[0].strip().split()[0].strip() if _scope else 'simple'
        if _scope not in _valid_scopes:
            _scope = 'simple'
        f['scope'] = _scope

    # ── 多期 each_period 过滤：特殊处理 ──
    each_period_filters = [f for f in filters if f.get('scope') == 'each_period']
    simple_filters = [f for f in filters if f.get('scope') != 'each_period']

    if each_period_filters and len(periods) >= 2:
        return _execute_multi_period_plan(plan, periods, each_period_filters, simple_filters, derived)

    # ── 构建 SQL ──
    if len(tables) == 1:
        t = tables[0]
        alias = t.get('alias', 't1')
        data_cols = [c for c in t.get('columns', []) if c not in ('stock_code', 'stock_abbr')]
        select_parts = [f"TRIM(stock_code) AS stock_code", f"TRIM(stock_abbr) AS stock_abbr"]
        if 'report_period' not in data_cols and len(periods) > 1:
            select_parts.append('report_period')
        select_parts.extend(data_cols)
        sql = f"SELECT {', '.join(select_parts)} FROM {t['name']}"
        wheres = []
        if periods:
            ph = ','.join(['?'] * len(periods))
            wheres.append(f"report_period IN ({ph})")
            params.extend(periods)
        for f in simple_filters:
            col = f['column']
            op = f['op']
            val = f['value']
            if op.upper() == 'LIKE':
                wheres.append(f"{col} LIKE ?")
                params.append(val)
            else:
                wheres.append(f"{col} {op} ?")
                params.append(val)
        if company:
            if 'stock_code' in company:
                wheres.append("TRIM(stock_code) = ?")
                params.append(company['stock_code'])
            elif 'stock_abbr' in company:
                wheres.append("TRIM(stock_abbr) = ?")
                params.append(company['stock_abbr'])
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
    else:
        primary = tables[0]
        p_alias = primary.get('alias', 't1')
        select_parts = [
            f"TRIM({p_alias}.stock_code) AS stock_code",
            f"TRIM({p_alias}.stock_abbr) AS stock_abbr",
        ]
        if len(periods) > 1:
            select_parts.append(f"{p_alias}.report_period")
        _all_data_cols = []
        for t in tables:
            a = t.get('alias', 't1')
            for c in t.get('columns', []):
                if c not in ('stock_code', 'stock_abbr', 'report_period', 'report_year'):
                    _all_data_cols.append((a, c))
        # Detect duplicate column names across tables → alias as tablealias_col
        from collections import Counter
        _col_counts = Counter(c for _, c in _all_data_cols)
        for a, c in _all_data_cols:
            if _col_counts[c] > 1:
                select_parts.append(f"{a}.{c} AS {a}_{c}")
            else:
                select_parts.append(f"{a}.{c}")
        sql = f"SELECT {', '.join(select_parts)} FROM {primary['name']} {p_alias}"
        for t in tables[1:]:
            a = t.get('alias', 't2')
            sql += (
                f" JOIN {t['name']} {a} "
                f"ON TRIM({p_alias}.stock_code) = TRIM({a}.stock_code) "
                f"AND {p_alias}.report_period = {a}.report_period"
            )
        wheres = []
        if periods:
            ph = ','.join(['?'] * len(periods))
            wheres.append(f"{p_alias}.report_period IN ({ph})")
            params.extend(periods)
        # Collect valid table aliases for cross-column reference detection
        _valid_aliases = {t.get('alias', f't{i+1}') for i, t in enumerate(tables)}
        for f in simple_filters:
            _fcol = f['column']
            _fval = f['value']
            # Prefix with primary alias if column is ambiguous across tables
            if '.' not in _fcol:
                _fcol = f"{p_alias}.{_fcol}"
            # Detect cross-column comparison: value like 't2.column_name'
            if isinstance(_fval, str) and '.' in _fval:
                _ref_parts = _fval.split('.', 1)
                if _ref_parts[0] in _valid_aliases:
                    # Direct column reference, not a parameterized value
                    wheres.append(f"{_fcol} {f['op']} {_fval}")
                    continue
            wheres.append(f"{_fcol} {f['op']} ?")
            params.append(_fval)
        if company:
            if 'stock_code' in company:
                wheres.append(f"TRIM({p_alias}.stock_code) = ?")
                params.append(company['stock_code'])
            elif 'stock_abbr' in company:
                wheres.append(f"TRIM({p_alias}.stock_abbr) = ?")
                params.append(company['stock_abbr'])
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)

    if group_by:
        gb_items = [group_by] if isinstance(group_by, str) else list(group_by)
        if len(tables) > 1:
            p_alias = tables[0].get('alias', 't1')
            gb_items = [f"{p_alias}.{g}" if '.' not in g else g for g in gb_items]
        sql += f" GROUP BY {', '.join(gb_items)}"
    if having:
        # 安全检查：不允许子查询或参数占位符
        if 'SELECT' in having.upper():
            raise ValueError('HAVING 子句不允许包含子查询')
        if '?' in having:
            _log.warning('[LLM-Planner] HAVING 含 ? 占位符，跳过: %s', having)
            having = None
        else:
            sql += f" HAVING {having}"
    if order:
        _ord_col = order['column']
        # For multi-table queries, prefix order column with alias if needed
        if len(tables) > 1 and '.' not in _ord_col:
            _found = False
            # Find which table has the column in its data columns
            for _t in tables:
                if _ord_col in _t.get('columns', []):
                    _ord_col = f"{_t.get('alias', 't1')}.{_ord_col}"
                    _found = True
                    break
            # Fallback: common columns (report_period etc.) exist in all tables
            # but are excluded from each table's column list — use primary alias
            if not _found:
                p_alias = tables[0].get('alias', 't1')
                _ord_col = f"{p_alias}.{_ord_col}"
        sql += f" ORDER BY {_ord_col} {order['direction']}"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    # 参数数量校验：? 个数必须与 params 长度一致
    _q_count = sql.count('?')
    if _q_count != len(params):
        _log.warning('[LLM-Planner] 参数数量不匹配: SQL有%d个?, params有%d个, 返回空结果', _q_count, len(params))
        return [], _build_display_sql(sql, params), source_tables

    _log.debug('[LLM-Planner] SQL: %s | Params: %s', sql, params)
    display_sql = _build_display_sql(sql, params)
    try:
        rows = [dict(r) for r in query_rows(sql, tuple(params))]
    except Exception as _sql_err:
        _log.warning('[LLM-Planner] SQL执行失败: %s | SQL: %s', _sql_err, sql)
        # Retry: strip GROUP BY / HAVING and try again
        _fallback_sql = sql
        for _clause in [' GROUP BY ', ' HAVING ']:
            if _clause in _fallback_sql:
                _tail_idx = _fallback_sql.index(_clause)
                _rest = _fallback_sql[_tail_idx:]
                # Find next major clause after this one
                _next = len(_rest)
                for _kw in [' ORDER BY ', ' LIMIT ']:
                    _ki = _rest.find(_kw)
                    if _ki > 0 and _ki < _next:
                        _next = _ki
                _fallback_sql = _fallback_sql[:_tail_idx] + (_rest[_next:] if _next < len(_rest) else '')
        if _fallback_sql != sql:
            _fq = _fallback_sql.count('?')
            if _fq == len(params):
                try:
                    rows = [dict(r) for r in query_rows(_fallback_sql, tuple(params))]
                    display_sql = _build_display_sql(_fallback_sql, params)
                    _log.info('[LLM-Planner] 简化SQL重试成功: %d行', len(rows))
                except Exception:
                    rows = []
            else:
                rows = []
        else:
            rows = []

    # 计算派生列
    _compute_derived(rows, derived)

    return rows, display_sql, source_tables


def _execute_multi_period_plan(
    plan: dict, periods: list[str],
    each_filters: list[dict], simple_filters: list[dict],
    derived: list[dict],
) -> tuple[list[dict], str, list[str]]:
    """多期 each_period 筛选：查出所有期间数据，在 Python 中筛选满足所有期间条件的公司。"""
    tables = plan['tables']
    t = tables[0]
    tname = t['name']
    data_cols = [c for c in t.get('columns', []) if c not in ('stock_code', 'stock_abbr')]
    if 'report_period' not in data_cols:
        data_cols = ['report_period'] + data_cols
    sel = ', '.join(['TRIM(stock_code) AS stock_code', 'TRIM(stock_abbr) AS stock_abbr'] + data_cols)

    ph = ','.join(['?'] * len(periods))
    params: list[Any] = list(periods)
    wheres = [f"report_period IN ({ph})"]

    company = plan.get('company_filter') or {}
    if company.get('stock_code'):
        wheres.append("TRIM(stock_code) = ?")
        params.append(company['stock_code'])

    sql = f"SELECT {sel} FROM {tname} WHERE {' AND '.join(wheres)}"
    all_rows = [dict(r) for r in query_rows(sql, tuple(params))]

    # 按公司分组
    by_company: dict[str, dict] = {}
    for r in all_rows:
        code = str(r.get('stock_code', '')).strip()
        abbr = str(r.get('stock_abbr', '')).strip()
        period = r.get('report_period')
        if not code or not period:
            continue
        if code not in by_company:
            by_company[code] = {'stock_code': code, 'stock_abbr': abbr, 'periods': {}}
        by_company[code]['periods'][period] = r

    # 筛选：每个期间都满足条件
    filtered = []
    for info in by_company.values():
        if len(info['periods']) < len(periods):
            continue
        ok = True
        for f in each_filters:
            col = f['column']
            op = f['op']
            thr = float(f['value'])
            for p in periods:
                row_data = info['periods'].get(p, {})
                try:
                    val = float(row_data.get(col, 0) or 0)
                except (TypeError, ValueError):
                    ok = False
                    break
                if op == '>' and val <= thr:
                    ok = False
                elif op == '>=' and val < thr:
                    ok = False
                elif op == '<' and val >= thr:
                    ok = False
                elif op == '<=' and val > thr:
                    ok = False
                elif op == '=' and val != thr:
                    ok = False
            if not ok:
                break
        if ok:
            row = {'stock_code': info['stock_code'], 'stock_abbr': info['stock_abbr']}
            # 只保留汇总信息，不展开每个报告期的列（避免 LLM 逐条罗列）
            row['qualifying_periods'] = len(info['periods'])
            for f in each_filters:
                col = f['column']
                vals = []
                for p in periods:
                    rd = info['periods'].get(p, {})
                    try:
                        vals.append(float(rd.get(col, 0) or 0))
                    except (TypeError, ValueError):
                        pass
                if vals:
                    row[f'{col}_min'] = round(min(vals), 2)
                    row[f'{col}_max'] = round(max(vals), 2)
                    row[f'{col}_avg'] = round(sum(vals) / len(vals), 2)
            filtered.append(row)

    # 记录筛选前总数
    if filtered and len(filtered) < len(by_company):
        for r in filtered:
            r['_total_before_filter'] = len(by_company)

    display_sql = _build_display_sql(sql, params)
    _compute_derived(filtered, derived)
    return filtered, display_sql, [tname]


def _compute_derived(rows: list[dict], derived: list[dict]):
    """安全计算派生列（仅允许四则运算）。"""
    if not derived:
        return
    for row in rows:
        for d in derived:
            try:
                formula = d['formula']
                expr = formula
                for col_name in sorted(row.keys(), key=len, reverse=True):
                    if col_name in expr:
                        val = row.get(col_name)
                        expr = expr.replace(col_name, str(float(val)) if val is not None else '0')
                # 安全检查：只允许数字、运算符和括号
                if re.match(r'^[\d\s+\-*/.()]+$', expr):
                    row[d['name']] = round(eval(expr), 4)
                else:
                    row[d['name']] = None
            except Exception:
                row[d['name']] = None


def _build_display_sql(sql: str, params: list) -> str:
    """将参数化SQL转为可展示的SQL（替换?为实际值）。"""
    display = sql
    for p in params:
        if isinstance(p, str):
            display = display.replace('?', f"'{p}'", 1)
        else:
            display = display.replace('?', str(p), 1)
    return display + ';'


def build_columns_from_plan(plan: dict, rows: list[dict]) -> list[dict[str, str]]:
    """根据查询计划和结果行构建列定义。"""
    from .assistant import TABLE_METRIC_COLUMNS
    columns = [
        {'key': 'stock_code', 'label': '股票代码'},
        {'key': 'stock_abbr', 'label': '公司简称'},
    ]
    seen = {'stock_code', 'stock_abbr'}

    # 从结果行的 key 中推断列
    if rows:
        for key in rows[0]:
            if key in seen:
                continue
            seen.add(key)
            # 尝试从 TABLE_METRIC_COLUMNS 获取标签
            label = key
            for t_cols in TABLE_METRIC_COLUMNS.values():
                if key in t_cols:
                    label = t_cols[key]
                    break
            # 检查是否是 period_column 格式（多期筛选）
            if re.match(r'^\d{4}(Q[13]|HY|FY)_', key):
                period_prefix = key.split('_', 1)[0] if '_' in key else key
                col_suffix = key.split('_', 1)[1] if '_' in key else ''
                col_label = col_suffix
                for t_cols in TABLE_METRIC_COLUMNS.values():
                    if col_suffix in t_cols:
                        col_label = t_cols[col_suffix]
                        break
                label = f"{period_prefix} {col_label}"
            # 检查是否是派生列
            for d in (plan.get('derived_columns') or []):
                if d['name'] == key:
                    label = d.get('label', key)
                    break
            columns.append({'key': key, 'label': label})
    return columns


# ═══════════════════════════════════════════════════════════════════════
# 多步管道计划器 (Pipeline Planner)
# ═══════════════════════════════════════════════════════════════════════

PIPELINE_PROMPT = """你是一个金融数据查询助手。当查询涉及**派生计算、聚合筛选、多步操作**时，请输出一个多步管道计划。

数据库表和字段与之前相同（4张表: core_performance_indicators_sheet, balance_sheet, income_sheet, cash_flow_sheet）。
report_period 格式：2025Q1, 2025HY, 2025Q3, 2025FY。金额单位万元，百分比是数值（10.5表示10.5%）。

## 字段参考
income_sheet: total_operating_revenue(营业总收入), operating_profit(营业利润), total_profit(利润总额), net_profit(净利润), operating_expense_cost_of_sales(营业成本), operating_expense_selling_expenses(销售费用), operating_expense_administrative_expenses(管理费用), operating_expense_financial_expenses(财务费用), operating_expense_rnd_expenses(研发费用), total_operating_expenses(营业总成本), operating_expense_taxes_and_surcharges(税金及附加), other_income(其他收益), asset_impairment_loss(资产减值损失), credit_impairment_loss(信用减值损失)
**【公式校验提示】** 营业利润的推导公式：operating_profit ≈ total_operating_revenue - total_operating_expenses + other_income（数据库中无投资收益/公允价值变动收益/资产处置收益字段，差异归因于此）。做公式校验时，必须查询 other_income 字段。
balance_sheet: asset_total_assets(总资产), liability_total_liabilities(总负债), equity_total_equity(所有者权益), asset_liability_ratio(资产负债率%), asset_total_assets_yoy_growth(总资产同比增长率%), liability_total_liabilities_yoy_growth(总负债同比增长率%), asset_inventory(存货), asset_accounts_receivable(应收账款), asset_cash_and_cash_equivalents(货币资金)
core_performance_indicators_sheet: total_operating_revenue(营业总收入), net_profit_10k_yuan(归母净利润), eps(每股收益), roe(加权ROE%), net_profit_excl_non_recurring(扣非净利润), operating_revenue_yoy_growth(营收同比增长率%), net_profit_yoy_growth(净利润同比增长率%), gross_profit_margin(毛利率%), net_profit_margin(净利率%)
cash_flow_sheet: operating_cf_net_amount(经营活动现金流量净额,万元), operating_cf_ratio_of_net_cf(经营现金流占净现金流比例,% 注意:分母是net_cash_flow不是净利润), operating_cf_cash_from_sales(销售商品收到的现金,万元), investing_cf_net_amount(投资活动现金流量净额,万元), investing_cf_cash_for_investments(投资支付的现金,万元), financing_cf_net_amount(筹资活动现金流量净额,万元), financing_cf_cash_from_borrowing(取得借款收到的现金,万元), financing_cf_cash_for_debt_repayment(偿还债务支付的现金,万元), net_cash_flow(现金及现金等价物净增加额,万元)

**重要**：如果问题要求"A与B的比值"且不存在现成字段，必须用join+compute计算，不要猜测列名。例如"经营现金流与净利润的比值"需要join income_sheet取net_profit再compute。
**语义映射**：用户说"利润"通常指净利润(net_profit)，不是利润总额(total_profit)；用户说"销售额/营收"指营业总收入(total_operating_revenue)。排序/Top N 时优先使用 net_profit。
**【命名规范——层级标注】**用户问题中"A-B"格式（如"营业总支出-研发费用""资产-应收账款"）表示的是B是A的子项，即取B字段的值，而非A减去B。例如"营业总支出-研发费用"指的是operating_expense_rnd_expenses（研发费用），"资产-应收账款"指的是asset_accounts_receivable（应收账款）。请勿将"-"理解为减法运算。

**【极其重要——Q3数据口径】**
income_sheet 和 cash_flow_sheet 中，Q3 报告期的金额字段存储的是【第三季度单季值】，不是前三季度累计值！
balance_sheet 中，Q3 报告期的金额字段存储的是【期末余额】（时点存量值）。
因此，当需要计算 balance_sheet 字段（如应收账款）与 income_sheet 字段（如营业总收入）的比值/占比时，如果报告期为 Q3，必须先用两步 query+join 取得 HY 和 Q3 两期的 income 数据，再 compute 相加得到前三季度累计值作为分母。
示例步骤：
  1. query income_sheet period="2025HY" 取 total_operating_revenue（重命名为 rev_hy）
  2. join income_sheet period="2025Q3" 取 total_operating_revenue（重命名为 rev_q3）
  3. join balance_sheet period="2025Q3" 取 asset_accounts_receivable
  4. compute: "asset_accounts_receivable / (rev_hy + rev_q3) * 100"
注意：Q1 是单季值，HY 是上半年累计值，FY 是全年累计值。仅 Q3 需要特殊处理（HY+Q3=前三季度累计）。
**【重要——请严格遵守】只有跨表计算（balance_sheet字段 / income_sheet字段）才需要 HY+Q3 累加！同一张表内的比率（如 net_profit / total_operating_revenue = 销售净利率、operating_expense_rnd_expenses / total_operating_revenue = 研发费用率）直接用 Q3 单期数据即可，绝对不要做 HY+Q3 累加！**
示例："2025Q3研发费用总额占营收比例" → query income_sheet period="2025Q3" cols=["operating_expense_rnd_expenses","total_operating_revenue"]，然后 sum + compute，不需要查 HY。

**【周转率计算——平均余额】**
周转率类指标（如存货周转率、应收账款周转率）的分母是"平均余额"，即 (期初值+期末值)/2。
balance_sheet 是时点存量值，因此：Q3的"期初"= 上一期末 = HY期末值，Q3的"期末"= Q3期末值。
示例——存货周转率 = 营业成本 / 平均存货：
  1. query balance_sheet period="2025HY" cols=["asset_inventory"] alias={"asset_inventory": "inv_start"}  （期初存货=HY期末）
  2. join balance_sheet period="2025Q3" cols=["asset_inventory"] alias={"asset_inventory": "inv_end"}  （期末存货=Q3期末）
  3. join income_sheet period="2025Q3" cols=["operating_expense_cost_of_sales"]
  4. compute: name="avg_inv", expr="(inv_start + inv_end) / 2", label="平均存货"
  5. compute: name="inv_turnover", expr="operating_expense_cost_of_sales / avg_inv", label="存货周转率"
同理，应收账款周转率 = 营业收入 / ((期初应收+期末应收)/2)。

请输出严格 JSON，格式：
{
  "steps": [
    {"op": "query", "table": "income_sheet", "period": "2025Q3", "cols": ["col1", "col2"], "alias": {"col1": "别名1"}},
    {"op": "join", "table": "balance_sheet", "cols": ["col3"], "alias": {"col3": "别名2"}},
    {"op": "compute", "name": "new_col", "expr": "col1 / col2 * 100", "label": "显示名"},
    {"op": "avg", "col": "new_col", "save_as": "avg_val"},
    {"op": "filter", "condition": "new_col < avg_val"},
    {"op": "diff", "col_a": "col1", "col_b": "col2", "name": "diff_col", "label": "差值"},
    {"op": "abs_filter", "col": "diff_col", "threshold": 10},
    {"op": "bucket", "col": "new_col", "ranges": [{"label": "0-5%", "min": 0, "max": 5}, {"label": "5-10%", "min": 5, "max": 10}, {"label": "10%以上", "min": 10}]},
    {"op": "rank", "by": "col3", "order": "desc", "limit": 5}
  ],
  "title": "图表标题",
  "chart_type": "bar"
}

## 操作说明
- query: 查某张表某期间的数据（必须是第一步）
- join: 从另一张表补充字段（按stock_code合并），可指定 period 和 alias
- alias: 可选的列别名映射（如 {"total_operating_revenue": "rev_hy"}），用于同一字段取不同期间时避免列名冲突。query 和 join 均可用。
- compute: 用四则运算创建新列（只能引用已有列名）
- avg/sum/min/max/median: 对某列求聚合，结果存入变量（save_as）
- filter: 过滤行（condition用 > < >= <= == != 和已存变量名）
- diff: 计算两列之差
- abs_filter: 按绝对值/差值过滤（差值 > threshold 才保留）
- bucket: 按数值区间分组统计（ranges中每个区间有label/min/max，min含max不含，最后一个区间可省略max表示≥min）。可选 agg_col 和 agg_op(avg/sum/min/max) 对组内另一列聚合，如 {"op":"bucket","col":"rnd_ratio","ranges":[...],"agg_col":"net_profit_yoy_growth","agg_op":"avg","agg_label":"平均净利润增长率"}
- rank: 排序取前N

## 规则
1. steps 必须以 query 开头
2. period 必须是具体代码如 "2025Q3"
3. compute 的 expr 只能用四则运算和已有列名
4. filter 的 condition 可以引用 save_as 变量
5. 只输出 JSON，不要其他文字
6. rank 必须是最后一步操作，rank 之后不要再添加 query/join/compute 步骤
7. 多意图问题（如①计算...②找出...③分析...）只需完成数据计算和排序，③的分析部分由系统结合研报自动完成，无需额外步骤
8. 验证同比增长率时，必须用 join 拉取基期（上一年同期）数据，例如验证2025Q3净利润同比增长率，需要 join income_sheet period="2024Q3" 取 net_profit 并用 alias 重命名（如 "net_profit_base"），然后用 compute 重新计算增长率：(net_profit - net_profit_base) / abs(net_profit_base) * 100。这样既可验证基数正负，也可在基数为负时重新计算。
"""


def get_pipeline_plan(question: str) -> dict[str, Any] | None:
    """让 LLM 生成多步管道计划。"""
    if not llm_available():
        return None
    try:
        plan = request_llm_json(
            PIPELINE_PROMPT, question,
            temperature=llm_temperature('query_plan_temperature', 0.1),
        )
        steps = plan.get('steps')
        if not steps or not isinstance(steps, list):
            return None
        if steps[0].get('op') != 'query':
            return None
        return plan
    except Exception as e:
        _log.warning('[Pipeline] 解析失败: %s', e)
        return None


def execute_pipeline_plan(plan: dict[str, Any]) -> tuple[list[dict], str, list[str], list[dict], str | None]:
    """
    执行多步管道计划。
    返回: (rows, display_sql, source_tables, columns, chart_title)
    """
    steps = plan['steps']
    # Strip invalid steps after rank (LLM sometimes adds re-query steps)
    _rank_idx = next((i for i, s in enumerate(steps) if s.get('op') == 'rank'), None)
    if _rank_idx is not None:
        steps = steps[:_rank_idx + 1]
    rows: list[dict[str, Any]] = []
    variables: dict[str, float] = {}
    source_tables: list[str] = []
    col_labels: dict[str, str] = {}  # col_name -> display label
    sql_parts: list[str] = []

    from .assistant import TABLE_METRIC_COLUMNS

    for step in steps:
        op = step.get('op', '')

        if op == 'query':
            table = step['table']
            if table not in ALLOWED_TABLES:
                raise ValueError(f'非法表名: {table}')
            source_tables.append(table)
            period = step.get('period', '')
            cols = step.get('cols', [])
            valid_cols = [c for c in cols if _validate_column(table, c) and c not in ('stock_code', 'stock_abbr')]
            if not valid_cols:
                raise ValueError(f'无有效列: {cols}')
            sel = ', '.join(valid_cols)
            aliases = step.get('alias') or {}
            sql = (
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {sel} "
                f"FROM {table} WHERE report_period = ?"
            )
            rows = [dict(r) for r in query_rows(sql, (period,))]
            sql_parts.append(f"SELECT stock_code, stock_abbr, {sel} FROM {table} WHERE report_period = '{period}';")
            # 应用列别名
            if aliases:
                for row in rows:
                    for orig, alias in aliases.items():
                        if orig in row:
                            row[alias] = row.pop(orig)
            # 记录列标签
            tmc = TABLE_METRIC_COLUMNS.get(table, {})
            for c in valid_cols:
                alias_name = aliases.get(c, c)
                col_labels[alias_name] = tmc.get(c, c)

        elif op == 'join':
            table = step['table']
            if table not in ALLOWED_TABLES:
                raise ValueError(f'非法表名: {table}')
            source_tables.append(table)
            cols = step.get('cols', [])
            valid_cols = [c for c in cols if _validate_column(table, c)]
            if not valid_cols or not rows:
                continue
            # 从 join 步骤自身的 period 或首个 query 步骤的 period
            period = step.get('period') or steps[0].get('period', '')
            sel = ', '.join(valid_cols)
            join_sql = (
                f"SELECT TRIM(stock_code) AS stock_code, {sel} "
                f"FROM {table} WHERE report_period = ?"
            )
            join_rows = {str(r['stock_code']).strip(): r for r in query_rows(join_sql, (period,))}
            aliases = step.get('alias') or {}
            for row in rows:
                code = str(row.get('stock_code', '')).strip()
                jr = join_rows.get(code, {})
                for c in valid_cols:
                    alias_name = aliases.get(c, c)
                    row[alias_name] = jr.get(c)
            sql_parts.append(f"SELECT stock_code, {sel} FROM {table} WHERE report_period = '{period}';")
            tmc = TABLE_METRIC_COLUMNS.get(table, {})
            for c in valid_cols:
                alias_name = aliases.get(c, c)
                col_labels[alias_name] = tmc.get(c, c)

        elif op == 'compute':
            name = step['name']
            expr_template = step['expr']
            label = step.get('label', name)
            col_labels[name] = label
            _non_numeric_keys = {'stock_code', 'stock_abbr', 'report_period', 'rank', 'range', 'companies'}
            for row in rows:
                try:
                    expr = expr_template
                    for col_name in sorted(row.keys(), key=len, reverse=True):
                        if col_name in expr and col_name not in _non_numeric_keys:
                            val = row.get(col_name)
                            expr = expr.replace(col_name, str(float(val)) if val is not None else '0')
                    if re.match(r'^[\d\s+\-*/.()]+$', expr):
                        result = eval(expr)
                        row[name] = round(result, 2) if result is not None and not (isinstance(result, float) and (result != result)) else None
                    else:
                        row[name] = None
                except (ZeroDivisionError, TypeError, ValueError):
                    row[name] = None
                except Exception:
                    row[name] = None
            pass  # compute is in-memory, no SQL to display

        elif op in ('avg', 'sum', 'min', 'max', 'median'):
            col = step['col']
            save_as = step.get('save_as', f'{op}_{col}')
            vals = []
            for row in rows:
                try:
                    v = float(row.get(col) or 0)
                    vals.append(v)
                except (TypeError, ValueError):
                    pass
            if vals:
                if op == 'avg':
                    variables[save_as] = round(sum(vals) / len(vals), 2)
                elif op == 'sum':
                    variables[save_as] = round(sum(vals), 2)
                elif op == 'min':
                    variables[save_as] = round(min(vals), 2)
                elif op == 'max':
                    variables[save_as] = round(max(vals), 2)
                elif op == 'median':
                    vals.sort()
                    _n = len(vals)
                    _mid = _n // 2
                    variables[save_as] = round(vals[_mid] if _n % 2 else (vals[_mid - 1] + vals[_mid]) / 2, 2)
            else:
                variables[save_as] = 0.0
            pass  # aggregate is in-memory, no SQL to display

        elif op == 'filter':
            condition = step['condition']
            filtered = []
            _non_numeric_keys = {'stock_code', 'stock_abbr', 'report_period', 'rank', 'range', 'companies'}
            for row in rows:
                try:
                    expr = condition
                    # 替换变量
                    for vname, vval in sorted(variables.items(), key=lambda x: len(x[0]), reverse=True):
                        expr = expr.replace(vname, str(vval))
                    # 替换列值
                    for col_name in sorted(row.keys(), key=len, reverse=True):
                        if col_name in expr and col_name not in _non_numeric_keys:
                            val = row.get(col_name)
                            if val is None:
                                expr = expr.replace(col_name, '0')
                            else:
                                expr = expr.replace(col_name, str(float(val)))
                    # 安全检查
                    if re.match(r'^[\d\s+\-*/.()<>=!]+$', expr):
                        if eval(expr):
                            filtered.append(row)
                    else:
                        filtered.append(row)  # 无法解析则保留
                except Exception:
                    pass  # 解析失败的行跳过
            # 记录筛选前总数，供 LLM 生成时区分"全部N家"和"筛选后M家"
            if len(filtered) < len(rows):
                for r in filtered:
                    r['_total_before_filter'] = len(rows)
            rows = filtered
            pass  # filter is in-memory, no SQL to display

        elif op == 'diff':
            col_a = step['col_a']
            col_b = step['col_b']
            name = step.get('name', 'diff')
            label = step.get('label', f'{col_a}-{col_b}')
            col_labels[name] = label
            for row in rows:
                try:
                    va = float(row.get(col_a) or 0)
                    vb = float(row.get(col_b) or 0)
                    row[name] = round(va - vb, 2)
                except (TypeError, ValueError):
                    row[name] = None
            pass  # diff is in-memory, no SQL to display

        elif op == 'abs_filter':
            col = step['col']
            threshold = float(step.get('threshold', 0))
            rows = [r for r in rows if r.get(col) is not None and abs(float(r[col])) > threshold]
            pass  # abs_filter is in-memory, no SQL to display

        elif op == 'bucket':
            col = step['col']
            ranges = step.get('ranges', [])
            agg_col = step.get('agg_col')
            agg_op = step.get('agg_op', 'avg')
            bucket_rows: list[dict[str, Any]] = []
            for rng in ranges:
                label = rng.get('label', '')
                r_min = rng.get('min')
                r_max = rng.get('max')
                count = 0
                companies: list[str] = []
                agg_vals: list[float] = []
                for row in rows:
                    try:
                        v = float(row.get(col) or 0)
                    except (TypeError, ValueError):
                        continue
                    in_range = True
                    if r_min is not None and v < float(r_min):
                        in_range = False
                    if r_max is not None and v >= float(r_max):
                        in_range = False
                    if in_range:
                        count += 1
                        companies.append(str(row.get('stock_abbr', '')))
                        if agg_col:
                            try:
                                agg_vals.append(float(row.get(agg_col) or 0))
                            except (TypeError, ValueError):
                                pass
                _brow: dict[str, Any] = {
                    'range': label,
                    'count': count,
                    'companies': ', '.join(companies[:10]) + ('...' if len(companies) > 10 else ''),
                }
                if agg_col and agg_vals:
                    if agg_op == 'avg':
                        _brow[f'avg_{agg_col}'] = round(sum(agg_vals) / len(agg_vals), 2)
                    elif agg_op == 'sum':
                        _brow[f'sum_{agg_col}'] = round(sum(agg_vals), 2)
                    elif agg_op == 'min':
                        _brow[f'min_{agg_col}'] = round(min(agg_vals), 2)
                    elif agg_op == 'max':
                        _brow[f'max_{agg_col}'] = round(max(agg_vals), 2)
                elif agg_col:
                    _brow[f'{agg_op}_{agg_col}'] = None
                bucket_rows.append(_brow)
            rows = bucket_rows
            col_labels['range'] = '区间'
            col_labels['count'] = '公司数量'
            col_labels['companies'] = '公司列表'
            if agg_col:
                _agg_key = f'{agg_op}_{agg_col}'
                col_labels[_agg_key] = step.get('agg_label', f'{agg_op}({col_labels.get(agg_col, agg_col)})')
            pass  # bucket is in-memory, no SQL to display

        elif op == 'rank':
            by = step['by']
            order = step.get('order', 'desc')
            limit = step.get('limit')
            reverse = (order.lower() == 'desc')
            # 记录 rank 前的总数，以便回答"共有多少家"类问题
            pre_rank_total = len(rows)
            variables['_pre_rank_total'] = float(pre_rank_total)
            rows.sort(key=lambda r: (float(r.get(by)) if r.get(by) is not None else float('-inf')), reverse=reverse)
            if limit:
                rows = rows[:int(limit)]
            for i, r in enumerate(rows):
                r['rank'] = i + 1
                r['_total_before_rank'] = pre_rank_total
            col_labels['rank'] = '排名'
            pass  # rank is in-memory, no SQL to display

        else:
            _log.warning('[Pipeline] 未知操作: %s', op)

    # 构建列定义（按 rank, stock_code, stock_abbr, 其余顺序）
    columns: list[dict[str, str]] = []
    seen: set[str] = set()
    priority_keys = ['rank', 'stock_code', 'stock_abbr']
    for k in priority_keys:
        if rows and k in rows[0]:
            columns.append({'key': k, 'label': col_labels.get(k, k)})
            seen.add(k)
    if rows:
        for k in rows[0]:
            if k not in seen:
                columns.append({'key': k, 'label': col_labels.get(k, k)})
                seen.add(k)

    # 变量信息附加到 SQL 注释
    var_notes = '\n'.join(f'-- Variable: {k} = {v}' for k, v in variables.items())
    display_sql = '\n'.join(sql_parts) + ('\n' + var_notes if var_notes else '')

    chart_title = plan.get('title', '查询结果')

    return rows, display_sql, list(set(source_tables)), columns, chart_title
