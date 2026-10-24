from __future__ import annotations

import difflib
import json
import re
from typing import Any

from fastapi import HTTPException
from config import CONFIG

from ..core.context import COMPANY_NAMES, PERIOD_SORT_ORDER, TABLE_LABELS, COMPANY_ALIAS, resolve_company_alias, resolve_all_companies_in_text
from ..core.db import create_id, open_connection, query_rows
from .llm import llm_available, llm_temperature, request_llm_json, request_llm_text, request_llm_text_stream, intent_llm_available
from .visualization import auto_generate_chart_payload
from .dialogue_state import (
    DialogueState, SlotSource, is_follow_up, detect_topic_switch,
    merge_slot, build_dynamic_attribution, apply_slot_ttl,
    get_cached_intent, set_cached_intent,
    should_summarize_history, build_summary_prompt,
)

RENDER_MODE_CHAT = 'chat'
RENDER_MODE_BATCH = 'batch_export'


def normalize_batch_export_text(text: str) -> str:
    """Strip markdown formatting and collapse whitespace for batch export output."""
    text = text.replace('{{CHART}}', '').replace('{{TABLE}}', '')
    text = re.sub(r'\{\{TASK_\d+\}\}', '', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\[参考\d+\]', '', text)
    text = re.sub(r'^\s*\d+\.\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


try:
    from .knowledge import search_kb as _search_kb
    from .knowledge import adaptive_retrieval_judge, self_rag_filter, multi_step_retrieve
    _KB_AVAILABLE = True
except Exception:
    _KB_AVAILABLE = False
    def _search_kb(query: str, top_k: int = 3) -> list:  # type: ignore[misc]
        return []

METRIC_DEFINITIONS: dict[str, dict[str, str]] = {}
METRIC_ALIASES: dict[str, str] = {}
TREND_PATTERN = r'(趋势|近几|变化|走势|波动|逐年|逐季|历年|每年|每季|近[三四五六七八九十\d]+年|近[三四五六七八九十\d]+[个]?季|同比.*变|环比.*变|增长.*情况|增速|下降.*情况|增减|涨跌|对比.*变|年度.*对比|季度.*对比)'
RANKING_PATTERN = r'(top\s*\d+|TOP\s*\d+|最高|最低|排名|排行|排序|最大|最小|最多|最少|最好|最差|最强|最弱|前\d+名?|后\d+名?|从高到低|从低到高|所有.*?公司|哪家.*?最|谁.*?最|哪个.*?最|排第[一二三1-9]|第[一二三1-9]名|龙头|垫底|倒数|领先)'
_SMALL_TALK_PATTERN = re.compile(
    r'(你是谁|介绍一下你|介绍下你|自我介绍|你能做什么|你会什么|怎么用|如何使用|如何提问|怎么提问|怎么问|帮助|hi|hello|你好|在吗|谢谢|再见)',
    re.IGNORECASE
)
_SELF_INTRO_PATTERN = re.compile(
    r'(你是谁|介绍一下你|介绍下你|自我介绍|介绍你自己|说说你自己|你能做什么|你会什么)',
    re.IGNORECASE
)
_KB_HINT_COMPANY_PART = '|'.join(re.escape(name) for name in COMPANY_NAMES) if COMPANY_NAMES else ''
_GENERAL_KB_HINT_PATTERN = re.compile(
    r'(研报|行业|个股|公司|企业|药品|医保|创新药|医药|市场|竞争格局|业务|产品|政策|景气度|基本面|催化|风险|趋势|波动|价格|预测|判断|依据|展望|前景|分析|影响|策略|建议'
    + (f'|{_KB_HINT_COMPANY_PART}' if _KB_HINT_COMPANY_PART else '')
    + r'|收入|营收|利润|净利|毛利|成本|费用|负债|资产|现金流|盈利|亏损|财务|报表|年报|季报)',
    re.IGNORECASE
)

_KB_CITE_PATTERN = re.compile(r'\[参考(\d+)\]')
_KB_SOURCE_TAIL_PATTERN = re.compile(r'(以上信息|上述内容|来源于|数据来源|参考资料|参考文献|资料来源|研报来源|可参考)')
_KB_PLACEHOLDER_PATTERN = re.compile(r'^\{\{(?:CHART|TABLE|TASK_\d+)\}\}$')
_KB_MARKDOWN_HEADING_PATTERN = re.compile(r'^\*\*[^*\n]+\*\*$')


def _normalize_kb_text(text: str) -> str:
    return re.sub(r'\s+', '', str(text or ''))


def _strip_kb_source_tail(content: str) -> str:
    if not content:
        return ''
    blocks = re.split(r'\n{2,}', content)
    cleaned: list[str] = []
    for block in blocks:
        cite_count = len(_KB_CITE_PATTERN.findall(block))
        normalized = _normalize_kb_text(block)
        if cite_count >= 4 and _KB_SOURCE_TAIL_PATTERN.search(normalized):
            continue
        stripped = block.strip('\n')
        if stripped:
            cleaned.append(stripped)
    return '\n\n'.join(cleaned).strip()


def _remove_invalid_kb_citations(content: str, ref_count: int) -> str:
    if not content:
        return ''

    def _repl(match: re.Match) -> str:
        idx = int(match.group(1))
        return match.group(0) if 1 <= idx <= ref_count else ''

    return _KB_CITE_PATTERN.sub(_repl, content)


def _build_kb_ref_terms(ref: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        normalized = _normalize_kb_text(term)
        if not normalized or normalized.isdigit() or len(normalized) < 2:
            return
        if len(normalized) > 18:
            normalized = normalized[:18]
        if normalized in seen:
            return
        seen.add(normalized)
        terms.append(normalized)

    for source in (
        str(ref.get('highlight', '') or ''),
        str(ref.get('text', '') or ''),
        str(ref.get('parent_text', '') or ''),
        str(ref.get('doc_name', '') or ''),
    ):
        for seq in re.findall(r'[\u4e00-\u9fff]{2,}', source):
            if len(seq) <= 12:
                _add(seq)
            else:
                _add(seq[:8])
                _add(seq[-8:])
        for seq in re.findall(r'[A-Za-z][A-Za-z0-9\-]{1,17}', source):
            _add(seq)
    return terms[:12]


def _score_kb_unit(text: str, ref_terms: list[str], ref_text: str) -> float:
    score = 0.0
    for term in ref_terms:
        if term and term in text:
            score += min(len(term), 8)
    if score > 0:
        return score
    if len(text) < 8 or not ref_text:
        return 0.0
    return difflib.SequenceMatcher(None, text[:160], ref_text[:160]).ratio() * 4.0


def _append_kb_citation(text: str, ref_index: int) -> str:
    stripped = text.rstrip()
    trailing = text[len(stripped):]
    if not stripped or _KB_CITE_PATTERN.search(stripped):
        return text
    return f'{stripped}[参考{ref_index}]{trailing}'


def _post_process_kb_answer(content: str, refs: list[dict[str, Any]]) -> str:
    cleaned = _strip_kb_source_tail(content)
    if not refs:
        return cleaned
    cleaned = _remove_invalid_kb_citations(cleaned, len(refs))
    existing = {int(m.group(1)) for m in _KB_CITE_PATTERN.finditer(cleaned)}
    if existing:
        return cleaned

    ref_infos = {
        idx: {
            'terms': _build_kb_ref_terms(ref),
            'text': _normalize_kb_text(f"{ref.get('text', '') or ''}{ref.get('parent_text', '') or ''}"),
        }
        for idx, ref in enumerate(refs, 1)
    }
    lines = cleaned.split('\n')
    parts_by_line: list[list[str] | None] = []
    eligible: list[tuple[int, int, str]] = []
    for line_idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or _KB_PLACEHOLDER_PATTERN.fullmatch(stripped) or _KB_MARKDOWN_HEADING_PATTERN.fullmatch(stripped):
            parts_by_line.append(None)
            continue
        parts = [part for part in re.split(r'(?<=[。！？；])', line) if part != '']
        parts_by_line.append(parts)
        for part_idx, part in enumerate(parts):
            unit = part.strip()
            normalized = _normalize_kb_text(unit)
            if not unit or _KB_CITE_PATTERN.search(unit) or len(normalized) < 8:
                continue
            eligible.append((line_idx, part_idx, normalized))

    max_injections = min(3, len(refs), len(eligible))
    if max_injections <= 0:
        return cleaned

    candidates: list[tuple[float, int, int, int]] = []
    for ref_idx, ref_info in ref_infos.items():
        for line_idx, part_idx, normalized in eligible:
            score = _score_kb_unit(normalized, ref_info['terms'], ref_info['text'])
            if score >= 3.0:
                candidates.append((score, line_idx, part_idx, ref_idx))

    assignments: dict[tuple[int, int], int] = {}
    used_units: set[tuple[int, int]] = set()
    used_refs: set[int] = set()
    for _score, line_idx, part_idx, ref_idx in sorted(candidates, key=lambda item: item[0], reverse=True):
        unit_key = (line_idx, part_idx)
        if unit_key in used_units or ref_idx in used_refs:
            continue
        assignments[unit_key] = ref_idx
        used_units.add(unit_key)
        used_refs.add(ref_idx)
        if len(assignments) >= max_injections:
            break

    if not assignments and eligible:
        first_line, first_part, _ = eligible[0]
        assignments[(first_line, first_part)] = 1

    for (line_idx, part_idx), ref_idx in assignments.items():
        parts = parts_by_line[line_idx]
        if parts is None:
            continue
        parts[part_idx] = _append_kb_citation(parts[part_idx], ref_idx)

    rebuilt: list[str] = []
    for line_idx, line in enumerate(lines):
        parts = parts_by_line[line_idx]
        rebuilt.append(''.join(parts) if parts is not None else line)
    return '\n'.join(rebuilt).strip()

TABLE_METRIC_COLUMNS: dict[str, dict[str, str]] = {
    'income_sheet': {
        'total_profit': '利润总额（万元）',
        'net_profit': '净利润（万元）',
        'total_operating_revenue': '营业总收入（万元）',
        'operating_profit': '营业利润（万元）',
        'operating_revenue_yoy_growth': '营业总收入同比增长率（%）',
        'net_profit_yoy_growth': '净利润同比增长率（%）',
        'other_income': '其他收益（万元）',
        'operating_expense_cost_of_sales': '营业成本（万元）',
        'operating_expense_selling_expenses': '销售费用（万元）',
        'operating_expense_administrative_expenses': '管理费用（万元）',
        'operating_expense_financial_expenses': '财务费用（万元）',
        'operating_expense_rnd_expenses': '研发费用（万元）',
        'operating_expense_taxes_and_surcharges': '税金及附加（万元）',
        'total_operating_expenses': '营业总支出（万元）',
        'asset_impairment_loss': '资产减值损失（万元）',
        'credit_impairment_loss': '信用减值损失（万元）',
    },
    'cash_flow_sheet': {
        'net_cash_flow': '净现金流（万元）',
        'net_cash_flow_yoy_growth': '净现金流同比增长率（%）',
        'operating_cf_net_amount': '经营活动现金流净额（万元）',
        'operating_cf_ratio_of_net_cf': '经营现金流净现金流占比（%）',
        'operating_cf_cash_from_sales': '销售商品收到的现金（万元）',
        'investing_cf_net_amount': '投资活动现金流净额（万元）',
        'investing_cf_ratio_of_net_cf': '投资现金流净现金流占比（%）',
        'investing_cf_cash_for_investments': '投资支付的现金（万元）',
        'investing_cf_cash_from_investment_recovery': '收回投资收到的现金（万元）',
        'financing_cf_net_amount': '融资活动现金流净额（万元）',
        'financing_cf_ratio_of_net_cf': '融资现金流净现金流占比（%）',
        'financing_cf_cash_from_borrowing': '取得借款收到的现金（万元）',
        'financing_cf_cash_for_debt_repayment': '偿还债务支付的现金（万元）',
    },
    'balance_sheet': {
        'asset_cash_and_cash_equivalents': '货币资金（万元）',
        'asset_accounts_receivable': '应收账款（万元）',
        'asset_inventory': '存货（万元）',
        'asset_trading_financial_assets': '交易性金融资产（万元）',
        'asset_construction_in_progress': '在建工程（万元）',
        'asset_total_assets': '总资产（万元）',
        'asset_total_assets_yoy_growth': '总资产同比增长率（%）',
        'liability_accounts_payable': '应付账款（万元）',
        'liability_advance_from_customers': '预收账款（万元）',
        'liability_contract_liabilities': '合同负债（万元）',
        'liability_short_term_loans': '短期借款（万元）',
        'liability_total_liabilities': '总负债（万元）',
        'liability_total_liabilities_yoy_growth': '总负债同比增长率（%）',
        'asset_liability_ratio': '资产负债率（%）',
        'equity_unappropriated_profit': '未分配利润（万元）',
        'equity_total_equity': '股东权益合计（万元）',
    },
    'core_performance_indicators_sheet': {
        'eps': '每股收益（元）',
        'net_profit_10k_yuan': '归母净利润（万元）',
        'net_profit_yoy_growth': '净利润同比增长率（%）',
        'net_profit_qoq_growth': '净利润环比增长率（%）',
        'net_asset_per_share': '每股净资产（元）',
        'roe': '净资产收益率（%）',
        'roe_weighted_excl_non_recurring': '加权扣非净资产收益率（%）',
        'operating_cf_per_share': '每股经营现金流（元）',
        'net_profit_excl_non_recurring': '扣非净利润（万元）',
        'net_profit_excl_non_recurring_yoy': '扣非净利润同比增长率（%）',
        'gross_profit_margin': '毛利率（%）',
        'net_profit_margin': '净利率（%）',
        'operating_revenue_qoq_growth': '营收环比增长率（%）',
        'total_operating_revenue': '营业总收入（万元）',
        'operating_revenue_yoy_growth': '营收同比增长率（%）',
    },
}
ALLOWED_TABLES: set[str] = set(TABLE_METRIC_COLUMNS.keys())
for _tbl_cols in TABLE_METRIC_COLUMNS.values():
    for _col, _lbl in _tbl_cols.items():
        if _col not in METRIC_DEFINITIONS:
            METRIC_DEFINITIONS[_col] = {
                'type': 'metric', 'column': _col,
                'label': re.sub(r'[（(【\[].*', '', _lbl).strip(),
                'intent': _col
            }
for _col, _defn in list(METRIC_DEFINITIONS.items()):
    METRIC_ALIASES.setdefault(_col, _col)
    METRIC_ALIASES.setdefault(_defn['label'], _col)
METRIC_ALIASES.update({
    '营收': 'total_operating_revenue', '研发': 'operating_expense_rnd_expenses',
    '收益率': 'roe', '回报率': 'roe', '净资产回报率': 'roe', 'ROE': 'roe',
    '收入增长': 'operating_revenue_yoy_growth', '营收增长': 'operating_revenue_yoy_growth',
    '营收增速': 'operating_revenue_yoy_growth', '收入增速': 'operating_revenue_yoy_growth',
    '利润增长': 'net_profit_yoy_growth', '利润增速': 'net_profit_yoy_growth',
    '净利润增长': 'net_profit_yoy_growth', '净利润增速': 'net_profit_yoy_growth',
    '扣非': 'net_profit_excl_non_recurring', '扣非净利润': 'net_profit_excl_non_recurring',
    '扣非净利': 'net_profit_excl_non_recurring', '扣非利润': 'net_profit_excl_non_recurring',
    '销售毛利率': 'gross_profit_margin', '销售净利率': 'net_profit_margin',
    '利润率': 'net_profit_margin', '净利率': 'net_profit_margin',
    '负债率': 'asset_liability_ratio', '资产负债率': 'asset_liability_ratio',
    '总资产': 'asset_total_assets', '资产规模': 'asset_total_assets',
    '总负债': 'liability_total_liabilities', '负债总额': 'liability_total_liabilities',
    '存货': 'asset_inventory', '应收账款': 'asset_accounts_receivable',
    '研发费用': 'operating_expense_rnd_expenses', '研发投入': 'operating_expense_rnd_expenses',
    '销售费用': 'operating_expense_selling_expenses', '管理费用': 'operating_expense_administrative_expenses',
    '财务费用': 'operating_expense_financial_expenses',
    '现金流': 'operating_cf_net_amount',
    '经营现金流': 'operating_cf_net_amount', '经营性现金流': 'operating_cf_net_amount',
    '经营性现金流量净额': 'operating_cf_net_amount', '现金流量净额': 'operating_cf_net_amount',
    '经营活动现金流': 'operating_cf_net_amount',
    '投资现金流': 'investing_cf_net_amount', '投资性现金流': 'investing_cf_net_amount',
    '筹资现金流': 'financing_cf_net_amount', '筹资性现金流': 'financing_cf_net_amount',
    '销售额': 'total_operating_revenue', '营业收入': 'total_operating_revenue',
    '收入': 'total_operating_revenue', '营业总收入': 'total_operating_revenue',
    '利润同比': 'net_profit_yoy_growth', '净利润同比': 'net_profit_yoy_growth',
    '利润年同比': 'net_profit_yoy_growth', '利润同比增长': 'net_profit_yoy_growth',
    '营收同比': 'operating_revenue_yoy_growth', '收入同比': 'operating_revenue_yoy_growth',
    '销售额同比': 'operating_revenue_yoy_growth', '销售额年同比': 'operating_revenue_yoy_growth',
    '营收年同比': 'operating_revenue_yoy_growth', '收入年同比': 'operating_revenue_yoy_growth',
    '营业总收入同比增长率': 'operating_revenue_yoy_growth', '营收同比增长率': 'operating_revenue_yoy_growth',
    '营业收入同比增长率': 'operating_revenue_yoy_growth', '收入同比增长率': 'operating_revenue_yoy_growth',
    '总资产同比增长率': 'asset_total_assets_yoy_growth', '总负债同比增长率': 'liability_total_liabilities_yoy_growth',
    '净利润同比增长率': 'net_profit_yoy_growth', '利润同比增长率': 'net_profit_yoy_growth',
    '毛利率': 'gross_profit_margin', '净利率': 'net_profit_margin',
    '每股收益': 'eps', '每股净资产': 'net_asset_per_share',
    '短期借款': 'liability_short_term_loans', '货币资金': 'asset_cash_and_cash_equivalents',
    '应付账款': 'liability_accounts_payable', '合同负债': 'liability_contract_liabilities',
    '交易性金融资产': 'asset_trading_financial_assets', '预付款项': 'asset_prepayments',
})

CALC_TRACE_DEFINITIONS: dict[str, dict[str, Any]] = {
    # ── 比率类（所有报告期通用） ─────────────────────────────────────
    'asset_liability_ratio': {
        'trace_type': 'ratio',
        'formula': '负债合计 ÷ 资产总计 × 100',
        'operands': [
            {'label': '负债合计', 'fields': ['liability_total_liabilities']},
            {'label': '资产总计', 'fields': ['asset_total_assets']},
        ],
    },
    'gross_profit_margin': {
        'trace_type': 'ratio',
        'formula': '(营业总收入 − 营业成本) ÷ 营业总收入 × 100',
        'operands': [
            {'label': '营业总收入', 'fields': ['total_operating_revenue']},
            {'label': '营业成本', 'fields': ['operating_expense_cost_of_sales']},
        ],
    },
    'net_profit_margin': {
        'trace_type': 'ratio',
        'formula': '净利润 ÷ 营业总收入 × 100',
        'operands': [
            {'label': '净利润', 'fields': ['net_profit_10k_yuan', 'net_profit']},
            {'label': '营业总收入', 'fields': ['total_operating_revenue']},
        ],
    },
    'operating_cf_ratio_of_net_cf': {
        'trace_type': 'ratio',
        'formula': '经营活动现金流净额 ÷ 现金净增加额 × 100',
        'operands': [
            {'label': '经营活动现金流净额', 'fields': ['operating_cf_net_amount']},
            {'label': '现金净增加额', 'fields': ['net_cash_flow']},
        ],
    },
    'investing_cf_ratio_of_net_cf': {
        'trace_type': 'ratio',
        'formula': '投资活动现金流净额 ÷ 现金净增加额 × 100',
        'operands': [
            {'label': '投资活动现金流净额', 'fields': ['investing_cf_net_amount']},
            {'label': '现金净增加额', 'fields': ['net_cash_flow']},
        ],
    },
    'financing_cf_ratio_of_net_cf': {
        'trace_type': 'ratio',
        'formula': '筹资活动现金流净额 ÷ 现金净增加额 × 100',
        'operands': [
            {'label': '筹资活动现金流净额', 'fields': ['financing_cf_net_amount']},
            {'label': '现金净增加额', 'fields': ['net_cash_flow']},
        ],
    },
    # ── 同比增长类 ──────────────────────────────────────────────────
    'asset_total_assets_yoy_growth': {
        'trace_type': 'growth',
        'formula': '(总资产 当期 − 上年同期) ÷ |上年同期| × 100',
        'operands': [
            {'label': '本期总资产', 'fields': ['asset_total_assets'], 'period_shift': 0},
            {'label': '上年同期总资产', 'fields': ['asset_total_assets'], 'period_shift': -1},
        ],
    },
    'liability_total_liabilities_yoy_growth': {
        'trace_type': 'growth',
        'formula': '(负债合计 当期 − 上年同期) ÷ |上年同期| × 100',
        'operands': [
            {'label': '本期负债合计', 'fields': ['liability_total_liabilities'], 'period_shift': 0},
            {'label': '上年同期负债合计', 'fields': ['liability_total_liabilities'], 'period_shift': -1},
        ],
    },
    'net_cash_flow_yoy_growth': {
        'trace_type': 'growth',
        'formula': '(现金净增加额 当期 − 上年同期) ÷ |上年同期| × 100',
        'operands': [
            {'label': '本期现金净增加额', 'fields': ['net_cash_flow'], 'period_shift': 0},
            {'label': '上年同期现金净增加额', 'fields': ['net_cash_flow'], 'period_shift': -1},
        ],
    },
    # ── 每股指标（总股本不在数据库中，仅引用分子） ───────────────────
    'net_asset_per_share': {
        'trace_type': 'derived',
        'formula': '归属净资产 ÷ 总股本',
        'operands': [
            {'label': '股东权益合计', 'fields': ['equity_total_equity']},
        ],
    },
    'operating_cf_per_share': {
        'trace_type': 'derived',
        'formula': '经营活动现金流净额 ÷ 总股本',
        'operands': [
            {'label': '经营活动现金流净额', 'fields': ['operating_cf_net_amount']},
        ],
    },
}

# ── Q3 累计→单季度换算字段（公式：本字段Q3前三季度累计 − 本字段半年度） ──
# 这些字段仅在 Q3 报告期被标记为 calculated，操作数为同字段的半年度值
_Q3_CONVERT_FIELDS: dict[str, str] = {
    'total_profit':                              '利润总额',
    'operating_profit':                          '营业利润',
    'other_income':                              '其他收益',
    'total_operating_expenses':                  '营业总成本',
    'operating_expense_cost_of_sales':           '营业成本',
    'operating_expense_selling_expenses':        '销售费用',
    'operating_expense_administrative_expenses': '管理费用',
    'operating_expense_financial_expenses':      '财务费用',
    'operating_expense_rnd_expenses':            '研发费用',
    'operating_expense_taxes_and_surcharges':    '税金及附加',
    'asset_impairment_loss':                     '资产减值损失',
    'credit_impairment_loss':                    '信用减值损失',
    'net_cash_flow':                             '现金净增加额',
    'operating_cf_net_amount':                   '经营活动现金流净额',
    'operating_cf_cash_from_sales':              '销售商品收到的现金',
    'investing_cf_net_amount':                   '投资活动现金流净额',
    'investing_cf_cash_for_investments':         '投资支付的现金',
    'investing_cf_cash_from_investment_recovery': '收回投资收到的现金',
    'financing_cf_net_amount':                   '筹资活动现金流净额',
    'financing_cf_cash_from_borrowing':          '取得借款收到的现金',
    'financing_cf_cash_for_debt_repayment':      '偿还债务支付的现金',
}
for _field, _label in _Q3_CONVERT_FIELDS.items():
    CALC_TRACE_DEFINITIONS.setdefault(_field, {
        'trace_type': 'q3_convert',
        'formula': f'{_label}(Q3前三季度累计) − {_label}(半年度)',
        'operands': [
            {'label': f'{_label}(Q3前三季度累计)', 'fields': [_field], 'period_ref': 'self_cumulative'},
            {'label': f'{_label}(半年度)', 'fields': [_field], 'period_ref': 'same_year_hy'},
        ],
    })
_CALC_FORMULA_PATTERN = re.compile(r'(÷|×|−|\(|\)|当期|上期|本期|同比|环比)')

# ── 前三季度（Q3C）可累加字段：流量类指标可以 HY + Q3 得到前三季度累计 ──
_Q3C_SUMMABLE_FIELDS: set[str] = {
    # income_sheet
    'total_profit', 'net_profit', 'total_operating_revenue', 'operating_profit',
    'other_income', 'total_operating_expenses',
    'operating_expense_cost_of_sales', 'operating_expense_selling_expenses',
    'operating_expense_administrative_expenses', 'operating_expense_financial_expenses',
    'operating_expense_rnd_expenses', 'operating_expense_taxes_and_surcharges',
    'asset_impairment_loss', 'credit_impairment_loss',
    # cash_flow_sheet
    'net_cash_flow', 'operating_cf_net_amount', 'operating_cf_cash_from_sales',
    'investing_cf_net_amount', 'investing_cf_cash_for_investments',
    'investing_cf_cash_from_investment_recovery',
    'financing_cf_net_amount', 'financing_cf_cash_from_borrowing',
    'financing_cf_cash_for_debt_repayment',
    # core_performance_indicators_sheet
    'eps', 'net_profit_10k_yuan', 'net_profit_excl_non_recurring',
    'operating_cf_per_share',
}


def _metric_label(field_name: str, include_unit: bool = False) -> str:
    for cols in TABLE_METRIC_COLUMNS.values():
        label = cols.get(field_name)
        if not label:
            continue
        if include_unit:
            return label
        return re.sub(r'[（(【\[].*', '', label).strip()
    return field_name


def _metric_unit(field_name: str) -> str:
    label = _metric_label(field_name, include_unit=True)
    matched = re.search(r'[（(]([^（）()]+)[）)]', label)
    return matched.group(1).strip() if matched else ''


def _format_metric_value(value: Any, field_name: str) -> str:
    if value is None or value == '':
        return '—'
    try:
        number = float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return str(value)
    unit = _metric_unit(field_name)
    if unit == '%':
        return f'{number:.2f}%'
    if unit:
        return f'{number:,.2f}{unit}'
    return f'{number:,.2f}'


def _extract_calc_formula_text(record: dict[str, Any]) -> str:
    for candidate in (str(record.get('source_text', '') or ''), str(record.get('source_table_name', '') or '')):
        normalized = re.sub(r'\s+', ' ', candidate).strip()
        if normalized and _CALC_FORMULA_PATTERN.search(normalized):
            return normalized
    return ''


def _previous_same_period(period: str) -> str:
    normalized = normalize_period_code(period)
    matched = re.match(r'^(20\d{2})(Q1|HY|Q3|FY)$', normalized)
    if not matched:
        return ''
    year, suffix = matched.groups()
    return f'{int(year) - 1}{suffix}'


def _same_year_hy_period(period: str) -> str:
    """2024Q3 → 2024HY"""
    normalized = normalize_period_code(period)
    matched = re.match(r'^(20\d{2})', normalized)
    return f'{matched.group(1)}HY' if matched else ''


def _fetch_support_provenance_record(stock_code: str, field_name: str, report_period: str) -> dict[str, Any] | None:
    if not stock_code or not field_name or not report_period:
        return None
    sql = (
        'SELECT id, stock_code, report_period, table_name, field_name, '
        'field_value, source_pdf AS source_file, page_no AS source_page, '
        'row_label AS source_table_name, raw_text AS source_text, '
        'confidence, extract_method FROM field_provenance '
        'WHERE stock_code = ? AND field_name = ? AND report_period = ? '
        "ORDER BY CASE WHEN extract_method = 'calculated' THEN 1 ELSE 0 END ASC, confidence DESC, page_no ASC LIMIT 1"
    )
    rows = query_rows(sql, (stock_code, field_name, report_period))
    return rows[0] if rows else None


def _build_calc_operand(
    operand_spec: dict[str, Any],
    trace_period: str,
    stock_code: str,
    provenance_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    period_ref = str(operand_spec.get('period_ref', '') or '')
    is_self_cumulative = (period_ref == 'self_cumulative')
    if period_ref == 'same_year_hy':
        operand_period = _same_year_hy_period(trace_period)
    elif period_ref == 'prior_year_same':
        operand_period = _previous_same_period(trace_period)
    elif is_self_cumulative:
        operand_period = trace_period  # look up Q3 record itself for evidence
    else:
        period_shift = int(operand_spec.get('period_shift', 0) or 0)
        operand_period = trace_period if period_shift == 0 else _previous_same_period(trace_period)
    candidate_fields = [str(field) for field in operand_spec.get('fields', []) if str(field).strip()]
    resolved_record: dict[str, Any] | None = None
    resolved_field = candidate_fields[0] if candidate_fields else ''
    for candidate in candidate_fields:
        rec = provenance_by_key.get((operand_period, candidate))
        if rec is None:
            rec = _fetch_support_provenance_record(stock_code, candidate, operand_period)
            if rec is not None:
                provenance_by_key[(operand_period, candidate)] = rec
        if rec is not None:
            resolved_record = rec
            resolved_field = candidate
            break
    if resolved_record is None:
        return {
            'label': str(operand_spec.get('label', resolved_field) or resolved_field),
            'fieldName': resolved_field or None,
            'value': None,
            'displayValue': '未获取到对应原值',
            'reportPeriod': operand_period or None,
            'evidenceIds': [],
            'evidences': [],
        }
    record_id = resolved_record.get('id')
    display_value = resolved_record.get('field_value')
    # self_cumulative: 当前记录 field_value 是单季度（已被覆写），需还原累计值 = 单季 + 半年度
    if is_self_cumulative:
        hy_period = _same_year_hy_period(trace_period)
        hy_rec = provenance_by_key.get((hy_period, resolved_field))
        if hy_rec is None:
            hy_rec = _fetch_support_provenance_record(stock_code, resolved_field, hy_period)
        if hy_rec is not None:
            try:
                sq_val = float(str(display_value).replace(',', ''))
                hy_val = float(str(hy_rec.get('field_value', 0)).replace(',', ''))
                display_value = str(round(sq_val + hy_val, 4))
            except (TypeError, ValueError):
                pass
    return {
        'label': str(operand_spec.get('label', _metric_label(resolved_field)) or _metric_label(resolved_field)),
        'fieldName': resolved_field,
        'value': display_value,
        'displayValue': _format_metric_value(display_value, resolved_field),
        'reportPeriod': resolved_record.get('report_period') or operand_period or None,
        'evidenceIds': [int(record_id)] if record_id is not None else [],
        'evidences': [resolved_record],
    }


def _build_calc_traces(provenance_records: list[dict[str, Any]], stock_code: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not provenance_records:
        return [], []
    provenance_by_key = {
        (str(record.get('report_period', '') or ''), str(record.get('field_name', '') or '')): record
        for record in provenance_records
    }
    traces: list[dict[str, Any]] = []
    citation_refs: list[dict[str, Any]] = []
    for record in provenance_records:
        if str(record.get('extract_method', '') or '') != 'calculated':
            continue
        field_name = str(record.get('field_name', '') or '')
        trace_period = str(record.get('report_period', '') or '')
        spec = CALC_TRACE_DEFINITIONS.get(field_name, {})
        operands = [
            _build_calc_operand(operand_spec, trace_period, stock_code, provenance_by_key)
            for operand_spec in spec.get('operands', [])
        ]
        formula_text = str(spec.get('formula') or _extract_calc_formula_text(record) or '').strip()
        trace_type = str(spec.get('trace_type') or ('formula' if formula_text else 'source_value'))
        final_value = _format_metric_value(record.get('field_value'), field_name)
        evidence_ids: list[int] = []
        record_id = record.get('id')
        if record_id is not None:
            evidence_ids.append(int(record_id))
        for operand in operands:
            for evidence_id in operand.get('evidenceIds', []):
                evidence_int = int(evidence_id)
                if evidence_int not in evidence_ids:
                    evidence_ids.append(evidence_int)
        if operands and formula_text:
            summary = f'该结果由 {len([operand for operand in operands if operand.get("evidenceIds")])} 个带来源的原始值按公式推导得到。'
        elif formula_text:
            summary = '该结果在溯源表中保留了计算规则，但当前没有补齐可直接下钻的中间操作数。'
        else:
            summary = '该结果在溯源表中被标记为计算值，当前底层仅保留了结果行证据。'
        trace_id = f'calc-{int(record_id or 0)}'
        traces.append({
            'id': trace_id,
            'provenanceId': int(record_id or 0),
            'fieldName': field_name,
            'title': f'{_metric_label(field_name)}计算溯源',
            'traceType': trace_type,
            'finalValue': final_value,
            'formula': formula_text or None,
            'summary': summary,
            'reportPeriod': trace_period or None,
            'evidenceIds': evidence_ids,
            'steps': [
                {
                    'id': f'{trace_id}-step-1',
                    'title': '计算链路',
                    'expression': formula_text or str(record.get('source_table_name', '') or _metric_label(field_name)),
                    'result': final_value,
                    'operands': operands,
                }
            ],
        })
        citation_refs.append({
            'provenanceId': int(record_id or 0),
            'type': 'calculation',
            'calcTraceId': trace_id,
            'label': '计算',
        })
    return traces, citation_refs


def format_period(period: str) -> str:
    matched = re.match(r'^(20\d{2})(Q1|HY|H1|Q3C?|FY)$', period)
    if not matched:
        return period
    year, suffix = matched.groups()
    mapping = {
        'Q1': '第一季度',
        'HY': '半年度',
        'H1': '半年度',
        'Q3': '第三季度',
        'Q3C': '前三季度',
        'FY': '年报'
    }
    return f"{year}年{mapping[suffix]}"


def normalize_period_code(period: str) -> str:
    cleaned = re.sub(r'\s+', '', period).upper()
    return cleaned.replace('H1', 'HY')


def period_sort_key(period: str) -> tuple[int, int, str]:
    normalized = normalize_period_code(period)
    matched = re.match(r'^(20\d{2})(Q1|HY|Q3C?|FY)$', normalized)
    if not matched:
        return (0, 99, normalized)
    year, suffix = matched.groups()
    return (int(year), PERIOD_SORT_ORDER.get(suffix, 99), normalized)


def extract_period(text: str) -> str:
    from datetime import datetime as _dt
    compact = re.sub(r'\s+', '', text)
    direct_q3c = re.search(r'(20\d{2})Q3C', compact, re.IGNORECASE)
    if direct_q3c:
        return f"{direct_q3c.group(1)}Q3C"
    direct = re.search(r'(20\d{2})Q([1-4])', compact, re.IGNORECASE)
    if direct:
        return f"{direct.group(1)}Q{direct.group(2)}"
    direct_half_or_year = re.search(r'(20\d{2})(HY|H1|FY)', compact, re.IGNORECASE)
    if direct_half_or_year:
        return f"{direct_half_or_year.group(1)}{direct_half_or_year.group(2).upper()}"
    year_match = re.search(r'(20\d{2})年', compact)
    year = year_match.group(1) if year_match else ''

    # ── 相对时间词解析（基于数据库最新 FY 年份，而非系统时钟） ──
    if not year:
        try:
            _data_latest = latest_fy_year()
        except Exception:
            _data_latest = _dt.now().year - 1
        if re.search(r'去年|上一?年(?:度)?', compact):
            year = str(_data_latest)
        elif re.search(r'前年', compact):
            year = str(_data_latest - 1)
        elif re.search(r'今年|本年', compact):
            year = str(_data_latest + 1)
        elif re.search(r'最新|最近', compact):
            year = str(_data_latest)

    if year and re.search(r'(一季报|第一季度)', compact):
        return f'{year}Q1'
    if year and re.search(r'(半年报|半年度|中报|H1|HY)', compact, re.IGNORECASE):
        return f'{year}HY'
    if year and re.search(r'前三季度', compact):
        return f'{year}Q3C'
    if year and re.search(r'(三季报|第三季度)', compact):
        return f'{year}Q3'
    if year and re.search(r'(年报|年度|FY)', compact, re.IGNORECASE):
        return f'{year}FY'
    if year:
        return f'{year}FY'
    return ''


def infer_company(question: str, history: list[dict[str, Any]], allow_default: bool = True) -> str:
    # 优先从问题中匹配（支持别名：999→华润三九, 白药→云南白药 等）
    resolved = resolve_company_alias(question)
    if resolved:
        return resolved
    for item in reversed(history):
        content = str(item.get('content', ''))
        resolved = resolve_company_alias(content)
        if resolved:
            return resolved
        meta = item.get('meta')
        if isinstance(meta, dict) and isinstance(meta.get('company'), str) and meta['company'] in COMPANY_NAMES:
            return meta['company']
    return COMPANY_NAMES[0] if allow_default and COMPANY_NAMES else ''


def infer_period(question: str, history: list[dict[str, Any]]) -> str:
    current = extract_period(question)
    if current:
        return normalize_period_code(current)
    for item in reversed(history):
        meta = item.get('meta')
        if isinstance(meta, dict) and isinstance(meta.get('report_period'), str):
            return normalize_period_code(meta['report_period'])
        rows = item.get('rows')
        if isinstance(rows, list) and rows:
            first = rows[0]
            if isinstance(first, dict) and isinstance(first.get('report_period'), str):
                return normalize_period_code(first['report_period'])
        content = str(item.get('content', ''))
        guessed = extract_period(content)
        if guessed:
            return normalize_period_code(guessed)
    return ''


def infer_metric_or_intent(question: str, history: list[dict[str, Any]]) -> dict[str, str]:
    compact = re.sub(r'\s+', '', question)
    if re.search(r'(现金流|经营.*现金|投资.*现金|筹资.*现金|融资.*现金)', compact):
        return {'type': 'cash_flow', 'intent': 'cash_flow'}
    for col, defn in METRIC_DEFINITIONS.items():
        if defn['label'] in compact:
            return dict(defn)
    for alias, col in METRIC_ALIASES.items():
        if alias and col and alias in compact and col in METRIC_DEFINITIONS:
            return dict(METRIC_DEFINITIONS[col])
    for item in reversed(history):
        meta = item.get('meta')
        if not isinstance(meta, dict):
            continue
        if meta.get('intent') == 'cash_flow' or meta.get('query_kind') == 'cash_flow':
            return {'type': 'cash_flow', 'intent': 'cash_flow'}
        metric_col = str(meta.get('metric', '')).strip()
        if metric_col and metric_col in METRIC_DEFINITIONS:
            return dict(METRIC_DEFINITIONS[metric_col])
    return {}


def _is_small_talk_question(question: str) -> bool:
    compact = re.sub(r'\s+', '', question)
    return bool(_SMALL_TALK_PATTERN.search(compact))


def _is_self_intro_question(question: str) -> bool:
    compact = re.sub(r'\s+', '', question)
    return bool(_SELF_INTRO_PATTERN.search(compact))


def _requested_reply_length(question: str) -> int:
    matched = re.search(r'(\d{1,4})\s*个?字', question)
    if matched:
        return min(max(int(matched.group(1)), 20), 300)
    if re.search(r'一句话', question):
        return 30
    if re.search(r'([两二2])句话', question):
        return 60
    if re.search(r'([三3])句话', question):
        return 90
    return 0


def _general_fallback_content(question: str, target_length: int) -> str:
    if _is_self_intro_question(question):
        if target_length >= 120:
            return '我是一个面向上市公司财报数据库的智能问答助手，主要帮你查询利润、营收、现金流、资产负债和常见财务指标，也能结合报告期生成表格、图表和简明分析。你只要告诉我公司名、想看的指标，以及报告期，例如“某公司最近年报净利润是多少”，我就会尽量基于数据库结果准确回答。'
        if target_length >= 95:
            return '我是一个面向上市公司财报数据的智能问答助手，擅长查询利润、营收、现金流、资产负债和常见财务指标，也能结合报告期做对比分析、输出表格图表和简明结论。只要你告诉我公司名、指标名与报告期，我就会尽量准确回答。'
        if target_length >= 70:
            return '我是面向上市公司财报数据库的智能问答助手，能帮你查询利润、营收、现金流、资产负债和常见财务指标，也能按报告期做对比分析并生成表格与图表。你给出公司名、指标和报告期，我就尽量准确作答。'
        return '我是财务报表智能问答助手，能帮你查利润、营收、现金流和资产负债，也能按报告期做对比分析。告诉我公司、指标和时间，我会尽量准确回答。'
    return '我是一个面向财务报表数据库的智能问答助手。你可以告诉我公司名、指标名和报告期，我会帮你查询并分析利润、营收、现金流等数据。'


def _should_use_kb_for_question(question: str, query_kind: str) -> bool:
    if query_kind in {'metric', 'cash_flow', 'ranking'}:
        return True
    compact = re.sub(r'\s+', '', question)
    if _is_small_talk_question(compact):
        return False
    if resolve_company_alias(compact):
        return True
    return bool(_GENERAL_KB_HINT_PATTERN.search(compact))


def _fetch_db_context_for_analysis(question: str, history: list[dict[str, Any]]) -> str:
    """Query DB for relevant financial data to enrich analytical LLM prompts."""
    company = infer_company(question, history, allow_default=False)
    if not company:
        return ''
    compact = re.sub(r'\s+', '', question)
    if re.search(r'(现金流|经营.*现金|投资.*现金|筹资.*现金)', compact):
        table = 'cash_flow_sheet'
        cols = list(TABLE_METRIC_COLUMNS.get(table, {}).keys())[:6]
    elif re.search(r'(资产|负债|所有者权益|净资产)', compact):
        table = 'balance_sheet'
        cols = list(TABLE_METRIC_COLUMNS.get(table, {}).keys())[:6]
    else:
        table = 'income_sheet'
        cols = []
        if re.search(r'(收入|营收|营业)', compact):
            cols.extend(['total_operating_revenue', 'operating_revenue_yoy_growth'])
        if re.search(r'(利润|盈利|亏损)', compact):
            cols.extend(['total_profit', 'net_profit', 'net_profit_yoy_growth'])
        if re.search(r'(成本|费用|支出)', compact):
            cols.extend(['operating_expense_cost_of_sales', 'operating_expense_selling_expenses',
                         'operating_expense_administrative_expenses', 'operating_expense_rnd_expenses'])
        if not cols:
            cols = ['total_operating_revenue', 'net_profit', 'total_profit', 'operating_revenue_yoy_growth']
        valid = TABLE_METRIC_COLUMNS.get(table, {})
        cols = [c for c in cols if c in valid]
    if not cols:
        return ''
    select_clause = 'report_period, ' + ', '.join(cols)
    try:
        rows = query_rows(
            f'SELECT {select_clause} FROM {table} WHERE TRIM(stock_abbr) = ? AND report_period IS NOT NULL ORDER BY report_period DESC LIMIT 8',
            (company,)
        )
    except Exception:
        return ''
    if not rows:
        return ''
    col_labels = TABLE_METRIC_COLUMNS.get(table, {})
    header = f'{company} 近期{TABLE_LABELS.get(table, table)}数据：'
    lines = [header]
    for row in reversed(rows):
        period = row.get('report_period', '')
        vals = '  '.join(f'{col_labels.get(c, c).split("（")[0]}: {row.get(c, "N/A")}' for c in cols)
        lines.append(f'  {period}  {vals}')
    return '\n'.join(lines)


def available_periods(table_name: str, company: str) -> list[str]:
    with open_connection() as connection:
        rows = connection.execute(
            f"SELECT DISTINCT report_period FROM {table_name} WHERE TRIM(stock_abbr) = ? AND report_period IS NOT NULL",
            (company,)
        ).fetchall()
    periods = sorted([normalize_period_code(row['report_period']) for row in rows], key=period_sort_key)
    return periods


def resolve_period(table_name: str, company: str, requested: str) -> str:
    periods = available_periods(table_name, company)
    normalized = normalize_period_code(requested)
    if normalized.endswith('Q3C'):
        _year = normalized[:4]
        if f'{_year}Q3' in periods and f'{_year}HY' in periods:
            return normalized
        return ''
    return normalized if normalized in periods else ''


def _y_axis_formatter(values: list[float]) -> dict[str, Any]:
    """根据数值量级自动生成 Y 轴格式配置"""
    max_v = max((abs(v) for v in values if v is not None), default=0)
    if max_v >= 1e8:
        return {'axisLabel': {'formatter': '{value}'}, 'name': '（亿元）', 'nameTextStyle': {'fontSize': 11, 'color': '#9ca3af'}}
    if max_v >= 1e4:
        return {'axisLabel': {'formatter': '{value}'}, 'name': '（万元）', 'nameTextStyle': {'fontSize': 11, 'color': '#9ca3af'}}
    return {}


def build_line_chart(title: str, labels: list[str], values: list[float]) -> dict[str, Any]:
    avg = round(sum(values) / len(values), 2) if values else 0
    use_zoom = len(labels) > 8
    option: dict[str, Any] = {
        'title': {'text': title, 'textStyle': {'fontSize': 14, 'fontWeight': 600, 'color': '#1f2937'}, 'left': 16, 'top': 8},
        'tooltip': {
            'trigger': 'axis',
            'formatter': '{b}<br/>{a}: <b>{c}</b>',
        },
        'grid': {'left': 16, 'right': 24, 'top': 52, 'bottom': 40 if not use_zoom else 68, 'containLabel': True},
        'xAxis': {'type': 'category', 'data': labels, 'axisLabel': {'rotate': len(labels) > 6 and 30 or 0}},
        'yAxis': dict({'type': 'value'}, **_y_axis_formatter(values)),
        'series': [{
            'name': title,
            'type': 'line',
            'smooth': True,
            'data': values,
            'symbolSize': 8,
            'lineStyle': {'width': 2.5, 'color': '#6366f1'},
            'itemStyle': {'color': '#6366f1', 'borderWidth': 2, 'borderColor': '#fff'},
            'areaStyle': {'color': {'type': 'linear', 'x': 0, 'y': 0, 'x2': 0, 'y2': 1,
                                    'colorStops': [{'offset': 0, 'color': 'rgba(99,102,241,0.18)'},
                                                   {'offset': 1, 'color': 'rgba(99,102,241,0.02)'}]}},
            'markPoint': {
                'data': [
                    {'type': 'max', 'name': '最高', 'label': {'formatter': '最高\n{c}', 'fontSize': 10}},
                    {'type': 'min', 'name': '最低', 'label': {'formatter': '最低\n{c}', 'fontSize': 10}},
                ],
                'symbolSize': 44,
                'itemStyle': {'color': '#6366f1'},
            },
            'markLine': {
                'silent': True,
                'data': [{'type': 'average', 'name': f'均值 {avg}'}],
                'lineStyle': {'color': '#f59e0b', 'type': 'dashed', 'width': 1.5},
                'label': {'formatter': '均值 {c}', 'fontSize': 10, 'color': '#f59e0b'},
            },
        }],
    }
    if use_zoom:
        option['dataZoom'] = [
            {'type': 'inside', 'start': max(0, 100 - int(8 / len(labels) * 100)), 'end': 100},
            {'type': 'slider', 'start': max(0, 100 - int(8 / len(labels) * 100)), 'end': 100, 'height': 20, 'bottom': 8},
        ]
    return {'title': title, 'typeLabel': '折线图', 'option': option}


_MULTI_LINE_COLORS = ['#6366f1', '#ef4444', '#10b981', '#f59e0b', '#3b82f6', '#ec4899', '#8b5cf6', '#14b8a6']


def build_multi_line_chart(title: str, labels: list[str], series_map: dict[str, list[float]]) -> dict[str, Any]:
    """Build a multi-series line chart. series_map: {series_name: [values]}"""
    use_zoom = len(labels) > 8
    series_list = []
    for i, (name, vals) in enumerate(series_map.items()):
        color = _MULTI_LINE_COLORS[i % len(_MULTI_LINE_COLORS)]
        series_list.append({
            'name': name, 'type': 'line', 'smooth': True, 'data': vals,
            'symbolSize': 8, 'lineStyle': {'width': 2.5, 'color': color},
            'itemStyle': {'color': color, 'borderWidth': 2, 'borderColor': '#fff'},
        })
    option: dict[str, Any] = {
        'title': {'text': title, 'textStyle': {'fontSize': 14, 'fontWeight': 600, 'color': '#1f2937'}, 'left': 16, 'top': 8},
        'tooltip': {'trigger': 'axis'},
        'legend': {'data': list(series_map.keys()), 'top': 32},
        'grid': {'left': 16, 'right': 24, 'top': 72, 'bottom': 40 if not use_zoom else 68, 'containLabel': True},
        'xAxis': {'type': 'category', 'data': labels, 'axisLabel': {'rotate': len(labels) > 6 and 30 or 0}},
        'yAxis': {'type': 'value'},
        'series': series_list,
    }
    if use_zoom:
        option['dataZoom'] = [
            {'type': 'inside', 'start': max(0, 100 - int(8 / len(labels) * 100)), 'end': 100},
            {'type': 'slider', 'start': max(0, 100 - int(8 / len(labels) * 100)), 'end': 100, 'height': 20, 'bottom': 8},
        ]
    return {'title': title, 'typeLabel': '折线图', 'option': option}


def build_bar_chart(title: str, labels: list[str], values: list[float], color: str = '#6366f1') -> dict[str, Any]:
    avg = round(sum(values) / len(values), 2) if values else 0
    gradient_color = {
        'type': 'linear', 'x': 0, 'y': 0, 'x2': 0, 'y2': 1,
        'colorStops': [
            {'offset': 0, 'color': color},
            {'offset': 1, 'color': color + '99'},
        ]
    }
    return {
        'title': title,
        'typeLabel': '柱状图',
        'option': {
            'title': {'text': title, 'textStyle': {'fontSize': 14, 'fontWeight': 600, 'color': '#1f2937'}, 'left': 16, 'top': 8},
            'tooltip': {'trigger': 'axis', 'formatter': '{b}: <b>{c}</b>'},
            'grid': {'left': 16, 'right': 24, 'top': 52, 'bottom': 56, 'containLabel': True},
            'xAxis': {'type': 'category', 'data': labels, 'axisLabel': {'rotate': len(labels) > 6 and 30 or 0}},
            'yAxis': dict({'type': 'value'}, **_y_axis_formatter(values)),
            'series': [{
                'name': title,
                'type': 'bar',
                'data': values,
                'barWidth': min(40, max(16, 300 // max(len(labels), 1))),
                'itemStyle': {'color': gradient_color, 'borderRadius': [4, 4, 0, 0]},
                'label': {'show': True, 'position': 'top', 'fontSize': 11, 'color': '#374151', 'formatter': '{c}'},
                'markLine': {
                    'silent': True,
                    'data': [{'type': 'average', 'name': f'均值 {avg}'}],
                    'lineStyle': {'color': '#f59e0b', 'type': 'dashed', 'width': 1.5},
                    'label': {'formatter': '均值 {c}', 'fontSize': 10, 'color': '#f59e0b'},
                },
            }],
        }
    }


def build_pie_chart(title: str, labels: list[str], values: list[float]) -> dict[str, Any]:
    total = sum(abs(v) for v in values) or 1
    data = [
        {'name': lbl, 'value': round(abs(v), 2),
         'label': {'formatter': f'{lbl}\n{abs(v) / total * 100:.1f}%'}}
        for lbl, v in zip(labels, values)
    ]
    colors = ['#6366f1', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4']
    return {
        'title': title,
        'typeLabel': '饼图',
        'option': {
            'title': {'text': title, 'textStyle': {'fontSize': 14, 'fontWeight': 600, 'color': '#1f2937'}, 'left': 'center', 'top': 8},
            'tooltip': {'trigger': 'item', 'formatter': '{b}: {c} ({d}%)'},
            'legend': {'orient': 'vertical', 'right': 16, 'top': 'center', 'textStyle': {'fontSize': 12}},
            'color': colors,
            'series': [{
                'name': title,
                'type': 'pie',
                'radius': ['35%', '65%'],
                'center': ['42%', '55%'],
                'data': data,
                'itemStyle': {'borderRadius': 6, 'borderWidth': 2, 'borderColor': '#fff'},
                'label': {'show': True, 'formatter': '{b}\n{d}%', 'fontSize': 11},
                'emphasis': {'itemStyle': {'shadowBlur': 10, 'shadowOffsetX': 0, 'shadowColor': 'rgba(0,0,0,0.2)'}},
            }],
        }
    }


def format_amount(value: Any) -> str:
    if value is None:
        return '暂无数据'
    return f'{float(value):,.2f} 万元'


def percent_text(value: Any) -> str:
    if value is None:
        return '-'
    return f'{float(value):.2f}%'


def latest_fy_year() -> int:
    with open_connection() as connection:
        row = connection.execute("SELECT MAX(report_year) AS year FROM income_sheet WHERE report_period LIKE '%FY'").fetchone()
    return int(row['year'] or 2024)


def _metric_from_value(value: Any) -> dict[str, str]:
    key = METRIC_ALIASES.get(str(value).strip(), '')
    metric = METRIC_DEFINITIONS.get(key)
    return dict(metric) if metric else {}


def _normalize_period_value(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ''
    guessed = extract_period(text) or text
    normalized = normalize_period_code(guessed)
    return normalized if re.match(r'^(20\d{2})(Q1|HY|Q3C?|FY)$', normalized) else ''


def _normalize_required_fields(value: Any) -> list[str]:
    allowed = {'company', 'report_period', 'metric'}
    items = _normalize_text_list(value, [], 3)
    return [item for item in items if item in allowed]


def _build_rule_intent(question: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    compact = re.sub(r'\s+', '', question)
    company = infer_company(question, history, allow_default=False)
    period = infer_period(question, history)
    inferred = infer_metric_or_intent(question, history)
    query_kind = 'general'
    analysis_mode = 'general'
    if re.search(RANKING_PATTERN, compact):
        query_kind = 'ranking'
        analysis_mode = 'ranking'
    elif inferred.get('type') == 'cash_flow':
        query_kind = 'cash_flow'
        analysis_mode = 'single'
    elif inferred.get('type') == 'metric':
        query_kind = 'metric'
        analysis_mode = 'trend' if re.search(TREND_PATTERN, compact) else 'single'
    required: list[str] = []
    if query_kind in {'metric', 'cash_flow'} and not company:
        required.append('company')
    if query_kind == 'metric' and not inferred:
        required.append('metric')
    if query_kind == 'metric' and analysis_mode != 'trend' and not period:
        required.append('report_period')
    if query_kind == 'cash_flow' and not period:
        required.append('report_period')
    return {
        'company': company,
        'period': period,
        'metric': inferred if inferred.get('type') == 'metric' else {},
        'query_kind': query_kind,
        'analysis_mode': analysis_mode,
        'required': required
    }


def _infer_intent_with_llm(question: str, history: list[dict[str, Any]], rule_intent: dict[str, Any]) -> dict[str, Any]:
    if not llm_available():
        return {}
    system_prompt = '你是财务数据库问答系统的意图识别器。你只能在给定支持范围内识别用户意图，不得编造公司、指标、报告期或查询场景。请返回 JSON。'
    intent_few_shot = (
        '=== 示例 ===\n'
        '问题: "同仁堂2024年净利润是多少"\n'
        '输出: {"queryKind":"metric","company":"同仁堂","period":"2024FY","metric":"net_profit","analysisMode":"single","needsClarification":[]}\n\n'
        '问题: "白云山近三年营业收入的变化趋势"\n'
        '输出: {"queryKind":"metric","company":"白云山","period":"","metric":"total_operating_revenue","analysisMode":"trend","needsClarification":[]}\n\n'
        '问题: "2024年哪家公司的净利润最高"\n'
        '输出: {"queryKind":"ranking","company":"","period":"2024FY","metric":"net_profit","analysisMode":"ranking","needsClarification":[]}\n\n'
        '问题: "片仔癀的现金流情况"\n'
        '输出: {"queryKind":"cash_flow","company":"片仔癀","period":"","metric":"","analysisMode":"single","needsClarification":["report_period"]}\n\n'
        '问题: "你能帮我查什么数据"\n'
        '输出: {"queryKind":"general","company":"","period":"","metric":"","analysisMode":"general","needsClarification":[]}\n\n'
        '问题: "净利润最高的公司是哪家"\n'
        '输出: {"queryKind":"ranking","company":"","period":"","metric":"net_profit","analysisMode":"ranking","needsClarification":["report_period"]}\n'
        '=== 示例结束 ===\n\n'
    )
    user_prompt = (
        '请根据用户问题返回 JSON 对象。注意：用户问题已经过改写，是一个独立完整的问题，请直接从问题本身提取信息，不要额外从对话历史中推断或添加信息。\n\n'
        '返回格式：{"queryKind":"metric|cash_flow|ranking|general","company":"","period":"","metric":"","analysisMode":"single|trend|ranking|general","needsClarification":["company","report_period","metric"]}\n'
        '要求：\n'
        '1. company 只在能从当前问题中明确识别且属于支持公司列表时输出；无法确认就输出空字符串。不要从历史中猜测公司。\n'
        '2. period 统一输出为 YYYYQ1、YYYYHY、YYYYQ3、YYYYQ3C（前三季度累计）、YYYYFY 这类代码；"前三季度"对应Q3C，"第三季度"对应Q3；无法确认则输出空字符串。\n'
        '3. metric 输出最匹配的字段名（仅限支持字段列表中的名称），无法确认或现金流场景则输出空字符串。\n'
        '4. queryKind 只允许输出 metric、cash_flow、ranking、general。\n'
        '5. analysisMode 只允许输出 single、trend、ranking、general。\n'
        '6. 如果用户问的是身份、能力范围或闲聊，queryKind 设为 general。\n'
        '7. 如果用户在问财务数据但缺少公司、报告期或指标，请在 needsClarification 中列出缺失字段。\n'
        '8. 仅返回 JSON 对象。\n'
        '9. 开放性分析/评估类问题（如"评估竞争壁垒""分析发展前景""竞争优势""行业地位""战略建议""技术冲击""在研管线""商业模式""核心竞争力""风险评估"等），queryKind 设为 general，analysisMode 设为 general，needsClarification 为空。这类问题需要结合研报定性分析，不是简单的数值查询。\n\n'
        f'{intent_few_shot}'
        f'支持公司：{json.dumps(COMPANY_NAMES, ensure_ascii=False)}\n'
        f'支持指标：{json.dumps({key: value["label"] for key, value in METRIC_DEFINITIONS.items()}, ensure_ascii=False)}\n'
        f'规则识别结果：{json.dumps({"company": rule_intent["company"], "period": rule_intent["period"], "metric": rule_intent["metric"].get("column", ""), "queryKind": rule_intent["query_kind"], "analysisMode": rule_intent["analysis_mode"], "required": rule_intent["required"]}, ensure_ascii=False)}\n'
        f'用户问题：{question}\n'
    )
    try:
        payload = request_llm_json(system_prompt, user_prompt, temperature=llm_temperature('intent_temperature', 0.1))
    except Exception:
        return {}
    query_kind = str(payload.get('queryKind') or payload.get('query_kind') or '').strip()
    if query_kind not in {'metric', 'cash_flow', 'ranking', 'general'}:
        query_kind = ''
    analysis_mode = str(payload.get('analysisMode') or payload.get('analysis_mode') or '').strip()
    if analysis_mode not in {'single', 'trend', 'ranking', 'general'}:
        analysis_mode = ''
    company = str(payload.get('company', '')).strip()
    if company not in COMPANY_NAMES:
        company = ''
    period = _normalize_period_value(payload.get('period') or payload.get('report_period') or payload.get('reportPeriod'))
    metric = _metric_from_value(payload.get('metric') or payload.get('metricLabel') or payload.get('metric_label'))
    required = _normalize_required_fields(payload.get('needsClarification') or payload.get('needs_clarification'))
    return {
        'company': company,
        'period': period,
        'metric': metric,
        'query_kind': query_kind,
        'analysis_mode': analysis_mode,
        'required': required
    }


def _resolve_intent(question: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    rule_intent = _build_rule_intent(question, history)
    llm_intent = _infer_intent_with_llm(question, history, rule_intent)
    company = rule_intent['company']
    llm_company = str(llm_intent.get('company', '')).strip()
    if llm_company and (not company or llm_company in question):
        company = llm_company
    period = rule_intent['period']
    if not period:
        llm_period = str(llm_intent.get('period', '')).strip()
        # Only accept LLM period if user explicitly provided year context
        _has_year_ctx = bool(re.search(r'(20\d{2}|今年|去年|最新|最近|近[几\d])', question))
        period = llm_period if _has_year_ctx or not llm_period else ''
    query_kind = str(rule_intent['query_kind']).strip()
    llm_query_kind = str(llm_intent.get('query_kind', '')).strip()
    if query_kind == 'general' and llm_query_kind in {'metric', 'cash_flow', 'ranking'}:
        query_kind = llm_query_kind
    metric = dict(rule_intent['metric']) if isinstance(rule_intent['metric'], dict) else {}
    if not metric and isinstance(llm_intent.get('metric'), dict):
        metric = dict(llm_intent['metric'])
    analysis_mode = str(rule_intent['analysis_mode']).strip()
    if query_kind == 'ranking':
        analysis_mode = 'ranking'
    elif query_kind == 'cash_flow':
        analysis_mode = 'single'
    elif query_kind == 'metric':
        if analysis_mode == 'trend' or llm_intent.get('analysis_mode') == 'trend':
            analysis_mode = 'trend'
        else:
            analysis_mode = 'single'
    else:
        analysis_mode = 'general'
    required: list[str] = []
    if query_kind in {'metric', 'cash_flow'} and not company:
        required.append('company')
    if query_kind == 'metric' and not metric:
        required.append('metric')
    if query_kind == 'metric' and analysis_mode != 'trend' and not period:
        required.append('report_period')
    if query_kind == 'cash_flow' and not period:
        required.append('report_period')
    for item in llm_intent.get('required', []):
        if item in {'company', 'report_period', 'metric'} and item not in required:
            required.append(item)
    required = [
        item for item in required
        if not (
            (item == 'company' and company)
            or (item == 'report_period' and period)
            or (item == 'metric' and metric)
        )
    ]
    return {
        'company': company,
        'period': period,
        'metric': metric,
        'query_kind': query_kind,
        'analysis_mode': analysis_mode,
        'required': required
    }


def _build_query_plan_with_llm(question: str, history: list[dict[str, Any]], resolved: dict[str, Any]) -> dict[str, Any]:
    if not llm_available():
        return {}
    table_desc = '\n'.join(
        f'  {tbl}: {chr(44).join(f"{col}→{lbl}" for col, lbl in cols.items())}'
        for tbl, cols in TABLE_METRIC_COLUMNS.items()
    )
    system_prompt = '你是财务数据库查询方案设计器。只允许使用给定的表和字段，不得编造字段或表名。请返回 JSON。'
    user_prompt = (
        '请根据以下信息输出结构化查询方案，返回 JSON 对象。\n\n'
        '返回格式：{"table":"","columns":[""],"analysisMode":"single|trend","orderBy":null或{"field":"","direction":"asc|desc"},"limit":null或数字}\n'
        '要求：\n'
        '1. table 只允许从支持列表中选择，选最匹配用户问题的表。\n'
        '2. columns 只允许从对应表的字段列表中选择，选最匹配的 1~3 个。\n'
        '3. analysisMode 为 single（查单期具体数值）或 trend（查多期趋势）。\n'
        '4. orderBy 仅排名场景使用，其他为 null。\n'
        '5. limit 仅限排名或分页场景，其他为 null。\n'
        '6. 现金流场景请使用 cash_flow_sheet，资产/负债场景使用 balance_sheet，利润/营收场景使用 income_sheet。\n'
        '7. 仅返回 JSON 对象，不得输出其他文本。\n\n'
        f'支持的表和字段：\n{table_desc}\n\n'
        f'已识别信息：公司={resolved.get("company","")}, 报告期={resolved.get("period","")}, '
        f'分析模式={resolved.get("analysis_mode","")}, 初步指标={resolved.get("metric",{}).get("column","")}\n'
        f'最近对话：{json.dumps(_recent_history(history), ensure_ascii=False)}\n'
        f'用户问题：{question}\n'
    )
    try:
        payload = request_llm_json(system_prompt, user_prompt, temperature=llm_temperature('query_plan_temperature', 0.1))
    except Exception:
        return {}
    table = str(payload.get('table', '')).strip()
    if table not in ALLOWED_TABLES:
        return {}
    raw_cols = payload.get('columns') or []
    if isinstance(raw_cols, str):
        raw_cols = [raw_cols]
    allowed_cols = TABLE_METRIC_COLUMNS.get(table, {})
    columns = [str(col).strip() for col in raw_cols if str(col).strip() in allowed_cols]
    if not columns:
        return {}
    analysis_mode = str(payload.get('analysisMode') or payload.get('analysis_mode') or '').strip()
    if analysis_mode not in {'single', 'trend'}:
        analysis_mode = str(resolved.get('analysis_mode', 'single'))
    order_by: dict[str, str] | None = None
    raw_order = payload.get('orderBy') or payload.get('order_by')
    if isinstance(raw_order, dict):
        field = str(raw_order.get('field', '')).strip()
        direction = str(raw_order.get('direction', 'desc')).strip().lower()
        if field in allowed_cols and direction in {'asc', 'desc'}:
            order_by = {'field': field, 'direction': direction}
    raw_limit = payload.get('limit')
    limit = int(raw_limit) if isinstance(raw_limit, (int, float)) and 1 <= int(raw_limit) <= 200 else None
    return {'table': table, 'columns': columns, 'analysis_mode': analysis_mode, 'orderBy': order_by, 'limit': limit}


def _compile_plan_to_sql(plan: dict[str, Any], company: str, period: str) -> tuple[str, tuple, list[dict[str, str]], str]:
    table = plan.get('table', '')
    cols = plan.get('columns', [])
    analysis_mode = plan.get('analysis_mode', 'single')
    order_by = plan.get('orderBy')
    limit = plan.get('limit')
    if not table or table not in ALLOWED_TABLES:
        return '', (), [], ''
    col_labels = TABLE_METRIC_COLUMNS.get(table, {})
    cols = [col for col in cols if col in col_labels]
    if not cols:
        return '', (), [], ''
    select_clause = 'report_period, ' + ', '.join(cols)
    where_parts: list[str] = []
    params: list[Any] = []
    if company:
        where_parts.append('TRIM(stock_abbr) = ?')
        params.append(company)
    if analysis_mode == 'single' and period:
        where_parts.append('report_period = ?')
        params.append(period)
    where_parts.append(f'{cols[0]} IS NOT NULL')
    where_clause = ' AND '.join(where_parts)
    if order_by:
        _ob_field = order_by['field']
        # TEXT 列需要 CAST 为数值排序（SQLite 所有指标列为 TEXT）
        _non_numeric = {'report_period', 'report_year', 'stock_abbr', 'stock_code'}
        _ob_expr = f"CAST({_ob_field} AS REAL)" if _ob_field not in _non_numeric else _ob_field
        order_clause = f" ORDER BY {_ob_expr} {order_by['direction'].upper()}"
    elif analysis_mode == 'trend':
        order_clause = ' ORDER BY report_year ASC, report_period ASC'
    else:
        order_clause = ''
    limit_clause = f' LIMIT {limit}' if limit else (' LIMIT 1' if analysis_mode == 'single' else '')
    exec_sql = f'SELECT {select_clause} FROM {table} WHERE {where_clause}{order_clause}{limit_clause}'
    disp_where: list[str] = []
    if company:
        disp_where.append(f"TRIM(stock_abbr) = '{company}'")
    if analysis_mode == 'single' and period:
        disp_where.append(f"report_period = '{period}'")
    disp_where.append(f'{cols[0]} IS NOT NULL')
    display_sql = f"SELECT {select_clause} FROM {table} WHERE {' AND '.join(disp_where)}{order_clause}{limit_clause};"
    columns_spec = [{'key': 'report_period', 'label': '报告期'}] + [
        {'key': col, 'label': col_labels.get(col, col)} for col in cols
    ]
    return exec_sql, tuple(params), columns_spec, display_sql


def _normalize_text_list(value: Any, default: list[str], limit: int) -> list[str]:
    if not isinstance(value, list):
        return default[:limit]
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in items:
            items.append(text)
        if len(items) >= limit:
            break
    return items or default[:limit]


def _recent_history(history: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in history[-limit:]:
        role = str(item.get('role', '')).strip() or 'assistant'
        content = str(item.get('content', '')).strip()
        if not content:
            continue
        entry: dict[str, Any] = {'role': role, 'content': content}
        if role == 'assistant':
            meta = item.get('meta')
            if isinstance(meta, dict):
                compact_meta = {k: v for k, v in meta.items()
                                if k in ('company', 'metric', 'report_period', 'query_kind', 'intent')
                                and v and str(v).strip() not in ('', 'None')}
                if compact_meta:
                    entry['meta'] = compact_meta
        result.append(entry)
    return result


def _rows_for_llm(rows: list[dict[str, Any]], columns: list[dict[str, str]], limit: int = 12) -> list[dict[str, Any]]:
    label_map = {str(item.get('key', '')): str(item.get('label', item.get('key', ''))) for item in columns}
    serialized: list[dict[str, Any]] = []
    for row in rows[:limit]:
        converted: dict[str, Any] = {}
        for key, value in row.items():
            label = f"{label_map.get(key, key)}<{key}>"
            # 将 report_period 编码转换为人类可读格式，避免 LLM 误读
            if key == 'report_period' and isinstance(value, str):
                value = format_period(value)
            converted[label] = value
        serialized.append(converted)
    return serialized


def _chart_for_llm(chart: dict[str, Any] | None) -> dict[str, Any] | None:
    if not chart:
        return None
    option = chart.get('option', {}) if isinstance(chart.get('option'), dict) else {}
    series = option.get('series', []) if isinstance(option.get('series', []), list) else []
    return {
        'title': chart.get('title'),
        'typeLabel': chart.get('typeLabel'),
        'seriesCount': len(series)
    }


_LISTING_QUESTION_RE = re.compile(r'(有哪些|列出|列举|哪几个|哪些|包括什么|分别是)')


def _retrieve_kb_context(question: str, company: str = '', *, fast_mode: bool = False) -> tuple[str, list[dict[str, Any]]]:
    """BM25-search the knowledge base; return (context_text, references_list).

    fast_mode=True skips adaptive-retrieval / self-RAG / multi-step to cut latency
    (useful for batch export where speed matters more than marginal recall).
    """
    if not _KB_AVAILABLE:
        return '', []
    assistant_config = CONFIG.get('assistant', {}) if isinstance(CONFIG.get('assistant'), dict) else {}
    config_top_k = assistant_config.get('kb_top_k', 5)
    try:
        config_top_k = min(max(int(config_top_k), 1), 10)
    except (TypeError, ValueError):
        config_top_k = 5
    # ── 判断是否为融合查询（需要从KB提取多家公司信息） ──
    _FUSION_QUERY_RE = re.compile(
        r'(龙头|头部|领军|领先|主要|重点|代表|行业.*公司|哪些.*公司|哪些.*企业'
        r'|对比|差异|偏离|业绩预告|[一二三四五六七八九十\d]+家)')
    _is_fusion = bool(_FUSION_QUERY_RE.search(question))
    # listing-type questions need more chunks to capture full table data;
    # fusion queries need more chunks to cover multiple companies;
    # non-listing questions cap at 5 to avoid noise drowning the answer
    if _LISTING_QUESTION_RE.search(question):
        top_k = max(config_top_k, 10)
    elif _is_fusion:
        top_k = max(config_top_k, 8)
    else:
        top_k = max(config_top_k, 7)
    # detect doc_type preference based on question topic
    _INDUSTRY_TOPIC_RE = re.compile(r'(医保|目录|政策|行业|市场|药品|产品|价格|纳入|新增|调整|集采|带量|招标|审批|上市)')
    if _INDUSTRY_TOPIC_RE.search(question):
        doc_type_pref = '行业研报'
    else:
        doc_type_pref = ''
    # listing questions may need many chunks from the same document (table rows);
    # fusion queries increase per-doc to 3 for richer context;
    # non-listing questions limit per-doc to ensure diversity
    if _LISTING_QUESTION_RE.search(question):
        per_doc = top_k
    elif _is_fusion:
        per_doc = 3
    else:
        per_doc = 3
    # rule-based decision for multi-step retrieval (replaces LLM adaptive_retrieval_judge)
    _COMPLEX_QUERY_RE = re.compile(r'(为什么|原因|归因|怎么.*变|如何.*变|前景|预测|综合.*分析|多.*维|对比.*行业|同行)')
    # 融合查询也需要二次检索，以覆盖更多公司
    retrieval_rounds = 2 if (_COMPLEX_QUERY_RE.search(question) or _is_fusion) else 1
    # 融合查询开启 query rewriting 提升 BM25 召回率
    _do_rewrite = _is_fusion
    try:
        hits = _search_kb(question, top_k=top_k, company_filter=company,
                          doc_type_preference=doc_type_pref, max_per_doc=per_doc,
                          rewrite_query=_do_rewrite)
    except Exception as _kb_err:
        import traceback
        with open('_kb_debug.log', 'a', encoding='utf-8') as _dbg:
            _dbg.write(f'[KB-DEBUG] _search_kb raised: {_kb_err}\n')
            traceback.print_exc(file=_dbg)
        return '', []
    with open('_kb_debug.log', 'a', encoding='utf-8') as _dbg:
        _dbg.write(f'[KB-DEBUG] initial search: {len(hits)} hits (company={company!r})\n')
    # Fallback: if company-specific search returned nothing, retry without company filter
    # to find relevant industry reports
    if not hits and company:
        with open('_kb_debug.log', 'a', encoding='utf-8') as _dbg:
            _dbg.write(f'[KB-DEBUG] fallback triggered for company={company!r}\n')
        try:
            hits = _search_kb(question, top_k=top_k, company_filter='',
                              doc_type_preference='行业研报', max_per_doc=per_doc,
                              rewrite_query=True)
            with open('_kb_debug.log', 'a', encoding='utf-8') as _dbg:
                _dbg.write(f'[KB-DEBUG] fallback returned {len(hits)} hits\n')
                if hits:
                    for _h in hits[:3]:
                        _dbg.write(f'  - {_h.get("doc_name","?")} score={_h.get("score",0):.4f}\n')
        except Exception as _fb_err:
            with open('_kb_debug.log', 'a', encoding='utf-8') as _dbg:
                _dbg.write(f'[KB-DEBUG] fallback exception: {_fb_err}\n')
    if not hits:
        with open('_kb_debug.log', 'a', encoding='utf-8') as _dbg:
            _dbg.write(f'[KB-DEBUG] returning empty (no hits after fallback)\n')
        return '', []

    # ── Self-RAG: LLM filters irrelevant chunks (skip in fast_mode) ──
    if not fast_mode:
        _pre_rag = len(hits)
        try:
            hits = self_rag_filter(question, hits)
        except Exception:
            pass
        with open('_kb_debug.log', 'a', encoding='utf-8') as _dbg:
            _dbg.write(f'[KB-DEBUG] after self-RAG: {len(hits)} hits (was {_pre_rag})\n')

    # ── Multi-step Retrieval: gap analysis + second round ──
    # 保留此步骤（即使 fast_mode），因为 Task3 复杂问题需要二次检索补充
    if retrieval_rounds >= 2:
        try:
            hits = multi_step_retrieve(question, hits, top_k=top_k,
                                       company_filter=company,
                                       doc_type_preference=doc_type_pref,
                                       max_per_doc=per_doc)
        except Exception:
            pass
    # For listing questions, expand context with adjacent chunks from the primary document
    # to ensure complete table rows are captured (e.g., if table rows are split across chunks)
    if _LISTING_QUESTION_RE.search(question) and hits:
        try:
            from backend.core.kb_store import kb_conn as _kb_conn_for_expand
            existing_ids: set[int] = {h['chunk_id'] for h in hits}
            # Find primary doc: the doc contributing the most (or highest-scoring) hits
            doc_scores: dict[int, float] = {}
            doc_meta: dict[int, dict] = {}
            for h in hits:
                did = h.get('doc_id')
                if did is not None:
                    doc_scores[did] = doc_scores.get(did, 0) + h['score']
                    if did not in doc_meta:
                        doc_meta[did] = {k: h[k] for k in (
                            'doc_name', 'doc_path', 'doc_type', 'stock_name',
                            'report_date', 'rating', 'analyst', 'research_firm',
                            'current_price', 'target_price', 'forecast_json', 'excel_metadata'
                        ) if k in h}
            if doc_scores:
                primary_doc_id = max(doc_scores, key=lambda d: doc_scores[d])
                # Get chunk_idx range already covered from this doc
                primary_idxs = [h['chunk_idx'] for h in hits
                                 if h.get('doc_id') == primary_doc_id and 'chunk_idx' in h]
                if primary_idxs:
                    min_idx = min(primary_idxs)
                    max_idx = max(primary_idxs)
                    # Fetch adjacent chunks within expanded window
                    with _kb_conn_for_expand() as conn:
                        adj_rows = conn.execute(
                            'SELECT id, chunk_idx, text FROM kb_chunks'
                            ' WHERE doc_id=? AND chunk_idx BETWEEN ? AND ?'
                            ' ORDER BY chunk_idx',
                            (primary_doc_id, max(0, min_idx - 2), max_idx + 4)
                        ).fetchall()
                    meta = doc_meta[primary_doc_id]
                    for ar in adj_rows:
                        if ar['id'] not in existing_ids:
                            existing_ids.add(ar['id'])
                            hits.append({
                                'chunk_id': ar['id'],
                                'chunk_idx': ar['chunk_idx'],
                                'doc_id': primary_doc_id,
                                'score': 0.0,  # injected, not ranked
                                'text': ar['text'],
                                **meta,
                            })
            # Sort: ranked hits first (score > 0), then injected adjacent chunks by idx
            hits.sort(key=lambda h: (-h['score'], h.get('chunk_idx', 9999)))
        except Exception:
            pass
    parts: list[str] = []
    refs: list[dict[str, Any]] = []
    injected_forecasts: set[str] = set()
    # Filter out garbage chunks (table headers, column labels with no real content)
    def _is_garbage_chunk(text: str) -> bool:
        clean = re.sub(r'\s+', '', text).strip()
        if len(clean) < 15:
            return True
        # Mostly percentage/metric labels with no sentence structure
        cjk_chars = len(re.findall(r'[\u4e00-\u9fff]', clean))
        pct_symbols = clean.count('%') + clean.count('(') + clean.count(')')
        if pct_symbols >= 3 and cjk_chars < 20:
            return True
        return False
    hits = [h for h in hits if not _is_garbage_chunk(h.get('text', '') or '')]
    for i, h in enumerate(hits, 1):
        header_parts: list[str] = []
        if h.get('report_date'):
            header_parts.append(f'日期:{h["report_date"]}')
        if h.get('rating'):
            header_parts.append(f'评级:{h["rating"]}')
        if h.get('current_price'):
            header_parts.append(f'当前价:{h["current_price"]}元')
        if h.get('target_price') and h['target_price'] not in ('——', '--', ''):
            header_parts.append(f'目标价:{h["target_price"]}元')
        if h.get('analyst'):
            header_parts.append(f'分析师:{h["analyst"]}')
        if h.get('research_firm'):
            header_parts.append(f'机构:{h["research_firm"]}')
        header = f'[参考{i}] 来源：{h["doc_name"]}'
        if header_parts:
            header += f' ({", ".join(header_parts)})'
        # append forecast data once per document (for cross-validation)
        doc_name = h['doc_name']
        fj = h.get('forecast_json', '')
        em = h.get('excel_metadata', '')
        forecast_text = ''
        if doc_name not in injected_forecasts and (fj or em):
            fc_lines: list[str] = []
            # 1) structured EPS/PE from Excel (most reliable)
            if em:
                try:
                    excel = json.loads(em)
                    eps_fields = [
                        ('predictLastYearEps',    '上年EPS(元)'),
                        ('predictThisYearEps',    '本年EPS(元)'),
                        ('predictNextYearEps',    '下年EPS(元)'),
                        ('predictNextTwoYearEps', '后年EPS(元)'),
                        ('predictLastYearPe',     '上年PE'),
                        ('predictThisYearPe',     '本年PE'),
                        ('predictNextYearPe',     '下年PE'),
                        ('predictNextTwoYearPe',  '后年PE'),
                        ('indvAimPriceT',         '目标价上限(元)'),
                        ('indvAimPriceL',         '目标价下限(元)'),
                    ]
                    for field, label in eps_fields:
                        v = excel.get(field)
                        if v and v not in ('None', ''):
                            fc_lines.append(f'  {label}: {v}')
                except Exception:
                    pass
            # 2) multi-year forecast from PDF extraction
            if fj:
                try:
                    fc = json.loads(fj)
                    years = fc.get('years', [])
                    fc_lines.append(f'  年份: {" ".join(years)}')
                    for key, label in [('revenue','营收(百万)'),('net_profit','净利润(百万)'),
                                       ('eps','EPS'),('roe','ROE'),('pe','PE'),('pb','PB')]:
                        if key in fc:
                            vals = [fc[key].get(y, '-') for y in years]
                            fc_lines.append(f'  {label}: {" ".join(vals)}')
                except Exception:
                    pass
            if fc_lines:
                forecast_text = '\n【分析师预测数据（用于校验）】\n' + '\n'.join(fc_lines)
                injected_forecasts.add(doc_name)
        parts.append(f'{header}\n{h["text"][:1200]}{forecast_text}')
        # Use child_text (fine-grained chunk) for evidence-level reference snippet
        evidence_text = h.get('child_text', h['text'])[:200]
        # 从证据文本中提取多个有意义的短语作为高亮关键词（按标点分句，取前5个≥4字的短语）
        _highlight_parts: list[str] = []
        for _seg in re.split(r'[，。；：、,.!?！？\n\r]+', evidence_text):
            _seg_clean = re.sub(r'\s+', '', _seg).strip()
            if len(_seg_clean) >= 4 and _seg_clean not in _highlight_parts:
                _highlight_parts.append(_seg_clean[:30])
            if len(_highlight_parts) >= 5:
                break
        # 兜底：若分句不足，提取连续CJK片段
        if len(_highlight_parts) < 2:
            for _run in (re.findall(r'[\u4e00-\u9fff]{4,30}', evidence_text) or []):
                if _run not in _highlight_parts:
                    _highlight_parts.append(_run)
                if len(_highlight_parts) >= 5:
                    break
        _highlight = '|'.join(_highlight_parts[:5])
        # Parse figure_titles JSON → pick the best one for paper_image
        _fig_titles: list[str] = []
        _raw_fig = h.get('figure_titles', '')
        if _raw_fig:
            try:
                _fig_titles = json.loads(_raw_fig) if isinstance(_raw_fig, str) else _raw_fig
            except Exception:
                pass
        refs.append({
            'paper_path': h['doc_path'],
            'text': evidence_text,
            'highlight_text': evidence_text,
            'parent_text': h.get('text', ''),
            'doc_name': h['doc_name'],
            'doc_type': h['doc_type'],
            'score': round(h['score'], 4),
            'page_no': h.get('page_no'),
            'highlight': _highlight,
            'figure_titles': _fig_titles,
        })
    return '\n\n'.join(parts), refs


def _build_llm_analysis(question: str, history: list[dict[str, Any]], sql: str, columns: list[dict[str, str]], rows: list[dict[str, Any]], source_tables: list[str], chart: dict[str, Any] | None, analysis_scene: str, kb_context: str = '', references: list[dict[str, Any]] | None = None, emit=None, deep_think: bool = True, data_coverage_note: str = '', render_mode: str = RENDER_MODE_CHAT) -> dict[str, Any]:
    default_follow_up_hints = ['对比另一个报告期的结果', '换一个财务指标继续分析']
    unavailable = {
        'content': '已完成结构化查询并返回结果，但模型总结服务当前不可用。你可以先查看表格与图表，稍后重试智能分析。',
        'insights': [],
        'followUpHints': default_follow_up_hints
    }
    if not llm_available():
        return unavailable
    system_prompt = (
        '你是一名资深A股财务分析师，擅长从数据中提炼洞察并用自然语言表达。\n'
        '\n'
        '【报告期编码规则——务必严格遵守】\n'
        '数据中的 report_period 字段含义如下：\n'
        '- 2024FY = 2024年度年报（不是2025年！）\n'
        '- 2025Q1 = 2025年第一季度报\n'
        '- 2024HY = 2024年半年度报\n'
        '- 2024Q3 = 2024年前三季度报\n'
        '引用数据时，必须使用数据实际所属的报告期年份，例如 2024FY 的数据必须说"2024年度"或"2024年年报"，'
        '绝不能说成"2025年"。只有查询结果中实际存在的 report_period 才能引用，禁止编造不存在的报告期数据。\n'
        '\n'
        '【数据优先级——极其重要】\n'
        '你可以同时看到两类信息：① SQL查询返回的结构化数据（精确数值），② 研报知识库中的文字描述。\n'
        '当两者矛盾时，必须以 SQL 数据为准！研报可能使用不同口径、不同指标、不同时间段，不可盲目引用。\n'
        '判断同比增减时，必须从 SQL 数据中找到"当期值"和"上年同期值"自行计算，不要直接照抄研报中的同比结论。\n'
        '如果 SQL 数据中没有上年同期数据，则明确说明"缺少同期对比数据，无法判断同比方向"，绝不编造。\n'
        '\n'
        '写作风格要求：\n'
        '1. 像和用户面对面聊天一样自然地回答，不要使用固定模板或分节框架。\n'
        '2. 用数据支撑观点，数字以万元/亿元/%表达，保留2位小数。\n'
        '3. 不要逐行罗列数据，而是把数据融入分析叙述中，突出关键变化和洞察。\n'
        '4. 原因归因要结合具体数值推理，不得使用套话。\n'
        '5. 语言专业、自然、有说服力，像一个靠谱的分析师朋友在帮你看数据。\n'
        '6. 当查询结果不足以完整回答用户问题时（例如：用户要求Top10但只返回了少量结果、某些指标缺失、某个时间段无数据等），'
        '必须在回答中主动、明确地说明数据覆盖范围的局限性，不要假装数据是完整的。'
    )
    # 提取查询结果中实际存在的报告期，供 LLM 参考
    _actual_periods = sorted(
        {str(r.get('report_period', '')) for r in rows if r.get('report_period')},
        key=period_sort_key
    )
    _readable_periods = [format_period(p) for p in _actual_periods]
    _periods_hint = (
        f'查询结果中包含的报告期：{", ".join(_readable_periods)}。'
        '请严格按照这些报告期引用数据，不要编造或推断不在此列表中的报告期数据。'
        '例如数据最晚只到"2024年年报"，就不能说"2025年"的年度数据。'
        if _actual_periods else ''
    )
    _data_limit = 200 if render_mode == RENDER_MODE_BATCH else 50
    data_block = (
        f'分析场景：{analysis_scene}\n'
        f'用户问题：{question}\n'
        f'最近对话：{json.dumps(_recent_history(history), ensure_ascii=False)}\n'
        f'来源数据表：{json.dumps([TABLE_LABELS.get(name, name) for name in source_tables], ensure_ascii=False)}\n'
        f'SQL：{sql}\n'
        f'字段：{json.dumps(columns, ensure_ascii=False)}\n'
        + (f'{_periods_hint}\n' if _periods_hint else '')
        + f'查询结果（共{len(rows)}条，展示前{min(len(rows), _data_limit)}条）：{json.dumps(_rows_for_llm(rows, columns, limit=_data_limit), ensure_ascii=False)}\n'
        + (f'⚠️ 查询共返回 {len(rows)} 条数据（已全部返回至前端表格），此处仅展示前{_data_limit}条供分析参考。请在回答中明确说明共有 {len(rows)} 家公司/条数据。\n' if len(rows) > _data_limit else '')
        + f'图表摘要：{json.dumps(_chart_for_llm(chart), ensure_ascii=False)}\n'
        + (f'\n研报知识库参考（必须引用）：\n{kb_context}\n'
           '【引用规则——必须遵守】\n'
           '1. 上方每条研报资料都已经预先分配了 [参考N] 编号，后续引用时只能使用这些编号。\n'
           '2. 你的回答中必须结合研报知识库的观点或分析，尤其在总结、展望、原因分析段落中，主动引用研报观点。\n'
           '3. 每次引用研报内容时，必须在对应句末直接标注 [参考N]（N 为上方参考编号），例如"机构预计2025年净利润增长15%[参考2]"。\n'
           '4. 至少引用1条研报参考。如果研报中有与查询数据相关的分析、预测或评级，务必纳入你的回答。\n'
           '5. 禁止在文末单独列出"数据来源""参考文献""参考资料"等段落，[参考N]必须自然嵌入分析正文句末。\n' if kb_context else '')
        + (f'\n⚠️ 数据覆盖注意：{data_coverage_note}\n' if data_coverage_note else '')
    )
    has_chart = chart is not None
    has_table = bool(rows)
    marker_instructions = ''
    if has_chart or has_table:
        marker_instructions = (
            '\n\n【图表插入规则】你的回答中必须在最合适的位置插入以下占位符，它们会被前端替换为真实图表/表格：\n'
        )
        if has_chart:
            marker_instructions += '- 在你认为最适合展示图表的位置（通常在概述之后、详细分析之前），独占一行写 {{CHART}}\n'
        if has_table:
            marker_instructions += '- 在你认为最适合展示数据表格的位置（通常在分析之后），独占一行写 {{TABLE}}\n'
        marker_instructions += '- 每种占位符只能出现一次，必须独占一行。\n'

    is_batch = render_mode == RENDER_MODE_BATCH

    if is_batch:
        content_instructions = (
            '请用简洁的纯文本回答用户的问题，输出将直接写入 Excel 单元格。具体要求：\n'
            '1. 直接给出核心数据和结论，不要使用任何 Markdown 格式（禁止 **加粗**、## 标题、- 列表等）。\n'
            '2. 用连续的自然语句回答，不要分段、不要换行、不要编号列表。\n'
            '3. 金额数字：数据库存储单位为万元，回答时超过10000万元（即1亿元）的金额必须换算为亿元表达（如474900万元→47.49亿元），不足1亿的用万元。百分比保留2位小数。\n'
            '4. 不要插入图表占位符、不要添加参考标注。\n'
            '5. 整体字数控制在80~6000字以内，一段话说完。【关键规则】当问题包含"所有""全部""按…排序"并要求列出公司清单时，必须把查询结果中的每一行都输出，绝不能只列前几名然后省略，即使有60、70家公司也必须全部列完。每家公司的信息格式必须完全一致——如果用户要求列出股票代码，则每家公司都必须附上股票代码，不能只对前几家给代码而后面省略。示例格式：白云山（600332）197.71亿元、云南白药（000538）93.97亿元……（所有公司均需按此格式，一直列到最后一家）。\n'
            '6. 不要加emoji，不要使用 HTML 标签。\n'
            '7. 【数据准确性——最高优先级】引用具体数值时，必须严格对照查询结果中的同一行数据，'
            '确保报告期和数值一一对应。\n'
            '8. 【时间表述——自然口语化】描述数据时，时间必须用自然的说法，如"2023年""2024年第一季度""2025年上半年"，'
            '禁止使用"年报亏损""季报下滑"等将报告类型当时间状语的写法。例如：应说"2023年亏损4105万元"，不说"2023年年报亏损4105万元"。\n'
            '10. 【简洁列出型问题】当问题明确说"列出股票代码和简称"或"列出代码和名称"等仅要求列表时，'
            '回答只需列出符合条件的公司代码和简称即可（如"002821凯莱英、300347泰格医药、603259药明康德"），'
            '不需要附加每家公司的具体数值、区间、均值等详细信息。先说明共几家公司满足条件，再列出清单。\n'
            '11. 【分析性——不要只列数据】不能只罗列各期数值，必须在数据基础上提炼趋势特征和变化原因。'
            '例如：先用一句话概括整体走势（如"呈V型反转走势""持续下滑""稳步增长"），再结合关键数据点分析拐点原因（如资产处置、业务调整、行业周期等），'
            '让回答像一段有洞察的分析而不是数据表的文字版。\n'
            '9. 【完整性——必须逐一回答】用户问题可能包含多个子问题（用？分隔），你必须逐一回答每个子问题，'
            '不要遗漏任何一个。如果用户问了多个指标（如利润同比、销售额同比），每家企业必须同时给出所有被问到的指标数值，'
            '不能只列其中一个。示例格式：「云南白药净利润47.49亿元（利润同比+16.02%，营收同比+7.13%）」。'
            '如果用户问了"最大/最高/最强的是哪家"，必须从前面已列出的企业中选出，在回答末尾用独立一句话点名。'
            '【关键】若回答中涉及了N个指标，则必须给出N个"最大"结论，每个指标一个，绝不能只给1个。'
            '必须使用如下格式结尾：「年同比上涨幅度最大的企业：净利润同比最高为XX（+YY%），营收同比最高为ZZ（+WW%）」。'
        )
    elif deep_think:
        content_instructions = (
            '请用自然、连贯的语言回答用户的问题。具体要求：\n'
            '1. 先用1~2句话概括核心结论，作为第一段，其中核心结论的关键词或关键短语用 **加粗** 标记。\n'
            '2. 后续可根据内容自然设定小标题来组织分析（用 **小标题** 独占一行表示），标题名称要根据实际内容自主拟定，禁止使用固定模板。\n'
            '   示例：**盈利能力分析**、**关键风险提示**、**同比变化**等，按实际需要灵活命名。\n'
            '3. **加粗** 的使用范围：不仅限于数值，还包括以下内容：\n'
            '   - 关键数值及其含义（如"营收同比增长**12.35%**，达到**15.8亿元**"）\n'
            '   - 重要结论或判断（如"整体盈利能力**显著改善**""现金流**承压明显**"）\n'
            '   - 关键财务术语或指标名称（如"**毛利率**回升至38.5%""**经营性现金流**转正"）\n'
            '   - 编号列表中每条的核心要点（如"1. **研发投入加大**：研发费用同比增长25%"）\n'
            '   总之，让读者快速扫读时能抓住每段的重点。\n'
            '4. 当有多个并列要点时，用编号列表自然呈现（如"1. xxx\n2. xxx"），每条可展开2~3句。\n'
            '5. 段落之间用空行分隔，让阅读舒适。不要加emoji，不要使用 ## 和 HTML 标签。\n'
            '6. 不要把图表标题作为正文标题重复输出（图表自带标题，正文用分析性小标题即可）。\n'
            '7. 整体字数300~600字。\n'
            '8. 【数据准确性——最高优先级】引用具体数值时，必须严格对照查询结果中的同一行数据，'
            '确保报告期和数值一一对应，绝不能把A报告期的数值写到B报告期下。'
            '如果不确定某个数值属于哪个报告期，宁可不写也不要猜。'
            + marker_instructions
        )
    else:
        content_instructions = (
            '请简洁地回答用户的问题。具体要求：\n'
            '1. 直接给出核心数据和结论，关键数值用 **加粗** 标记。\n'
            '2. 不需要详细分析、不需要小标题、不需要分节。\n'
            '3. 用2~4句话简明扼要地回答，突出最关键的数据和结论即可。\n'
            '4. 整体字数控制在80~150字以内。\n'
            '5. 不要加emoji，不要使用 ## 和 HTML 标签。\n'
            '6. 不要把图表标题作为正文标题重复输出。'
            + marker_instructions
        )
    # --- batch mode: 对"列出所有"类查询，程序化生成完整列表 ---
    _is_validation_query = bool(re.search(r'(检查|校验|核[对查]|验证|是否等于|是否一致|是否相等|公式)', question))
    _is_list_all_batch = (
        is_batch
        and len(rows) >= 10
        and re.search(r'(所有|全部|按.*排序)', question)
        and any(c.get('key') not in ('stock_code', 'stock_abbr', 'report_period', 'rank') for c in columns)
        and not _is_validation_query
    )
    if _is_list_all_batch:
        # 直接从查询结果格式化完整列表
        _metric_cols = [c for c in columns if c.get('key') not in ('stock_code', 'stock_abbr', 'report_period', 'rank', '_total_before_rank')]
        # 排名/占比查询：优先只展示核心指标列（占比/率/比值），避免冗长
        if re.search(r'排名|排序|占比', question) and len(_metric_cols) > 1:
            _ratio_cols = [c for c in _metric_cols if any(k in (c.get('label', '') + c.get('key', '')) for k in ('率', '占比', '比例', '比值', 'ratio', 'margin'))]
            if _ratio_cols:
                _metric_cols = _ratio_cols

        def _fmt_val(_v, _label):
            if _v is None:
                return '无数据'
            try:
                _fv = float(_v)
                if '%' in _label or '率' in _label or '比' in _label:
                    return f'{_fv:.2f}%'
                elif abs(_fv) >= 10000:
                    return f'{_fv/10000:.2f}亿元'
                else:
                    return f'{_fv:.2f}万元'
            except (TypeError, ValueError):
                return str(_v)

        _list_parts = []
        _single_metric = len(_metric_cols) == 1
        for _r in rows:
            _name = _r.get('stock_abbr', '')
            _code = _r.get('stock_code', '')
            if _single_metric:
                _v = _r.get(_metric_cols[0]['key'])
                _list_parts.append(f'{_name}（{_code}）{_fmt_val(_v, _metric_cols[0].get("label", ""))}')
            else:
                _vals = [f'{_mc.get("label", _mc["key"])}{_fmt_val(_r.get(_mc["key"]), _mc.get("label", ""))}' for _mc in _metric_cols]
                _list_parts.append(f'{_name}（{_code}）{"、".join(_vals)}')
        _prebuilt_list = '、'.join(_list_parts)
        # 让 LLM 仅加概述，列表已生成
        _batch_list_prompt = (
            '请根据以下结构化信息回答用户问题，直接输出纯文本。\n\n'
            f'用户问题：{question}\n'
            f'查询结果共 {len(rows)} 条。以下是程序已格式化好的完整公司列表，你必须原样包含在回答中，不得省略任何一家：\n\n'
            f'{_prebuilt_list}\n\n'
            '请在列表前加一句简短概述（如"2025年第三季度共有67家公司，按营收从高到低排序如下："），然后直接输出上面的完整列表。'
            '不要使用 Markdown 格式，不要换行分段，不要加emoji。'
        )
        try:
            content = request_llm_text_stream(
                system_prompt, _batch_list_prompt,
                temperature=llm_temperature('analysis_temperature', 0.3),
                on_chunk=(lambda t: emit('content_chunk', t)) if callable(emit) else None,
            )
        except Exception:
            content = f'2025年第三季度共有{len(rows)}家公司数据如下：{_prebuilt_list}'
        content = content.strip()
        if not content:
            content = f'2025年第三季度共有{len(rows)}家公司数据如下：{_prebuilt_list}'
        return {
            'content': content,
            'insights': [],
            'followUpHints': default_follow_up_hints,
        }

    # --- streaming path: emit content_chunk tokens, use plain text mode ---
    if callable(emit):
        stream_user_prompt = (
            '请根据以下结构化信息回答用户问题，直接输出纯文本（不要输出 JSON）。\n\n'
            f'{content_instructions}\n\n'
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
                temperature=llm_temperature('analysis_temperature', 0.3),
                on_chunk=_on_chunk,
            )
        except Exception as exc:
            print(f'[LLM] analysis stream error: {exc}')
            return unavailable
        pi = content.find('📎')
        if pi > 0:
            content = content[:pi].rstrip()
        content = _post_process_kb_answer(content, references or []) if kb_context else _strip_kb_source_tail(content)
        if not content:
            return unavailable
        return {
            'content': content,
            'insights': [],
            'followUpHints': default_follow_up_hints,
        }
    # --- non-streaming path: original JSON mode ---
    user_prompt = (
        '请根据以下结构化信息回答用户问题，返回 JSON 对象。\n\n'
        '返回格式：{"content":"...","followUpHints":["..."]}\n\n'
        '字段要求：\n'
        f'1. content：{content_instructions}\n'
        '2. followUpHints：3条追问建议，每条含公司名、指标名或报告期，可直接点击发送。\n'
        '3. 仅返回 JSON，不输出其他任何文字。\n\n'
        f'{data_block}'
    )
    try:
        payload = request_llm_json(system_prompt, user_prompt, temperature=llm_temperature('analysis_temperature', 0.3))
    except Exception:
        return unavailable
    raw_content = payload.get('content', '')
    if isinstance(raw_content, dict):
        parts = []
        for k, v in raw_content.items():
            if v:
                parts.append(f'{k}\n{str(v).strip()}')
        raw_content = '\n\n'.join(parts)
    content = str(raw_content).strip()
    # Strip 📎 section (shown in attribution panel)
    pi = content.find('📎')
    if pi > 0:
        content = content[:pi].rstrip()
    content = _post_process_kb_answer(content, references or []) if kb_context else _strip_kb_source_tail(content)
    if not content:
        return unavailable
    return {
        'content': content,
        'insights': [],
        'followUpHints': _normalize_text_list(payload.get('followUpHints') or payload.get('follow_up_hints'), default_follow_up_hints, 3)
    }


def _build_general_llm_reply(question: str, history: list[dict[str, Any]], kb_context: str = '', references: list[dict[str, Any]] | None = None, emit=None, render_mode: str = RENDER_MODE_CHAT) -> dict[str, Any]:
    default_follow_up_hints = ['最近一个完整年度利润最高的前十家企业有哪些？', '营业总收入同比增长最快的企业有哪些？', '经营活动现金流表现较好的企业有哪些？']
    target_length = _requested_reply_length(question)
    is_self_intro = _is_self_intro_question(question)
    fallback = {
        'content': _general_fallback_content(question, target_length),
        'insights': [],
        'followUpHints': default_follow_up_hints
    }
    if _is_small_talk_question(question):
        kb_context = ''
    if is_self_intro:
        return {
            'content': fallback['content'],
            'insights': [],
            'followUpHints': default_follow_up_hints
        }
    if not llm_available():
        return fallback

    # ---- build system_prompt & content_hint for both paths ----
    if kb_context:
        _FINANCIAL_TOPIC_RE = re.compile(r'(收入|营收|利润|净利|毛利|盈利|亏损|成本|费用|资产|负债|现金流|营业|财报|财务|报表|趋势|同比|环比|增长|增速|ROE|ROA|EPS|市盈率)')
        # 仅当问题明确提及某家公司时，才从DB补充该公司的财务数据
        _q_has_company = bool(resolve_company_alias(question))
        if _FINANCIAL_TOPIC_RE.search(question) and _q_has_company:
            db_context = _fetch_db_context_for_analysis(question, history)
        else:
            db_context = ''
        system_prompt = (
            '你是一名专业的A股财务分析助手，用自然、连贯的方式回答问题。\n'
            '回答规则：\n'
            '1. 优先依据提供的数据库查询结果和知识库参考资料，不得编造数字或事实。\n'
            '2. 参考资料可能包含从 PDF 解析的表格，表格数据会以换行分隔的序列形式出现，你必须仔细解读这些数据。\n'
            '3. 不要使用固定分节框架或emoji标题（如"📊 数据概览""📈 趋势分析"等），像和朋友聊天一样自然叙述。\n'
            '4. 事实性问题直接列出答案，逐条完整列出，禁止用"等"省略。分析性问题自然展开讨论。\n'
            '5. 在文字中嵌入实际数值（万元/元/%），避免空洞表述。\n'
            '6. 语言简洁、专业、自然，使用中文。\n'
            '7. 当参考资料不足以完整回答用户问题时（如用户问的信息在现有资料中未涵盖、或数据不完整），'
            '必须在回答中主动说明局限性，不要假装信息是完整的。'
        )
        content_hint = (
            '直接回答用户的问题，用自然连贯的语言，不要使用固定框架或emoji标题。\n'
            '- 请仔细阅读下方参考资料中的每一个数字和细节。如果参考资料中包含答案，绝对不能说"未提及"或"未找到"。\n'
            '- 事实性提问（如"有哪些"）：综合所有参考资料，逐条编号列出每一项，不得遗漏，严禁用"等"省略。\n'
            '- 当参考资料包含编号表格时，以表格行数为准确定条目总数，每行单独列为一条，不得合并。\n'
            '- 分析性问题：可用 **小标题** 独占一行来组织内容，标题名称根据实际内容自主拟定，不要用固定模板。\n'
            '- 关键数值和核心结论用 **加粗** 标记，让重点一目了然。\n'
            '- 当有多个并列要点时，用编号列表自然呈现。段落间用空行分隔。\n'
            '- 整体字数150~600字，不使用 ## 和 HTML 标签，不使用emoji。可以使用 **加粗**。'
        )
        data_block = (
            f'用户问题：{question}\n'
            f'最近对话：{json.dumps(_recent_history(history), ensure_ascii=False)}\n'
            + (f'\n数据库结构化数据（用于量化分析）：\n{db_context}\n' if db_context else '')
            + f'\n研报知识库参考（必须引用）：\n{kb_context}\n'
            '【引用规则——必须遵守】\n'
            '1. 上方每条研报资料都已经预先分配了 [参考N] 编号，后续引用时只能使用这些编号。\n'
            '2. 你的回答中必须结合研报知识库的观点或分析，主动引用研报观点，不要忽略这些参考资料。\n'
            '3. 每次引用研报内容时，必须在对应句末直接标注 [参考N]（N 为上方参考编号），例如"机构预计2025年净利润增长15%[参考2]"。\n'
            '4. 至少引用1条研报参考。如果研报中有相关分析、预测或评级，务必纳入你的回答。\n'
            '5. 禁止在文末单独列出"数据来源""参考文献""参考资料"等段落，[参考N]必须自然嵌入分析正文句末。\n'
        )
    else:
        if is_self_intro:
            length_hint = f'尽量控制在{target_length}字左右（允许上下浮动15字）' if target_length else '控制在80到140字'
            system_prompt = '你是财务报表数据库的智能助手。用户当前在询问你的身份、能力范围或使用方式。请先直接回答用户的问题本身，不要套用固定的财务分析模板，不要输出与问题无关的数据库表名、行业结论或研报内容。请用中文回答。'
            content_hint = (
                f'必须先完成自我介绍或能力说明，不要只把用户引导回提问模板。{length_hint}，语言自然，像产品在介绍自己。'
                '如果用户问"你"，默认介绍当前助手本身，不要介绍公司、行业或数据库表。'
            )
        else:
            system_prompt = (
                '你是一个智能助手，主要擅长上市公司财报数据分析，但也能回答其他领域的通用问题。\n'
                '规则：\n'
                '1. 无论用户问什么话题，都要直接、认真地回答，不要拒绝或说"我只能回答财务问题"。\n'
                '2. 如果问题与财务无关，就像一个知识渊博的助手一样正常作答。\n'
                '3. 如果问题与财务相关但当前没有数据，可以基于通用财务知识给出分析思路或建议。\n'
                '4. 重要：如果用户的问题很短（如"为什么""然后呢""继续""还有吗"），这是对前一轮对话的追问，你必须结合"最近对话"中的上下文来理解用户意图并回答。\n'
                '5. 语言自然、有帮助，使用中文。'
            )
            content_hint = (
                '直接回答用户问题。如果用户的问题很短或模糊（如"为什么""说说看""继续"），必须根据下方"最近对话"中的上下文来理解用户在问什么，不要把追问当成独立问题。'
                '内容充实、有帮助，不要敷衍。如果是通用问题就正常回答；如果是财务分析类问题，可以给出分析框架和思路。'
            )
        data_block = (
            f'用户问题：{question}\n'
            f'最近对话：{json.dumps(_recent_history(history), ensure_ascii=False)}\n'
        )

    # override content_hint for batch export mode
    if render_mode == RENDER_MODE_BATCH:
        _is_validation_query = bool(re.search(r'(验证|校验|核实|核对|是否一致|是否相符|是否匹配)', question))
        if _is_validation_query:
            content_hint = (
                '这是一个数据校验/公式核对题，请按照以下格式回答：\n'
                '1. 先说明校验公式是什么（如"营业利润 = 营业总收入 - 营业总支出 + 其他收益等"）。\n'
                '2. 列出每家公司的校验结果：公司名称、报表值、公式推导值、差异值。\n'
                '3. 最后给出结论：几家一致、几家存在差异、差异原因可能是什么。\n'
                '4. 用纯文本，不要 Markdown 格式。数字保留2位小数，以万元/亿元表达。\n'
                '5. 不要插入参考标注（如[参考N]）。不要加emoji。'
            )
        else:
            content_hint = (
                '请用简洁的纯文本回答用户的问题，输出将直接写入 Excel 单元格。具体要求：\n'
                '1. 直接给出核心数据和结论，不要使用任何 Markdown 格式（禁止 **加粗**、## 标题、- 列表等）。\n'
                '2. 用连续的自然语句回答，不要分段、不要换行、不要编号列表。\n'
                '3. 数字以万元/亿元/%表达，保留2位小数。\n'
                '4. 不要插入参考标注（如[参考N]）。\n'
                '5. 整体字数控制在80~200字以内，一段话说完。\n'
                '6. 不要加emoji，不要使用 HTML 标签。'
            )

    # factual listing questions need very low temperature for completeness
    if kb_context and _LISTING_QUESTION_RE.search(question):
        temp = 0.1
    elif kb_context:
        temp = llm_temperature('analysis_temperature', 0.3)
    else:
        temp = llm_temperature('general_reply_temperature', 0.4)

    # ---- streaming path: emit content_chunk tokens ----
    if callable(emit):
        stream_user_prompt = (
            '请根据以下信息回答用户问题，直接输出纯文本（不要输出 JSON）。\n\n'
            f'要求：{content_hint}\n\n'
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
                temperature=temp, on_chunk=_on_chunk,
            )
        except Exception as exc:
            print(f'[LLM] general reply stream error: {exc}')
            return fallback
        content = _post_process_kb_answer(content, references or []) if kb_context else _strip_kb_source_tail(content)
        if not content:
            return fallback
        return {
            'content': content,
            'insights': [],
            'followUpHints': default_follow_up_hints,
        }

    # ---- non-streaming path: original JSON mode ----
    user_prompt = (
        '请根据以下信息回答用户问题，返回 JSON 对象。\n\n'
        '返回格式：{"content":"...","followUpHints":["..."]}\n\n'
        '字段要求：\n'
        f'1. content：{content_hint}\n'
        '2. followUpHints：3条后续追问，每条必须包含具体公司名、指标名或报告期。\n'
        '3. 仅返回 JSON，不输出其他文字。\n\n'
        f'{data_block}'
    )
    try:
        payload = request_llm_json(system_prompt, user_prompt, temperature=temp)
    except Exception:
        return fallback
    content = str(payload.get('content', '')).strip()
    content = _post_process_kb_answer(content, references or []) if kb_context else _strip_kb_source_tail(content)
    if not content:
        return fallback
    if is_self_intro and target_length:
        plain_length = len(re.sub(r'\s+', '', content))
        if plain_length < max(40, target_length - 25):
            content = fallback['content']
    return {
        'content': content,
        'insights': [],
        'followUpHints': _normalize_text_list(payload.get('followUpHints') or payload.get('follow_up_hints'), default_follow_up_hints, 3)
    }


def _build_smart_clarify_reply(
    question: str,
    history: list[dict[str, Any]],
    understood: dict[str, str],
    missing: str,
    options: list[str],
    source_tables: list[str],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    understood_desc = '、'.join(f'{k}={v}' for k, v in understood.items() if v and v not in ('None', ''))
    fallback_content = (
        f'已理解你的查询（{understood_desc}），还需要补充【{missing}】。'
        + (f'可选：{"、".join(options[:5])}' if options else '')
    )
    base = {
        'id': create_id(), 'role': 'assistant',
        'clarifyMissing': missing,
        'clarifyOptions': options[:8],
        'followUpHints': options[:4],
        'sourceTables': source_tables,
        'status': 'clarify',
        'meta': meta or {},
    }
    if not llm_available():
        content = fallback_content
    else:
        recent_text = json.dumps(_recent_history(history), ensure_ascii=False)
        system_prompt = (
            '你是一名善于沟通的财务助手。用户的查询信息不完整，请用一句自然、友好的中文提问，引导用户补充缺失的信息。'
            '要求：①直接问，不要解释系统逻辑；②如有可选项，从中举2-3个具体例子作为提示；③不超过60字；④只返回纯文字，无JSON。'
        )
        user_prompt = (
            f'用户原始问题：{question}\n'
            f'已识别信息：{understood_desc}\n'
            f'缺失字段：{missing}\n'
            f'数据库中的可选值：{"、".join(options[:6]) if options else "暂无"}\n'
            f'最近对话：{recent_text}\n'
            '请生成一句自然的澄清问题：'
        )
        try:
            from .llm import request_llm_text
            content = request_llm_text(system_prompt, user_prompt, temperature=0.5)
            if not content:
                content = fallback_content
        except Exception:
            content = fallback_content
    table_labels = [TABLE_LABELS.get(t, t) for t in source_tables]
    attribution = {
        'steps': [
            {'label': '意图理解', 'status': 'done', 'detail': understood_desc or '已识别部分查询条件'},
            {'label': '信息校验', 'status': 'done', 'detail': f'当前仍缺少【{missing}】'},
            {'label': '澄清提问', 'status': 'done', 'detail': '补全缺失条件后即可继续检索与分析'},
        ],
        'sql': '',
        'kb_sources': [],
        'data_summary': {
            'tables': source_tables,
            'table_labels': table_labels,
            'row_count': 0,
            'columns': list(understood.keys()),
        },
        'thinking_map': _build_clarify_thinking_map(question, understood, missing, options, source_tables),
    }
    return {**base, 'content': content, 'attribution': attribution}


def _build_thinking_map(question: str, sql: str, columns: list[dict], rows: list[dict], source_tables: list[str], table_labels: list[str], col_labels: list[str], refs: list[dict], analysis: dict) -> dict:
    """构建类似秘塔 AI 风格的思维导图节点树——展示真实推理过程而非抽象流程。"""

    company = ''
    periods = []
    key_values = []
    for r in rows[:12]:
        p = str(r.get('report_period', r.get('报告期', '')))
        if p and p not in periods:
            periods.append(p)
        if not company:
            company = str(r.get('stock_name', r.get('company_name', '')))
    periods_str = '、'.join(periods[:4]) + ('等' if len(periods) > 4 else '')
    focus_metrics = '、'.join(col_labels[:3]) + ('等' if len(col_labels) > 3 else '') if col_labels else '关键指标'

    numeric_cols = [
        c for c in columns
        if c.get('key') and any(isinstance(r.get(c.get('key', '')), (int, float)) for r in rows[:8])
    ]
    for nc in numeric_cols[:2]:
        k = nc.get('key', '')
        label = nc.get('label', k)
        vals = [r.get(k) for r in rows if r.get(k) is not None]
        if vals:
            key_values.append(
                f'{label}: {_format_node_value(vals[0])}' + (f' → {_format_node_value(vals[-1])}' if len(vals) > 1 else '')
            )

    data_children = []
    for nc in numeric_cols[:3]:
        k = nc.get('key', '')
        label = nc.get('label', k)
        vals = [r.get(k) for r in rows if r.get(k) is not None]
        if vals:
            data_children.append({
                'id': f'metric_{k}',
                'label': f'{company}{label}是多少？' if company else f'{label}数据查询',
                'detail': _describe_metric_detail(label, vals, periods),
                'type': 'data',
                'edge_label': '具体数值',
                'children': [],
            })

    kb_children = []
    for i, r in enumerate(refs[:3]):
        kb_children.append({
            'id': f'kb_{i}',
            'label': _one_line_snippet(str(r.get('doc_name', '参考资料')), 25),
            'detail': _describe_reference_detail(r),
            'type': 'source',
            'edge_label': r.get('doc_type', '研报'),
            'children': [],
        })

    intent_parts = [f'先对问题「{_one_line_snippet(question, 26)}」做要素拆解']
    if company:
        intent_parts.append(f'识别到主体是 {company}')
    if col_labels:
        intent_parts.append(f'重点关注 {focus_metrics}')
    if table_labels:
        intent_parts.append(f'优先查询 {"、".join(table_labels[:2])}')
    intent_detail = '；'.join(intent_parts) + '。'

    sql_detail_parts = [
        f'先生成结构化查询，在 {"、".join(table_labels) if table_labels else "相关数据表"} 中检索到 {len(rows)} 条记录'
    ]
    if periods_str:
        sql_detail_parts.append(f'覆盖 {periods_str}')
    if key_values:
        sql_detail_parts.append(f'关键读数包括 {"；".join(key_values[:2])}')
    if sql:
        sql_detail_parts.append(f'SQL 条件主要围绕：{_one_line_snippet(sql, 96)}')

    sql_branch = {
        'id': 'sql_query',
        'label': f'{company}{col_labels[0]}等数据查询' if company and col_labels else '结构化数据查询',
        'detail': '。'.join(part.rstrip('。') for part in sql_detail_parts if part) + '。',
        'type': 'process',
        'edge_label': '数据检索',
        'children': data_children[:3],
    }

    children_of_intent = [sql_branch]
    if refs:
        children_of_intent.append({
            'id': 'kb_search',
            'label': f'研报知识库检索',
            'detail': f'同步命中 {len(refs)} 条外部参考，用来补充行业背景、政策影响或机构观点，避免只依赖结构化数据下结论。',
            'type': 'knowledge',
            'edge_label': '知识增强',
            'children': kb_children,
        })

    insights_list = analysis.get('insights', [])
    insight_text = _one_line_snippet('；'.join(str(item) for item in insights_list[:2]), 108)
    synthesis_detail = (
        f'把 {len(rows)} 条结构化数据' + (f' 与 {len(refs)} 条知识证据' if refs else '') +
        '放在一起交叉判断，优先保留能直接回答问题的结论与依据。'
    )
    if insight_text:
        synthesis_detail += f' 当前形成的关键判断是：{insight_text}'

    output_detail = '最终回答会先直接回应问题，再补充最关键的数据或证据依据。'
    if insight_text:
        output_detail += f' 当前准备输出的核心内容是：{insight_text}'
    elif key_values:
        output_detail += f' 当前最关键的读数包括：{"；".join(key_values[:2])}。'

    synthesis_children = [
        {
            'id': 'output',
            'label': '最终回答',
            'detail': output_detail,
            'type': 'result',
            'edge_label': '输出',
            'children': [],
        },
    ]

    return {
        'id': 'root',
        'label': question[:40] + ('…' if len(question) > 40 else ''),
        'detail': f'系统需要围绕 {company or "目标对象"} 的 {focus_metrics} 回答这个问题，并判断哪些数据与外部证据最能直接支撑结论。',
        'type': 'question',
        'children': [
            {
                'id': 'intent',
                'label': '意图理解',
                'detail': intent_detail,
                'type': 'process',
                'edge_label': '分析',
                'children': children_of_intent,
            },
            {
                'id': 'synthesis',
                'label': 'AI 综合推理',
                'detail': synthesis_detail,
                'type': 'synthesis',
                'edge_label': '推理',
                'children': synthesis_children,
            },
        ],
    }


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


def _describe_metric_detail(label: str, values: list[Any], periods: list[str]) -> str:
    numeric_values = [float(v) for v in values if isinstance(v, (int, float))]
    period_desc = ''
    if periods:
        period_desc = f'覆盖 {periods[0]}' + (f' 至 {periods[-1]}' if len(periods) > 1 else '')
    if not numeric_values:
        return f'{period_desc + "，" if period_desc else ""}围绕 {label} 提取到了 {len(values)} 个可用结果，用于核对具体数值。'
    if len(numeric_values) == 1:
        return f'{period_desc + "，" if period_desc else ""}{label}提取到 1 个有效值，为 {_format_node_value(numeric_values[0])}，这是回答中直接引用的数据依据。'
    first, last = numeric_values[0], numeric_values[-1]
    if last > first:
        trend = '整体呈上升'
    elif last < first:
        trend = '整体呈下降'
    else:
        trend = '整体基本持平'
    return (
        f'{period_desc + "，" if period_desc else ""}共提取 {len(numeric_values)} 个有效值，'
        f'{label}由 {_format_node_value(first)} 变化到 {_format_node_value(last)}，{trend}，这是后续判断结论时直接使用的数据依据。'
    )


def _describe_reference_detail(ref: dict[str, Any]) -> str:
    doc_name = _one_line_snippet(str(ref.get('doc_name', '参考资料')), 28) or '参考资料'
    doc_type = str(ref.get('doc_type', '研报')).strip() or '研报'
    snippet = _one_line_snippet(str(ref.get('text', '') or ''), 96)
    if snippet:
        return f'这份{doc_type}《{doc_name}》提供了与当前问题直接相关的证据片段：{snippet}'
    return f'这份{doc_type}《{doc_name}》用于补充行业背景、政策口径或机构观点。'


def _build_clarify_thinking_map(question: str, understood: dict[str, str], missing: str, options: list[str], source_tables: list[str]) -> dict:
    understood_desc = '；'.join(f'{k}={v}' for k, v in understood.items() if v and v not in ('None', ''))
    table_labels = [TABLE_LABELS.get(t, t) for t in source_tables]
    option_children = [
        {
            'id': f'option_{i}',
            'label': _one_line_snippet(opt, 24),
            'detail': f'如果补充为「{_one_line_snippet(opt, 30)}」，系统就能据此缩小【{missing}】的取值范围，继续后续查询。',
            'type': 'data',
            'edge_label': '示例',
            'children': [],
        }
        for i, opt in enumerate(options[:3])
    ]
    return {
        'id': 'root',
        'label': question[:40] + ('…' if len(question) > 40 else ''),
        'detail': f'当前问题已经识别出部分查询条件，但因为缺少【{missing}】，暂时还不能生成足够精确的检索路径。',
        'type': 'question',
        'children': [
            {
                'id': 'intent',
                'label': '意图理解',
                'detail': (understood_desc + '；已能初步判断查询方向，但还不足以直接生成精确查询。').lstrip('；') if understood_desc else '已识别出部分查询条件，仍需补全关键信息后才能继续执行。',
                'type': 'process',
                'edge_label': '分析',
                'children': [
                    {
                        'id': 'scope',
                        'label': '查询范围',
                        'detail': f'当前预计从 {"、".join(table_labels) if table_labels else "候选数据表"} 中继续检索，但必须先确认缺失条件后才能锁定具体记录范围。',
                        'type': 'data',
                        'edge_label': '范围',
                        'children': [],
                    },
                ],
            },
            {
                'id': 'validation',
                'label': '信息校验',
                'detail': f'检查发现当前仍缺少【{missing}】这一关键条件；如果直接继续执行，查询范围会过宽，结果也可能不准确。',
                'type': 'process',
                'edge_label': '校验',
                'children': option_children,
            },
            {
                'id': 'clarify',
                'label': '澄清提问',
                'detail': f'因此系统会先向你补问【{missing}】，确认后再继续进入数据检索与回答生成。',
                'type': 'result',
                'edge_label': '输出',
                'children': [],
            },
        ],
    }


def _build_general_thinking_map(question: str, refs: list[dict[str, Any]], content: str, query_kind: str = 'general') -> dict:
    kb_children = [
        {
            'id': f'kb_{i}',
            'label': _one_line_snippet(str(ref.get('doc_name', '参考资料')), 26),
            'detail': _describe_reference_detail(ref),
            'type': 'source',
            'edge_label': str(ref.get('doc_type', '研报')) or '研报',
            'children': [],
        }
        for i, ref in enumerate(refs[:3])
    ]
    intent_detail = '识别为知识增强问答，优先结合研报和上下文组织回答。' if refs else (
        '识别为通用问答，基于上下文直接组织回复。' if query_kind == 'general' else f'识别为{query_kind}类问题，按非结构化问答路径处理。'
    )
    children = [
        {
            'id': 'intent',
            'label': '意图理解',
            'detail': intent_detail + (' 这类问题更依赖文本语境与知识证据，而不是直接生成结构化 SQL。' if refs else ' 这类问题优先基于上下文直接组织自然语言回答。'),
            'type': 'process',
            'edge_label': '分析',
            'children': [],
        },
    ]
    if refs:
        children.append({
            'id': 'kb_search',
            'label': '知识参考检索',
            'detail': f'命中 {len(refs)} 条相关参考，系统会优先抽取其中能直接支撑回答的原文片段，再用来补强结论与背景说明。',
            'type': 'knowledge',
            'edge_label': '检索',
            'children': kb_children,
        })
    children.append({
        'id': 'synthesis',
        'label': '回答组织',
        'detail': '先对齐当前问题、上下文与参考证据，再组织成自然语言回答，避免只机械复述单条资料。',
        'type': 'synthesis',
        'edge_label': '推理',
        'children': [
            {
                'id': 'output',
                'label': '最终回答',
                'detail': f'最终回答会先给出直接结论，再补充最关键的解释与依据。摘要：{_one_line_snippet(content, 110) or "生成最终回答。"}',
                'type': 'result',
                'edge_label': '输出',
                'children': [],
            },
        ],
    })
    return {
        'id': 'root',
        'label': question[:40] + ('…' if len(question) > 40 else ''),
        'detail': '系统会先判断这个问题是否需要引入外部知识或上下文证据，再决定如何组织最终回答。',
        'type': 'question',
        'children': children,
    }


def _build_done_reply(question: str, history: list[dict[str, Any]], sql: str, chart: dict[str, Any] | None, columns: list[dict[str, str]], rows: list[dict[str, Any]], source_tables: list[str], analysis_scene: str, meta: dict[str, Any] | None = None, kb_context: str = '', references: list[dict[str, Any]] | None = None, emit=None, deep_think: bool = True, data_coverage_note: str = '', render_mode: str = RENDER_MODE_CHAT) -> dict[str, Any]:
    def _emit(label: str, status: str, detail: str = ''):
        if callable(emit):
            try:
                emit('step', {'label': label, 'status': status, 'detail': detail})
            except Exception:
                pass
    def _think(text: str):
        if not deep_think:
            return
        if callable(emit):
            try:
                emit('thinking', text)
            except Exception:
                pass
    chart = auto_generate_chart_payload(
        question,
        columns,
        rows,
        title=str(chart.get('title', '')) if isinstance(chart, dict) else '',
        analysis_mode=str((meta or {}).get('analysis_mode', analysis_scene)),
        existing_chart=chart,
    )
    table_desc = '、'.join(TABLE_LABELS.get(t, t) for t in source_tables)
    _emit('数据库查询', 'running', f'正在查询 {table_desc}…')
    _emit('数据库查询', 'done', f'查询完成，返回 {len(rows)} 条数据')
    if rows:
        # generate data-insight thinking
        col_names = [c.get('label', c.get('key', '')) for c in columns[:5]]
        if len(rows) >= 5:
            _think(f'获取到 {len(rows)} 条数据，时间跨度较大，可以较好地反映整体情况。')
        elif len(rows) >= 2:
            _think(f'获取到 {len(rows)} 条数据记录，虽然不算多但足够进行分析。')
        # try to extract a data insight from the actual values
        numeric_cols = [c for c in columns if any(isinstance(rows[0].get(c.get('key','')), (int, float)) for _ in [1])]
        if numeric_cols and len(rows) >= 2:
            key0 = numeric_cols[0].get('key', '')
            label0 = numeric_cols[0].get('label', key0)
            vals = [r.get(key0) for r in rows if r.get(key0) is not None and isinstance(r.get(key0), (int, float))]
            if len(vals) >= 2:
                if vals[-1] > vals[0]:
                    _think(f'初步观察，{label0}呈现增长态势，接下来分析具体的变化幅度和原因。')
                elif vals[-1] < vals[0]:
                    _think(f'初步观察，{label0}有所下降，需要进一步分析背后的驱动因素。')
                else:
                    _think(f'{label0}整体比较平稳，重点关注各期之间是否有结构性变化。')
    else:
        _think('暂未查到符合条件的数据，尝试调整分析维度。')
    if callable(emit):
        if chart:
            try:
                emit('chart', chart)
            except Exception:
                pass
        if rows and columns:
            try:
                emit('table', {'columns': columns, 'rows': rows[:200]})
            except Exception:
                pass
    _think('综合数据表现和行业背景，形成分析结论。')
    _emit('AI 分析', 'running', '正在进行多维度财务分析…')
    analysis = _build_llm_analysis(
        question, history, sql, columns, rows, source_tables, chart, analysis_scene,
        kb_context, references, emit=emit, deep_think=deep_think, data_coverage_note=data_coverage_note,
        render_mode=render_mode,
    )
    _emit('AI 分析', 'done', f'分析完成，生成 {len(analysis["content"])} 字结构化报告')
    refs = references or []
    # ── 查询数据溯源记录 ────────────────────────────────────────────────────
    provenance_list: list[dict[str, Any]] = []
    _stock_code = ''
    try:
        _meta = meta or {}
        _stock_map = CONFIG.get('stock_abbr_to_code', {})
        _select_cols = (
            'SELECT id, stock_code, report_period, table_name, field_name, '
            'field_value, source_pdf AS source_file, page_no AS source_page, '
            'row_label AS source_table_name, raw_text AS source_text, '
            'confidence, extract_method FROM field_provenance'
        )
        # collect (stock_code → set[period]) pairs from meta + rows
        _prov_pairs: dict[str, set[str]] = {}
        _prov_company = _meta.get('company', '')
        if _prov_company:
            _sc = _stock_map.get(_prov_company, '')
            if _sc:
                _stock_code = _sc
                _prov_pairs.setdefault(_sc, set())
                _mp = _meta.get('report_period', '')
                if _mp:
                    _prov_pairs[_sc].add(_mp)
        # also extract from result rows (critical for ranking / multi-company queries)
        if rows:
            for _row in rows:
                _abbr = str(_row.get('stock_abbr', '')).strip()
                _sc = _stock_map.get(_abbr, '')
                if _sc:
                    if not _stock_code:
                        _stock_code = _sc
                    _prov_pairs.setdefault(_sc, set())
                # 趋势查询等场景：rows 可能没有 stock_abbr 但有 report_period
                # 此时把 period 添加到已有的 stock_code 上
                _rp = str(_row.get('report_period', '')).strip()
                if _rp:
                    _target_sc = _sc or _stock_code
                    if _target_sc and _target_sc in _prov_pairs:
                        _prov_pairs[_target_sc].add(_rp)
        # query provenance for all collected stock codes
        _seen: dict[tuple[str, str, str], dict[str, Any]] = {}
        for _sc, _periods in _prov_pairs.items():
            if _periods:
                _ph = ','.join(['?'] * len(_periods))
                _prov_sql = f'{_select_cols} WHERE stock_code = ? AND report_period IN ({_ph}) ORDER BY page_no ASC'
                _prov_rows = query_rows(_prov_sql, tuple([_sc] + sorted(_periods)))
            else:
                _prov_sql = f'{_select_cols} WHERE stock_code = ? ORDER BY page_no ASC LIMIT 200'
                _prov_rows = query_rows(_prov_sql, (_sc,))
            for pr in _prov_rows:
                _key = (pr.get('stock_code', ''), pr.get('report_period', ''), pr.get('field_name', ''))
                _existing = _seen.get(_key)
                if _existing is None or (pr.get('confidence', 0) or 0) >= (_existing.get('confidence', 0) or 0):
                    _seen[_key] = pr
        provenance_list = list(_seen.values())
    except Exception:
        pass
    table_labels = [TABLE_LABELS.get(t, t) for t in source_tables]
    col_labels = [c.get('label', c.get('key', '')) for c in columns]
    attribution = {
        'steps': [
            {
                'label': '知识检索',
                'status': 'done',
                'detail': f'从研报知识库检索到 {len(refs)} 条相关文献片段' if refs else '未命中知识库，基于结构化数据作答',
            },
            {
                'label': 'SQL 生成',
                'status': 'done',
                'detail': f'查询 {", ".join(table_labels)}，返回 {len(rows)} 行 × {len(columns)} 列数据',
            },
            {
                'label': '回答生成',
                'status': 'done',
                'detail': f'LLM 基于查询结果与知识库参考生成结构化分析，共 {len(analysis["content"])} 字',
            },
            {
                'label': '归因分析',
                'status': 'done',
                'detail': f'生成推理链路，引用 {len(refs)} 条知识库来源，结论可追溯至原始数据',
            },
        ],
        'sql': sql,
        'kb_sources': refs,
        'data_summary': {
            'tables': source_tables,
            'table_labels': table_labels,
            'row_count': len(rows),
            'columns': col_labels,
        },
        'thinking_map': _build_thinking_map(question, sql, columns, rows, source_tables, table_labels, col_labels, refs, analysis),
    }
    calc_traces: list[dict[str, Any]] = []
    citation_refs: list[dict[str, Any]] = []
    try:
        calc_traces, citation_refs = _build_calc_traces(provenance_list, _stock_code)
    except Exception:
        pass
    return {
        'id': create_id(),
        'role': 'assistant',
        'content': analysis['content'],
        'sql': sql,
        'chart': chart,
        'columns': columns,
        'rows': rows,
        'sourceTables': source_tables,
        'insights': analysis['insights'],
        'followUpHints': analysis['followUpHints'],
        'references': refs,
        'attribution': attribution,
        'provenance': provenance_list,
        'calcTraces': calc_traces,
        'citationRefs': citation_refs,
        'meta': meta or {},
        'status': 'done'
    }


def _find_table_for_column(col: str) -> str:
    """Find which table contains the given column key."""
    for tbl, cols in TABLE_METRIC_COLUMNS.items():
        if col in cols:
            return tbl
    return 'income_sheet'


_RANKING_KEYWORD_MAP = {
    '营收': 'total_operating_revenue',
    '收入': 'total_operating_revenue',
    '营业总收入': 'total_operating_revenue',
    '营业收入': 'total_operating_revenue',
    '净利润': 'net_profit',
    '净利': 'net_profit',
    '利润总额': 'total_profit',
    '利润': 'total_profit',
    '营业利润': 'operating_profit',
    'ROE': 'roe',
    'roe': 'roe',
    '净资产收益率': 'roe',
    '收益率': 'roe',
    '回报率': 'roe',
    '净资产回报率': 'roe',
    '毛利率': 'gross_profit_margin',
    '销售毛利率': 'gross_profit_margin',
    '净利率': 'net_profit_margin',
    '销售净利率': 'net_profit_margin',
    '利润率': 'net_profit_margin',
    '负债率': 'asset_liability_ratio',
    '资产负债率': 'asset_liability_ratio',
    '总资产': 'asset_total_assets',
    '资产规模': 'asset_total_assets',
    '总负债': 'liability_total_liabilities',
    '负债总额': 'liability_total_liabilities',
    'EPS': 'eps',
    'eps': 'eps',
    '每股收益': 'eps',
    '每股净资产': 'net_asset_per_share',
    '研发费用': 'operating_expense_rnd_expenses',
    '研发投入': 'operating_expense_rnd_expenses',
    '销售费用': 'operating_expense_selling_expenses',
    '管理费用': 'operating_expense_administrative_expenses',
    '财务费用': 'operating_expense_financial_expenses',
    '扣非净利润': 'net_profit_excl_non_recurring',
    '扣非净利': 'net_profit_excl_non_recurring',
    '扣非': 'net_profit_excl_non_recurring',
    '营收增长': 'operating_revenue_yoy_growth',
    '营收增速': 'operating_revenue_yoy_growth',
    '收入增速': 'operating_revenue_yoy_growth',
    '利润增速': 'net_profit_yoy_growth',
    '利润增长': 'net_profit_yoy_growth',
    '现金流': 'operating_cf_net_amount',
    '经营现金流': 'operating_cf_net_amount',
    '存货': 'asset_inventory',
    '应收账款': 'asset_accounts_receivable',
    '短期借款': 'liability_short_term_loans',
    '货币资金': 'asset_cash_and_cash_equivalents',
    '应付账款': 'liability_accounts_payable',
    '预收账款': 'liability_advance_from_customers',
    '合同负债': 'liability_contract_liabilities',
    '交易性金融资产': 'asset_trading_financial_assets',
    '总负债': 'liability_total_liabilities',
    '预付款项': 'asset_prepayments',
}


def _match_metric_keywords(compact: str, keyword_map: dict[str, str]) -> list[tuple[str, str]]:
    """Match metric keywords in text using longest-match-first strategy.
    Returns list of (keyword, column) pairs that actually match.
    Prevents short aliases like '净利' from matching when '净利率' already matched.
    """
    # sort by keyword length descending so longer matches take priority
    sorted_kw = sorted(keyword_map.items(), key=lambda x: len(x[0]), reverse=True)
    matched: list[tuple[str, str]] = []
    consumed_positions: set[int] = set()
    for kw, col in sorted_kw:
        idx = compact.find(kw)
        if idx < 0:
            continue
        # check if this keyword's position overlaps with already-matched longer keyword
        positions = set(range(idx, idx + len(kw)))
        if positions & consumed_positions:
            continue
        matched.append((kw, col))
        consumed_positions |= positions
    return matched


def _parse_ranking_request(compact: str, metric: dict) -> tuple[str, str, str, int]:
    """Parse ranking parameters from the question.

    Returns: (column, table, direction, limit)
    """
    # extract limit
    _CN_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    limit_match = re.search(r'(?:top|TOP|前|后)\s*(\d+)', compact)
    # 也匹配 "最大的5家" / "最高的10家" / "N家公司" 等模式
    limit_match_ctx = re.search(r'(?:最大的|最高的|最低的|最小的)\s*(\d+)\s*家', compact) if not limit_match else None
    limit_match_cn = re.search(r'(?:前|后)([一二三四五六七八九十])', compact) if not limit_match and not limit_match_ctx else None
    # 匹配中文数字: "最高的五家" / "最低的三家" / "五家公司" / "十家企业"
    limit_match_ctx_cn = None
    if not limit_match and not limit_match_ctx and not limit_match_cn:
        limit_match_ctx_cn = re.search(r'(?:最大的|最高的|最低的|最小的)\s*([一二三四五六七八九十]+)\s*家', compact)
    if not limit_match and not limit_match_ctx and not limit_match_cn and not limit_match_ctx_cn:
        limit_match_ctx_cn = re.search(r'([一二三四五六七八九十]+)\s*家\s*(?:公司|企业)', compact)
    if limit_match:
        rank_limit = int(limit_match.group(1))
    elif limit_match_ctx:
        rank_limit = int(limit_match_ctx.group(1))
    elif limit_match_cn:
        rank_limit = _CN_NUM.get(limit_match_cn.group(1), 10)
    elif limit_match_ctx_cn:
        _cn_str = limit_match_ctx_cn.group(1)
        rank_limit = _CN_NUM.get(_cn_str, 10) if len(_cn_str) == 1 else 10
    elif re.search(r'所有|哪些|列出|有哪些|筛选|找出', compact):
        rank_limit = 200
    else:
        rank_limit = 200  # 默认不限制，返回全部

    # direction
    direction = 'asc' if re.search(r'(最低|最小|后\d|从低到高)', compact) else 'desc'

    # metric column: prefer already-resolved metric, then keyword match
    rank_col = ''
    if metric and metric.get('column'):
        rank_col = metric['column']
    else:
        for keyword, col in _RANKING_KEYWORD_MAP.items():
            if keyword in compact:
                rank_col = col
                break
    if not rank_col:
        rank_col = 'total_profit'  # default

    # validate column is in ALLOWED_TABLES
    rank_table = _find_table_for_column(rank_col)

    return rank_col, rank_table, direction, rank_limit


def _get_last_assistant_topic(history: list[dict[str, Any]]) -> str:
    """Extract a short topic phrase from the last assistant message for fallback rewrite."""
    for item in reversed(history):
        if str(item.get('role', '')).strip() != 'assistant':
            continue
        meta = item.get('meta')
        if isinstance(meta, dict):
            parts = []
            c = str(meta.get('company', '')).strip()
            if c:
                parts.append(c)
            m = str(meta.get('metric', '')).strip()
            if m and m in METRIC_DEFINITIONS:
                parts.append(METRIC_DEFINITIONS[m]['label'])
            elif m:
                parts.append(m)
            if parts:
                return '、'.join(parts)
        content = str(item.get('content', '')).strip()
        if content:
            return content[:60]
    return ''


def _rewrite_follow_up(question: str, history: list[dict[str, Any]]) -> str:
    """Rewrite a short follow-up question into a self-contained question using LLM."""
    if not llm_available():
        prev = _get_last_assistant_topic(history)
        return f'关于{prev}，{question}' if prev else question
    recent = _recent_history(history, limit=4)
    system_prompt = '你是一个对话改写器。将用户的追问改写为一个自包含的完整问题，只输出改写后的问题，不加任何解释。'
    user_prompt = (
        f'最近对话：{json.dumps(recent, ensure_ascii=False)}\n'
        f'用户追问：{question}\n'
        f'改写后的完整问题：'
    )
    try:
        rewritten = request_llm_text(system_prompt, user_prompt, temperature=0.1)
        return rewritten.strip() or question
    except Exception:
        prev = _get_last_assistant_topic(history)
        return f'关于{prev}，{question}' if prev else question


def _rewrite_query(question: str, history: list[dict[str, Any]]) -> tuple[str, bool]:
    """Rewrite ANY user question into a standalone query using LLM context resolution.

    Returns (rewritten_question, is_new_topic).
    This replaces manual slot inheritance — the LLM decides what context to carry.
    """
    if not history or not llm_available():
        return question, True

    # build alias hint: collect company aliases mentioned in recent history
    _alias_pairs: list[str] = []
    _seen_aliases: set[str] = set()
    _hist_text = ' '.join(str(item.get('content', '')) for item in history[-12:])
    for _a, _off in COMPANY_ALIAS.items():
        if _off in COMPANY_NAMES and _a != _off and _a not in _seen_aliases:
            if _a in _hist_text or _a in question:
                _alias_pairs.append(f'{_a}={_off}')
                _seen_aliases.add(_a)
    _alias_hint = ('\n公司别名参考：' + '，'.join(_alias_pairs[:15]) + '\n') if _alias_pairs else ''

    # build concise history context: user questions + meta + content summary from AI
    context_lines: list[str] = []
    for item in history[-12:]:
        role = str(item.get('role', '')).strip()
        if role == 'user':
            context_lines.append(f'用户：{str(item.get("content", "")).strip()[:200]}')
        elif role == 'assistant':
            meta = item.get('meta')
            content = str(item.get('content', '')).strip()
            content_brief = content.split('\n')[0][:600] if content else ''
            # 从 rows 中提取公司列表，确保改写器能看到所有公司
            _rows_data = item.get('rows') or []
            _company_list = []
            if _rows_data and isinstance(_rows_data, list):
                for _rr in _rows_data:
                    _abbr = str(_rr.get('stock_abbr', '')).strip()
                    _code = str(_rr.get('stock_code', '')).strip()
                    if _abbr and _abbr not in _company_list:
                        _company_list.append(f'{_abbr}({_code})' if _code else _abbr)
            _company_hint = f' [涉及公司：{"、".join(_company_list)}]' if _company_list else ''
            if isinstance(meta, dict):
                parts = []
                c = str(meta.get('company', '')).strip()
                if c and c != 'None':
                    parts.append(f'公司={c}')
                m = str(meta.get('metric', '')).strip()
                if m:
                    parts.append(f'指标={m}')
                p = str(meta.get('report_period', '')).strip()
                if p:
                    parts.append(f'报告期={p}')
                qk = str(meta.get('query_kind', '')).strip()
                if qk:
                    parts.append(f'类型={qk}')
                intent = str(meta.get('intent', '')).strip()
                if intent:
                    parts.append(f'改写问题={intent[:60]}')
                if parts:
                    meta_str = ', '.join(parts)
                    context_lines.append(f'助手：{content_brief}{_company_hint} [{meta_str}]' if content_brief else f'助手回复摘要：{_company_hint} [{meta_str}]')
                    continue
            if content_brief:
                context_lines.append(f'助手：{content_brief}{_company_hint}')

    context_str = '\n'.join(context_lines[-10:])

    system_prompt = (
        '你是一个智能问题改写器。根据对话历史，将用户的当前问题改写为一个**独立完整**的问题。\n'
        '规则：\n'
        '1. 如果当前问题本身已经完整（包含明确的公司名、指标或查询意图），直接原样输出，不要添加历史中的公司或指标。\n'
        '2. 如果当前问题是对前文的追问或省略了主语（如"那营业收入呢""为什么""继续""它的现金流呢"），必须从历史中补全公司名、报告期等缺失信息。\n'
        '3. 如果当前问题包含代词（"它""这家公司""该公司""上面""这些"），必须替换为具体的公司名或实体。\n'
        '4. **关键规则**：如果当前问题引用了一组公司（如"这五家公司""它们""这些公司""上述公司""排名前五的公司"），'
        '必须从助手上一轮回复中找到**所有**对应的公司名，**全部列出**。'
        '例如助手说"排名前五为白云山、云南白药、华润三九、昆药集团、同仁堂"，用户追问"它们的净利润"，'
        '改写为"白云山、云南白药、华润三九、昆药集团、同仁堂的2025年第三季度净利润分别是多少"。\n'
        '5. 如果当前问题虽然简短但含有新的指标/主题词且没有代词，优先视为延续最近一轮讨论的同一公司。\n'
        '6. 如果用户明确提到了新的公司名，这是话题切换，不要混入历史中其他公司的信息。\n'
        '7. 当历史中涉及多家公司时，延续最近一轮用户提问所涉及的公司。\n'
        '8. 只输出改写后的问题，不加解释。如果无需改写就原样输出。\n'
        '9. 在问题末尾另起一行输出 [NEW] 或 [CONTINUE]，表示这是一个全新话题还是对前文的延续。\n'
        '10. **行业/板块/市场级问题禁止携带公司名**：如果当前问题是关于整个行业、板块、市场或所有公司的'
        '（如"行业整体营收情况""板块走势""市场表现""医药行业龙头"），'
        '即使历史中讨论了某家公司，也**不要**在改写中添加任何公司名。这类问题应标记为 [NEW]。'
    )
    user_prompt = (
        f'对话历史：\n{context_str}\n'
        f'{_alias_hint}'
        f'\n当前问题：{question}\n'
        f'改写后：'
    )
    try:
        result = request_llm_text(system_prompt, user_prompt, temperature=0.1).strip()
        # parse [NEW] / [CONTINUE] tag (case-insensitive, whitespace-tolerant)
        is_new = True
        _tag_re = re.search(r'\[\s*(NEW|CONTINUE)\s*\]', result, re.IGNORECASE)
        if _tag_re:
            is_new = _tag_re.group(1).upper() != 'CONTINUE'
            result = result[:_tag_re.start()] + result[_tag_re.end():]
            result = result.strip()
        return result or question, is_new
    except Exception:
        return question, True


def _preprocess(question: str, history: list[dict[str, Any]]) -> DialogueState:
    """Layer 0: detect small talk, topic switch, rewrite follow-ups, compress history."""

    # ⓪ hierarchical history compression (3-tier sliding window)
    # Tier 1 (Working Memory): last 6 messages verbatim
    # Tier 2 (Short-term):     messages 7~16 → meta-enriched compact lines
    # Tier 3 (Long-term):      messages 17+ → LLM summary paragraph
    _TIER1_COUNT = 6   # recent verbatim
    _TIER2_COUNT = 10  # mid-range compact
    if len(history) > _TIER1_COUNT:
        tier1 = history[-_TIER1_COUNT:]
        older = history[:-_TIER1_COUNT]

        # Tier 2: compact meta representation of mid-range turns
        tier2_items = older[-_TIER2_COUNT:] if len(older) > _TIER2_COUNT else older
        tier2_lines: list[str] = []
        for item in tier2_items:
            role = str(item.get('role', '')).strip()
            if role == 'user':
                tier2_lines.append(f'用户：{str(item.get("content", "")).strip()[:120]}')
            elif role == 'assistant':
                meta = item.get('meta')
                if isinstance(meta, dict):
                    parts = []
                    for mk in ('company', 'metric', 'report_period', 'query_kind'):
                        mv = str(meta.get(mk, '')).strip()
                        if mv and mv != 'None' and mv != '':
                            parts.append(f'{mk}={mv}')
                    if parts:
                        tier2_lines.append(f'助手：[{", ".join(parts)}]')
                        continue
                content = str(item.get('content', '')).strip()
                if content:
                    tier2_lines.append(f'助手：{content.split(chr(10))[0][:100]}')
        tier2_ctx = '\n'.join(tier2_lines) if tier2_lines else ''

        # Tier 3: compact summary of oldest turns (rule-based, no LLM call for speed)
        tier3_items = older[:-_TIER2_COUNT] if len(older) > _TIER2_COUNT else []
        tier3_ctx = ''
        if tier3_items:
            tier3_lines: list[str] = []
            for item in tier3_items[-6:]:
                role = str(item.get('role', '')).strip()
                content = str(item.get('content', '')).strip()[:80]
                if role == 'user' and content:
                    tier3_lines.append(f'用户：{content}')
                elif role == 'assistant' and content:
                    tier3_lines.append(f'助手：{content.split(chr(10))[0][:60]}')
            tier3_ctx = '\n'.join(tier3_lines) if tier3_lines else ''

        # Reassemble: [tier3 summary] + [tier2 compact] + [tier1 verbatim]
        prefix: list[dict[str, Any]] = []
        if tier3_ctx:
            prefix.append({'role': 'system', 'content': f'[早期对话摘要] {tier3_ctx}'})
        if tier2_ctx:
            prefix.append({'role': 'system', 'content': f'[近期对话概要]\n{tier2_ctx}'})
        history = prefix + tier1

    state = DialogueState(raw_question=question, history=history)

    # ① small talk / self-intro — highest priority, skip everything else
    if _is_small_talk_question(question):
        state.is_small_talk = True
        state.query_kind = 'general'
        state.rewritten_question = question
        return state

    # ② LLM-based query rewriting: handles BOTH follow-up and topic switch
    state.is_follow_up = is_follow_up(question)
    _has_real_history = any(item.get('role') in ('user', 'assistant') for item in history)
    if _has_real_history:
        rewritten, is_new_topic = _rewrite_query(question, history)
        state.rewritten_question = rewritten
        state.is_topic_switch = is_new_topic
        # also check rule-based topic switch as backup
        if not is_new_topic:
            state.is_topic_switch = False
        elif detect_topic_switch(question, history, COMPANY_NAMES):
            state.is_topic_switch = True
    else:
        state.rewritten_question = question
        state.is_topic_switch = True  # first question is always a new topic

    # ③ 公司别名规范化：将问题中的别名替换为官方简称
    # "999与白云山" → "华润三九与白云山"
    _q = state.rewritten_question
    for alias in sorted(COMPANY_ALIAS.keys(), key=len, reverse=True):
        if alias in _q:
            official = COMPANY_ALIAS.get(alias, '')
            if official and official in COMPANY_NAMES and alias != official:
                # 跳过：如果官方全名已存在于文本中（避免"云南白药"中的"白药"再被替换）
                if official in _q:
                    continue
                _q = _q.replace(alias, official)
    state.rewritten_question = _q

    return state


def _resolve_intent_to_state(state: DialogueState) -> DialogueState:
    """Layer 1: unified intent recognition populating DialogueState slots.

    Strategy: try Function-Calling router first (most accurate), fall back to
    legacy rule+LLM pipeline if intent_llm is not configured or call fails.
    """
    question = state.effective_question
    history = state.history
    intent_history = [] if state.is_topic_switch else history
    allow_hist = not state.is_topic_switch

    # ── 跨表/派生计算查询检测 → 跳过 Intent LLM，直接走管道计划器 ────────
    _CROSS_TABLE_RE = re.compile(
        r'(关联.*表|联合.*表|跨表|JOIN|join'
        r'|取自.*表|来自.*表|从.*表.*取'
        r'|(利润表|资产负债表|现金流量表|核心业绩).*(利润表|资产负债表|现金流量表|核心业绩))'
    )
    _DERIVED_CALC_RE = re.compile(
        r'(（[^）]*[/÷×\-\+\*][^）]*）|计算.*[/÷×]|毛利率.*营业[收入成本]|保留.*小数|分组|分[为成].*区间|区间.*统计|费用占比|占["""\s]*营[收业]|占.*?比例.*?超过|占比.*排|与.*的?比值|与.*的?差值|差值.*绝对值|差额|[/÷].*分布'
        r'|[\u4e00-\u9fff]+[/÷][\u4e00-\u9fff]+.*?比[值率]'
        r'|金额["""\s]*超过["""\s]*["""\s]*|["""].*超过["""]|["""].*大于["""]|["""].*高于["""]|["""].*低于["""]'
        r'|为正数|为负数|大于零|小于零|为正[^面确]|为负[^面债]|流入大于流出|流出大于流入'
        r'|相关性|相关系数|散点图|回归分析)')
    _skip_intent_llm = bool(_CROSS_TABLE_RE.search(question) or
                            (_DERIVED_CALC_RE.search(question) and _AGGREGATE_QUERY_RE.search(question)))
    if _skip_intent_llm:
        print(f'[Intent] 检测到跨表/派生计算查询，跳过 Intent LLM → 管道计划器', flush=True)

    # ── Try Function-Calling intent router (Approach 2+4) ────────────────
    fc_result: list[dict[str, Any]] | None = None
    if intent_llm_available() and not state.is_small_talk and not _skip_intent_llm:
        try:
            from .intent_router import route_intent
            fc_result = route_intent(question, intent_history, COMPANY_NAMES, TABLE_METRIC_COLUMNS)
        except Exception as _fc_err:
            import traceback
            print(f'[intent_router] function-calling 失败，降级到规则+LLM: {_fc_err}', flush=True)
            traceback.print_exc()
            fc_result = None

    if fc_result and len(fc_result) > 0:
        # ── Parse function-calling result ────────────────────────────────
        primary = fc_result[0]

        # multi-intent: multiple tool_calls
        if len(fc_result) >= 2 and not state.is_follow_up:
            state.is_multi_intent = True
            state._fc_intents = fc_result  # type: ignore[attr-defined]
        else:
            state.is_multi_intent = False

        # query_kind & analysis_mode from tool selection
        _fc_qk = primary.get('query_kind', 'general')
        _fc_am = primary.get('analysis_mode', 'general')
        # Safety: if intent LLM says 'general' but rule-based detects a concrete metric+company,
        # prefer the rule-based result to avoid losing valid SQL queries
        if _fc_qk == 'general':
            _rule_check = _build_rule_intent(question, intent_history)
            if _rule_check.get('query_kind') in ('metric', 'cash_flow', 'ranking'):
                _fc_qk = _rule_check['query_kind']
                _fc_am = _rule_check.get('analysis_mode', _fc_am)
        state.query_kind = _fc_qk
        state.analysis_mode = _fc_am

        # company slot (支持别名解析：999→华润三九 等)
        fc_company = primary.get('company', '')
        if fc_company and fc_company not in COMPANY_NAMES:
            fc_company = resolve_company_alias(fc_company) or fc_company
        if fc_company and fc_company in COMPANY_NAMES:
            state.company = SlotSource(value=fc_company, origin='tool_call', confidence=0.95)
        else:
            current_company = resolve_company_alias(question)
            hist_company = ''
            if allow_hist:
                for item in reversed(history):
                    meta = item.get('meta')
                    if isinstance(meta, dict) and str(meta.get('company', '')).strip() in COMPANY_NAMES:
                        hist_company = str(meta['company']).strip()
                        break
            state.company = merge_slot(
                current=current_company, rule='', llm=fc_company,
                history_val=hist_company, allow_history=allow_hist,
            )

        # period slot
        fc_period = str(primary.get('period', '') or '').strip()
        current_period = extract_period(question)
        # Guard: if user's question has NO year context, discard LLM-hallucinated period
        _has_year_context = bool(re.search(r'(20\d{2}|今年|去年|最新|最近|近[几\d])', question))
        if not _has_year_context and not current_period:
            fc_period = ''  # LLM may guess a year — reject it
        hist_period = ''
        if allow_hist:
            for item in reversed(history):
                meta = item.get('meta')
                if isinstance(meta, dict) and isinstance(meta.get('report_period'), str):
                    hist_period = normalize_period_code(meta['report_period'])
                    break
        state.period = merge_slot(
            current=normalize_period_code(current_period) if current_period else '',
            rule='', llm=fc_period,
            history_val=hist_period, allow_history=allow_hist,
        )

        # metric slot — from tool's metrics list
        fc_metrics = primary.get('metrics', [])
        if fc_metrics:
            first_metric = fc_metrics[0]
            if first_metric in METRIC_DEFINITIONS:
                state.metric = SlotSource(
                    value=dict(METRIC_DEFINITIONS[first_metric]),
                    origin='tool_call', confidence=0.95,
                )
            else:
                state.metric = SlotSource(value={}, origin='none', confidence=0.0)
        else:
            current_metric_info = infer_metric_or_intent(question, [])
            state.metric = SlotSource(
                value=current_metric_info if current_metric_info.get('type') == 'metric' else {},
                origin='rule' if current_metric_info.get('type') == 'metric' else 'none',
                confidence=0.7 if current_metric_info.get('type') == 'metric' else 0.0,
            )

        # apply slot TTL
        state.company = apply_slot_ttl(state.company, history)
        state.period = apply_slot_ttl(state.period, history)
        state.metric = apply_slot_ttl(state.metric, history)

        state.required = _compute_required_fields(state)
        return state

    # ── Legacy fallback: rule-based + LLM JSON intent ────────────────────
    rule = _build_rule_intent(question, intent_history)

    cached = get_cached_intent(question, len(intent_history))
    if cached is not None:
        llm = cached
    else:
        llm = _infer_intent_with_llm(question, intent_history, rule)
        set_cached_intent(question, len(intent_history), llm)

    # company
    current_company = ''
    for c in COMPANY_NAMES:
        if c in question:
            current_company = c
            break
    hist_company = ''
    if allow_hist:
        for item in reversed(history):
            meta = item.get('meta')
            if isinstance(meta, dict) and str(meta.get('company', '')).strip() in COMPANY_NAMES:
                hist_company = str(meta['company']).strip()
                break
    state.company = merge_slot(
        current=current_company, rule=rule['company'],
        llm=str(llm.get('company', '')).strip(),
        history_val=hist_company, allow_history=allow_hist,
    )

    # period
    current_period = extract_period(question)
    hist_period = ''
    if allow_hist:
        for item in reversed(history):
            meta = item.get('meta')
            if isinstance(meta, dict) and isinstance(meta.get('report_period'), str):
                hist_period = normalize_period_code(meta['report_period'])
                break
    state.period = merge_slot(
        current=normalize_period_code(current_period) if current_period else '',
        rule=rule['period'], llm=str(llm.get('period', '')).strip(),
        history_val=hist_period, allow_history=allow_hist,
    )

    # metric
    current_metric_info = infer_metric_or_intent(question, [])
    hist_metric: dict = {}
    if allow_hist:
        hist_info = infer_metric_or_intent('', history)
        if hist_info:
            hist_metric = hist_info
    state.metric = merge_slot(
        current=current_metric_info if current_metric_info.get('type') == 'metric' else {},
        rule=rule['metric'], llm=llm.get('metric', {}),
        history_val=hist_metric if hist_metric.get('type') == 'metric' else {},
        allow_history=allow_hist,
    )

    # apply slot TTL
    state.company = apply_slot_ttl(state.company, history)
    state.period = apply_slot_ttl(state.period, history)
    state.metric = apply_slot_ttl(state.metric, history)

    # query_kind
    query_kind = str(rule['query_kind']).strip()
    llm_qk = str(llm.get('query_kind', '')).strip()
    if query_kind == 'general' and llm_qk in {'metric', 'cash_flow', 'ranking'}:
        query_kind = llm_qk
    state.query_kind = query_kind

    # analysis_mode
    analysis_mode = str(rule['analysis_mode']).strip()
    if query_kind == 'ranking':
        analysis_mode = 'ranking'
    elif query_kind == 'cash_flow':
        analysis_mode = 'single'
    elif query_kind == 'metric':
        if analysis_mode == 'trend' or llm.get('analysis_mode') == 'trend':
            analysis_mode = 'trend'
        else:
            analysis_mode = 'single'
    else:
        analysis_mode = 'general'
    state.analysis_mode = analysis_mode

    # multi-intent (legacy: separate detection)
    if not state.is_follow_up and not state.is_multi_intent:
        from .planner import is_multi_intent as _is_multi
        state.is_multi_intent = _is_multi(question)

    state.required = _compute_required_fields(state)
    return state


# keywords indicating queries that operate on ALL companies (no specific company needed)
_AGGREGATE_QUERY_RE = re.compile(
    r'(统计|数量|多少家|多少个|哪些公司|公司.*哪些|所有公司|全部公司|所有企业|每家|各公司|各企业'
    r'|\d+家公司|\d+家企业|整体.*率|总额.*占|总额.*比[例率]'
    r'|筛选|过滤|找出.*公司|公司数量|排名|前[\d一二三四五六七八九十百]+|行业|同比增长的公司|环比增长的公司'
    r'|增长.*公司|下降.*公司|亏损.*公司|盈利.*公司|最高|最低|最大|最小|平均|均值|中位数|合计|总计|分布|相关性|散点|回归)',
)


def _compute_required_fields(state: DialogueState) -> list[str]:
    """Determine which slots are still missing for the resolved intent."""
    query_kind = state.query_kind
    analysis_mode = state.analysis_mode
    company_val = state.company_value
    period_val = state.period_value
    metric_val = state.metric_value
    question_text = state.effective_question
    required: list[str] = []
    # company is NOT required for ranking or aggregate/filter queries across all companies
    _needs_company = (
        query_kind in {'metric', 'cash_flow'}
        and not company_val
        and analysis_mode != 'ranking'
        and not _AGGREGATE_QUERY_RE.search(question_text)
    )
    if _needs_company:
        # also check if any specific company name appears in the question
        from ..core.context import COMPANY_NAMES as _CRF_NAMES
        if any(c in question_text for c in _CRF_NAMES):
            required.append('company')  # user mentioned a company but we couldn't resolve it
        elif not _CRF_NAMES:
            required.append('company')
        # else: no company mentioned → all-company query, don't require
    _is_aggregate = _AGGREGATE_QUERY_RE.search(question_text)
    if query_kind == 'metric' and not metric_val and not _is_aggregate:
        required.append('metric')
    if query_kind == 'metric' and analysis_mode != 'trend' and not period_val and not _is_aggregate:
        required.append('report_period')
    if query_kind == 'cash_flow' and not period_val:
        required.append('report_period')
    return required


def _ensure_meta(reply: dict[str, Any], state: 'DialogueState | None' = None) -> dict[str, Any]:
    """Guarantee every reply carries a complete meta block for downstream context."""
    meta = reply.get('meta') or {}
    if state is not None:
        meta.setdefault('company', state.company_value)
        metric_val = state.metric_value
        meta.setdefault('metric', metric_val.get('column', '') if isinstance(metric_val, dict) else '')
        meta.setdefault('report_period', state.period_value)
        meta.setdefault('query_kind', state.query_kind)
        meta.setdefault('analysis_mode', state.analysis_mode)
        meta.setdefault('intent', state.effective_question[:120])
    for key in ('company', 'metric', 'report_period', 'query_kind', 'analysis_mode', 'intent'):
        meta.setdefault(key, '')
    reply['meta'] = meta
    return reply


def build_assistant_reply(question: str, history: list[dict[str, Any]], emit=None, deep_think: bool = True, fast_mode: bool = False, render_mode: str = RENDER_MODE_CHAT, session_id: str = '', skip_kb: bool = False) -> dict[str, Any]:
    # ── Inject session memory context into history head ──────────────────────
    if session_id:
        from .session_memory import get_session_memory
        _smem = get_session_memory(session_id)
        _mem_ctx = _smem.to_context_string()
        if _mem_ctx:
            history = [{'role': 'system', 'content': f'[会话记忆] {_mem_ctx}'}] + list(history)

    def _emit(step_label: str, status: str, detail: str = ''):
        if callable(emit):
            try:
                emit('step', {'label': step_label, 'status': status, 'detail': detail})
            except Exception:
                pass

    def _think(text: str):
        if not deep_think:
            return
        if callable(emit):
            try:
                emit('thinking', text)
            except Exception:
                pass

    # ── Layer 0: preprocess ──────────────────────────────────────────────────
    state = _preprocess(question, history)
    if state.is_follow_up and state.rewritten_question != question:
        _emit('追问改写', 'done', f'改写为：{state.rewritten_question[:80]}')
        _think(f'这是对之前话题的追问，核心意思应该是：「{state.rewritten_question[:60]}」')

    # ── Layer 1: launch KB in background BEFORE intent (parallel) ─────────
    from concurrent.futures import ThreadPoolExecutor as _KbTPE
    _kb_future = None
    _kb_pool = None
    _kb_need = (not skip_kb) and not state.is_small_talk and _should_use_kb_for_question(state.effective_question, '')
    if _kb_need:
        _emit('知识库检索', 'running', '正在从研报知识库中检索相关文献…')
        _kb_pool = _KbTPE(max_workers=1)
        _quick_company = resolve_company_alias(state.effective_question) or ''
        # When question asks for research reports, don't restrict KB to a single company
        if re.search(r'(?:行业研报|行业报告|行业.*研报|结合.*行业|结合.*研报|结合.*报告|请.*研报)', state.effective_question):
            _quick_company = ''
        _kb_future = _kb_pool.submit(_retrieve_kb_context, state.effective_question, company=_quick_company, fast_mode=fast_mode)

    # ── Layer 1: unified intent recognition (runs while KB retrieves) ────
    if not state.is_small_talk:
        _emit('意图识别', 'running', '正在解析问题意图、公司与指标…')
        state = _resolve_intent_to_state(state)
    else:
        _emit('意图识别', 'done', '识别为闲聊/帮助类问题')

    # extract convenience variables from state
    company = state.company_value
    period = state.period_value
    metric = state.metric_value
    query_kind = state.query_kind
    analysis_mode = state.analysis_mode
    required = list(state.required)
    q = state.effective_question
    compact = re.sub(r'\s+', '', q)

    # ── Guard: reject hallucinated / history-inherited company for industry questions ──
    # If the original question doesn't mention any company, and the company was
    # inferred by LLM (hallucination) or inherited from history, clear it so that
    # downstream SQL layers don't fixate on a single company.
    # BUT: skip this guard for follow-up / continuation questions — the rewriter
    # may have legitimately inherited the company from conversation context.
    _q_explicit_company = resolve_company_alias(q)
    if company and not _q_explicit_company and not state.is_follow_up:
        company = ''
        state.company = SlotSource()

    intent_desc = f'{company}的{metric.get("label", query_kind)}' if company and company != 'None' else query_kind
    _emit('意图识别', 'done', f'识别为【{intent_desc}】查询')
    state.record_step('意图识别', 'done', f'识别为【{intent_desc}】查询')

    # ── KB retrieval setup (moved early so global guard can use _resolve_kb) ──
    kb_context: str = ''
    references: list[dict[str, Any]] = []
    # ── Smart KB skip: 简单数据查询（单公司单期事实/现金流）跳过 KB 以加速响应 ──
    # 趋势分析、排名、综合分析等复杂查询仍然使用 KB
    _explicitly_needs_kb = bool(re.search(r'(?:研报|研究报告|行业报告|行业研报|结合.*报告|结合.*研报)', q))
    _needs_analysis_kb = bool(re.search(r'(原因|为什么|为何|分析|归因|怎么回事|背后|解释|影响因素)', q))
    _has_non_db_metric = bool(re.search(r'(境外收入|海外收入|境内收入|国内收入|国外收入|收入占比|收入结构|业务.*收入|分[业部].*收入|毛利率变化|收入贡献)', q))
    _has_multi_metric = bool(re.search(r'(及|以及|和|并|同时).*(率|占比|增长|费用|收入|利润)', q))
    _simple_data_query = (
        query_kind in ('metric', 'cash_flow')
        and analysis_mode == 'single'
        and not _explicitly_needs_kb
        and not _needs_analysis_kb
        and not _has_non_db_metric
        and not _has_multi_metric
    )
    if _simple_data_query and _kb_future is not None:
        _kb_future.cancel()
        _kb_future = None
        _kb_need = False
    if not _kb_need:
        _skip_reason = '简单数据查询，跳过研报检索以加速响应' if _simple_data_query else '当前问题无需研报知识库增强'
        _emit('知识库检索', 'done', _skip_reason)
        state.record_step('知识库检索', 'done', _skip_reason)

    def _resolve_kb():
        """Block until background KB retrieval completes (no-op if already resolved)."""
        nonlocal kb_context, references, _kb_future
        if _kb_future is not None:
            try:
                kb_context, references = _kb_future.result()
            except Exception:
                kb_context, references = '', []
            _emit('知识库检索', 'done', f'检索完成，命中 {len(references)} 条文献片段')
            if references:
                _think(f'研报中有相关分析观点，可以结合机构研究来丰富回答。')
            state.record_step('知识库检索', 'done', f'命中 {len(references)} 条')
            _kb_future = None

    # ── Global guard: open-ended questions without company → skip ALL SQL, go KB ──
    # An "open-ended" question has no explicit company AND doesn't require ranking/aggregation.
    _needs_ranking_early = bool(re.search(
        r'(排[名序]|前\d+|[Tt]op\s*\d+|最[大高低小].*几家|哪[些个几].*(?:公司|企业)|(?:公司|企业)有哪[些个几]'
        r'|[一二三四五六七八九十\d]+家.*?(?:公司|企业)'
        r'|满足.*(?:公司|企业|条件)|符合.*(?:公司|企业|条件)'
        r'|(?:超过?|大于|高于|低于|小于|不[足超])[^，。]*(?:的公司|的企业)'
        r'|(?:为负|为正|负数|正数)[^，。]*(?:的公司|的企业|公司|企业)'
        r'|统计.*(?:公司|企业|数量|个数)|(?:多少|几)家.*(?:公司|企业)'
        r'|筛选|过滤|(?:营收|收入|利润|负债|现金流).*(?:超|大于|高于|低于).*(?:公司|企业))', q))
    _agg_match = _AGGREGATE_QUERY_RE.search(q)
    # "行业龙头/头部/领军" 等描述性用语应从研报获取，不算聚合查询
    _is_aggregate_early = bool(_agg_match) and not (
        _agg_match.group(0) == '行业' and re.search(r'行业(龙头|头部|领军|领先|主要|重点|代表)', q)
    )
    _is_cross_table_early = bool(re.search(
        r'(关联.*表|联合.*表|跨表|JOIN|join|(利润表|资产负债表|现金流量表|核心业绩).*(利润表|资产负债表|现金流量表|核心业绩))', q))
    # CAGR/复合增长率 需要 SQL 计算
    _needs_cagr_early = bool(re.search(r'(复合增长率|CAGR|年均增长率|复合增速)', q))
    # Detect: question mentions a known DB metric + refers to companies generically
    _KNOWN_METRIC_RE = re.compile(
        r'(资产负债率|毛利率|净利率|净利润率|营[业收]|收入|利润|净利|现金流|负债|研发费用|每股收益|ROE|roe|费用占比|周转率)')
    _mentions_company_group = bool(re.search(
        r'(中药|医药|上市|板块|行业|所有|全部|各|各家|\d+家)', q)) and bool(re.search(r'(公司|企业)', q))
    _needs_metric_for_group = bool(_KNOWN_METRIC_RE.search(q)) and _mentions_company_group
    # "2025年的财务数据怎么样" — year + financial terms → need DB
    _needs_financial_overview = bool(re.search(r'20\d{2}', q)) and bool(re.search(
        r'(财务数据|财务状况|财务|财报|业绩|经营数据|经营情况|经营|数据.*怎么样|数据.*如何)', q))
    # Detect fusion queries early: KB identifies companies, then DB queries their data
    _is_fusion_q = bool(re.search(r'(研报|个股研报|行业研报)', q)) and bool(re.search(
        r'(查询|检索|统计|找出).*(?:公司|企业)|(?:这些|那些|上述).*(?:公司|企业).*(?:查询|数据|指标|比[例率]|情况)', q))
    # Leader/head queries need KB to identify which companies are leaders
    _is_leader_q_early = bool(re.search(r'(龙头|头部|领军|领先)', q)) and bool(re.search(
        r'(公司|企业)', q))
    _skip_all_sql = (
        _is_fusion_q  # fusion queries always go KB-first + Hybrid DB supplement
        or _is_leader_q_early  # leader queries need KB to identify companies first
        or (not _q_explicit_company
            and not _needs_ranking_early
            and not _is_aggregate_early
            and not _is_cross_table_early
            and not _needs_cagr_early
            and not _needs_metric_for_group
            and not _needs_financial_overview)
    )
    _h_sqls: list[str] = []
    _h_src_tables: list[str] = []
    _h_db_supplement_text = ''
    if _skip_all_sql:
        # Jump directly to Layer 3: KB-based reply
        _resolve_kb()
        # Carry forward DB supplement from previous turns
        for _prev_msg in reversed(history):
            if _prev_msg.get('role') == 'assistant' and _prev_msg.get('_db_supplement'):
                kb_context = (kb_context or '') + _prev_msg['_db_supplement']
                break
        # ── Hybrid: KB + DB supplement ──
        # 触发条件：问题涉及DB可查的财务指标，且属于以下场景之一：
        #   1) 行业龙头/头部类 → 从KB提取公司，查DB实际数据
        #   2) 预告/对比/差异类 → 从KB提取公司和预测值，查DB实际数据做对比
        #   3) 融合查询：从研报中识别公司，再查DB数据
        _METRIC_HINT_RE = re.compile(r'(营[业收]|收入|利润|净利|毛利|成本|费用|资产|负债|现金流|研发|应收|商誉|减值|负债率|周转|权益|股东|总资产|股东权益|销售|增速|增长率)')
        _is_leader_q = bool(re.search(r'(龙头|头部|领军|领先|主要|重点|代表)', q))
        _is_compare_q = bool(re.search(r'(对比|差异|偏离|偏差|实际.*预[告测]|预[告测].*实际|业绩预告)', q))
        _is_group_metric_q = bool(re.search(r'(各|所有|全部|中药|医药)', q)) and bool(re.search(r'(公司|企业)', q))
        if _METRIC_HINT_RE.search(q) and (_is_leader_q or _is_compare_q or _is_fusion_q or _is_group_metric_q):
            # detect metric columns from question
            _h_compact = re.sub(r'\s+', '', q)
            _h_cols: list[str] = []
            if re.search(r'(收入|营收|营业总收入|销售额|销售)', _h_compact):
                _h_cols.append('total_operating_revenue')
            if re.search(r'(净利润|净利)', _h_compact):
                _h_cols.append('net_profit')
            if re.search(r'(利润总额|利润)', _h_compact) and 'net_profit' not in _h_cols:
                _h_cols.append('total_profit')
            if re.search(r'(成本|营业成本)', _h_compact):
                _h_cols.append('operating_expense_cost_of_sales')
            if re.search(r'(研发|研发费用)', _h_compact):
                _h_cols.append('operating_expense_rnd_expenses')
            if re.search(r'(应收账款)', _h_compact):
                _h_cols.append('asset_accounts_receivable')
            if re.search(r'(资产负债率)', _h_compact):
                _h_cols.append('asset_liability_ratio')
            if re.search(r'(资产减值|减值损失)', _h_compact):
                _h_cols.append('asset_impairment_loss')
            if re.search(r'(营[业收].*增长率|收入增长率|营收增长|增速|同比增长)', _h_compact):
                _h_cols.append('operating_revenue_yoy_growth')
            if re.search(r'(总资产)', _h_compact):
                _h_cols.append('asset_total_assets')
            if re.search(r'(股东权益|所有者权益)', _h_compact):
                _h_cols.append('equity_total_equity')
            if re.search(r'(总负债)', _h_compact):
                _h_cols.append('liability_total_liabilities')
            if re.search(r'(存货周转率)', _h_compact):
                _h_cols.append('asset_inventory')
                _h_cols.append('operating_expense_cost_of_sales')
            elif re.search(r'(存货)', _h_compact):
                _h_cols.append('asset_inventory')
            if re.search(r'(货币资金)', _h_compact):
                _h_cols.append('asset_cash_and_cash_equivalents')
            # Track derived (computed) metrics
            _h_derived: dict[str, str] = {}  # derived_col → label
            if re.search(r'(存货周转率)', _h_compact):
                _h_derived['_calc_inventory_turnover'] = '存货周转率（次）'
            if re.search(r'(应收账款.*占.*收入|应收.*比[例率])', _h_compact):
                _h_derived['_calc_receivable_ratio'] = '应收账款占营收比例（%）'
                if 'asset_accounts_receivable' not in _h_cols:
                    _h_cols.append('asset_accounts_receivable')
                if 'total_operating_revenue' not in _h_cols:
                    _h_cols.append('total_operating_revenue')
            if not _h_cols:
                _h_cols = ['total_operating_revenue', 'net_profit']
            # detect periods
            _h_yr_range = re.search(r'(20\d{2})[-–~至到](20\d{2})', _h_compact)
            _h_periods: list[str] = []
            if _h_yr_range:
                _s, _e = int(_h_yr_range.group(1)), int(_h_yr_range.group(2))
                if re.search(r'前三季度', _h_compact):
                    _h_suf = 'Q3C'
                elif re.search(r'(第三季度|三季度|[Qq]3)', _h_compact):
                    _h_suf = 'Q3'
                elif re.search(r'(第一季度|一季度|[Qq]1)', _h_compact):
                    _h_suf = 'Q1'
                elif re.search(r'(半年|[Hh][Yy])', _h_compact):
                    _h_suf = 'HY'
                else:
                    _h_suf = 'FY'
                _h_periods = [f'{y}{_h_suf}' for y in range(_s, _e + 1)]
            if not _h_periods:
                # single year (4-digit or 2-digit abbreviation like "25年")
                _h_yr_m = re.search(r'(20\d{2})', _h_compact)
                if not _h_yr_m:
                    _h_yr_m2 = re.search(r'(?<!\d)(2[0-5])年', _h_compact)
                    if _h_yr_m2:
                        class _YrWrap:
                            def group(self, n): return f'20{_h_yr_m2.group(1)}'
                        _h_yr_m = _YrWrap()
                if not _h_yr_m:
                    # No year mentioned at all → default to 2025
                    class _YrDefault:
                        def group(self, n): return '2025'
                    _h_yr_m = _YrDefault()
                if _h_yr_m:
                    _yr = _h_yr_m.group(1)
                    if re.search(r'前三季度', _h_compact):
                        _h_periods = [f'{_yr}Q3C']
                    elif re.search(r'(第三季度|三季度|[Qq]3)', _h_compact):
                        _h_periods = [f'{_yr}Q3']
                    elif re.search(r'(半年|[Hh][Yy])', _h_compact):
                        _h_periods = [f'{_yr}HY']
                    else:
                        _h_periods = [f'{_yr}FY']
            if _h_periods:
                # Step 1: try to get company list from KB
                _kb_companies = resolve_all_companies_in_text(kb_context) if kb_context else []
                # Step 2: if KB has no companies, use DB top N as "行业龙头"
                _h_primary_col = _h_cols[0]
                _h_table = _find_table_for_column(_h_primary_col)
                _h_db_periods = [p.replace('Q3C', 'Q3') for p in _h_periods]
                if not _kb_companies:
                    _latest_db_p = _h_db_periods[-1]
                    _fallback_limit = 'LIMIT 10'
                    _fallback_label = '龙头识别'
                    if _is_group_metric_q:
                        _fallback_limit = ''  # query all companies for group queries
                        _fallback_label = '全部企业'
                    try:
                        _top_rows = query_rows(
                            f"SELECT TRIM(stock_abbr) AS stock_abbr FROM {_h_table} "
                            f"WHERE report_period = ? AND {_h_primary_col} IS NOT NULL "
                            f"ORDER BY CAST({_h_primary_col} AS REAL) DESC {_fallback_limit}",
                            (_latest_db_p,)
                        )
                        _kb_companies = [str(r['stock_abbr']).strip() for r in _top_rows if r.get('stock_abbr')]
                        if _kb_companies:
                            state.record_step(_fallback_label, 'done',
                                              f'识别 {len(_kb_companies)} 家企业: {", ".join(_kb_companies[:5])}等')
                    except Exception:
                        pass
                if _kb_companies:
                    _h_ph_p = ','.join(['?'] * len(_h_db_periods))
                    _h_ph_c = ','.join(['?'] * len(_kb_companies))
                    # Group columns by their table to handle cross-table queries
                    _h_table_cols: dict[str, list[str]] = {}
                    for _hc in _h_cols:
                        _hc_tbl = _find_table_for_column(_hc)
                        _h_table_cols.setdefault(_hc_tbl, []).append(_hc)
                    # Query each table and merge results
                    _h_merged: dict[tuple[str, str], dict] = {}  # (company, period) → row
                    _h_total_rows = 0
                    _h_sqls: list[str] = []
                    _h_src_tables: list[str] = []
                    for _ht, _ht_cols in _h_table_cols.items():
                        _ht_sel = ', '.join(_ht_cols)
                        _ht_sql_tpl = (f"SELECT TRIM(stock_abbr) AS stock_abbr, report_period, {_ht_sel} "
                                        f"FROM {_ht} "
                                        f"WHERE TRIM(stock_abbr) IN ({','.join(repr(c) for c in _kb_companies)}) "
                                        f"AND report_period IN ({','.join(repr(p) for p in _h_db_periods)}) "
                                        f"ORDER BY stock_abbr, report_period ASC")
                        try:
                            _ht_rows = query_rows(
                                f"SELECT TRIM(stock_abbr) AS stock_abbr, report_period, {_ht_sel} "
                                f"FROM {_ht} "
                                f"WHERE TRIM(stock_abbr) IN ({_h_ph_c}) AND report_period IN ({_h_ph_p}) "
                                f"ORDER BY stock_abbr, report_period ASC",
                                tuple(_kb_companies) + tuple(_h_db_periods)
                            )
                            _h_sqls.append(_ht_sql_tpl)
                            _h_src_tables.append(_ht)
                            for _r in (_ht_rows or []):
                                _key = (str(_r.get('stock_abbr', '')).strip(), str(_r.get('report_period', '')))
                                if _key not in _h_merged:
                                    _h_merged[_key] = {'stock_abbr': _key[0], 'report_period': _key[1]}
                                for _c in _ht_cols:
                                    _h_merged[_key][_c] = _r.get(_c, '')
                                _h_total_rows += 1
                        except Exception:
                            pass
                    if _h_merged:
                        # Compute derived metrics
                        for _key in _h_merged:
                            _r = _h_merged[_key]
                            if '_calc_inventory_turnover' in _h_derived:
                                try:
                                    _inv = float(_r.get('asset_inventory', 0) or 0)
                                    _cogs = float(_r.get('operating_expense_cost_of_sales', 0) or 0)
                                    _r['_calc_inventory_turnover'] = round(_cogs / _inv, 4) if _inv else ''
                                except (ValueError, ZeroDivisionError):
                                    _r['_calc_inventory_turnover'] = ''
                            if '_calc_receivable_ratio' in _h_derived:
                                try:
                                    _ar = float(_r.get('asset_accounts_receivable', 0) or 0)
                                    _rev = float(_r.get('total_operating_revenue', 0) or 0)
                                    _r['_calc_receivable_ratio'] = round(_ar / _rev * 100, 2) if _rev else ''
                                except (ValueError, ZeroDivisionError):
                                    _r['_calc_receivable_ratio'] = ''
                        _all_labels = {}
                        for _ht in _h_table_cols:
                            _all_labels.update(TABLE_METRIC_COLUMNS.get(_ht, {}))
                        _all_labels.update(_h_derived)
                        _display_cols = _h_cols + list(_h_derived.keys())
                        _h_header = '|公司|报告期|' + '|'.join(_all_labels.get(c, c) for c in _display_cols) + '|'
                        _h_lines = [_h_header]
                        for _key in sorted(_h_merged.keys()):
                            _r = _h_merged[_key]
                            _vals = [str(_r.get('stock_abbr', '')), str(_r.get('report_period', ''))]
                            _vals += [str(_r.get(c, '')) for c in _display_cols]
                            _h_lines.append('|' + '|'.join(_vals) + '|')
                        _db_supplement = '\n\n【数据库实际查询结果】\n' + '\n'.join(_h_lines)
                        _h_db_supplement_text = _db_supplement
                        kb_context = (kb_context or '') + _db_supplement
                        state.record_step('数据库补充', 'done',
                                          f'查询 {len(_kb_companies)} 家企业 {len(_h_db_periods)} 个报告期，共 {len(_h_merged)} 条数据')
        _emit('AI 分析', 'running', '开放性问题，正在基于知识库作答…')
        general = _build_general_llm_reply(q, history, kb_context, references, emit=emit, render_mode=render_mode)
        _emit('AI 分析', 'done', f'回答生成完成，共 {len(general["content"])} 字')
        state.record_step('回答生成', 'done', f'生成内容共 {len(general["content"])} 字')
        refs = references or []
        response: dict[str, Any] = {
            'id': create_id(),
            'role': 'assistant',
            'content': general['content'],
            'followUpHints': general['followUpHints'],
            'sourceTables': list(set(_h_src_tables)) if _h_src_tables else [],
            'sql': '\n'.join(_h_sqls) if _h_sqls else '',
            'insights': general['insights'],
            'references': refs,
            'meta': {'query_kind': 'general', 'analysis_mode': 'general'},
            '_db_supplement': _h_db_supplement_text if _h_db_supplement_text else '',
            'status': 'done'
        }
        response['attribution'] = build_dynamic_attribution(
            state,
            refs=refs,
            thinking_map=_build_general_thinking_map(q, refs, general['content'], query_kind=query_kind),
        )
        return _ensure_meta(response, state)

    # ── Financial overview handler ("2025年的财务数据怎么样") ──────────────
    if _needs_financial_overview and not (company and company != 'None') and query_kind in ('general', 'metric'):
        _fo_yr_m = re.search(r'(20\d{2})', q)
        _fo_yr = int(_fo_yr_m.group(1)) if _fo_yr_m else 2025
        # find latest available period for this year
        _fo_period = None
        for _fo_suffix in ['Q3', 'HY', 'Q1', 'FY']:
            _fo_test = f'{_fo_yr}{_fo_suffix}'
            _fo_cnt = query_rows(f"SELECT COUNT(*) AS cnt FROM income_sheet WHERE report_period = ?", (_fo_test,))
            if _fo_cnt and int(_fo_cnt[0].get('cnt', 0)) > 0:
                _fo_period = _fo_test
                break
        if not _fo_period:
            # try previous year FY as fallback
            _fo_period = f'{_fo_yr - 1}FY'
        _emit('数据库查询', 'running', f'正在查询{_fo_yr}年财务概览数据…')
        _think(f'用户询问{_fo_yr}年财务数据概况，需要查询主要财务指标并进行汇总分析。')
        _fo_cols = ['total_operating_revenue', 'net_profit', 'operating_revenue_yoy_growth', 'net_profit_yoy_growth']
        _fo_sel = ', '.join(_fo_cols)
        _fo_sql = (
            f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_fo_sel} "
            f"FROM income_sheet WHERE report_period = '{_fo_period}' "
            f"ORDER BY CAST(total_operating_revenue AS REAL) DESC"
        )
        _fo_rows = query_rows(
            f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_fo_sel} "
            f"FROM income_sheet WHERE report_period = ?",
            (_fo_period,)
        )
        if _fo_rows:
            _fo_col_labels = TABLE_METRIC_COLUMNS.get('income_sheet', {})
            _fo_columns = [
                {'key': 'stock_code', 'label': '股票代码'},
                {'key': 'stock_abbr', 'label': '公司简称'},
            ] + [{'key': c, 'label': _fo_col_labels.get(c, c)} for c in _fo_cols]
            _fo_chart_labels = [str(r.get('stock_abbr', '')) for r in _fo_rows[:15]]
            try:
                _fo_chart_values = [float(r.get('total_operating_revenue') or 0) for r in _fo_rows[:15]]
                _fo_chart = build_bar_chart(
                    f'{format_period(_fo_period)} 营业总收入TOP15', _fo_chart_labels, _fo_chart_values, '#6366f1'
                )
            except (TypeError, ValueError):
                _fo_chart = None
            state.record_step('数据库查询', 'done', f'查询{_fo_period}财务概览，共{len(_fo_rows)}家公司')
            _resolve_kb()
            return _ensure_meta(_build_done_reply(
                q, history, _fo_sql, _fo_chart, _fo_columns, _fo_rows,
                ['income_sheet'], 'metric_ranking',
                {'intent': 'financial_overview', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                 'report_period': _fo_period},
                kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
            ), state)

    # ── Fast-track: 跨表/派生/分组查询直接走管道计划器 ─────────────────────
    _CROSS_TABLE_RE2 = re.compile(
        r'(关联.*表|联合.*表|跨表|JOIN|join'
        r'|取自.*表|来自.*表|从.*表.*取'
        r'|(利润表|资产负债表|现金流量表|核心业绩).*(利润表|资产负债表|现金流量表|核心业绩))'
    )
    _DERIVED_CALC_RE2 = re.compile(
        r'(（[^）]*[/÷×\-\+\*][^）]*）|分组|分[为成].*区间|区间.*统计|费用占比|占["""\s]*营[收业]|占.*?比例.*?超过|占比.*排|与.*的?比值|与.*的?差值|差值.*绝对值|差额|[/÷].*分布'
        r'|[\u4e00-\u9fff]+[/÷][\u4e00-\u9fff]+.*?比[值率]'
        r'|周转率|周转天数'
        r'|金额["""\s]*超过["""\s]*["""\s]*|["""].*超过["""]|["""].*大于["""]|["""].*高于["""]|["""].*低于["""]'
        r'|为正数|为负数|大于零|小于零|为正[^面确]|为负[^面债]|流入大于流出|流出大于流入)')
    # 排除"占比前N与后N"模式（由专用箱线图处理器处理）
    _is_ratio_group_pattern = bool(re.search(
        r'占比.*?前(\d+|[一二三四五六七八九十]+).*?后(\d+|[一二三四五六七八九十]+)', q))
    _needs_pipeline_calc = bool(_DERIVED_CALC_RE2.search(q) and re.search(r'(周转率|周转天数)', q))
    _fast_track_pipeline = bool(
        not _is_ratio_group_pattern
        and (not state.is_multi_intent or _needs_pipeline_calc)
        and (_CROSS_TABLE_RE2.search(q)
             or (_DERIVED_CALC_RE2.search(q) and _AGGREGATE_QUERY_RE.search(q)))
    )
    if _fast_track_pipeline:
        try:
            from .llm_query_planner import get_pipeline_plan, execute_pipeline_plan
            if llm_available():
                _emit('智能查询', 'running', '跨表/派生查询，启动管道计划器…')
                _think('这是一个跨表关联查询，需要从多张表中取数据并进行派生计算。')
                # 等待 KB
                kb_context, references = '', []
                if _kb_future:
                    try:
                        _kb_result = _kb_future.result(timeout=30)
                        if _kb_result:
                            if isinstance(_kb_result, tuple):
                                kb_context = _kb_result[0] or ''
                                references = _kb_result[1] if len(_kb_result) > 1 else []
                            elif isinstance(_kb_result, dict):
                                kb_context = _kb_result.get('context', '')
                                references = _kb_result.get('references', [])
                    except Exception:
                        pass
                    finally:
                        if _kb_pool:
                            _kb_pool.shutdown(wait=False)
                            _kb_pool = None; _kb_future = None
                _pipeline = get_pipeline_plan(q)
                if _pipeline and _pipeline.get('steps'):
                    _p_rows, _p_sql, _p_tables, _p_cols, _p_title = execute_pipeline_plan(_pipeline)
                    # 空结果处理: pipeline 执行成功但筛选后无匹配行
                    if _p_rows is not None and len(_p_rows) == 0 and _p_sql:
                        state.record_step('管道计划', 'done', '跨表查询返回 0 条结果')
                        _empty_answer = f'根据数据库查询，没有符合条件的公司。{_p_title or ""}'
                        response = {
                            'type': 'chat', 'content': _empty_answer,
                            'sql': _p_sql, 'tables': list(set(_p_tables)),
                        }
                        response['attribution'] = build_dynamic_attribution(
                            state, refs=references,
                            thinking_map={'data_source': '管道计划器跨表查询', 'result': '0 条匹配'},
                        )
                        return _ensure_meta(response, state)
                    if _p_rows:
                        _p_chart = None
                        # bucket 结果特殊处理
                        _is_bucket = 'range' in _p_rows[0] and 'count' in _p_rows[0]
                        if _is_bucket and len(_p_rows) >= 2:
                            _chart_labels = [str(r.get('range', '')) for r in _p_rows]
                            _chart_values = [int(r.get('count', 0)) for r in _p_rows]
                            _p_chart = build_bar_chart(
                                _p_title or '分组统计', _chart_labels, _chart_values, '#6366f1'
                            )
                        else:
                            _chart_val_key = None
                            for _ck in _p_cols:
                                if _ck['key'] not in ('rank', 'stock_code', 'stock_abbr', 'report_period', 'companies'):
                                    _chart_val_key = _ck['key']
                            for _st in reversed(_pipeline['steps']):
                                if _st.get('op') == 'rank':
                                    _chart_val_key = _st.get('by', _chart_val_key)
                                    break
                            if _chart_val_key and len(_p_rows) >= 2:
                                _chart_labels = [str(r.get('stock_abbr', r.get('stock_code', ''))) for r in _p_rows[:20]]
                                try:
                                    _chart_values = [float(r.get(_chart_val_key) or 0) for r in _p_rows[:20]]
                                    _p_chart = build_bar_chart(
                                        _p_title or '查询结果', _chart_labels, _chart_values, '#6366f1'
                                    )
                                except (TypeError, ValueError):
                                    pass
                        # 如果 rank 步骤截断了数据，告知 LLM 实际匹配总数
                        _pre_rank = _p_rows[0].get('_total_before_rank') if _p_rows else None
                        _pre_filter = _p_rows[0].get('_total_before_filter') if _p_rows else None
                        _coverage_note = ''
                        if _pre_rank and int(_pre_rank) > len(_p_rows):
                            _coverage_note = f'⚠ 符合筛选条件的公司共有 {int(_pre_rank)} 家，此处仅展示排名前 {len(_p_rows)} 家。回答时必须先说明总共有 {int(_pre_rank)} 家符合条件。'
                        if _pre_filter and int(_pre_filter) > len(_p_rows):
                            _coverage_note += f'⚠ 查询总共涉及 {int(_pre_filter)} 家公司，经筛选后有 {len(_p_rows)} 家符合条件。回答时必须先说明"全部 {int(_pre_filter)} 家公司中，有 {len(_p_rows)} 家符合条件"，不要把筛选后的数量说成全部公司数。'
                        state.record_step('管道计划', 'done', f'跨表查询返回 {len(_p_rows)} 条结果')
                        return _ensure_meta(_build_done_reply(
                            q, history, _p_sql, _p_chart, _p_cols, _p_rows,
                            _p_tables, 'metric_ranking',
                            {'intent': 'pipeline_cross_table', 'query_kind': 'ranking', 'analysis_mode': 'ranking'},
                            kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                            data_coverage_note=_coverage_note,
                        ), state)
        except Exception as _ct_err:
            print(f'[Pipeline-CrossTable] failed: {_ct_err}', flush=True)
            import traceback; traceback.print_exc()

    # generate contextual thinking based on question specifics
    metric_label = metric.get('label', '') if metric else ''
    has_company = company and company != 'None'
    if analysis_mode == 'trend':
        if has_company:
            _think(f'{company}的{metric_label}趋势变化，需要拉取多个报告期的数据来观察走势。')
            _think(f'重点关注{metric_label}的同比变动幅度，以及是否存在明显的拐点或异常波动。')
        else:
            _think(f'{metric_label}的趋势分析，需要横跨多个时间段的数据来判断整体走向。')
    elif analysis_mode == 'ranking':
        _think(f'这是一个排名类问题，需要对比多家公司的{metric_label}数据，筛选出表现突出的企业。')
        _think(f'排名对比时要注意不同公司的体量差异，同时关注行业均值水平。')
    elif has_company and metric_label:
        period_desc = format_period(period) if period else '相关报告期'
        _think(f'需要查看{company}在{period_desc}的{metric_label}数据，并结合行业背景进行解读。')
    elif query_kind == 'cash_flow':
        _think(f'现金流分析需要同时关注经营、投资、筹资三类活动，判断资金流转是否健康。')
    if state.is_multi_intent:
        _think('这个问题涉及多个维度，需要分别查询后综合对比分析。')

    # build a resolved dict for backward compatibility with _build_query_plan_with_llm
    resolved = {
        'company': company, 'period': period, 'metric': metric,
        'query_kind': query_kind, 'analysis_mode': analysis_mode, 'required': required,
    }
    # ── 后处理：问题中无显式报告期时补充 required ─────────────────────────────
    # A period is "explicit" only if BOTH year AND period-suffix are present,
    # OR the user says "最新/今年/去年/最近" (which implies a specific time).
    # Bare "第三季度" without a year is NOT explicit — we must ask which year.
    # Explicit = has a concrete year (2024) or a relative time word (今年/去年/最新/最近)
    # Bare "第三季度" alone (no year context) is NOT explicit
    _period_is_explicit = bool(re.search(r'(20\d{2}|今年|去年|最新|最近|近[几\d])', q))
    if (query_kind in {'metric', 'cash_flow'} and analysis_mode == 'single'
            and 'report_period' not in required
            and not period
            and not _period_is_explicit):
        if render_mode == RENDER_MODE_BATCH:
            _auto_periods = available_periods('income_sheet', company)
            if _auto_periods:
                period = _auto_periods[-1]
                resolved['period'] = period
        else:
            required.append('report_period')

    # ── 跨列比较查询（"A超过B的公司"/"A大于B的公司"） ──────────────────
    # Strip category prefixes like "负债-", "资产-" used in financial notation
    _cmp_compact = re.sub(r'(负债|资产|现金流|利润|收入|费用)[-\-]', '', compact)
    _cross_cmp_m = re.search(
        r'([\u4e00-\u9fff]{2,15})(超过|大于|高于|多于|小于|低于|少于|不足)([\u4e00-\u9fff]{2,15})',
        _cmp_compact)
    if _cross_cmp_m:
        _ccmp_left = _cross_cmp_m.group(1)
        _ccmp_op = _cross_cmp_m.group(2)
        _ccmp_right = _cross_cmp_m.group(3)
        _all_m = {**METRIC_ALIASES, **_RANKING_KEYWORD_MAP}
        _ccmp_col_a, _ccmp_col_b = None, None
        for _alias in sorted(_all_m.keys(), key=len, reverse=True):
            if _alias in _ccmp_left and not _ccmp_col_a:
                _ccmp_col_a = _all_m[_alias]
            if _alias in _ccmp_right and not _ccmp_col_b:
                _ccmp_col_b = _all_m[_alias]
            if _ccmp_col_a and _ccmp_col_b:
                break
        if _ccmp_col_a and _ccmp_col_b and _ccmp_col_a != _ccmp_col_b:
            _emit('数据库查询', 'running', '正在查询跨列比较…')
            _ccmp_tbl_a = _find_table_for_column(_ccmp_col_a)
            _ccmp_tbl_b = _find_table_for_column(_ccmp_col_b)
            _ccmp_lbl_a = TABLE_METRIC_COLUMNS.get(_ccmp_tbl_a, {}).get(_ccmp_col_a, _ccmp_col_a)
            _ccmp_lbl_b = TABLE_METRIC_COLUMNS.get(_ccmp_tbl_b, {}).get(_ccmp_col_b, _ccmp_col_b)
            _ccmp_period = period
            if not _ccmp_period:
                _yr_m = re.search(r'(20\d{2})', compact)
                _yr = int(_yr_m.group(1)) if _yr_m else 2025
                if re.search(r'前三季度', compact):
                    _ccmp_period = f'{_yr}Q3C'
                elif re.search(r'(第三季度|三季度|[Qq]3)', compact):
                    _ccmp_period = f'{_yr}Q3'
                elif re.search(r'(第一季度|一季度|[Qq]1)', compact):
                    _ccmp_period = f'{_yr}Q1'
                elif re.search(r'(半年|[Hh][Yy])', compact):
                    _ccmp_period = f'{_yr}HY'
                else:
                    _ccmp_period = f'{_yr}FY'
            _ccmp_db_period = _ccmp_period.replace('Q3C', 'Q3')
            _ccmp_tables = {_ccmp_tbl_a, _ccmp_tbl_b}
            _ccmp_data: dict[str, dict[str, Any]] = {}
            for _tbl in _ccmp_tables:
                _cols_in = []
                if _ccmp_tbl_a == _tbl:
                    _cols_in.append(_ccmp_col_a)
                if _ccmp_tbl_b == _tbl:
                    _cols_in.append(_ccmp_col_b)
                _sel = ', '.join(list(dict.fromkeys(_cols_in)))
                _rows = query_rows(
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_sel} "
                    f"FROM {_tbl} WHERE report_period = ?",
                    (_ccmp_db_period,)
                )
                for _r in _rows:
                    _c = str(_r.get('stock_code', '')).strip()
                    if _c not in _ccmp_data:
                        _ccmp_data[_c] = {'stock_code': _c, 'stock_abbr': _r.get('stock_abbr', '')}
                    for _col in _cols_in:
                        _ccmp_data[_c][_col] = _r.get(_col)
            _ccmp_gt = _ccmp_op in ('超过', '大于', '高于', '多于')
            _ccmp_filtered: list[dict[str, Any]] = []
            for _info in _ccmp_data.values():
                _raw_a = _info.get(_ccmp_col_a)
                _raw_b = _info.get(_ccmp_col_b)
                if _raw_a is None or _raw_b is None:
                    continue
                try:
                    _va = float(_raw_a)
                    _vb = float(_raw_b)
                except (TypeError, ValueError):
                    continue
                if (_ccmp_gt and _va > _vb) or (not _ccmp_gt and _va < _vb):
                    _info['_diff'] = round(_va - _vb, 2)
                    _ccmp_filtered.append(_info)
            _ccmp_filtered.sort(key=lambda r: abs(r.get('_diff', 0)), reverse=True)
            _short_a = _ccmp_lbl_a.split('（')[0]
            _short_b = _ccmp_lbl_b.split('（')[0]
            _ccmp_rows: list[dict[str, Any]] = []
            for _i, _r in enumerate(_ccmp_filtered):
                _ccmp_rows.append({
                    'rank': _i + 1,
                    'stock_code': _r['stock_code'],
                    'stock_abbr': _r['stock_abbr'],
                    _ccmp_col_a: round(float(_r.get(_ccmp_col_a) or 0), 2),
                    _ccmp_col_b: round(float(_r.get(_ccmp_col_b) or 0), 2),
                    'diff': _r['_diff'],
                })
            _ccmp_columns = [
                {'key': 'rank', 'label': '排名'},
                {'key': 'stock_code', 'label': '股票代码'},
                {'key': 'stock_abbr', 'label': '公司简称'},
                {'key': _ccmp_col_a, 'label': _ccmp_lbl_a},
                {'key': _ccmp_col_b, 'label': _ccmp_lbl_b},
                {'key': 'diff', 'label': f'{_short_a}与{_short_b}差额'},
            ]
            _ccmp_op_label = '超过' if _ccmp_gt else '低于'
            _ccmp_chart_title = f'{format_period(_ccmp_period)} {_short_a}{_ccmp_op_label}{_short_b}的公司（差额排名）'
            _ccmp_chart = build_bar_chart(
                _ccmp_chart_title,
                [str(r['stock_abbr']) for r in _ccmp_rows[:20]],
                [r['diff'] for r in _ccmp_rows[:20]],
                '#6366f1'
            ) if _ccmp_rows else None
            _ccmp_sql = (
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, "
                f"{_ccmp_col_a}, {_ccmp_col_b}, "
                f"ROUND(CAST({_ccmp_col_a} AS REAL) - CAST({_ccmp_col_b} AS REAL), 2) AS diff "
                f"FROM {_ccmp_tbl_a}"
                + (f" JOIN {_ccmp_tbl_b} USING (stock_code, report_period)" if _ccmp_tbl_a != _ccmp_tbl_b else "")
                + f" WHERE report_period = '{_ccmp_db_period}'"
                f" AND CAST({_ccmp_col_a} AS REAL) {'>' if _ccmp_gt else '<'} CAST({_ccmp_col_b} AS REAL)"
                f" ORDER BY ABS(diff) DESC;"
            )
            _ccmp_note = (
                f'{format_period(_ccmp_period)} {_short_a}{_ccmp_op_label}{_short_b}的公司共{len(_ccmp_filtered)}家'
                + (f'，差额最大: {_ccmp_rows[0]["stock_abbr"]}({_ccmp_rows[0]["diff"]}万元)' if _ccmp_rows else '')
            )
            state.record_step('数据库查询', 'done', _ccmp_note)
            _resolve_kb()
            return _ensure_meta(_build_done_reply(
                q, history, _ccmp_sql, _ccmp_chart, _ccmp_columns, _ccmp_rows,
                list(_ccmp_tables), 'metric_ranking',
                {'intent': 'cross_column_compare', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                 'report_period': _ccmp_period},
                kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                data_coverage_note=_ccmp_note,
            ), state)

    # ── 相关性散点图（"A与B的相关性，散点图"） ──────────────────────────
    # 检测原始问题中是否含公司名，而非 state 中 LLM/历史推断的 company
    _q_has_company = any(c in compact for c in COMPANY_NAMES)
    _is_corr_scatter = bool(
        re.search(r'(相关性|相关关系|关联性)', compact)
        and not _q_has_company
    )
    if _is_corr_scatter:
        _all_m = {**METRIC_ALIASES, **_RANKING_KEYWORD_MAP}
        _corr_hits: list[tuple[int, int, str, str, str]] = []
        for _alias, _mc in _all_m.items():
            _pos = compact.find(_alias)
            if _pos >= 0:
                _end = _pos + len(_alias)
                overlap = False
                for _ep, _ee, _, _, _ in _corr_hits:
                    if _pos < _ee and _end > _ep:
                        if len(_alias) <= _ee - _ep:
                            overlap = True
                            break
                        else:
                            _corr_hits = [h for h in _corr_hits if not (_pos < h[1] and _end > h[0])]
                if not overlap:
                    _tbl = _find_table_for_column(_mc)
                    _lbl = TABLE_METRIC_COLUMNS.get(_tbl, {}).get(_mc, _mc)
                    _corr_hits.append((_pos, _end, _mc, _tbl, _lbl))
        _corr_hits.sort(key=lambda h: h[0])
        _corr_metrics = [(c, t, l) for _, _, c, t, l in _corr_hits]
        # deduplicate by column
        _seen_cols: set[str] = set()
        _corr_unique: list[tuple[str, str, str]] = []
        for _c, _t, _l in _corr_metrics:
            if _c not in _seen_cols:
                _seen_cols.add(_c)
                _corr_unique.append((_c, _t, _l))
        if len(_corr_unique) >= 2:
            _cx, _tx, _lx = _corr_unique[0]
            _cy, _ty, _ly = _corr_unique[1]
            _short_x = _lx.split('（')[0]
            _short_y = _ly.split('（')[0]
            _emit('数据库查询', 'running', f'正在查询{_short_x}与{_short_y}的相关性数据…')
            # resolve period
            _corr_period = period
            if not _corr_period:
                _yr_m = re.search(r'(20\d{2})', compact)
                _yr = int(_yr_m.group(1)) if _yr_m else 2025
                if re.search(r'前三季度', compact):
                    _corr_period = f'{_yr}Q3C'
                elif re.search(r'(第三季度|三季度|[Qq]3)', compact):
                    _corr_period = f'{_yr}Q3'
                elif re.search(r'(第一季度|一季度|[Qq]1)', compact):
                    _corr_period = f'{_yr}Q1'
                elif re.search(r'(半年|[Hh][Yy])', compact):
                    _corr_period = f'{_yr}HY'
                else:
                    _corr_period = f'{_yr}FY'
            _corr_db_period = _corr_period.replace('Q3C', 'Q3')
            # query data
            _corr_tables = {_tx, _ty}
            _corr_data: dict[str, dict[str, Any]] = {}
            for _tbl in _corr_tables:
                _cols_in = []
                if _tx == _tbl:
                    _cols_in.append(_cx)
                if _ty == _tbl:
                    _cols_in.append(_cy)
                _sel = ', '.join(list(dict.fromkeys(_cols_in)))
                _rows = query_rows(
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_sel} "
                    f"FROM {_tbl} WHERE report_period = ?",
                    (_corr_db_period,)
                )
                for _r in _rows:
                    _sc = str(_r.get('stock_code', '')).strip()
                    if _sc not in _corr_data:
                        _corr_data[_sc] = {'stock_code': _sc, 'stock_abbr': _r.get('stock_abbr', '')}
                    for _col in _cols_in:
                        _corr_data[_sc][_col] = _r.get(_col)
            # build scatter data
            _scatter_points: list[list] = []
            _corr_rows: list[dict[str, Any]] = []
            _xvals: list[float] = []
            _yvals: list[float] = []
            for _info in _corr_data.values():
                try:
                    _vx = float(_info.get(_cx) or 0)
                    _vy = float(_info.get(_cy) or 0)
                except (TypeError, ValueError):
                    continue
                if _vx == 0 and _vy == 0:
                    continue
                _scatter_points.append([_vx, _vy])
                _xvals.append(_vx)
                _yvals.append(_vy)
                _corr_rows.append({
                    'stock_code': _info['stock_code'],
                    'stock_abbr': _info['stock_abbr'],
                    _cx: round(_vx, 2),
                    _cy: round(_vy, 2),
                })
            # compute correlation coefficient
            _corr_coeff = 0.0
            if len(_xvals) >= 2:
                _mx = sum(_xvals) / len(_xvals)
                _my = sum(_yvals) / len(_yvals)
                _sxy = sum((_x - _mx) * (_y - _my) for _x, _y in zip(_xvals, _yvals))
                _sxx = sum((_x - _mx) ** 2 for _x in _xvals)
                _syy = sum((_y - _my) ** 2 for _y in _yvals)
                if _sxx > 0 and _syy > 0:
                    _corr_coeff = round(_sxy / (_sxx ** 0.5 * _syy ** 0.5), 4)
            _corr_columns = [
                {'key': 'stock_code', 'label': '股票代码'},
                {'key': 'stock_abbr', 'label': '公司简称'},
                {'key': _cx, 'label': _lx},
                {'key': _cy, 'label': _ly},
            ]
            _corr_chart_title = f'{format_period(_corr_period)} {_short_x}与{_short_y}相关性（r={_corr_coeff:.2f}）'
            _corr_chart = {
                'title': _corr_chart_title,
                'typeLabel': '散点图',
                'option': {
                    'title': {'text': _corr_chart_title, 'left': 'center'},
                    'tooltip': {'trigger': 'item'},
                    'xAxis': {'type': 'value', 'name': f'{_short_x}（万元）', 'nameLocation': 'center', 'nameGap': 30},
                    'yAxis': {'type': 'value', 'name': f'{_short_y}（万元）'},
                    'series': [{
                        'type': 'scatter',
                        'data': _scatter_points,
                        'symbolSize': 10,
                        'itemStyle': {'color': '#6366f1', 'opacity': 0.7},
                    }],
                },
            } if _scatter_points else None
            _corr_sql = (
                f"SELECT TRIM(a.stock_code) AS stock_code, TRIM(a.stock_abbr) AS stock_abbr, "
                f"a.{_cx}, b.{_cy} "
                f"FROM {_tx} a"
                + (f" JOIN {_ty} b ON TRIM(a.stock_code)=TRIM(b.stock_code) AND a.report_period=b.report_period" if _tx != _ty else "")
                + f" WHERE a.report_period = '{_corr_db_period}';"
            )
            if _tx == _ty:
                _corr_sql = (
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, "
                    f"{_cx}, {_cy} FROM {_tx} WHERE report_period = '{_corr_db_period}';"
                )
            _corr_note = (
                f'{format_period(_corr_period)} {_short_x}与{_short_y}相关性分析: '
                f'共{len(_scatter_points)}家公司, 相关系数r={_corr_coeff:.4f}'
            )
            state.record_step('数据库查询', 'done', _corr_note)
            _resolve_kb()
            return _ensure_meta(_build_done_reply(
                q, history, _corr_sql, _corr_chart, _corr_columns, _corr_rows,
                list(_corr_tables), 'metric_ranking',
                {'intent': 'correlation_scatter', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                 'report_period': _corr_period},
                kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                data_coverage_note=_corr_note,
            ), state)

    # ── 占比分组对比+箱线图（"A占比前十与后十公司的B，箱线图"） ──────────
    _ratio_group_m = re.search(
        r'([\u4e00-\u9fff]{2,8})占比.*?前(\d+|[一二三四五六七八九十]+).*?后(\d+|[一二三四五六七八九十]+).*?([\u4e00-\u9fff]{2,12}率)',
        compact)
    if _ratio_group_m:
        _CN_NUM_MAP = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
        _rg_ratio_name = _ratio_group_m.group(1)  # e.g. 研发费用
        _rg_top_s = _ratio_group_m.group(2)
        _rg_bot_s = _ratio_group_m.group(3)
        _rg_compare_name = _ratio_group_m.group(4)  # e.g. 净利润同比增长率
        _rg_top_n = int(_rg_top_s) if _rg_top_s.isdigit() else _CN_NUM_MAP.get(_rg_top_s, 10)
        _rg_bot_n = int(_rg_bot_s) if _rg_bot_s.isdigit() else _CN_NUM_MAP.get(_rg_bot_s, 10)
        _all_m = {**METRIC_ALIASES, **_RANKING_KEYWORD_MAP}
        # resolve ratio numerator (研发费用 → operating_expense_rnd_expenses)
        _rg_num_col = None
        for _alias in sorted(_all_m.keys(), key=len, reverse=True):
            if _alias in _rg_ratio_name:
                _rg_num_col = _all_m[_alias]
                break
        # resolve compare metric (净利润同比增长率)
        _rg_cmp_col = None
        for _alias in sorted(_all_m.keys(), key=len, reverse=True):
            if _alias in _rg_compare_name:
                _rg_cmp_col = _all_m[_alias]
                break
        # denominator is always 营业总收入 for "占比"
        _rg_den_col = 'total_operating_revenue'
        if _rg_num_col and _rg_cmp_col:
            _emit('数据库查询', 'running', f'正在计算{_rg_ratio_name}占比分组对比…')
            _rg_num_tbl = _find_table_for_column(_rg_num_col)
            _rg_den_tbl = _find_table_for_column(_rg_den_col)
            _rg_cmp_tbl = _find_table_for_column(_rg_cmp_col)
            _rg_num_lbl = TABLE_METRIC_COLUMNS.get(_rg_num_tbl, {}).get(_rg_num_col, _rg_num_col).split('（')[0]
            _rg_cmp_lbl = TABLE_METRIC_COLUMNS.get(_rg_cmp_tbl, {}).get(_rg_cmp_col, _rg_cmp_col).split('（')[0]
            # resolve period
            _rg_period = period
            if not _rg_period:
                _yr_m = re.search(r'(20\d{2})', compact)
                _yr = int(_yr_m.group(1)) if _yr_m else 2025
                if re.search(r'前三季度', compact):
                    _rg_period = f'{_yr}Q3C'
                elif re.search(r'(第三季度|三季度|[Qq]3)', compact):
                    _rg_period = f'{_yr}Q3'
                elif re.search(r'(第一季度|一季度|[Qq]1)', compact):
                    _rg_period = f'{_yr}Q1'
                elif re.search(r'(半年|[Hh][Yy])', compact):
                    _rg_period = f'{_yr}HY'
                else:
                    _rg_period = f'{_yr}FY'
            _rg_db_period = _rg_period.replace('Q3C', 'Q3')
            # query all needed data
            _rg_all_tables = {_rg_num_tbl, _rg_den_tbl, _rg_cmp_tbl}
            _rg_data: dict[str, dict[str, Any]] = {}
            for _tbl in _rg_all_tables:
                _cols_in = []
                if _rg_num_tbl == _tbl:
                    _cols_in.append(_rg_num_col)
                if _rg_den_tbl == _tbl:
                    _cols_in.append(_rg_den_col)
                if _rg_cmp_tbl == _tbl:
                    _cols_in.append(_rg_cmp_col)
                _sel = ', '.join(list(dict.fromkeys(_cols_in)))
                _rows = query_rows(
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_sel} "
                    f"FROM {_tbl} WHERE report_period = ?",
                    (_rg_db_period,)
                )
                for _r in _rows:
                    _sc = str(_r.get('stock_code', '')).strip()
                    if _sc not in _rg_data:
                        _rg_data[_sc] = {'stock_code': _sc, 'stock_abbr': _r.get('stock_abbr', '')}
                    for _col in _cols_in:
                        _rg_data[_sc][_col] = _r.get(_col)
            # compute ratio and sort
            _rg_items: list[dict[str, Any]] = []
            for _info in _rg_data.values():
                try:
                    _num = float(_info.get(_rg_num_col) or 0)
                    _den = float(_info.get(_rg_den_col) or 0)
                    _raw_cmp = _info.get(_rg_cmp_col)
                    if _raw_cmp is None or str(_raw_cmp).strip() == '' or str(_raw_cmp).strip().lower() == 'none':
                        continue
                    _cmp = float(_raw_cmp)
                except (TypeError, ValueError):
                    continue
                if _den > 0 and _num >= 0:
                    _ratio = round(_num / _den * 100, 2)
                    _rg_items.append({
                        'stock_code': _info['stock_code'],
                        'stock_abbr': _info['stock_abbr'],
                        'ratio': _ratio,
                        'compare_val': round(_cmp, 2),
                    })
            _rg_items.sort(key=lambda r: r['ratio'], reverse=True)
            _rg_top = _rg_items[:_rg_top_n]
            _rg_bottom = _rg_items[-_rg_bot_n:] if len(_rg_items) >= _rg_bot_n else _rg_items[_rg_top_n:]
            _rg_top_vals = sorted([r['compare_val'] for r in _rg_top])
            _rg_bot_vals = sorted([r['compare_val'] for r in _rg_bottom])
            # compute boxplot stats: [min, Q1, median, Q3, max]
            def _box_stats(vals: list[float]) -> list[float]:
                if not vals:
                    return [0, 0, 0, 0, 0]
                n = len(vals)
                s = sorted(vals)
                q1 = s[n // 4] if n >= 4 else s[0]
                q3 = s[3 * n // 4] if n >= 4 else s[-1]
                median = s[n // 2] if n % 2 == 1 else round((s[n // 2 - 1] + s[n // 2]) / 2, 2)
                return [round(s[0], 2), round(q1, 2), round(median, 2), round(q3, 2), round(s[-1], 2)]
            _rg_box_top = _box_stats(_rg_top_vals)
            _rg_box_bot = _box_stats(_rg_bot_vals)
            # build result rows (all companies in both groups)
            _rg_rows: list[dict[str, Any]] = []
            for _grp, _label in [(_rg_top, '前十'), (_rg_bottom, '后十')]:
                for _r in _grp:
                    _rg_rows.append({
                        'group': _label,
                        'stock_code': _r['stock_code'],
                        'stock_abbr': _r['stock_abbr'],
                        'ratio': _r['ratio'],
                        'compare_val': _r['compare_val'],
                    })
            _rg_columns = [
                {'key': 'group', 'label': '分组'},
                {'key': 'stock_code', 'label': '股票代码'},
                {'key': 'stock_abbr', 'label': '公司简称'},
                {'key': 'ratio', 'label': f'{_rg_num_lbl}占比（%）'},
                {'key': 'compare_val', 'label': _rg_cmp_lbl},
            ]
            _rg_chart_title = (
                f'{format_period(_rg_period)} {_rg_num_lbl}占比前{_rg_top_n}与后{_rg_bot_n}公司的{_rg_cmp_lbl}分布对比'
            )
            _rg_chart = {
                'title': _rg_chart_title,
                'typeLabel': '箱线图',
                'option': {
                    'title': {'text': _rg_chart_title, 'left': 'center'},
                    'tooltip': {'trigger': 'item'},
                    'xAxis': {'type': 'category', 'data': [f'占比前{_rg_top_n}', f'占比后{_rg_bot_n}']},
                    'yAxis': {'type': 'value', 'name': _rg_cmp_lbl + '（%）'},
                    'series': [{
                        'type': 'boxplot',
                        'data': [_rg_box_top, _rg_box_bot],
                    }],
                },
            }
            _rg_sql = (
                f"SELECT stock_code, stock_abbr, "
                f"ROUND(CAST({_rg_num_col} AS REAL) / CAST({_rg_den_col} AS REAL) * 100, 2) AS ratio, "
                f"{_rg_cmp_col} FROM {_rg_num_tbl}"
                + (f" JOIN {_rg_cmp_tbl} USING (stock_code, report_period)" if _rg_num_tbl != _rg_cmp_tbl else "")
                + f" WHERE report_period = '{_rg_db_period}' AND CAST({_rg_den_col} AS REAL) > 0"
                f" ORDER BY ratio DESC;"
            )
            _rg_note = (
                f'{format_period(_rg_period)} {_rg_num_lbl}占比前{_rg_top_n}公司{_rg_cmp_lbl}中位数: {_rg_box_top[2]}%, '
                f'后{_rg_bot_n}公司中位数: {_rg_box_bot[2]}%'
            )
            state.record_step('数据库查询', 'done', _rg_note)
            _resolve_kb()
            return _ensure_meta(_build_done_reply(
                q, history, _rg_sql, _rg_chart, _rg_columns, _rg_rows,
                list(_rg_all_tables), 'metric_ranking',
                {'intent': 'ratio_group_boxplot', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                 'report_period': _rg_period},
                kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                data_coverage_note=_rg_note,
            ), state)

    # ── multi-intent routing (after all guards) ──────────────────────────────
    # Skip multi-intent for same-company single-metric questions with follow-up details
    # e.g. "方盛制药营收多少？同比增长多少？原因是什么" → single metric + KB analysis
    if state.is_multi_intent and company and metric and query_kind == 'metric':
        state.is_multi_intent = False
    if state.is_multi_intent:
        try:
            assistant_config = CONFIG.get('assistant', {}) if isinstance(CONFIG.get('assistant'), dict) else {}
            if bool(assistant_config.get('enable_multi_intent', True)):
                from .planner import build_multi_intent_reply
                _emit('意图识别', 'done', '识别为多意图查询，启动子任务规划…')
                fc_intents = getattr(state, '_fc_intents', None)
                result = build_multi_intent_reply(question, history, emit=emit, deep_think=deep_think, render_mode=render_mode, fc_intents=fc_intents)
                return _ensure_meta(result, state)
        except Exception:
            pass  # fall through to single-intent

    # ── Layer 3: single-intent execution ─────────────────────────────────────

    # ── 跨表对比检测：提到两个不同表名 + 对比关键词 → 跳过规则引擎，交给 LLM Planner ──
    _table_name_map = {
        '核心业绩指标': 'core_performance_indicators_sheet',
        '核心指标': 'core_performance_indicators_sheet',
        '利润表': 'income_sheet',
        '资产负债表': 'balance_sheet',
        '现金流量表': 'cash_flow_sheet',
    }
    _mentioned_tables = [t for kw, t in _table_name_map.items() if kw in compact]
    _is_cross_table_compare = (
        len(set(_mentioned_tables)) >= 2
        and bool(re.search(r'(不一致|差异|对比|比较|不同|是否一致|一致性)', compact))
    )
    if _is_cross_table_compare:
        query_kind = '_cross_table_compare'  # skip rule engine, fall through to LLM planner

    # ── 复合增长率 (CAGR) 专用处理器 ─────────────────────────────────────
    _is_cagr = bool(re.search(r'复合增长率|CAGR|年均增长率|复合增速', compact))
    if _is_cagr and query_kind in ('ranking', 'metric'):
        _emit('数据库查询', 'running', '正在计算复合增长率…')
        # 解析 CAGR 指标列 — CAGR 需要基础指标（如营业总收入），不能用同比增长率列
        _cagr_col = 'total_operating_revenue'
        for _kw, _mc in _RANKING_KEYWORD_MAP.items():
            if _kw in compact and 'yoy_growth' not in _mc:
                _cagr_col = _mc
                break
        if metric and metric.get('column'):
            _mc = metric['column']
            # 如果意图解析返回的是 yoy_growth 列，转换为基础指标
            if 'yoy_growth' in _mc:
                _mc = _mc.replace('_yoy_growth', '')
                # 验证基础列存在
                if _find_table_for_column(_mc) != 'income_sheet' or _mc not in TABLE_METRIC_COLUMNS.get(_find_table_for_column(_mc), {}):
                    _mc = _cagr_col  # fallback
            _cagr_col = _mc
        _cagr_table = _find_table_for_column(_cagr_col)
        _cagr_col_label = TABLE_METRIC_COLUMNS.get(_cagr_table, {}).get(_cagr_col, _cagr_col)

        # 提取起止年份
        _yr_range = re.findall(r'(20\d{2})', compact)
        _yr_range = sorted(set(int(y) for y in _yr_range))
        _start_yr = _yr_range[0] if _yr_range else 2022
        _end_yr = _yr_range[-1] if len(_yr_range) > 1 else 2025

        # 确定结束期的类型（季度/半年/年）
        _end_suffix = 'FY'
        _is_cum_q3 = bool(re.search(r'前三季度', compact))  # 前三季度 = HY + Q3
        if _is_cum_q3:
            _end_suffix = 'Q3C'  # cumulative 9 months
        elif re.search(r'(第三季度|三季度|[Qq]3)', compact):
            _end_suffix = 'Q3'  # single quarter
        elif re.search(r'(第一季度|一季度|[Qq]1)', compact):
            _end_suffix = 'Q1'
        elif re.search(r'(半年|上半年|[Hh][Yy])', compact):
            _end_suffix = 'HY'

        # ── 同口径对比：起始期和结束期使用相同的报告期类型 ──
        _n_years = _end_yr - _start_yr

        def _fetch_period_val(yr: int, suffix: str) -> dict[str, tuple[str, str, float]]:
            """返回 {stock_code: (stock_code, stock_abbr, value)}"""
            result: dict[str, tuple[str, str, float]] = {}
            if suffix == 'Q3C':  # 前三季度 = HY + Q3
                _hy_r = query_rows(
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_cagr_col} "
                    f"FROM {_cagr_table} WHERE report_period = ? AND {_cagr_col} IS NOT NULL",
                    (f'{yr}HY',))
                _q3_r = query_rows(
                    f"SELECT TRIM(stock_code) AS stock_code, {_cagr_col} "
                    f"FROM {_cagr_table} WHERE report_period = ? AND {_cagr_col} IS NOT NULL",
                    (f'{yr}Q3',))
                _hy_m = {}
                _sa_m = {}
                for r in _hy_r:
                    c = str(r.get('stock_code', '')).strip()
                    try:
                        _hy_m[c] = float(r[_cagr_col])
                        _sa_m[c] = r.get('stock_abbr', '')
                    except (TypeError, ValueError):
                        pass
                _q3_m = {}
                for r in _q3_r:
                    c = str(r.get('stock_code', '')).strip()
                    try:
                        _q3_m[c] = float(r[_cagr_col])
                    except (TypeError, ValueError):
                        pass
                for c in set(_hy_m) & set(_q3_m):
                    result[c] = (c, _sa_m.get(c, ''), _hy_m[c] + _q3_m[c])
            else:
                period_str = f'{yr}{suffix}'
                rows = query_rows(
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_cagr_col} "
                    f"FROM {_cagr_table} WHERE report_period = ? AND {_cagr_col} IS NOT NULL",
                    (period_str,))
                for r in rows:
                    c = str(r.get('stock_code', '')).strip()
                    try:
                        result[c] = (c, r.get('stock_abbr', ''), float(r[_cagr_col]))
                    except (TypeError, ValueError):
                        pass
            return result

        _start_suffix_used = _end_suffix
        _start_data = _fetch_period_val(_start_yr, _end_suffix)
        # 如果起始期同口径数据不足（<5家），逐年往后找同口径有数据的年份
        if len(_start_data) < 5 and _end_suffix != 'FY':
            for _try_yr in range(_start_yr + 1, _end_yr):
                _start_data = _fetch_period_val(_try_yr, _end_suffix)
                if len(_start_data) >= 5:
                    _start_yr = _try_yr
                    break
            _n_years = _end_yr - _start_yr
        _end_data = _fetch_period_val(_end_yr, _end_suffix)
        _start_map = {}
        for _c, (_sc, _sa, _sv) in _start_data.items():
            if _sv > 0:
                _start_map[_c] = {'stock_code': _sc, 'stock_abbr': _sa, 'start_val': _sv}
        _end_map = {}
        for _c, (_sc, _sa, _ev) in _end_data.items():
            _end_map[_c] = _ev

        # 用于 display_sql 和 columns 的期间标签
        if _start_suffix_used == 'Q3C':
            _start_period = f'{_start_yr}HY+{_start_yr}Q3'
        else:
            _start_period = f'{_start_yr}{_start_suffix_used}'
        if _end_suffix == 'Q3C':
            _end_period_label = f'{_end_yr}HY+{_end_yr}Q3'
        else:
            _end_period_label = f'{_end_yr}{_end_suffix}'

        # ── 融合查询：如果问题提到"龙头/头部"，先从KB提取目标公司 ──
        _cagr_is_leader = bool(re.search(r'(龙头|头部|领军|领先|主要|重点|代表)', compact))
        _cagr_kb_companies: list[str] = []
        if _cagr_is_leader:
            _resolve_kb()
            if kb_context:
                _cagr_kb_companies = resolve_all_companies_in_text(kb_context)
            if not _cagr_kb_companies:
                # KB 无公司 → 用 DB 按该指标排名的 Top N 作为"龙头"
                _CN_NUM_L = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
                _leader_n_m = re.search(r'([一二三四五六七八九十\d]+)\s*家', compact)
                _leader_n = 10
                if _leader_n_m:
                    _v = _leader_n_m.group(1)
                    _leader_n = _CN_NUM_L.get(_v, int(_v) if _v.isdigit() else 10)
                _latest_p = f'{_end_yr}{_end_suffix}' if _end_suffix != 'Q3C' else f'{_end_yr}Q3'
                try:
                    _top_r = query_rows(
                        f"SELECT TRIM(stock_abbr) AS stock_abbr FROM {_cagr_table} "
                        f"WHERE report_period = ? AND {_cagr_col} IS NOT NULL "
                        f"ORDER BY CAST({_cagr_col} AS REAL) DESC LIMIT ?",
                        (_latest_p, _leader_n))
                    _cagr_kb_companies = [str(r['stock_abbr']).strip() for r in _top_r if r.get('stock_abbr')]
                except Exception:
                    pass
            if _cagr_kb_companies:
                state.record_step('龙头识别', 'done',
                                  f'识别龙头企业: {", ".join(_cagr_kb_companies[:5])}{"等" if len(_cagr_kb_companies) > 5 else ""}')

        # 计算 CAGR
        import math
        _cagr_results = []
        # 如果有 KB 筛选的目标公司，只对这些公司计算
        _cagr_target_abbrs = set(_cagr_kb_companies) if _cagr_kb_companies else None
        for _c in set(_start_map) & set(_end_map.keys()):
            _sv = _start_map[_c]['start_val']
            _ev = _end_map[_c]
            _abbr = str(_start_map[_c].get('stock_abbr', '')).strip()
            if _cagr_target_abbrs and _abbr not in _cagr_target_abbrs:
                continue
            if _sv > 0 and _ev > 0 and _n_years > 0:
                _cagr_val = ((_ev / _sv) ** (1 / _n_years) - 1) * 100
                _cagr_results.append({
                    'stock_code': _start_map[_c]['stock_code'],
                    'stock_abbr': _start_map[_c]['stock_abbr'],
                    f'start_{_cagr_col}': round(_sv, 2),
                    f'end_{_cagr_col}': round(_ev, 2),
                    'cagr_pct': round(_cagr_val, 2),
                })

        # 排序和截断
        _CN_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
        _cagr_limit_m = re.search(r'(?:前|top|TOP)\s*(\d+)', compact)
        _cagr_limit_cn = re.search(r'(?:前)([一二三四五六七八九十])', compact) if not _cagr_limit_m else None
        if _cagr_limit_m:
            _cagr_limit = int(_cagr_limit_m.group(1))
        elif _cagr_limit_cn:
            _cagr_limit = _CN_NUM.get(_cagr_limit_cn.group(1), 200)
        else:
            _cagr_limit = 200

        _cagr_desc = not bool(re.search(r'(最低|最小|后\d)', compact))
        _cagr_results.sort(key=lambda x: x['cagr_pct'], reverse=_cagr_desc)
        _cagr_results = _cagr_results[:_cagr_limit]

        # 添加排名
        for _i, _r in enumerate(_cagr_results):
            _r['rank'] = _i + 1

        _cagr_columns = [
            {'key': 'rank', 'label': '排名'},
            {'key': 'stock_code', 'label': '股票代码'},
            {'key': 'stock_abbr', 'label': '公司简称'},
            {'key': f'start_{_cagr_col}', 'label': f'{_start_period} {_cagr_col_label}'},
            {'key': f'end_{_cagr_col}', 'label': f'{_end_period_label} {_cagr_col_label}'},
            {'key': 'cagr_pct', 'label': '复合增长率（%）'},
        ]
        _cagr_chart = build_bar_chart(
            f'{_cagr_col_label.split("（")[0]}复合增长率排名',
            [str(r['stock_abbr']) for r in _cagr_results[:20]],
            [r['cagr_pct'] for r in _cagr_results[:20]],
            '#10b981'
        ) if _cagr_results else None
        # build display SQL (pure SQL, no comments) — use actual start suffix
        if _start_suffix_used == 'Q3C':
            _start_sql = (
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_cagr_col} "
                f"FROM {_cagr_table} WHERE report_period IN ('{_start_yr}HY', '{_start_yr}Q3') AND {_cagr_col} IS NOT NULL;"
            )
        else:
            _start_sql = (
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_cagr_col} "
                f"FROM {_cagr_table} WHERE report_period = '{_start_yr}{_start_suffix_used}' AND {_cagr_col} IS NOT NULL;"
            )
        if _end_suffix == 'Q3C':
            _end_sql = (
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_cagr_col} "
                f"FROM {_cagr_table} WHERE report_period IN ('{_end_yr}HY', '{_end_yr}Q3') AND {_cagr_col} IS NOT NULL;"
            )
        else:
            _end_sql = (
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_cagr_col} "
                f"FROM {_cagr_table} WHERE report_period = '{_end_yr}{_end_suffix}' AND {_cagr_col} IS NOT NULL;"
            )
        _cagr_sql = f"{_start_sql}\n{_end_sql}"
        state.record_step('数据库查询', 'done', f'CAGR计算: {len(_cagr_results)} 家公司, n={_n_years}年')
        _resolve_kb()
        return _ensure_meta(_build_done_reply(
            q, history, _cagr_sql, _cagr_chart, _cagr_columns, _cagr_results,
            [_cagr_table], 'metric_ranking',
            {'intent': 'cagr', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
             'report_period': f'{_start_period}-{_end_yr}{_end_suffix}'},
            kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
        ), state)

    # ── 差值排名处理器（"A与B的差值绝对值最大的N家"/"A超过B N个百分点"） ─
    _diff_threshold_m = re.search(r'超过.*?(\d+(?:\.\d+)?)\s*个?\s*百分点', compact)
    _is_diff_query = (
        bool(re.search(r'差值|差额|之差|相差', compact))
        or bool(_diff_threshold_m)
    ) and query_kind in ('ranking', 'metric')
    if _is_diff_query:
        _emit('数据库查询', 'running', '正在计算差值排名…')
        # 从问题中识别两个指标 — 按出现位置排序，去除重叠短别名
        _all_metrics_map = {**METRIC_ALIASES, **_RANKING_KEYWORD_MAP}
        _diff_hits: list[tuple[int, int, str, str, str]] = []  # (pos, end, col, table, label)
        for _alias, _mc in _all_metrics_map.items():
            _pos = compact.find(_alias)
            if _pos >= 0:
                _dt = _find_table_for_column(_mc)
                _dl = TABLE_METRIC_COLUMNS.get(_dt, {}).get(_mc, _mc)
                if not any(c == _mc for _, _, c, _, _ in _diff_hits):
                    _diff_hits.append((_pos, _pos + len(_alias), _mc, _dt, _dl))
        _diff_hits.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        _diff_deduped: list[tuple[int, int, str, str, str]] = []
        for _h in _diff_hits:
            if _diff_deduped and _h[0] >= _diff_deduped[-1][0] and _h[1] <= _diff_deduped[-1][1]:
                continue
            _diff_deduped.append(_h)
        # 对于"A超过B"模式，用"超过"位置分割A/B
        _chao_pos = compact.find('超过')
        if _diff_threshold_m and _chao_pos >= 0:
            _a_cands = [(e, mc, dt, dl) for p, e, mc, dt, dl in _diff_deduped if e <= _chao_pos + 1]
            _b_cands = [(p, mc, dt, dl) for p, e, mc, dt, dl in _diff_deduped if p >= _chao_pos + 2]
            _a_cands.sort(key=lambda x: x[0], reverse=True)  # closest to 超过
            _b_cands.sort(key=lambda x: x[0])  # first after 超过
            _diff_cols: list[tuple[str, str, str]] = []
            if _a_cands:
                _diff_cols.append((_a_cands[0][1], _a_cands[0][2], _a_cands[0][3]))
            if _b_cands:
                _b = _b_cands[0]
                if not _diff_cols or _b[1] != _diff_cols[0][0]:
                    _diff_cols.append((_b[1], _b[2], _b[3]))
        else:
            _diff_cols = [(_mc, _dt, _dl) for _, _, _mc, _dt, _dl in _diff_deduped]
        # 确保至少两个不同指标
        if len(_diff_cols) >= 2:
            _col_a, _tbl_a, _lbl_a = _diff_cols[0]
            _col_b, _tbl_b, _lbl_b = _diff_cols[1]
            # 提取 limit
            _diff_limit_m = re.search(r'(?:前|top|TOP|最大的|最高的)\s*(\d+)', compact)
            _diff_limit_cn = re.search(r'(?:前)([一二三四五六七八九十])', compact) if not _diff_limit_m else None
            _CN_NUM_D = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
            if _diff_limit_m:
                _diff_limit = int(_diff_limit_m.group(1))
            elif _diff_limit_cn:
                _diff_limit = _CN_NUM_D.get(_diff_limit_cn.group(1), 200)
            else:
                _diff_limit = 200
            # 提取报告期
            _diff_period = period or ''
            if not _diff_period:
                _yr_m = re.search(r'(20\d{2})', compact)
                _yr = int(_yr_m.group(1)) if _yr_m else 2025
                if re.search(r'(第三季度|三季度|[Qq]3)', compact):
                    _diff_period = f'{_yr}Q3'
                elif re.search(r'(第一季度|一季度|[Qq]1)', compact):
                    _diff_period = f'{_yr}Q1'
                elif re.search(r'(半年|上半年|[Hh][Yy])', compact):
                    _diff_period = f'{_yr}HY'
                else:
                    _diff_period = f'{_yr}FY'
            # 查两个指标的数据
            _tables_set = {_tbl_a, _tbl_b}
            _data_by_code: dict[str, dict[str, Any]] = {}
            for _tbl in _tables_set:
                _cols_in_tbl = [c for c, t, _ in _diff_cols if t == _tbl]
                _sel = ', '.join(_cols_in_tbl)
                _tbl_rows = query_rows(
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_sel} "
                    f"FROM {_tbl} WHERE report_period = ?",
                    (_diff_period,)
                )
                for _r in _tbl_rows:
                    _c = str(_r.get('stock_code', '')).strip()
                    if _c not in _data_by_code:
                        _data_by_code[_c] = {'stock_code': _c, 'stock_abbr': _r.get('stock_abbr', '')}
                    _data_by_code[_c].update({k: _r[k] for k in _cols_in_tbl if k in _r})
            # 计算差值
            _diff_results = []
            _is_abs = '绝对值' in compact
            for _info in _data_by_code.values():
                try:
                    _va = float(_info.get(_col_a) or 0)
                    _vb = float(_info.get(_col_b) or 0)
                except (TypeError, ValueError):
                    continue
                _diff_val = _va - _vb
                _diff_results.append({
                    'stock_code': _info['stock_code'],
                    'stock_abbr': _info['stock_abbr'],
                    _col_a: round(_va, 2),
                    _col_b: round(_vb, 2),
                    'diff': round(abs(_diff_val) if _is_abs else _diff_val, 2),
                })
            # 阈值过滤（如 "超过10个百分点以上"）
            _diff_thresh = float(_diff_threshold_m.group(1)) if _diff_threshold_m else None
            if _diff_thresh is not None:
                _diff_results = [r for r in _diff_results if r['diff'] > _diff_thresh]
            _diff_results.sort(key=lambda x: x['diff'], reverse=True)
            _diff_results = _diff_results[:_diff_limit]
            for _i, _r in enumerate(_diff_results):
                _r['rank'] = _i + 1
            _short_a = _lbl_a.split('（')[0]
            _short_b = _lbl_b.split('（')[0]
            _diff_columns = [
                {'key': 'rank', 'label': '排名'},
                {'key': 'stock_code', 'label': '股票代码'},
                {'key': 'stock_abbr', 'label': '公司简称'},
                {'key': _col_a, 'label': _lbl_a},
                {'key': _col_b, 'label': _lbl_b},
                {'key': 'diff', 'label': f'{_short_a}与{_short_b}差值' + ('(绝对值)' if _is_abs else '')},
            ]
            _diff_chart = build_bar_chart(
                f'{_short_a}与{_short_b}差值排名',
                [str(r['stock_abbr']) for r in _diff_results[:20]],
                [r['diff'] for r in _diff_results[:20]],
                '#ef4444'
            ) if _diff_results else None
            _diff_sql = (
                f"SELECT stock_code, stock_abbr, {_col_a}, {_col_b}, "
                f"ABS(CAST({_col_a} AS REAL) - CAST({_col_b} AS REAL)) AS diff "
                f"FROM {_tbl_a if _tbl_a == _tbl_b else _tbl_a + ' JOIN ' + _tbl_b} "
                f"WHERE report_period = '{_diff_period}' ORDER BY diff DESC LIMIT {_diff_limit};"
            )
            state.record_step('数据库查询', 'done', f'差值排名: {len(_diff_results)} 家公司')
            _resolve_kb()
            return _ensure_meta(_build_done_reply(
                q, history, _diff_sql, _diff_chart, _diff_columns, _diff_results,
                list(_tables_set), 'metric_ranking',
                {'intent': 'diff_ranking', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                 'report_period': _diff_period},
                kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
            ), state)

    # ── 比例 + 行业均值筛选 + 排名处理器 ──────────────────────────────────
    _is_ratio_avg_query = (
        bool(re.search(r'占.*?比例|比例|占比', compact))
        and bool(re.search(r'行业均值|行业平均|平均值|均值', compact))
        and query_kind in ('ranking', 'metric')
    )
    if _is_ratio_avg_query:
        _emit('数据库查询', 'running', '正在计算行业均值筛选…')
        # 识别分子、分母、排名指标 — 基于问题结构 "A占B比例…C最高"
        _all_m = {**METRIC_ALIASES, **_RANKING_KEYWORD_MAP}
        _ratio_hits: list[tuple[int, int, str, str, str]] = []  # (pos, end, col, table, label)
        for _alias, _mc in _all_m.items():
            _pos = compact.find(_alias)
            if _pos >= 0:
                _dt = _find_table_for_column(_mc)
                _dl = TABLE_METRIC_COLUMNS.get(_dt, {}).get(_mc, _mc)
                if not any(c == _mc for _, _, c, _, _ in _ratio_hits):
                    _ratio_hits.append((_pos, _pos + len(_alias), _mc, _dt, _dl))
        # 去除短别名被长别名包含的重叠匹配
        _ratio_hits.sort(key=lambda x: (x[0], -(x[1] - x[0])))  # by pos asc, length desc
        _deduped: list[tuple[int, int, str, str, str]] = []
        for _h in _ratio_hits:
            if _deduped and _h[0] >= _deduped[-1][0] and _h[1] <= _deduped[-1][1]:
                continue  # shorter overlap, skip
            _deduped.append(_h)
        _ratio_hits = _deduped
        # 找到 "占" 和 "比例/均值" 的位置来划分区域
        _zhan_pos = compact.find('占')
        _bili_m = re.search(r'比例|占比', compact)
        _bili_pos = _bili_m.start() if _bili_m else len(compact)
        _rank_m = re.search(r'(最高|最低|最大|最小)', compact)
        _rank_pos = _rank_m.start() if _rank_m else len(compact)
        # 分子: 最接近"占"的左侧指标（取end最大的，即最靠近"占"的）
        _numer_cands = [(e, mc, dt, dl) for p, e, mc, dt, dl in _ratio_hits if e <= _zhan_pos + 1]
        # 分母: "占"到"比例"之间的指标
        _denom_cands = [(p, mc, dt, dl) for p, e, mc, dt, dl in _ratio_hits if p > _zhan_pos and p < _bili_pos + 5]
        # 排名列: "最高/最低"附近的指标
        _rank_cands = [(abs(p - _rank_pos), mc, dt, dl) for p, e, mc, dt, dl in _ratio_hits if p > _bili_pos]
        _numer_cands.sort(key=lambda x: x[0], reverse=True)  # closest to 占
        _denom_cands.sort(key=lambda x: x[0])  # first after 占
        _rank_cands.sort(key=lambda x: x[0])   # closest to 最高/最低
        _ratio_metrics: list[tuple[str, str, str]] = []
        if _numer_cands:
            _ratio_metrics.append((_numer_cands[0][1], _numer_cands[0][2], _numer_cands[0][3]))
        if _denom_cands:
            _d = _denom_cands[0]
            if not _ratio_metrics or _d[1] != _ratio_metrics[0][0]:
                _ratio_metrics.append((_d[1], _d[2], _d[3]))
        if _rank_cands:
            _rk = _rank_cands[0]
            if not any(_rk[1] == c for c, _, _ in _ratio_metrics):
                _ratio_metrics.append((_rk[1], _rk[2], _rk[3]))
        # 至少需要3个指标：分子、分母、排名指标
        if len(_ratio_metrics) >= 3:
            _numer_col, _numer_tbl, _numer_lbl = _ratio_metrics[0]
            _denom_col, _denom_tbl, _denom_lbl = _ratio_metrics[1]
            _rank_col, _rank_tbl, _rank_lbl = _ratio_metrics[2]
        elif len(_ratio_metrics) == 2:
            _numer_col, _numer_tbl, _numer_lbl = _ratio_metrics[0]
            _denom_col, _denom_tbl, _denom_lbl = _ratio_metrics[1]
            _rank_col, _rank_tbl, _rank_lbl = _ratio_metrics[1]
        else:
            _is_ratio_avg_query = False
    if _is_ratio_avg_query and len(_ratio_metrics) >= 2:
        # 提取 limit
        _ra_limit_m = re.search(r'(?:前|top|TOP|最高的|最大的)\s*(\d+)', compact)
        _ra_limit = int(_ra_limit_m.group(1)) if _ra_limit_m else 200
        # 提取报告期
        _ra_period = period or ''
        if not _ra_period:
            _yr_m = re.search(r'(20\d{2})', compact)
            _yr = int(_yr_m.group(1)) if _yr_m else 2025
            if re.search(r'(第三季度|三季度|[Qq]3)', compact):
                _ra_period = f'{_yr}Q3'
            elif re.search(r'(半年|[Hh][Yy])', compact):
                _ra_period = f'{_yr}HY'
            else:
                _ra_period = f'{_yr}FY'
        # 方向：低于均值还是高于均值
        _ra_below = bool(re.search(r'低于|小于|不超过', compact))
        # 查数据（可能跨表）
        _ra_tables = {_numer_tbl, _denom_tbl, _rank_tbl}
        _ra_data: dict[str, dict[str, Any]] = {}
        for _tbl in _ra_tables:
            _cols = list({c for c, t, _ in _ratio_metrics if t == _tbl})
            _sel = ', '.join(_cols)
            _rows = query_rows(
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_sel} "
                f"FROM {_tbl} WHERE report_period = ?",
                (_ra_period,)
            )
            for _r in _rows:
                _c = str(_r.get('stock_code', '')).strip()
                if _c not in _ra_data:
                    _ra_data[_c] = {'stock_code': _c, 'stock_abbr': _r.get('stock_abbr', '')}
                _ra_data[_c].update({k: _r[k] for k in _cols if k in _r})
        # 计算比例
        _ra_ratios = []
        for _info in _ra_data.values():
            try:
                _nv = float(_info.get(_numer_col) or 0)
                _dv = float(_info.get(_denom_col) or 0)
                _rv = float(_info.get(_rank_col) or 0)
            except (TypeError, ValueError):
                continue
            if abs(_dv) < 1e-6:
                continue
            _pct = round(_nv / _dv * 100, 2)
            _info['_ratio_pct'] = _pct
            _info['_rank_val'] = _rv
            _ra_ratios.append(_info)
        # 行业均值
        if _ra_ratios:
            _avg_pct = round(sum(r['_ratio_pct'] for r in _ra_ratios) / len(_ra_ratios), 2)
        else:
            _avg_pct = 0.0
        # 筛选
        if _ra_below:
            _filtered = [r for r in _ra_ratios if r['_ratio_pct'] < _avg_pct]
        else:
            _filtered = [r for r in _ra_ratios if r['_ratio_pct'] > _avg_pct]
        # 排名方向
        _ra_desc = not bool(re.search(r'最低|最小', compact))
        _filtered.sort(key=lambda x: x['_rank_val'], reverse=_ra_desc)
        _filtered = _filtered[:_ra_limit]
        for _i, _r in enumerate(_filtered):
            _r['rank'] = _i + 1
        _short_n = _numer_lbl.split('（')[0]
        _short_d = _denom_lbl.split('（')[0]
        _short_r = _rank_lbl.split('（')[0]
        _ra_rows = []
        for _r in _filtered:
            _ra_rows.append({
                'rank': _r['rank'],
                'stock_code': _r['stock_code'],
                'stock_abbr': _r['stock_abbr'],
                _numer_col: round(float(_r.get(_numer_col) or 0), 2),
                _denom_col: round(float(_r.get(_denom_col) or 0), 2),
                'ratio_pct': _r['_ratio_pct'],
                _rank_col: round(_r['_rank_val'], 2),
            })
        _ra_columns = [
            {'key': 'rank', 'label': '排名'},
            {'key': 'stock_code', 'label': '股票代码'},
            {'key': 'stock_abbr', 'label': '公司简称'},
            {'key': _numer_col, 'label': _numer_lbl},
            {'key': _denom_col, 'label': _denom_lbl},
            {'key': 'ratio_pct', 'label': f'{_short_n}占{_short_d}比例(%)'},
            {'key': _rank_col, 'label': _rank_lbl},
        ]
        _direction_label = '低于' if _ra_below else '高于'
        _ra_chart = build_bar_chart(
            f'{_short_n}占{_short_d}比例{_direction_label}行业均值({_avg_pct}%)的公司 - {_short_r}排名',
            [str(r['stock_abbr']) for r in _ra_rows[:20]],
            [r[_rank_col] for r in _ra_rows[:20]],
            '#6366f1'
        ) if _ra_rows else None
        _ra_sql = (
            f"SELECT stock_code, stock_abbr, {_numer_col}, {_denom_col}, "
            f"ROUND({_numer_col}*100.0/{_denom_col}, 2) AS ratio_pct, {_rank_col} "
            f"FROM {_numer_tbl} WHERE report_period = '{_ra_period}' "
            f"AND ratio_pct {'<' if _ra_below else '>'} {_avg_pct} "
            f"ORDER BY {_rank_col} {'DESC' if _ra_desc else 'ASC'} LIMIT {_ra_limit};"
        )
        _coverage = f'行业均值({_short_n}占{_short_d}比例): {_avg_pct}%，共{len(_ra_ratios)}家公司参与计算，{len(_filtered)}家{_direction_label}均值。'
        state.record_step('数据库查询', 'done', _coverage)
        _resolve_kb()
        return _ensure_meta(_build_done_reply(
            q, history, _ra_sql, _ra_chart, _ra_columns, _ra_rows,
            list(_ra_tables), 'metric_ranking',
            {'intent': 'ratio_avg_filter', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
             'report_period': _ra_period},
            kb_context, references, emit, deep_think=deep_think,
            data_coverage_note=_coverage, render_mode=render_mode,
        ), state)

    # ── 正负分布处理器（"正负分布"/"正负情况"） ─────────────────────────
    _is_posneg = bool(re.search(r'正负.*分布|正负.*情况|正.*负.*分布|为正.*为负|正值.*负值', compact))
    if _is_posneg and query_kind in ('ranking', 'metric'):
        _emit('数据库查询', 'running', '正在统计正负分布…')
        # resolve metric & table
        _pn_col, _pn_table, _, _ = _parse_ranking_request(compact, metric)
        _pn_col_label = TABLE_METRIC_COLUMNS.get(_pn_table, {}).get(_pn_col, _pn_col)
        _pn_cn = _Q3_CONVERT_FIELDS.get(_pn_col, _pn_col_label)
        # resolve period
        _pn_year_m = re.match(r'^(20\d{2})', period) if period else None
        _pn_year = int(_pn_year_m.group(1)) if _pn_year_m else latest_fy_year()
        _pn_period = period
        if not _pn_period:
            if re.search(r'(第三季度|三季度|[Qq]3)', compact):
                _pn_period = f'{_pn_year}Q3'
            elif re.search(r'(半年|上半年|[Hh][Yy])', compact):
                _pn_period = f'{_pn_year}HY'
            elif re.search(r'(第一季度|一季度|[Qq]1)', compact):
                _pn_period = f'{_pn_year}Q1'
            else:
                _pn_period = f'{_pn_year}FY'
        _pn_db_period = _pn_period.replace('Q3C', 'Q3')

        _pn_rows = query_rows(
            f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_pn_col} "
            f"FROM {_pn_table} WHERE report_period = ? AND {_pn_col} IS NOT NULL",
            (_pn_db_period,)
        )
        _pn_pos = [r for r in _pn_rows if float(r.get(_pn_col, 0) or 0) > 0]
        _pn_neg = [r for r in _pn_rows if float(r.get(_pn_col, 0) or 0) < 0]
        _pn_zero = [r for r in _pn_rows if float(r.get(_pn_col, 0) or 0) == 0]
        _pn_total = len(_pn_rows)
        _pn_pos_cnt = len(_pn_pos)
        _pn_neg_cnt = len(_pn_neg)
        _pn_pos_avg = round(sum(float(r[_pn_col]) for r in _pn_pos) / _pn_pos_cnt, 4) if _pn_pos_cnt else 0
        _pn_neg_avg = round(sum(float(r[_pn_col]) for r in _pn_neg) / _pn_neg_cnt, 4) if _pn_neg_cnt else 0
        _pn_pos_pct = round(_pn_pos_cnt / _pn_total * 100, 2) if _pn_total else 0
        _pn_neg_pct = round(_pn_neg_cnt / _pn_total * 100, 2) if _pn_total else 0

        # Build grouped bar chart: x=["正值公司","负值公司"], series=[数量, 均值]
        _pn_title = f'{format_period(_pn_db_period)} {_pn_cn}正负分布'
        _pn_chart = {
            'title': _pn_title,
            'typeLabel': '分组柱状图',
            'option': {
                'title': {'text': _pn_title, 'textStyle': {'fontSize': 14, 'fontWeight': 600, 'color': '#1f2937'}, 'left': 'center', 'top': 8},
                'tooltip': {'trigger': 'axis'},
                'legend': {'top': 32, 'data': ['公司数量（家）', '净额均值（万元）']},
                'grid': {'left': 24, 'right': 24, 'top': 72, 'bottom': 40, 'containLabel': True},
                'xAxis': {'type': 'category', 'data': [f'正值（{_pn_pos_cnt}家）', f'负值（{_pn_neg_cnt}家）']},
                'yAxis': [
                    {'type': 'value', 'name': '公司数量（家）', 'nameTextStyle': {'fontSize': 11, 'color': '#9ca3af'}},
                    {'type': 'value', 'name': '均值（万元）', 'nameTextStyle': {'fontSize': 11, 'color': '#9ca3af'}},
                ],
                'series': [
                    {
                        'name': '公司数量（家）', 'type': 'bar', 'yAxisIndex': 0,
                        'data': [_pn_pos_cnt, _pn_neg_cnt],
                        'itemStyle': {'color': '#10b981', 'borderRadius': [4, 4, 0, 0]},
                        'barWidth': 50,
                        'label': {'show': True, 'position': 'top', 'fontSize': 13, 'fontWeight': 'bold',
                                  'formatter': '{c}家'},
                    },
                    {
                        'name': '净额均值（万元）', 'type': 'bar', 'yAxisIndex': 1,
                        'data': [round(_pn_pos_avg, 2), round(_pn_neg_avg, 2)],
                        'itemStyle': {'borderRadius': [4, 4, 0, 0]},
                        'barWidth': 50,
                        'label': {'show': True, 'position': 'top', 'fontSize': 13, 'fontWeight': 'bold'},
                    },
                ],
                'color': ['#10b981', '#6366f1'],
            }
        }

        # Build SQL display
        _pn_sql = (
            f"SELECT TRIM(stock_abbr) AS stock_abbr, {_pn_col} "
            f"FROM {_pn_table} WHERE report_period = '{_pn_db_period}' AND {_pn_col} IS NOT NULL;"
        )

        # Build result rows for _build_done_reply
        _pn_result_rows = [
            {'类别': '正值公司', '数量': _pn_pos_cnt, '占比(%)': _pn_pos_pct, f'{_pn_cn}均值(万元)': round(_pn_pos_avg, 2)},
            {'类别': '负值公司', '数量': _pn_neg_cnt, '占比(%)': _pn_neg_pct, f'{_pn_cn}均值(万元)': round(_pn_neg_avg, 2)},
        ]
        if _pn_zero:
            _pn_result_rows.append({'类别': '零值公司', '数量': len(_pn_zero), '占比(%)': round(len(_pn_zero) / _pn_total * 100, 2), f'{_pn_cn}均值(万元)': 0})
        _pn_columns = [
            {'key': '类别', 'label': '类别'},
            {'key': '数量', 'label': '公司数量'},
            {'key': '占比(%)', 'label': '占比(%)'},
            {'key': f'{_pn_cn}均值(万元)', 'label': f'{_pn_cn}均值(万元)'},
        ]

        # Top positive / negative companies for context
        _pn_pos_sorted = sorted(_pn_pos, key=lambda r: float(r[_pn_col]), reverse=True)
        _pn_neg_sorted = sorted(_pn_neg, key=lambda r: float(r[_pn_col]))
        _pn_top_pos = _pn_pos_sorted[:3] if _pn_pos_sorted else []
        _pn_top_neg = _pn_neg_sorted[:3] if _pn_neg_sorted else []

        _coverage = (
            f'{format_period(_pn_db_period)}共{_pn_total}家公司有{_pn_cn}数据。'
            f'正值{_pn_pos_cnt}家（{_pn_pos_pct}%），均值{_pn_pos_avg:,.2f}万元；'
            f'负值{_pn_neg_cnt}家（{_pn_neg_pct}%），均值{_pn_neg_avg:,.2f}万元。'
        )
        if _pn_top_pos:
            _coverage += f'正值最大：' + '、'.join(f"{r.get('stock_abbr','')}" for r in _pn_top_pos[:3]) + '。'
        if _pn_top_neg:
            _coverage += f'负值最大：' + '、'.join(f"{r.get('stock_abbr','')}" for r in _pn_top_neg[:3]) + '。'

        state.record_step('数据库查询', 'done', _coverage)
        _resolve_kb()
        return _ensure_meta(_build_done_reply(
            q, history, _pn_sql, _pn_chart, _pn_columns, _pn_result_rows,
            [_pn_table], 'metric_ranking',
            {'intent': 'pos_neg_distribution', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
             'report_period': _pn_db_period},
            kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
            data_coverage_note=_coverage,
        ), state)

    # ── 多公司查询检测：追问中列举多家公司 → 升级为 ranking 模式 ──
    _all_companies_in_q = resolve_all_companies_in_text(q)
    if len(_all_companies_in_q) >= 3 and query_kind == 'metric':
        query_kind = 'ranking'
        analysis_mode = 'ranking'

    if query_kind == 'ranking':
        # ── 检测是否为指定公司对比（"A与B相比"/"A和B"/多公司列举） ──
        _compare_companies = resolve_all_companies_in_text(q)
        _is_company_compare = len(_compare_companies) >= 2 and bool(
            re.search(r'(相比|对比|比较|和|与|跟|VS|vs)', q)
            or len(_compare_companies) >= 3  # 3家以上公司列举也视为对比
        )
        # 优先从已解析的 period 中提取年份（正确处理"去年"等相对词）
        _period_year_match = re.match(r'^(20\d{2})', period) if period else None
        if _period_year_match:
            year = int(_period_year_match.group(1))
        else:
            year_match = re.search(r'(20\d{2})年', compact)
            year = int(year_match.group(1)) if year_match else latest_fy_year()
        # detect report period suffix from question
        rank_period = period  # use already-resolved period if available
        if not rank_period:
            if re.search(r'前三季度', compact):
                rank_period = f'{year}Q3C'
            elif re.search(r'(第三季度|三季度|[Qq]3|3季报)', compact):
                rank_period = f'{year}Q3'
            elif re.search(r'(第一季度|一季度|[Qq]1|1季报|一季报)', compact):
                rank_period = f'{year}Q1'
            elif re.search(r'(半年|上半年|[Hh][Yy]|半年报|中报)', compact):
                rank_period = f'{year}HY'
            else:
                rank_period = f'{year}FY'
        # dynamic ranking: determine metric, table, direction, limit from question
        rank_col, rank_table, rank_dir, rank_limit = _parse_ranking_request(compact, metric)
        rank_col_label = TABLE_METRIC_COLUMNS.get(rank_table, {}).get(rank_col, rank_col)
        order_dir = 'DESC' if rank_dir == 'desc' else 'ASC'
        # ── 派生周转率排名（"存货周转率"/"应收账款周转率"/"总资产周转率"） ──
        _DERIVED_TURNOVER_MAP = {
            '存货周转率': {
                'numerator': ('operating_expense_cost_of_sales', 'income_sheet', '营业成本'),
                'denominator': ('asset_inventory', 'balance_sheet', '存货'),
                'label': '存货周转率（次）',
            },
            '应收账款周转率': {
                'numerator': ('total_operating_revenue', 'income_sheet', '营业总收入'),
                'denominator': ('asset_accounts_receivable', 'balance_sheet', '应收账款'),
                'label': '应收账款周转率（次）',
            },
            '总资产周转率': {
                'numerator': ('total_operating_revenue', 'income_sheet', '营业总收入'),
                'denominator': ('asset_total_assets', 'balance_sheet', '总资产'),
                'label': '总资产周转率（次）',
            },
        }
        _turnover_key = None
        for _tk in _DERIVED_TURNOVER_MAP:
            if _tk in compact:
                _turnover_key = _tk
                break
        if _turnover_key:
            _tv = _DERIVED_TURNOVER_MAP[_turnover_key]
            _num_col, _num_tbl, _num_lbl = _tv['numerator']
            _den_col, _den_tbl, _den_lbl = _tv['denominator']
            _tv_label = _tv['label']
            _tv_db_period = rank_period.replace('Q3C', 'Q3')
            _emit('数据库查询', 'running', f'正在计算{_turnover_key}排名…')
            # query both tables
            _num_rows = query_rows(
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_num_col} "
                f"FROM {_num_tbl} WHERE report_period = ?",
                (_tv_db_period,)
            )
            _den_rows = query_rows(
                f"SELECT TRIM(stock_code) AS stock_code, {_den_col} "
                f"FROM {_den_tbl} WHERE report_period = ?",
                (_tv_db_period,)
            )
            _den_map = {str(r.get('stock_code', '')).strip(): r.get(_den_col) for r in _den_rows}
            _tv_results: list[dict[str, Any]] = []
            for _r in _num_rows:
                _sc = str(_r.get('stock_code', '')).strip()
                try:
                    _nv = float(_r.get(_num_col) or 0)
                    _dv = float(_den_map.get(_sc) or 0)
                except (TypeError, ValueError):
                    continue
                if _dv > 0 and _nv >= 0:
                    _ratio = round(_nv / _dv, 4)
                    _tv_results.append({
                        'stock_code': _sc,
                        'stock_abbr': _r.get('stock_abbr', ''),
                        _num_col: round(_nv, 2),
                        _den_col: round(_dv, 2),
                        'turnover_rate': _ratio,
                    })
            _tv_results.sort(key=lambda r: r['turnover_rate'], reverse=(rank_dir == 'desc'))
            _tv_results = _tv_results[:rank_limit]
            _tv_rows: list[dict[str, Any]] = []
            for _i, _r in enumerate(_tv_results):
                _tv_rows.append({
                    'rank': _i + 1,
                    'stock_code': _r['stock_code'],
                    'stock_abbr': _r['stock_abbr'],
                    _num_col: _r[_num_col],
                    _den_col: _r[_den_col],
                    'turnover_rate': _r['turnover_rate'],
                })
            _tv_columns = [
                {'key': 'rank', 'label': '排名'},
                {'key': 'stock_code', 'label': '股票代码'},
                {'key': 'stock_abbr', 'label': '公司简称'},
                {'key': _num_col, 'label': f'{_num_lbl}（万元）'},
                {'key': _den_col, 'label': f'{_den_lbl}（万元）'},
                {'key': 'turnover_rate', 'label': _tv_label},
            ]
            _tv_chart_labels = [str(r['stock_abbr']) for r in _tv_rows]
            _tv_chart_vals = [r['turnover_rate'] for r in _tv_rows]
            _tv_chart = {
                'title': f'{format_period(rank_period)} {_turnover_key}排名（前{rank_limit}）',
                'typeLabel': '横向柱状图',
                'option': {
                    'title': {'text': f'{format_period(rank_period)} {_turnover_key}排名（前{rank_limit}）', 'left': 'center'},
                    'tooltip': {'trigger': 'axis'},
                    'grid': {'left': 80, 'right': 30, 'top': 56, 'bottom': 24, 'containLabel': True},
                    'yAxis': {'type': 'category', 'data': _tv_chart_labels, 'inverse': True},
                    'xAxis': {'type': 'value', 'name': '次'},
                    'series': [
                        {'name': _turnover_key, 'type': 'bar', 'data': _tv_chart_vals,
                         'itemStyle': {'color': '#6366f1', 'borderRadius': [0, 4, 4, 0]},
                         'label': {'show': True, 'position': 'right', 'formatter': '{c}次'}},
                    ],
                },
            } if _tv_rows else None
            _tv_sql = (
                f"SELECT a.stock_code, a.stock_abbr, a.{_num_col}, b.{_den_col}, "
                f"ROUND(CAST(a.{_num_col} AS REAL) / CAST(b.{_den_col} AS REAL), 4) AS turnover_rate "
                f"FROM {_num_tbl} a JOIN {_den_tbl} b ON a.stock_code = b.stock_code "
                f"AND a.report_period = b.report_period "
                f"WHERE a.report_period = '{_tv_db_period}' AND CAST(b.{_den_col} AS REAL) > 0 "
                f"ORDER BY turnover_rate {'DESC' if rank_dir == 'desc' else 'ASC'} LIMIT {rank_limit};"
            )
            _tv_note = f'{format_period(rank_period)} {_turnover_key}前{rank_limit}: ' + \
                       '、'.join(f'{r["stock_abbr"]}({r["turnover_rate"]:.2f}次)' for r in _tv_rows[:3])
            state.record_step('数据库查询', 'done', _tv_note)
            _resolve_kb()
            return _ensure_meta(_build_done_reply(
                q, history, _tv_sql, _tv_chart, _tv_columns, _tv_rows,
                [_num_tbl, _den_tbl], 'metric_ranking',
                {'intent': 'derived_turnover_ranking', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                 'report_period': rank_period},
                kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                data_coverage_note=_tv_note,
            ), state)

        # ── 多年对比排名（"对比2024年和2025年...前十"） ──────────────────
        _multi_year_compare_m = re.search(r'(20\d{2})\s*年?\s*[和与及]\s*(20\d{2})\s*年', compact)
        _is_multi_year_compare = bool(
            _multi_year_compare_m
            and re.search(r'(对比|比较|变化|VS|vs)', compact)
        )
        if _is_multi_year_compare:
            _cmp_yr1 = int(_multi_year_compare_m.group(1))
            _cmp_yr2 = int(_multi_year_compare_m.group(2))
            # detect period suffix
            if re.search(r'前三季度', compact):
                _cmp_suffix = 'Q3C'
            elif re.search(r'(第三季度|三季度|[Qq]3)', compact):
                _cmp_suffix = 'Q3'
            elif re.search(r'(第一季度|一季度|[Qq]1)', compact):
                _cmp_suffix = 'Q1'
            elif re.search(r'(半年|[Hh][Yy])', compact):
                _cmp_suffix = 'HY'
            else:
                _cmp_suffix = 'FY'
            _cmp_periods = [f'{_cmp_yr1}{_cmp_suffix}', f'{_cmp_yr2}{_cmp_suffix}']
            _order_expr = f"CAST({rank_col} AS REAL)"
            # query top N for each period
            _cmp_all_rows: dict[str, dict[str, Any]] = {}  # stock_code -> merged row
            _cmp_sql_parts: list[str] = []
            for _cp in _cmp_periods:
                _cp_db = _cp.replace('Q3C', 'Q3')
                _cp_rows = query_rows(
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {rank_col} "
                    f"FROM {rank_table} WHERE report_period = ? AND {rank_col} IS NOT NULL "
                    f"ORDER BY {_order_expr} {order_dir} LIMIT ?",
                    (_cp_db, rank_limit)
                )
                _cmp_sql_parts.append(
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {rank_col} "
                    f"FROM {rank_table} WHERE report_period = '{_cp_db}' AND {rank_col} IS NOT NULL "
                    f"ORDER BY {_order_expr} {order_dir} LIMIT {rank_limit};"
                )
                for _i, _r in enumerate(_cp_rows):
                    _sc = str(_r.get('stock_code', '')).strip()
                    if _sc not in _cmp_all_rows:
                        _cmp_all_rows[_sc] = {
                            'stock_code': _sc,
                            'stock_abbr': _r.get('stock_abbr', ''),
                        }
                    _cmp_all_rows[_sc][f'{rank_col}_{_cp}'] = _r.get(rank_col)
                    _cmp_all_rows[_sc][f'rank_{_cp}'] = _i + 1
            # build result rows (union of both periods' top N)
            _cmp_result: list[dict[str, Any]] = list(_cmp_all_rows.values())
            # sort by first period rank, then second
            _p1, _p2 = _cmp_periods[0], _cmp_periods[1]
            _cmp_result.sort(key=lambda r: (r.get(f'rank_{_p1}', 999), r.get(f'rank_{_p2}', 999)))
            _cmp_columns = [
                {'key': 'stock_code', 'label': '股票代码'},
                {'key': 'stock_abbr', 'label': '公司简称'},
                {'key': f'rank_{_p1}', 'label': f'{format_period(_p1)}排名'},
                {'key': f'{rank_col}_{_p1}', 'label': f'{format_period(_p1)}{rank_col_label.split("（")[0]}'},
                {'key': f'rank_{_p2}', 'label': f'{format_period(_p2)}排名'},
                {'key': f'{rank_col}_{_p2}', 'label': f'{format_period(_p2)}{rank_col_label.split("（")[0]}'},
            ]
            # build grouped bar chart with both periods
            _chart_rows = _cmp_result[:20]
            _chart_labels = [str(r.get('stock_abbr', '')) for r in _chart_rows]
            _p1_label = format_period(_p1)
            _p2_label = format_period(_p2)
            _p1_vals = []
            _p2_vals = []
            for _r in _chart_rows:
                try:
                    _p1_vals.append(float(_r.get(f'{rank_col}_{_p1}') or 0))
                except (TypeError, ValueError):
                    _p1_vals.append(0)
                try:
                    _p2_vals.append(float(_r.get(f'{rank_col}_{_p2}') or 0))
                except (TypeError, ValueError):
                    _p2_vals.append(0)
            _short_label = rank_col_label.split('（')[0]
            _cmp_chart = {
                'title': f'{_p1_label}与{_p2_label}{_short_label}前{rank_limit}对比',
                'typeLabel': '横向柱状图',
                'option': {
                    'title': {'text': f'{_p1_label}与{_p2_label}{_short_label}前{rank_limit}对比', 'left': 'center'},
                    'tooltip': {'trigger': 'axis'},
                    'legend': {'top': 28},
                    'grid': {'left': 80, 'right': 30, 'top': 56, 'bottom': 24, 'containLabel': True},
                    'yAxis': {'type': 'category', 'data': _chart_labels, 'inverse': True},
                    'xAxis': {'type': 'value'},
                    'series': [
                        {'name': _p1_label, 'type': 'bar', 'data': _p1_vals,
                         'itemStyle': {'color': '#6366f1', 'borderRadius': [0, 4, 4, 0]}},
                        {'name': _p2_label, 'type': 'bar', 'data': _p2_vals,
                         'itemStyle': {'color': '#10b981', 'borderRadius': [0, 4, 4, 0]}},
                    ],
                },
            }
            _cmp_sql = '\n'.join(_cmp_sql_parts)
            _cmp_note = f'{_p1_label}前{rank_limit}: {sum(1 for r in _cmp_result if r.get(f"rank_{_p1}"))}家; ' \
                        f'{_p2_label}前{rank_limit}: {sum(1 for r in _cmp_result if r.get(f"rank_{_p2}"))}家; ' \
                        f'合并去重后共{len(_cmp_result)}家。'
            state.record_step('数据库查询', 'done', _cmp_note)
            _resolve_kb()
            return _ensure_meta(_build_done_reply(
                q, history, _cmp_sql, _cmp_chart, _cmp_columns, _cmp_result,
                [rank_table], 'metric_ranking',
                {'intent': 'multi_year_compare', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                 'report_period': ','.join(_cmp_periods)},
                kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                data_coverage_note=_cmp_note,
            ), state)

        # ── 多期筛选型查询（"2022-2025年第三季度...均超过10%的公司"） ──
        _year_range_m = re.search(r'(20\d{2})[-–~至到](20\d{2})', compact)
        if not _year_range_m:
            _year_range_m = re.search(r'(20\d{2})年?[-–~至到](20\d{2})年', compact)
        if _year_range_m and bool(re.search(r'(超过|大于|高于|均超|都超|均大|均高|连续)', compact)):
            _start_yr = int(_year_range_m.group(1))
            _end_yr = int(_year_range_m.group(2))
            if re.search(r'前三季度', compact):
                _range_suffix = 'Q3C'
            elif re.search(r'(第三季度|三季度|[Qq]3)', compact):
                _range_suffix = 'Q3'
            elif re.search(r'(第一季度|一季度|[Qq]1)', compact):
                _range_suffix = 'Q1'
            elif re.search(r'(半年度|半年|[Hh][Yy])', compact):
                _range_suffix = 'HY'
            elif re.search(r'(年度|年报|[Ff][Yy])', compact):
                _range_suffix = 'FY'
            else:
                _range_suffix = 'Q3'
            _multi_ps = [f'{y}{_range_suffix}' for y in range(_start_yr, _end_yr + 1)]
            with open_connection() as _conn:
                _avail = {r[0] for r in _conn.execute(
                    f"SELECT DISTINCT report_period FROM {rank_table} WHERE report_period IS NOT NULL"
                ).fetchall()}
            _multi_ps = [p for p in _multi_ps if p in _avail]
            _thr_m = re.search(r'(?:超过|大于|高于)\s*([\d.]+)', compact)
            _mp_threshold = float(_thr_m.group(1)) if _thr_m else 10.0
            _mp_op = '>'
            if re.search(r'(?:小于|低于|不超过)\s*[\d.]', compact):
                _mp_op = '<'
            if len(_multi_ps) >= 2:
                _mp_placeholders = ','.join(['?'] * len(_multi_ps))
                _mp_exec = (
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, "
                    f"report_period, {rank_col} "
                    f"FROM {rank_table} "
                    f"WHERE report_period IN ({_mp_placeholders}) AND {rank_col} IS NOT NULL"
                )
                _mp_rows = query_rows(_mp_exec, tuple(_multi_ps))
                _mp_by_company: dict[str, dict] = {}
                for _r in _mp_rows:
                    _c = str(_r.get('stock_code', '')).strip()
                    _a = str(_r.get('stock_abbr', '')).strip()
                    _p = _r.get('report_period')
                    _v = _r.get(rank_col)
                    if _c and _p and _v is not None:
                        if _c not in _mp_by_company:
                            _mp_by_company[_c] = {'stock_code': _c, 'stock_abbr': _a, 'periods': {}}
                        try:
                            _mp_by_company[_c]['periods'][_p] = float(_v)
                        except (TypeError, ValueError):
                            pass
                _mp_filtered = []
                for _info in _mp_by_company.values():
                    if len(_info['periods']) < len(_multi_ps):
                        continue
                    if _mp_op == '>':
                        _ok = all(_info['periods'].get(p, -9999) > _mp_threshold for p in _multi_ps)
                    else:
                        _ok = all(_info['periods'].get(p, 9999) < _mp_threshold for p in _multi_ps)
                    if _ok:
                        _row = {'stock_code': _info['stock_code'], 'stock_abbr': _info['stock_abbr']}
                        for _p in _multi_ps:
                            _row[_p] = round(_info['periods'].get(_p, 0), 4)
                        _mp_filtered.append(_row)
                _mp_filtered.sort(
                    key=lambda r: min(r.get(p, 0) for p in _multi_ps), reverse=True
                )
                _mp_col_label = TABLE_METRIC_COLUMNS.get(rank_table, {}).get(rank_col, rank_col)
                _mp_columns = [
                    {'key': 'stock_code', 'label': '股票代码'},
                    {'key': 'stock_abbr', 'label': '公司简称'},
                ] + [{'key': p, 'label': f'{format_period(p)} {_mp_col_label}'} for p in _multi_ps]
                _mp_display_sql = (
                    f"SELECT TRIM(stock_code), TRIM(stock_abbr), report_period, {rank_col} "
                    f"FROM {rank_table} "
                    f"WHERE report_period IN ({','.join(repr(p) for p in _multi_ps)}) "
                    f"AND {rank_col} IS NOT NULL "
                    f"GROUP BY stock_code "
                    f"HAVING COUNT(CASE WHEN {rank_col} {_mp_op} {_mp_threshold} THEN 1 END) = {len(_multi_ps)};"
                )
                _mp_chart = build_bar_chart(
                    f'{_mp_col_label} 连续{len(_multi_ps)}期{_mp_op}{_mp_threshold}%',
                    [str(r['stock_abbr']) for r in _mp_filtered[:20]],
                    [min(r.get(p, 0) for p in _multi_ps) for r in _mp_filtered[:20]],
                    '#10b981'
                ) if _mp_filtered else None
                state.record_step('数据库查询', 'done',
                                  f'多期筛选({",".join(_multi_ps)}): {len(_mp_filtered)}/{len(_mp_by_company)} 家公司满足条件')
                _resolve_kb()
                return _ensure_meta(_build_done_reply(
                    q, history, _mp_display_sql, _mp_chart, _mp_columns, _mp_filtered,
                    [rank_table], 'metric_ranking',
                    {'intent': 'multi_period_filter', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                     'report_period': ','.join(_multi_ps)},
                    kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                ), state)
        # ── 年份范围多期排名（"2022-2025年第三季度的营业总收入"，无过滤条件） ──
        if _year_range_m and not re.search(r'(超过|大于|高于|均超|都超|均大|均高|连续|小于|低于|不超过)', compact):
            _rng_start = int(_year_range_m.group(1))
            _rng_end = int(_year_range_m.group(2))
            if re.search(r'前三季度', compact):
                _rng_suffix = 'Q3C'
            elif re.search(r'(第三季度|三季度|[Qq]3)', compact):
                _rng_suffix = 'Q3'
            elif re.search(r'(第一季度|一季度|[Qq]1)', compact):
                _rng_suffix = 'Q1'
            elif re.search(r'(半年|[Hh][Yy])', compact):
                _rng_suffix = 'HY'
            else:
                _rng_suffix = 'FY'
            _rng_periods = [f'{y}{_rng_suffix}' for y in range(_rng_start, _rng_end + 1)]
            with open_connection() as _conn:
                _rng_avail = {r[0] for r in _conn.execute(
                    f"SELECT DISTINCT report_period FROM {rank_table} WHERE report_period IS NOT NULL"
                ).fetchall()}
            _rng_periods = [p for p in _rng_periods if p in _rng_avail]
            if len(_rng_periods) >= 2:
                _rng_db_periods = [p.replace('Q3C', 'Q3') for p in _rng_periods]
                _rng_ph = ','.join(['?'] * len(_rng_db_periods))
                _rng_rows = query_rows(
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, "
                    f"report_period, {rank_col} "
                    f"FROM {rank_table} WHERE report_period IN ({_rng_ph}) AND {rank_col} IS NOT NULL",
                    tuple(_rng_db_periods)
                )
                # pivot: group by company, each period as a column
                _rng_by_co: dict[str, dict] = {}
                for _r in _rng_rows:
                    _sc = str(_r.get('stock_code', '')).strip()
                    _sa = str(_r.get('stock_abbr', '')).strip()
                    _p = str(_r.get('report_period', '')).strip()
                    _v = _r.get(rank_col)
                    if not _sc or _v is None:
                        continue
                    if _sc not in _rng_by_co:
                        _rng_by_co[_sc] = {'stock_code': _sc, 'stock_abbr': _sa}
                    try:
                        _rng_by_co[_sc][_p] = float(_v)
                    except (TypeError, ValueError):
                        pass
                # sort by latest period value
                _last_p = _rng_db_periods[-1]
                _rng_result = sorted(
                    _rng_by_co.values(),
                    key=lambda r: r.get(_last_p, 0),
                    reverse=(rank_dir == 'desc')
                )[:rank_limit]
                _rng_col_label = TABLE_METRIC_COLUMNS.get(rank_table, {}).get(rank_col, rank_col)
                _rng_short = _rng_col_label.split('（')[0]
                _rng_columns = [
                    {'key': 'stock_code', 'label': '股票代码'},
                    {'key': 'stock_abbr', 'label': '公司简称'},
                ] + [{'key': p, 'label': f'{format_period(p)} {_rng_short}'} for p in _rng_db_periods]
                # multi-series bar chart (top 15)
                _rng_chart_rows = _rng_result[:15]
                _rng_labels = [str(r.get('stock_abbr', '')) for r in _rng_chart_rows]
                _rng_series = []
                _bar_colors = ['#6366f1', '#10b981', '#f59e0b', '#ef4444', '#3b82f6', '#ec4899']
                for _ci, _p in enumerate(_rng_db_periods):
                    _vals = []
                    for _r in _rng_chart_rows:
                        try:
                            _vals.append(float(_r.get(_p, 0)))
                        except (TypeError, ValueError):
                            _vals.append(0)
                    _rng_series.append({
                        'name': format_period(_p),
                        'type': 'bar',
                        'data': _vals,
                        'itemStyle': {'color': _bar_colors[_ci % len(_bar_colors)], 'borderRadius': [0, 4, 4, 0]},
                    })
                _rng_chart = {
                    'title': f'{_rng_short} {format_period(_rng_periods[0])}~{format_period(_rng_periods[-1])} 前{rank_limit}排名',
                    'typeLabel': '横向柱状图',
                    'option': {
                        'title': {'text': f'{_rng_short} 多期排名对比', 'left': 'center'},
                        'tooltip': {'trigger': 'axis'},
                        'legend': {'top': 28},
                        'grid': {'left': 80, 'right': 30, 'top': 56, 'bottom': 24, 'containLabel': True},
                        'yAxis': {'type': 'category', 'data': _rng_labels, 'inverse': True},
                        'xAxis': {'type': 'value'},
                        'series': _rng_series,
                    },
                }
                _rng_sql = (
                    f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, "
                    f"report_period, {rank_col} FROM {rank_table} "
                    f"WHERE report_period IN ({','.join(repr(p) for p in _rng_db_periods)}) "
                    f"AND {rank_col} IS NOT NULL "
                    f"ORDER BY {rank_col} {order_dir};"
                )
                state.record_step('数据库查询', 'done',
                                  f'多期排名: {",".join(format_period(p) for p in _rng_periods)}, 前{rank_limit}家')
                _resolve_kb()
                return _ensure_meta(_build_done_reply(
                    q, history, _rng_sql, _rng_chart, _rng_columns, _rng_result,
                    [rank_table], 'metric_ranking',
                    {'intent': 'multi_period_ranking', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                     'report_period': ','.join(_rng_periods)},
                    kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                ), state)

        # ── 条件筛选型查询（"为负数且...为正数"/"大于X"/"小于X"/"超X"等） ──
        _cond_re = re.compile(r'为(负数?|正数?)|(大于|小于|超过?|低于|高于|不[足超])\s*(\d+(?:\.\d+)?)\s*(亿|万亿|万)?')
        _cond_matches = list(_cond_re.finditer(compact))
        _is_filter_query = len(_cond_matches) >= 1 and bool(re.search(r'(为负|为正|大于|小于|超过?|低于|高于|不[足超])', compact)) and bool(re.search(r'(哪些|筛选|找出|列出|且|满足|符合|、)', compact))
        if _is_filter_query:
            _all_known_metrics = {**METRIC_ALIASES, **_RANKING_KEYWORD_MAP}
            _filter_conditions: list[tuple[str, str, str, float]] = []
            _all_filter_cols: dict[str, str] = {}
            for _cm in _cond_matches:
                _prefix = compact[max(0, _cm.start() - 20):_cm.start()]
                _col = None
                _best_alias = ''
                _best_end = -1
                for _a, _c in _all_known_metrics.items():
                    _pos = _prefix.rfind(_a)
                    if _pos >= 0 and (_pos + len(_a)) > _best_end:
                        _best_end = _pos + len(_a)
                        _col = _c
                        _best_alias = _a
                if not _col:
                    continue
                _tbl = _find_table_for_column(_col)
                if _cm.group(1):
                    _op_text = _cm.group(1)
                    _threshold = 0.0
                else:
                    _op_text = _cm.group(2)
                    _threshold = float(_cm.group(3)) if _cm.group(3) else 0.0
                    _unit = _cm.group(4) or ''
                    if _unit == '亿':
                        _threshold *= 10000  # DB stores in 万元
                    elif _unit == '万亿':
                        _threshold *= 10000 * 10000
                if _op_text in ('负数', '负', '小于', '低于', '不足', '不超'):
                    _filter_conditions.append((_col, _tbl, '<', _threshold))
                else:
                    _filter_conditions.append((_col, _tbl, '>', _threshold))
                _all_filter_cols[_col] = _tbl
            if len(_filter_conditions) >= 2 or (len(_filter_conditions) >= 1 and ('且' in compact or '满足' in compact or '符合' in compact)):
                _db_period = rank_period.replace('Q3C', 'Q3') if rank_period.endswith('Q3C') else rank_period
                _tables_needed: dict[str, list[str]] = {}
                for _col, _tbl in _all_filter_cols.items():
                    _tables_needed.setdefault(_tbl, []).append(_col)
                _table_data: dict[str, dict[str, dict]] = {}
                _sql_parts: list[str] = []
                for _tbl, _cols in _tables_needed.items():
                    _sel = ', '.join(_cols)
                    _exec = (
                        f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_sel} "
                        f"FROM {_tbl} WHERE report_year = ? AND report_period = ?"
                    )
                    _tbl_rows = query_rows(_exec, (year, _db_period))
                    _table_data[_tbl] = {str(r.get('stock_code', '')).strip(): dict(r) for r in _tbl_rows}
                    _sql_parts.append(
                        f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_sel} "
                        f"FROM {_tbl} WHERE report_year = {year} AND report_period = '{_db_period}';"
                    )
                _all_codes = set()
                for _td in _table_data.values():
                    _all_codes |= set(_td.keys())
                _merged: list[dict[str, Any]] = []
                for _code in _all_codes:
                    _row: dict[str, Any] = {'stock_code': _code, 'stock_abbr': ''}
                    for _tbl, _td in _table_data.items():
                        if _code in _td:
                            _row.update(_td[_code])
                    if _row.get('stock_abbr'):
                        _merged.append(_row)
                _filtered: list[dict[str, Any]] = []
                for _row in _merged:
                    _passes = True
                    for _col, _tbl, _op, _thr in _filter_conditions:
                        try:
                            _val = float(_row.get(_col) or 0)
                        except (TypeError, ValueError):
                            _passes = False
                            break
                        if _op == '<' and _val >= _thr:
                            _passes = False
                        elif _op == '>' and _val <= _thr:
                            _passes = False
                    if _passes:
                        _filtered.append(_row)
                _filtered.sort(key=lambda r: float(r.get(rank_col) or 0), reverse=(rank_dir == 'desc'))
                _all_display_cols = list(dict.fromkeys([rank_col] + list(_all_filter_cols.keys())))
                _columns = [
                    {'key': 'stock_code', 'label': '股票代码'},
                    {'key': 'stock_abbr', 'label': '公司简称'},
                ]
                for _dc in _all_display_cols:
                    _lbl = ''
                    for _t_cols in TABLE_METRIC_COLUMNS.values():
                        if _dc in _t_cols:
                            _lbl = _t_cols[_dc]
                            break
                    _columns.append({'key': _dc, 'label': _lbl or _dc})
                sql = '\n'.join(_sql_parts)
                _filter_desc = '、'.join(
                    f'{TABLE_METRIC_COLUMNS.get(_find_table_for_column(c), {}).get(c, c).split("（")[0]}{"<" if o == "<" else ">"}{t}'
                    for c, _, o, t in _filter_conditions
                )
                chart = build_bar_chart(
                    f'{format_period(rank_period)}符合条件的公司',
                    [str(r['stock_abbr']) for r in _filtered[:20]],
                    [float(r.get(_all_display_cols[0]) or 0) for r in _filtered[:20]],
                    '#f59e0b'
                ) if _filtered else None
                state.record_step('数据库查询', 'done', f'条件筛选返回 {len(_filtered)}/{len(_merged)} 家公司')
                _resolve_kb()
                return _ensure_meta(_build_done_reply(
                    q, history, sql, chart, _columns, _filtered,
                    list(_tables_needed.keys()), 'metric_ranking',
                    {'intent': 'conditional_filter', 'query_kind': 'ranking', 'analysis_mode': 'ranking', 'report_period': rank_period},
                    kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                ), state)
        # ── 多指标交集排名（"营业总收入和净利润均排名前五"） ──────────
        _is_intersect_rank = bool(re.search(r'(均排名|都排名|均在前|都在前|均进入前|都进入前|均.*?(?:前|top|TOP)\s*\d)', compact))
        if _is_intersect_rank:
            _all_metrics_kw = {**METRIC_ALIASES, **_RANKING_KEYWORD_MAP}
            _ir_matched = _match_metric_keywords(compact, _all_metrics_kw)
            # 至少需要两个不同的指标
            _ir_cols: list[tuple[str, str, str]] = []  # (col, table, label)
            _ir_seen: set[str] = set()
            for _kw, _mc in _ir_matched:
                if _mc not in _ir_seen:
                    _irt = _find_table_for_column(_mc)
                    if _irt:
                        _ir_lbl = TABLE_METRIC_COLUMNS.get(_irt, {}).get(_mc, _mc)
                        _ir_cols.append((_mc, _irt, _ir_lbl))
                        _ir_seen.add(_mc)
            if len(_ir_cols) >= 2:
                _emit('数据库查询', 'running', f'正在查询多指标交集排名…')
                _db_period_ir = rank_period.replace('Q3C', 'Q3') if rank_period.endswith('Q3C') else rank_period
                _order_dir_ir = 'DESC' if rank_dir == 'desc' else 'ASC'
                # 对每个指标分别查 top N
                _ir_topn_sets: list[set[str]] = []
                _ir_data: dict[str, dict[str, Any]] = {}  # stock_code -> merged row
                _ir_sql_parts: list[str] = []
                for _ic, _it, _il in _ir_cols:
                    _ir_order = f"CAST({_ic} AS REAL)"
                    _ir_rows = query_rows(
                        f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_ic} "
                        f"FROM {_it} WHERE report_period = ? AND {_ic} IS NOT NULL "
                        f"ORDER BY {_ir_order} {_order_dir_ir} LIMIT ?",
                        (_db_period_ir, rank_limit)
                    )
                    _ir_sql_parts.append(
                        f"-- {_il} TOP{rank_limit}\n"
                        f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {_ic} "
                        f"FROM {_it} WHERE report_period = '{_db_period_ir}' AND {_ic} IS NOT NULL "
                        f"ORDER BY {_ir_order} {_order_dir_ir} LIMIT {rank_limit};"
                    )
                    _topn_codes: set[str] = set()
                    for _i, _rr in enumerate(_ir_rows):
                        _sc = str(_rr.get('stock_code', '')).strip()
                        _topn_codes.add(_sc)
                        if _sc not in _ir_data:
                            _ir_data[_sc] = {'stock_code': _sc, 'stock_abbr': _rr.get('stock_abbr', '')}
                        _ir_data[_sc][_ic] = _rr.get(_ic)
                        _ir_data[_sc][f'rank_{_ic}'] = _i + 1
                    _ir_topn_sets.append(_topn_codes)
                # 取交集
                _ir_intersection = _ir_topn_sets[0]
                for _ts in _ir_topn_sets[1:]:
                    _ir_intersection = _ir_intersection & _ts
                _ir_result = [_ir_data[c] for c in _ir_intersection if c in _ir_data]
                # 按第一个指标的排名排序
                _first_rank_key = f'rank_{_ir_cols[0][0]}'
                _ir_result.sort(key=lambda r: r.get(_first_rank_key, 999))
                # 构建列
                _ir_columns = [
                    {'key': 'stock_code', 'label': '股票代码'},
                    {'key': 'stock_abbr', 'label': '公司简称'},
                ]
                for _ic, _it, _il in _ir_cols:
                    _ir_columns.append({'key': f'rank_{_ic}', 'label': f'{_il.split("（")[0]}排名'})
                    _ir_columns.append({'key': _ic, 'label': _il})
                # 构建图表
                _ir_chart_labels = [str(r.get('stock_abbr', '')) for r in _ir_result[:20]]
                _ir_chart_series = []
                _colors = ['#6366f1', '#10b981', '#f59e0b', '#ef4444']
                for _ci, (_ic, _it, _il) in enumerate(_ir_cols):
                    _vals = []
                    for _rr in _ir_result[:20]:
                        try:
                            _vals.append(round(float(_rr.get(_ic) or 0), 2))
                        except (TypeError, ValueError):
                            _vals.append(0)
                    _ir_chart_series.append({
                        'name': _il.split('（')[0], 'type': 'bar', 'data': _vals,
                        'itemStyle': {'color': _colors[_ci % len(_colors)], 'borderRadius': [0, 4, 4, 0]},
                    })
                _metric_names = '与'.join(il.split('（')[0] for _, _, il in _ir_cols)
                _ir_chart = {
                    'title': f'{format_period(rank_period)} {_metric_names}均排名前{rank_limit}',
                    'typeLabel': '横向柱状图',
                    'option': {
                        'title': {'text': f'{format_period(rank_period)} {_metric_names}均排名前{rank_limit}', 'left': 'center'},
                        'tooltip': {'trigger': 'axis'},
                        'legend': {'top': 28},
                        'grid': {'left': 80, 'right': 30, 'top': 56, 'bottom': 24, 'containLabel': True},
                        'yAxis': {'type': 'category', 'data': _ir_chart_labels, 'inverse': True},
                        'xAxis': {'type': 'value'},
                        'series': _ir_chart_series,
                    },
                } if _ir_result else None
                _ir_sql = '\n'.join(_ir_sql_parts)
                _ir_note = f'{_metric_names}均排名前{rank_limit}的公司共{len(_ir_result)}家'
                if _ir_result:
                    _ir_note += '：' + '、'.join(r.get('stock_abbr', '') for r in _ir_result[:5])
                state.record_step('数据库查询', 'done', _ir_note)
                _resolve_kb()
                return _ensure_meta(_build_done_reply(
                    q, history, _ir_sql, _ir_chart, _ir_columns, _ir_result,
                    list(set(t for _, t, _ in _ir_cols)), 'metric_ranking',
                    {'intent': 'intersect_ranking', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                     'report_period': rank_period},
                    kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                    data_coverage_note=_ir_note,
                ), state)

        # ── 检测用户提到的额外指标（同表）一起查出 ──
        _extra_cols: list[str] = []
        _table_cols = TABLE_METRIC_COLUMNS.get(rank_table, {})
        _combined_kw_map = {**METRIC_ALIASES, **_RANKING_KEYWORD_MAP}
        for _kw, _mc in _match_metric_keywords(compact, _combined_kw_map):
            if _mc != rank_col and _mc in _table_cols and _mc not in _extra_cols:
                _extra_cols.append(_mc)
        # "同比"/"年同比" 泛匹配：自动附加同表的 yoy 增长率字段
        if re.search(r'同比|增长率|增速|涨幅', compact):
            _yoy_fields = [c for c in _table_cols if 'yoy_growth' in c]
            for _yf in _yoy_fields:
                if _yf != rank_col and _yf not in _extra_cols:
                    _extra_cols.append(_yf)
        _select_cols = f"TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {rank_col}"
        if _extra_cols:
            _select_cols += ', ' + ', '.join(_extra_cols)
        # ── Q3C（前三季度）: 累加 HY + Q3 单季度 ──
        _is_q3c = rank_period.endswith('Q3C')
        if _is_q3c and rank_col in _Q3C_SUMMABLE_FIELDS:
            _q3c_year = rank_period[:4]
            _q3_period = f'{_q3c_year}Q3'
            _hy_period = f'{_q3c_year}HY'
            _q3_rows = query_rows(
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {rank_col} "
                f"FROM {rank_table} WHERE report_year = ? AND report_period = ? AND {rank_col} IS NOT NULL",
                (year, _q3_period)
            )
            _hy_rows = query_rows(
                f"SELECT TRIM(stock_code) AS stock_code, {rank_col} "
                f"FROM {rank_table} WHERE report_year = ? AND report_period = ?",
                (year, _hy_period)
            )
            _hy_map: dict[str, float] = {}
            for _hr in _hy_rows:
                _hc = str(_hr.get('stock_code', '')).strip()
                _hv = _hr.get(rank_col)
                if _hc and _hv is not None:
                    try:
                        _hy_map[_hc] = float(_hv)
                    except (TypeError, ValueError):
                        pass
            _cum_rows: list[dict[str, Any]] = []
            for _qr in _q3_rows:
                _qc = str(_qr.get('stock_code', '')).strip()
                try:
                    _q3_val = float(_qr.get(rank_col))
                except (TypeError, ValueError):
                    continue
                _cum_val = _q3_val + _hy_map.get(_qc, 0.0)
                _cum_rows.append({
                    'stock_code': _qc,
                    'stock_abbr': _qr['stock_abbr'],
                    rank_col: _cum_val,
                })
            _reverse = (rank_dir == 'desc')
            _cum_rows.sort(key=lambda x: float(x.get(rank_col) or 0), reverse=_reverse)
            # 指定公司对比模式：只保留目标公司
            if _is_company_compare and _compare_companies:
                _cc_set = set(_compare_companies)
                _cum_rows = [r for r in _cum_rows if r.get('stock_abbr', '').strip() in _cc_set]
            rows = _cum_rows[:rank_limit]
            sql = (
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {rank_col} "
                f"FROM {rank_table} WHERE report_period = '{_hy_period}' AND {rank_col} IS NOT NULL;\n"
                f"SELECT TRIM(stock_code) AS stock_code, TRIM(stock_abbr) AS stock_abbr, {rank_col} "
                f"FROM {rank_table} WHERE report_period = '{_q3_period}' AND {rank_col} IS NOT NULL;\n"
                f"SELECT TRIM(stock_code) AS stock_code, SUM({rank_col}) AS {rank_col} FROM ({rank_table}) GROUP BY stock_code ORDER BY {rank_col} {order_dir} LIMIT {rank_limit}"
            )
        else:
            _db_period = rank_period.replace('Q3C', 'Q3') if _is_q3c else rank_period
            _order_expr = f"CAST({rank_col} AS REAL)"
            if _is_company_compare and _compare_companies:
                # ── 指定公司对比模式：只查这些公司 ──
                _cc_placeholders = ','.join(['?'] * len(_compare_companies))
                rows = query_rows(
                    f"SELECT {_select_cols} "
                    f"FROM {rank_table} WHERE report_year = ? AND report_period = ? "
                    f"AND TRIM(stock_abbr) IN ({_cc_placeholders}) AND {rank_col} IS NOT NULL "
                    f"ORDER BY {_order_expr} {order_dir}",
                    tuple([year, _db_period] + _compare_companies)
                )
                _cc_display = "', '".join(_compare_companies)
                sql = (
                    f"SELECT {_select_cols} FROM {rank_table} "
                    f"WHERE report_year = {year} AND report_period = '{_db_period}' "
                    f"AND TRIM(stock_abbr) IN ('{_cc_display}') AND {rank_col} IS NOT NULL "
                    f"ORDER BY {_order_expr} {order_dir};"
                )
            else:
                rows = query_rows(
                    f"SELECT {_select_cols} "
                    f"FROM {rank_table} WHERE report_year = ? AND report_period = ? AND {rank_col} IS NOT NULL "
                    f"ORDER BY {_order_expr} {order_dir} LIMIT ?",
                    (year, _db_period, rank_limit)
                )
                sql = (
                    f"SELECT {_select_cols} FROM {rank_table} "
                    f"WHERE report_year = {year} AND report_period = '{_db_period}' AND {rank_col} IS NOT NULL "
                    f"ORDER BY {_order_expr} {order_dir} LIMIT {rank_limit};"
                )
        ranked_rows = []
        for index, row in enumerate(rows):
            _row_data: dict[str, Any] = {
                'rank': index + 1,
                'stock_code': row.get('stock_code', ''),
                'stock_abbr': row['stock_abbr'],
                rank_col: row[rank_col],
            }
            for _ec in _extra_cols:
                _row_data[_ec] = row.get(_ec)
            ranked_rows.append(_row_data)
        # ── 跨表补充：检测问题中提到的其他表指标并合并 ──
        _cross_extra: list[tuple[str, str, str]] = []  # (col, table, label)
        if ranked_rows:
            _all_metrics = {**METRIC_ALIASES, **_RANKING_KEYWORD_MAP}
            _cross_seen = set()
            for _alias, _mc in _match_metric_keywords(compact, _all_metrics):
                if _mc != rank_col and _mc not in _extra_cols and _mc not in _cross_seen:
                    _xt = _find_table_for_column(_mc)
                    if _xt and _xt != rank_table:
                        _cross_seen.add(_mc)
                        _xlbl = TABLE_METRIC_COLUMNS.get(_xt, {}).get(_mc, _mc)
                        _cross_extra.append((_mc, _xt, _xlbl))
            if _cross_extra:
                _ranked_codes = [str(r.get('stock_code', '')).strip() for r in ranked_rows]
                _db_p_x = rank_period.replace('Q3C', 'Q3') if rank_period.endswith('Q3C') else rank_period
                _xt_groups: dict[str, list[str]] = {}
                for _xc, _xt, _ in _cross_extra:
                    _xt_groups.setdefault(_xt, []).append(_xc)
                for _xt, _xcols in _xt_groups.items():
                    _xsel = ', '.join(_xcols)
                    _xph = ','.join(['?'] * len(_ranked_codes))
                    _xrows = query_rows(
                        f"SELECT TRIM(stock_code) AS stock_code, {_xsel} "
                        f"FROM {_xt} WHERE report_period = ? AND TRIM(stock_code) IN ({_xph})",
                        (_db_p_x, *_ranked_codes)
                    )
                    _xmap = {str(r.get('stock_code', '')).strip(): r for r in _xrows}
                    for _rr in ranked_rows:
                        _rc = str(_rr.get('stock_code', '')).strip()
                        if _rc in _xmap:
                            for _xc in _xcols:
                                _rr[_xc] = _xmap[_rc].get(_xc)
        # ── 同比变化补充：如果用户问"变化幅度"，补查上年同期数据 ──
        _want_yoy_change = bool(re.search(r'(变化幅度|变化|同比变化|同比增[长减]|增减|涨跌)', q))
        if _want_yoy_change and ranked_rows:
            _prev_year = int(year) - 1
            _db_p_yoy = rank_period.replace('Q3C', 'Q3') if rank_period.endswith('Q3C') else rank_period
            _yoy_cols = [rank_col] + list(_extra_cols)
            _ranked_codes = [str(r.get('stock_code', '')).strip() for r in ranked_rows]
            _ph = ','.join(['?'] * len(_ranked_codes))
            _yoy_tables: dict[str, list[str]] = {}
            for _yc in _yoy_cols:
                _yt = _find_table_for_column(_yc)
                if _yt:
                    _yoy_tables.setdefault(_yt, []).append(_yc)
            for _yt, _ycols in _yoy_tables.items():
                _ysel = ', '.join(_ycols)
                _prev_rows = query_rows(
                    f"SELECT TRIM(stock_code) AS stock_code, {_ysel} "
                    f"FROM {_yt} WHERE report_year = ? AND report_period = ? "
                    f"AND TRIM(stock_code) IN ({_ph})",
                    (_prev_year, _db_p_yoy, *_ranked_codes)
                )
                _prev_map = {str(r.get('stock_code', '')).strip(): r for r in _prev_rows}
                for _rr in ranked_rows:
                    _rc = str(_rr.get('stock_code', '')).strip()
                    if _rc in _prev_map:
                        for _yc in _ycols:
                            _prev_key = f'{_yc}_prev_year'
                            _rr[_prev_key] = _prev_map[_rc].get(_yc)
            # 添加到 _extra_cols 以便列头显示
            _yoy_extra_columns: list[tuple[str, str]] = []
            for _yc in _yoy_cols:
                _prev_key = f'{_yc}_prev_year'
                if any(_prev_key in r for r in ranked_rows):
                    _yt = _find_table_for_column(_yc)
                    _lbl = TABLE_METRIC_COLUMNS.get(_yt or '', {}).get(_yc, _yc)
                    _yoy_extra_columns.append((_prev_key, f'{_lbl}({_prev_year}年同期)'))

        actual_count = len(ranked_rows)
        dir_label = '最高' if rank_dir == 'desc' else '最低'
        period_label = format_period(rank_period)

        # ── 行业统计量计算：均值 / 中位数 ──
        _industry_avg_note = ''
        _want_avg = bool(re.search(r'(行业均值|行业平均|均值|平均水平|平均值)', q))
        _want_median = bool(re.search(r'中位数', q))
        if ranked_rows and (_want_avg or _want_median):
            _avg_cols = [rank_col] + list(_extra_cols)
            _db_p_avg = rank_period.replace('Q3C', 'Q3') if rank_period.endswith('Q3C') else rank_period
            _stat_parts: list[str] = []
            for _ac in _avg_cols:
                _at = _find_table_for_column(_ac)
                if not _at:
                    continue
                _all_vals_rows = query_rows(
                    f"SELECT CAST({_ac} AS REAL) AS v "
                    f"FROM {_at} WHERE report_period = ? AND {_ac} IS NOT NULL",
                    (_db_p_avg,)
                )
                _all_vals = sorted([float(r['v']) for r in _all_vals_rows if r.get('v') is not None])
                if not _all_vals:
                    continue
                _cnt = len(_all_vals)
                _ac_label = TABLE_METRIC_COLUMNS.get(_at, {}).get(_ac, _ac)
                _parts = [f'基于{_cnt}家公司']
                if _want_avg:
                    _avg_v = round(sum(_all_vals) / _cnt, 2)
                    _parts.append(f'均值={_avg_v}')
                if _want_median:
                    _mid = _cnt // 2
                    _median_v = round(_all_vals[_mid] if _cnt % 2 else (_all_vals[_mid - 1] + _all_vals[_mid]) / 2, 2)
                    _parts.append(f'中位数={_median_v}')
                _stat_parts.append(f'{_ac_label}行业统计（{"，".join(_parts)}）')
            if _stat_parts:
                _industry_avg_note = '行业统计参考：' + '；'.join(_stat_parts) + '。请在回答中将各公司数据与行业统计值进行对比分析。'

        # chart: show top 20 for readability when too many companies
        chart_rows = ranked_rows[:20] if actual_count > 20 else ranked_rows
        if _is_company_compare and _compare_companies:
            chart_suffix = f'（{"vs".join(_compare_companies)}）'
            chart_title = f'{period_label}{rank_col_label.split("（")[0]}对比{chart_suffix}'
        else:
            chart_suffix = f'（前20名）' if actual_count > 20 else ''
            chart_title = f'{period_label}{rank_col_label.split("（")[0]}排名{chart_suffix}'
        chart = build_bar_chart(
            chart_title,
            [str(row['stock_abbr']) for row in chart_rows],
            [float(row.get(rank_col) or 0) for row in chart_rows],
            '#f59e0b'
        ) if chart_rows else None
        columns = [
            {'key': 'rank', 'label': '排名'},
            {'key': 'stock_code', 'label': '股票代码'},
            {'key': 'stock_abbr', 'label': '公司简称'},
            {'key': rank_col, 'label': rank_col_label},
        ]
        for _ec in _extra_cols:
            _ec_lbl = _table_cols.get(_ec, '')
            if not _ec_lbl:
                for _tc in TABLE_METRIC_COLUMNS.values():
                    if _ec in _tc:
                        _ec_lbl = _tc[_ec]
                        break
            columns.append({'key': _ec, 'label': _ec_lbl or _ec})
        for _xc, _xt, _xlbl in _cross_extra:
            columns.append({'key': _xc, 'label': _xlbl})
        if _want_yoy_change and '_yoy_extra_columns' in dir():
            for _yk, _ylbl in _yoy_extra_columns:
                columns.append({'key': _yk, 'label': _ylbl})
        state.record_step('数据库查询', 'done', f'排名查询返回 {actual_count} 条')
        rank_coverage = _industry_avg_note or ''
        _user_asked_all = bool(re.search(r'所有', compact))
        if not _user_asked_all and rank_limit > actual_count:
            rank_coverage += (
                f'用户要求排名前{rank_limit}的企业，但数据库中{period_label}仅有{actual_count}家企业的数据，已全部列出。'
            )
        _resolve_kb()
        return _ensure_meta(_build_done_reply(
            q, history, sql, chart, columns, ranked_rows,
            [rank_table], 'metric_ranking',
            {'intent': 'metric_ranking', 'metric': rank_col, 'query_kind': 'ranking', 'analysis_mode': 'ranking', 'report_period': rank_period},
            kb_context, references, emit, deep_think=deep_think,
            data_coverage_note=rank_coverage, render_mode=render_mode,
        ), state)

    if query_kind == 'cash_flow':
        if 'company' in required:
            period_text = format_period(period)
            options: list[str] = []
            return _ensure_meta(_build_smart_clarify_reply(
                q, history,
                understood={'报告期': period_text or '尚未指定', '查询类型': '现金流结构'},
                missing='公司名称', options=options,
                source_tables=['cash_flow_sheet'],
                meta={'intent': 'cash_flow', 'required': 'company'}
            ), state)
        if 'report_period' in required:
            raw_periods = available_periods('cash_flow_sheet', company)[-5:]
            options = [format_period(p) for p in raw_periods] if raw_periods else ['最近年报', '最近半年报', '最近季报']
            return _ensure_meta(_build_smart_clarify_reply(
                q, history,
                understood={'公司': company, '查询类型': '现金流结构'},
                missing='报告期', options=options,
                source_tables=['cash_flow_sheet'],
                meta={'intent': 'cash_flow', 'company': company, 'required': 'report_period'}
            ), state)
        resolved_period = resolve_period('cash_flow_sheet', company, period)
        if not resolved_period:
            raw_periods = available_periods('cash_flow_sheet', company)[-5:]
            options = [format_period(p) for p in raw_periods] if raw_periods else []
            return _ensure_meta(_build_smart_clarify_reply(
                q, history,
                understood={'公司': company, '查询类型': '现金流结构', '指定期': format_period(period)},
                missing='有效报告期（指定期不在数据库）', options=options,
                source_tables=['cash_flow_sheet']
            ), state)
        cf_plan = _build_query_plan_with_llm(q, history, resolved)
        cf_labels = TABLE_METRIC_COLUMNS['cash_flow_sheet']
        cf_cols = [c for c in (cf_plan.get('columns') or []) if c in cf_labels] if cf_plan and cf_plan.get('table') == 'cash_flow_sheet' else []
        if not cf_cols:
            cf_cols = ['operating_cf_net_amount', 'investing_cf_net_amount', 'financing_cf_net_amount']
        select_clause = 'report_period, ' + ', '.join(cf_cols)
        exec_sql = f'SELECT {select_clause} FROM cash_flow_sheet WHERE TRIM(stock_abbr) = ? AND report_period = ? LIMIT 1'
        display_sql = f"SELECT {select_clause} FROM cash_flow_sheet WHERE TRIM(stock_abbr) = '{company}' AND report_period = '{resolved_period}' LIMIT 1;"
        rows = query_rows(exec_sql, (company, resolved_period))
        if not rows:
            raise HTTPException(status_code=404, detail=f'未找到 {company} 在 {resolved_period} 的现金流数据')
        row = rows[0]
        chart_labels = [cf_labels.get(c, c).split('（')[0] for c in cf_cols]
        chart_values = [float(row.get(c) or 0) for c in cf_cols]
        detail_rows = [{'item': cf_labels.get(c, c).split('（')[0], 'amount': row.get(c)} for c in cf_cols]
        columns = [{'key': 'item', 'label': '项目'}, {'key': 'amount', 'label': '金额（万元）'}]
        chart = build_pie_chart(f'{company}现金流结构', chart_labels, chart_values) if chart_values else None
        state.record_step('数据库查询', 'done', f'现金流查询返回 {len(detail_rows)} 项')
        _resolve_kb()
        return _ensure_meta(_build_done_reply(
            q, history, display_sql, chart, columns, detail_rows, ['cash_flow_sheet'], 'cash_flow_structure',
            {'intent': 'cash_flow', 'company': company, 'query_kind': 'cash_flow', 'analysis_mode': 'single', 'report_period': resolved_period},
            kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
        ), state)

    if query_kind == 'metric' and 'company' in required:
        period_text = format_period(period)
        metric_text = metric.get('label', '财务数据')
        options: list[str] = []
        return _ensure_meta(_build_smart_clarify_reply(
            q, history,
            understood={'报告期': period_text or '尚未指定', '查询指标': metric_text},
            missing='公司名称', options=options,
            source_tables=['income_sheet'],
            meta={'intent': metric.get('intent', ''), 'metric': metric.get('column', ''), 'required': 'company'}
        ), state)

    if query_kind == 'metric' and 'metric' in required:
        period_text = format_period(period)
        base = company or period_text or '当前查询'
        options = [f'{base}的利润总额', f'{base}的净利润', f'{base}的营业总收入', f'{base}的现金流', f'{base}的资产负债']
        return _ensure_meta(_build_smart_clarify_reply(
            q, history,
            understood={'公司': company or '尚未指定', '报告期': period_text or '尚未指定'},
            missing='财务指标', options=options,
            source_tables=['income_sheet', 'cash_flow_sheet', 'balance_sheet'],
            meta={'company': company, 'report_period': period, 'query_kind': 'metric', 'required': 'metric'}
        ), state)

    _q_has_explicit_company = bool(resolve_company_alias(q))
    if query_kind == 'metric' and metric and analysis_mode == 'trend' and (company or _q_has_explicit_company):
        plan = _build_query_plan_with_llm(q, history, resolved)
        plan_table = plan.get('table', 'income_sheet') if plan else 'income_sheet'
        plan_cols = plan.get('columns', [metric['column']]) if plan else [metric['column']]
        # ── 检测用户提到的额外指标，补充到 plan_cols（支持双轴图/多折线等）──
        _trend_table_cols = TABLE_METRIC_COLUMNS.get(plan_table, {})
        for _alias, _mc in METRIC_ALIASES.items():
            if _alias in compact and _mc not in plan_cols and _mc in _trend_table_cols:
                plan_cols.append(_mc)
        for _kw, _mc in _RANKING_KEYWORD_MAP.items():
            if _kw in compact and _mc not in plan_cols and _mc in _trend_table_cols:
                plan_cols.append(_mc)
        if re.search(r'同比|增长率|增速|涨幅', compact):
            for _yf in _trend_table_cols:
                if 'yoy_growth' in _yf and _yf not in plan_cols:
                    plan_cols.append(_yf)
        # ── 多公司趋势对比 ──
        _trend_companies = resolve_all_companies_in_text(q)
        _is_multi_trend = len(_trend_companies) >= 2 and bool(
            re.search(r'对比|比较|对照|和|与|VS|vs|多.*?折线|多系列', q)
        )
        if _is_multi_trend:
            # 提取报告期范围（如 2022-2025年第三季度）
            _period_suffix_m = re.search(r'(第三季度|三季度|Q3)', compact)
            if _period_suffix_m:
                _psuffix = 'Q3'
            elif re.search(r'(第一季度|一季度|Q1)', compact):
                _psuffix = 'Q1'
            elif re.search(r'(半年|HY)', compact):
                _psuffix = 'HY'
            else:
                _psuffix = 'FY'
            _yr_range = re.findall(r'(20\d{2})', compact)
            _start_yr = int(min(_yr_range)) if _yr_range else 2022
            _end_yr = int(max(_yr_range)) if _yr_range else 2025
            _target_periods = [f'{y}{_psuffix}' for y in range(_start_yr, _end_yr + 1)]
            first_col = plan_cols[0]
            first_label = TABLE_METRIC_COLUMNS.get(plan_table, {}).get(first_col) or metric.get('label', first_col)
            _sel_cols = ', '.join(plan_cols)
            _placeholders = ', '.join(['?'] * len(_target_periods))
            all_rows = []
            series_map: dict[str, list[float]] = {}
            _sql_parts = []
            for _co in _trend_companies:
                _co_rows = query_rows(
                    f"SELECT TRIM(stock_abbr) AS stock_abbr, report_period, {_sel_cols} "
                    f"FROM {plan_table} WHERE TRIM(stock_abbr) = ? AND report_period IN ({_placeholders}) "
                    f"ORDER BY report_year ASC, report_period ASC",
                    (_co, *_target_periods)
                )
                _co_rows = sorted(_co_rows, key=lambda r: period_sort_key(str(r.get('report_period', ''))))
                all_rows.extend(_co_rows)
                # 构建 series：每个公司一条线
                _val_map = {str(r.get('report_period', '')): float(r.get(first_col) or 0) for r in _co_rows}
                series_map[_co] = [_val_map.get(p, 0) for p in _target_periods]
                pass  # no comment needed in display SQL
            chart_labels = [format_period(p) for p in _target_periods]
            chart = build_multi_line_chart(
                f'{first_label}趋势对比',
                chart_labels, series_map
            ) if series_map else None
            plan_col_spec = [
                {'key': 'stock_abbr', 'label': '公司简称'},
                {'key': 'report_period', 'label': '报告期'},
            ] + [{'key': c, 'label': TABLE_METRIC_COLUMNS.get(plan_table, {}).get(c, c)} for c in plan_cols]
            _cos_str = "', '".join(_trend_companies)
            display_sql = (
                f"SELECT stock_abbr, report_period, {_sel_cols} FROM {plan_table} "
                f"WHERE stock_abbr IN ('{_cos_str}') AND report_period IN ({', '.join(repr(p) for p in _target_periods)}) "
                f"ORDER BY stock_abbr, report_year ASC, report_period ASC;"
            )
            state.record_step('数据库查询', 'done', f'多公司趋势: {len(_trend_companies)}家, {len(all_rows)}条')
            _resolve_kb()
            return _ensure_meta(_build_done_reply(
                q, history, display_sql, chart, plan_col_spec, all_rows,
                [plan_table], 'metric_trend',
                {'intent': metric.get('intent', ''), 'company': ', '.join(_trend_companies),
                 'metric': first_col, 'query_kind': 'metric', 'analysis_mode': 'trend'},
                kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
            ), state)
        # ── 单公司趋势 ──
        exec_sql, params, plan_col_spec, display_sql = _compile_plan_to_sql(
            {'table': plan_table, 'columns': plan_cols, 'analysis_mode': 'trend', 'orderBy': None, 'limit': None},
            company, ''
        )
        if not exec_sql:
            plan_table = 'income_sheet'
            exec_sql = f"SELECT report_period, {metric['column']} FROM income_sheet WHERE TRIM(stock_abbr) = ? AND {metric['column']} IS NOT NULL ORDER BY report_year ASC, report_period ASC"
            params = (company,)
            display_sql = f"SELECT report_period, {metric['column']} FROM income_sheet WHERE TRIM(stock_abbr) = '{company}' AND {metric['column']} IS NOT NULL ORDER BY report_year ASC, report_period ASC;"
            plan_col_spec = [{'key': 'report_period', 'label': '报告期'}, {'key': metric['column'], 'label': f"{metric['label']}"}]
        rows = query_rows(exec_sql, params)
        rows = sorted(rows, key=lambda item: period_sort_key(str(item.get('report_period', ''))))
        if not rows:
            raise HTTPException(status_code=404, detail=f'未找到 {company} 的趋势数据')
        first_col = plan_cols[0]
        first_label = TABLE_METRIC_COLUMNS.get(plan_table, {}).get(first_col) or metric.get('label', first_col)
        chart_labels = [str(row['report_period']) for row in rows]
        chart_values = [float(row.get(first_col) or 0) for row in rows]
        chart = build_line_chart(f'{company}{first_label}趋势', chart_labels, chart_values)
        state.record_step('数据库查询', 'done', f'趋势查询返回 {len(rows)} 条')
        _resolve_kb()
        return _ensure_meta(_build_done_reply(
            q, history, display_sql, chart, plan_col_spec, rows, [plan_table], 'metric_trend',
            {'intent': metric.get('intent', ''), 'company': company, 'metric': first_col, 'query_kind': 'metric', 'analysis_mode': 'trend'},
            kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
        ), state)

    # ── 环比增长率专用处理器（Q2 = HY - Q1 推算） ─────────────────────────
    _is_qoq_query = (
        query_kind == 'metric' and metric and company
        and bool(re.search(r'环比', compact))
        and analysis_mode == 'single'
        and 'qoq_growth' not in (metric.get('column') or '')
    )
    if _is_qoq_query:
        _emit('数据库查询', 'running', '正在计算环比增长率…')
        _qoq_col = metric['column']
        _qoq_label = metric.get('label', _qoq_col)
        # 解析目标期间
        _qoq_period = period or ''
        if not _qoq_period:
            _yr_m = re.search(r'(20\d{2})', compact)
            _yr = int(_yr_m.group(1)) if _yr_m else 2025
            if re.search(r'(第三季度|三季度|Q3)', compact):
                _qoq_period = f'{_yr}Q3'
            elif re.search(r'(第一季度|一季度|Q1)', compact):
                _qoq_period = f'{_yr}Q1'
            elif re.search(r'(半年|HY)', compact):
                _qoq_period = f'{_yr}HY'
            else:
                _qoq_period = f'{_yr}FY'
        _qoq_yr = int(_qoq_period[:4])
        _qoq_suffix = _qoq_period[4:]
        # 确定"上期"和推算逻辑
        # Q3 的上期 = Q2 = HY - Q1
        # Q1 的上期 = 上年Q3 - 上年HY（或直接用上年Q3做单季度）
        # HY 的上期 = Q1
        # FY 的上期 = Q3（FY - Q3 = Q4 单季度对比上期 = Q3 单季度）
        _qoq_table = _find_table_for_column(_qoq_col)
        if not _qoq_table:
            _qoq_table = 'income_sheet'
        _cur_val = None
        _prev_val = None
        _prev_label = ''
        _cur_label = format_period(_qoq_period)
        _display_parts = []
        if _qoq_suffix == 'Q3':
            # Q3 单季 = Q3累计, Q2 = HY - Q1
            _q3_rows = query_rows(
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE TRIM(stock_abbr) = ? AND report_period = ?",
                (company, f'{_qoq_yr}Q3')
            )
            _hy_rows = query_rows(
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE TRIM(stock_abbr) = ? AND report_period = ?",
                (company, f'{_qoq_yr}HY')
            )
            _q1_rows = query_rows(
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE TRIM(stock_abbr) = ? AND report_period = ?",
                (company, f'{_qoq_yr}Q1')
            )
            if _q3_rows:
                _cur_val = float(_q3_rows[0].get(_qoq_col) or 0)
            if _hy_rows and _q1_rows:
                _hy_v = float(_hy_rows[0].get(_qoq_col) or 0)
                _q1_v = float(_q1_rows[0].get(_qoq_col) or 0)
                _prev_val = _hy_v - _q1_v
            _prev_label = f'{_qoq_yr}年第二季度'
            _display_parts = [
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE stock_abbr = '{company}' AND report_period = '{_qoq_yr}Q3';",
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE stock_abbr = '{company}' AND report_period = '{_qoq_yr}HY';",
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE stock_abbr = '{company}' AND report_period = '{_qoq_yr}Q1';",
            ]
        elif _qoq_suffix == 'HY':
            _hy_rows = query_rows(
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE TRIM(stock_abbr) = ? AND report_period = ?",
                (company, f'{_qoq_yr}HY')
            )
            _q1_rows = query_rows(
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE TRIM(stock_abbr) = ? AND report_period = ?",
                (company, f'{_qoq_yr}Q1')
            )
            if _hy_rows and _q1_rows:
                _hy_v = float(_hy_rows[0].get(_qoq_col) or 0)
                _q1_v = float(_q1_rows[0].get(_qoq_col) or 0)
                _cur_val = _hy_v - _q1_v  # Q2 单季
                _prev_val = _q1_v
            _cur_label = f'{_qoq_yr}年第二季度'
            _prev_label = f'{_qoq_yr}年第一季度'
            _display_parts = [
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE stock_abbr = '{company}' AND report_period IN ('{_qoq_yr}HY', '{_qoq_yr}Q1');",
            ]
        elif _qoq_suffix == 'Q1':
            _q1_rows = query_rows(
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE TRIM(stock_abbr) = ? AND report_period = ?",
                (company, f'{_qoq_yr}Q1')
            )
            _prev_hy = query_rows(
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE TRIM(stock_abbr) = ? AND report_period = ?",
                (company, f'{_qoq_yr - 1}HY')
            )
            _prev_q3 = query_rows(
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE TRIM(stock_abbr) = ? AND report_period = ?",
                (company, f'{_qoq_yr - 1}Q3')
            )
            if _q1_rows:
                _cur_val = float(_q1_rows[0].get(_qoq_col) or 0)
            if _prev_q3 and _prev_hy:
                _q3_v = float(_prev_q3[0].get(_qoq_col) or 0)
                _hy_v = float(_prev_hy[0].get(_qoq_col) or 0)
                _prev_val = _q3_v - _hy_v  # 上年Q3单季
            _prev_label = f'{_qoq_yr - 1}年第三季度'
            _display_parts = [
                f"SELECT {_qoq_col} FROM {_qoq_table} WHERE stock_abbr = '{company}' AND report_period IN ('{_qoq_yr}Q1', '{_qoq_yr - 1}Q3', '{_qoq_yr - 1}HY');",
            ]
        else:
            _is_qoq_query = False  # FY 环比不常见，跳过
        if _is_qoq_query and _cur_val is not None and _prev_val is not None and _prev_val != 0:
            _qoq_rate = round((_cur_val - _prev_val) / abs(_prev_val) * 100, 2)
            _qoq_rows = [
                {'period': _prev_label, _qoq_col: round(_prev_val, 2), 'qoq_growth_pct': '—'},
                {'period': _cur_label, _qoq_col: round(_cur_val, 2), 'qoq_growth_pct': f'{_qoq_rate}%'},
            ]
            _qoq_columns = [
                {'key': 'period', 'label': '期间'},
                {'key': _qoq_col, 'label': _qoq_label},
                {'key': 'qoq_growth_pct', 'label': '环比增长率'},
            ]
            _qoq_chart = build_bar_chart(
                f'{company}{_qoq_label}环比',
                [_prev_label, _cur_label],
                [round(_prev_val, 2), round(_cur_val, 2)]
            )
            _qoq_sql = '\n'.join(_display_parts) + f'\n-- 环比增长率 = ({round(_cur_val, 2)} - {round(_prev_val, 2)}) / |{round(_prev_val, 2)}| * 100 = {_qoq_rate}%'
            state.record_step('数据库查询', 'done', f'环比: {_prev_label} {round(_prev_val, 2)} → {_cur_label} {round(_cur_val, 2)}, QoQ={_qoq_rate}%')
            _resolve_kb()
            return _ensure_meta(_build_done_reply(
                q, history, _qoq_sql, _qoq_chart, _qoq_columns, _qoq_rows,
                [_qoq_table], 'single_metric',
                {'intent': 'qoq_growth', 'company': company, 'metric': _qoq_col,
                 'query_kind': 'metric', 'analysis_mode': 'single', 'report_period': _qoq_period},
                kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
            ), state)

    if query_kind == 'metric' and metric and (company or _q_has_explicit_company):
        if 'report_period' in required:
            raw_periods = available_periods('income_sheet', company)[-5:]
            if render_mode == RENDER_MODE_BATCH and raw_periods:
                period = raw_periods[-1]
                resolved['period'] = period
                required = [r for r in required if r != 'report_period']
            else:
                options = [format_period(p) for p in raw_periods] if raw_periods else ['最近年报', '最近半年报', '最近季报']
                return _ensure_meta(_build_smart_clarify_reply(
                    q, history,
                    understood={'公司': company, '查询指标': metric['label']},
                    missing='报告期', options=options,
                    source_tables=['income_sheet'],
                    meta={'intent': metric.get('intent', metric['column']), 'company': company, 'metric': metric['column'], 'required': 'report_period'}
                ), state)
        plan = _build_query_plan_with_llm(q, history, resolved)
        plan_table = plan.get('table', 'income_sheet') if plan else 'income_sheet'
        plan_cols = plan.get('columns', [metric['column']]) if plan else [metric['column']]
        # ── 检测用户提到的额外指标，补充到 plan_cols（支持雷达图/瀑布图/饼图等多指标场景） ──
        _single_table_cols = TABLE_METRIC_COLUMNS.get(plan_table, {})
        for _alias, _mc in METRIC_ALIASES.items():
            if _alias in compact and _mc not in plan_cols and _mc in _single_table_cols:
                plan_cols.append(_mc)
        for _kw, _mc in _RANKING_KEYWORD_MAP.items():
            if _kw in compact and _mc not in plan_cols and _mc in _single_table_cols:
                plan_cols.append(_mc)
        resolved_period = resolve_period(plan_table, company, period)
        if not resolved_period and plan_table != 'income_sheet':
            resolved_period = resolve_period('income_sheet', company, period)
            if resolved_period:
                plan_table = 'income_sheet'
        if not resolved_period:
            # ── 同后缀降年回退：2025Q3 不存在 → 尝试 2024Q3, 2023Q3 ──
            _suffix = (period or '')[4:]  # e.g. 'Q3', 'FY', 'HY', 'Q1'
            if _suffix and not _suffix.startswith('FY'):
                _req_year = int((period or '0000')[:4])
                _all_periods = available_periods(plan_table, company)
                for _try_year in range(_req_year - 1, _req_year - 4, -1):
                    _try_p = f'{_try_year}{_suffix}'
                    if _try_p in _all_periods:
                        resolved_period = _try_p
                        break
            # ── YYYYFY 不存在时，尝试查询该年所有可用报告期 ──
            _fy_fallback_done = False
            if not resolved_period and period and period.endswith('FY'):
                _fy_year = period[:4]
                _all_periods = available_periods(plan_table, company)
                _year_periods = sorted([p for p in _all_periods if p.startswith(_fy_year)])
                if _year_periods:
                    _sel_cols = ', '.join(plan_cols)
                    _placeholders = ','.join(['?'] * len(_year_periods))
                    exec_sql = (
                        f"SELECT report_period, {_sel_cols} FROM {plan_table} "
                        f"WHERE TRIM(stock_abbr) = ? AND report_period IN ({_placeholders}) "
                        f"ORDER BY report_period ASC"
                    )
                    params = (company, *_year_periods)
                    display_sql = (
                        f"SELECT report_period, {_sel_cols} FROM {plan_table} "
                        f"WHERE TRIM(stock_abbr) = '{company}' AND report_period IN ({','.join(repr(p) for p in _year_periods)}) "
                        f"ORDER BY report_period ASC;"
                    )
                    rows = query_rows(exec_sql, params)
                    plan_col_spec = [{'key': 'report_period', 'label': '报告期'}] + [
                        {'key': c, 'label': TABLE_METRIC_COLUMNS.get(plan_table, {}).get(c, c)} for c in plan_cols
                    ]
                    if rows:
                        _fy_fallback_done = True
                        state.record_step('数据库查询', 'done', f'{_fy_year}年年报未公布，已查询该年{len(rows)}个报告期数据')
                        _resolve_kb()
                        return _ensure_meta(_build_done_reply(
                            q, history, display_sql, None, plan_col_spec, rows, [plan_table], 'single_metric',
                            {'intent': metric.get('intent', ''), 'company': company, 'metric': plan_cols[0],
                             'query_kind': 'metric', 'analysis_mode': 'single', 'report_period': ','.join(_year_periods)},
                            kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                        ), state)
            if not _fy_fallback_done and not resolved_period:
                raw_periods = available_periods(plan_table, company)[-5:]
                options = [format_period(p) for p in raw_periods] if raw_periods else []
                return _ensure_meta(_build_smart_clarify_reply(
                    q, history,
                    understood={'公司': company, '查询指标': metric.get('label', ''), '指定期': format_period(period)},
                    missing='有效报告期（指定期不在数据库）', options=options,
                    source_tables=[plan_table]
                ), state)
        # ── 多期对比检测（"2025Q3与2024Q3相比"/"变化幅度"等） ──
        _compare_years = re.findall(r'(20\d{2})年', q)
        _has_compare_kw = bool(re.search(r'(相比|对比|比较|变化|同期|分别|与.*季度|和.*季度|跟.*季度|及.*季度)', compact))
        # 如果只提到一个年份但问"变化"，自动补上一年同期
        if _has_compare_kw and len(set(_compare_years)) == 1:
            _only_year = int(_compare_years[0])
            _compare_years = [str(_only_year - 1), str(_only_year)]
        if resolved_period and _has_compare_kw and len(set(_compare_years)) >= 2:
            _suffix = resolved_period[4:]  # e.g. 'Q3', 'FY', 'HY', 'Q1'
            _multi_periods = sorted(set(f'{y}{_suffix}' for y in _compare_years))
            _avail = available_periods(plan_table, company)
            _valid_periods = [p for p in _multi_periods if p in _avail]
            if len(_valid_periods) >= 2:
                _sel_cols = ', '.join(plan_cols)
                _placeholders = ','.join(['?'] * len(_valid_periods))
                exec_sql = (
                    f"SELECT report_period, {_sel_cols} FROM {plan_table} "
                    f"WHERE TRIM(stock_abbr) = ? AND report_period IN ({_placeholders}) "
                    f"ORDER BY report_period ASC"
                )
                params = (company, *_valid_periods)
                display_sql = (
                    f"SELECT report_period, {_sel_cols} FROM {plan_table} "
                    f"WHERE TRIM(stock_abbr) = '{company}' AND report_period IN ({','.join(repr(p) for p in _valid_periods)}) "
                    f"ORDER BY report_period ASC;"
                )
                rows = query_rows(exec_sql, params)
                plan_col_spec = [{'key': 'report_period', 'label': '报告期'}] + [
                    {'key': c, 'label': TABLE_METRIC_COLUMNS.get(plan_table, {}).get(c, c)} for c in plan_cols
                ]
                if rows:
                    first_col = plan_cols[0]
                    _first_label = TABLE_METRIC_COLUMNS.get(plan_table, {}).get(first_col) or metric.get('label', first_col)
                    chart = build_bar_chart(
                        f'{company}{_first_label}同期对比',
                        [str(r['report_period']) for r in rows],
                        [float(r.get(first_col) or 0) for r in rows],
                        '#6366f1'
                    )
                    state.record_step('数据库查询', 'done', f'多期对比查询返回 {len(rows)} 个报告期')
                    _resolve_kb()
                    return _ensure_meta(_build_done_reply(
                        q, history, display_sql, chart, plan_col_spec, rows, [plan_table], 'single_metric',
                        {'intent': metric.get('intent', ''), 'company': company, 'metric': plan_cols[0],
                         'query_kind': 'metric', 'analysis_mode': 'single', 'report_period': ','.join(_valid_periods)},
                        kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                    ), state)
        # ── Q3C（前三季度）单指标：HY + Q3 累加 ──
        _is_q3c_metric = resolved_period.endswith('Q3C')
        _q3c_metric_cols = [c for c in plan_cols if c in _Q3C_SUMMABLE_FIELDS] if _is_q3c_metric else []
        if _is_q3c_metric and _q3c_metric_cols:
            _q3c_year = resolved_period[:4]
            _q3_p = f'{_q3c_year}Q3'
            _hy_p = f'{_q3c_year}HY'
            _sel = ', '.join(_q3c_metric_cols)
            _q3_rows = query_rows(
                f"SELECT report_period, {_sel} FROM {plan_table} WHERE TRIM(stock_abbr) = ? AND report_period = ? LIMIT 1",
                (company, _q3_p)
            )
            _hy_rows = query_rows(
                f"SELECT {_sel} FROM {plan_table} WHERE TRIM(stock_abbr) = ? AND report_period = ? LIMIT 1",
                (company, _hy_p)
            )
            if _q3_rows:
                _combined = dict(_q3_rows[0])
                _combined['report_period'] = resolved_period
                _hy_data = _hy_rows[0] if _hy_rows else {}
                for _fc in _q3c_metric_cols:
                    try:
                        _v_q3 = float(_combined.get(_fc) or 0)
                    except (TypeError, ValueError):
                        _v_q3 = 0.0
                    try:
                        _v_hy = float(_hy_data.get(_fc) or 0)
                    except (TypeError, ValueError):
                        _v_hy = 0.0
                    _combined[_fc] = _v_q3 + _v_hy
                rows = [_combined]
            else:
                rows = []
            display_sql = (
                f"SELECT report_period, {_sel} FROM {plan_table} WHERE TRIM(stock_abbr) = '{company}' AND report_period = '{_hy_p}' LIMIT 1;\n"
                f"SELECT report_period, {_sel} FROM {plan_table} WHERE TRIM(stock_abbr) = '{company}' AND report_period = '{_q3_p}' LIMIT 1;\n"
            )
            plan_col_spec = [{'key': 'report_period', 'label': '报告期'}] + [
                {'key': c, 'label': TABLE_METRIC_COLUMNS.get(plan_table, {}).get(c, c)} for c in _q3c_metric_cols
            ]
        else:
            _db_period = resolved_period.replace('Q3C', 'Q3') if _is_q3c_metric else resolved_period
            exec_sql, params, plan_col_spec, display_sql = _compile_plan_to_sql(
                {'table': plan_table, 'columns': plan_cols, 'analysis_mode': 'single', 'orderBy': None, 'limit': 1},
                company, _db_period
            )
            if not exec_sql:
                plan_table = 'income_sheet'
                exec_sql = f"SELECT report_period, {metric['column']} FROM income_sheet WHERE TRIM(stock_abbr) = ? AND report_period = ? LIMIT 1"
                params = (company, _db_period)
                display_sql = f"SELECT report_period, {metric['column']} FROM income_sheet WHERE TRIM(stock_abbr) = '{company}' AND report_period = '{_db_period}' LIMIT 1;"
                plan_col_spec = [{'key': 'report_period', 'label': '报告期'}, {'key': metric['column'], 'label': metric['label']}]
            rows = query_rows(exec_sql, params)
        if not rows and metric['column'].endswith('_yoy_growth') and company and _db_period:
            # Fallback: compute YoY growth from raw values when pre-computed column is NULL
            _yoy_base_map = {
                'operating_revenue_yoy_growth': ('total_operating_revenue', 'income_sheet'),
                'net_profit_yoy_growth': ('net_profit', 'income_sheet'),
                'asset_total_assets_yoy_growth': ('asset_total_assets', 'balance_sheet'),
                'liability_total_liabilities_yoy_growth': ('liability_total_liabilities', 'balance_sheet'),
            }
            _yoy_info = _yoy_base_map.get(metric['column'])
            if _yoy_info:
                _base_col, _base_table = _yoy_info
                _cur_year_m = re.match(r'^(20\d{2})', _db_period)
                if _cur_year_m:
                    _prev_period = f'{int(_cur_year_m.group(1)) - 1}{_db_period[4:]}'
                    try:
                        _cur_row = query_rows(
                            f"SELECT {_base_col} FROM {_base_table} WHERE TRIM(stock_abbr) = ? AND report_period = ?",
                            (company, _db_period))
                        _prev_row = query_rows(
                            f"SELECT {_base_col} FROM {_base_table} WHERE TRIM(stock_abbr) = ? AND report_period = ?",
                            (company, _prev_period))
                        if _cur_row and _prev_row:
                            _cv = float(_cur_row[0][_base_col])
                            _pv = float(_prev_row[0][_base_col])
                            if _pv != 0:
                                _yoy_val = round((_cv - _pv) / abs(_pv) * 100, 2)
                                rows = [{'report_period': _db_period, 'stock_abbr': company, metric['column']: _yoy_val}]
                                plan_col_spec = [{'key': 'report_period', 'label': '报告期'}, {'key': metric['column'], 'label': metric['label']}]
                                display_sql = (f"SELECT {_base_col} FROM income_sheet WHERE TRIM(stock_abbr) = '{company}' "
                                               f"AND report_period IN ('{_db_period}', '{_prev_period}');")
                    except Exception:
                        pass
        if not rows:
            raise HTTPException(status_code=404, detail=f'未找到 {company} 在 {format_period(resolved_period)} 的{metric.get("label", "指标")}数据')
        first_col = plan_cols[0]
        # ── 单行结果自动补充历史同期数据，避免单条数据画无意义图表（仅交互模式） ──
        _single_chart = None
        if len(rows) == 1 and render_mode != 'batch_export':
            _rp = str(resolved_period)
            _year_m = re.match(r'^(20\d{2})', _rp)
            _suffix = _rp[4:] if _year_m else ''
            if _year_m and _suffix:
                _cur_year = int(_year_m.group(1))
                _hist_periods = [f'{y}{_suffix}' for y in range(_cur_year - 3, _cur_year)]
                _hist_cols_str = ', '.join(plan_cols)
                _ph = ','.join(['?'] * len(_hist_periods))
                _hist_rows = query_rows(
                    f"SELECT report_period, {_hist_cols_str} FROM {plan_table} "
                    f"WHERE TRIM(stock_abbr) = ? AND report_period IN ({_ph}) "
                    f"AND {first_col} IS NOT NULL ORDER BY report_period ASC",
                    tuple([company] + _hist_periods)
                )
                if _hist_rows:
                    rows = _hist_rows + rows
                    rows = sorted(rows, key=lambda r: str(r.get('report_period', '')))
                    _chart_labels = [str(r['report_period']) for r in rows]
                    _chart_values = [float(r.get(first_col) or 0) for r in rows]
                    _first_label = TABLE_METRIC_COLUMNS.get(plan_table, {}).get(first_col) or metric.get('label', first_col)
                    _single_chart = build_bar_chart(
                        f'{company}{_first_label}历年同期对比',
                        _chart_labels, _chart_values
                    )
                    display_sql += (
                        f"\n-- historical same-period supplement\n"
                        f"SELECT report_period, {_hist_cols_str} FROM {plan_table} "
                        f"WHERE TRIM(stock_abbr) = '{company}' AND report_period IN ({','.join(repr(p) for p in _hist_periods)}) "
                        f"AND {first_col} IS NOT NULL ORDER BY report_period ASC;"
                    )
        state.record_step('数据库查询', 'done', f'单指标查询返回 {len(rows)} 条')
        _resolve_kb()
        return _ensure_meta(_build_done_reply(
            q, history, display_sql, _single_chart, plan_col_spec, rows, [plan_table], 'single_metric',
            {'intent': metric.get('intent', ''), 'company': company, 'metric': first_col, 'query_kind': 'metric', 'analysis_mode': 'single', 'report_period': resolved_period},
            kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
        ), state)

    # Skip SQL planners for questions that don't mention a specific company
    # Check the ORIGINAL question text (not the LLM/history-inferred company variable)
    _question_has_company = bool(resolve_company_alias(q))
    _needs_ranking = bool(re.search(
        r'(排[名序]|前\d+|[Tt]op\s*\d+|最[大高低小].*几家|哪[些个几家].*公司|哪[些个几家].*企业|是哪家'
        r'|(?:公司|企业)有哪[些个几]|(?:公司|企业).*有多少'
        r'|满足.*(?:公司|企业|条件)|符合.*(?:公司|企业|条件)'
        r'|(?:超过?|大于|高于|低于|小于|不[足超])[^，。]*(?:的公司|的企业)'
        r'|(?:正增长|负增长|为正|为负|亏损|盈利|均超|均大于|均高于).*(?:的公司|的企业|公司有)'
        r'|统计.*(?:公司|企业|数量|个数)|(?:多少|几)家.*(?:公司|企业)'
        r'|筛选|过滤|(?:营收|收入|利润|负债|现金流).*(?:超|大于|高于|低于).*(?:公司|企业))', q))
    _is_aggregate_late = bool(_AGGREGATE_QUERY_RE.search(q))
    _skip_sql_planner = not _question_has_company and not _needs_ranking and not _is_aggregate_late

    # ── Layer 2.4: 多步管道计划器 (Pipeline Planner) ────────────────────────
    try:
        from .llm_query_planner import get_pipeline_plan, execute_pipeline_plan
        if llm_available() and not _skip_sql_planner:
            _emit('智能查询', 'running', '规则引擎未匹配，尝试多步管道计划…')
            _pipeline = get_pipeline_plan(q)
            if _pipeline and _pipeline.get('steps'):
                _p_rows, _p_sql, _p_tables, _p_cols, _p_title = execute_pipeline_plan(_pipeline)
                if _p_rows:
                    _p_chart = None
                    _chart_type = _pipeline.get('chart_type', 'bar')
                    # bucket 结果特殊处理
                    _is_bucket = 'range' in _p_rows[0] and 'count' in _p_rows[0]
                    if _is_bucket and len(_p_rows) >= 2:
                        _chart_labels = [str(r.get('range', '')) for r in _p_rows]
                        _chart_values = [int(r.get('count', 0)) for r in _p_rows]
                        _p_chart = build_bar_chart(
                            _p_title or '分组统计', _chart_labels, _chart_values, '#6366f1'
                        )
                    else:
                        # 找到排名列以外的第一个数值列作为图表数据
                        _chart_val_key = None
                        for _ck in _p_cols:
                            if _ck['key'] not in ('rank', 'stock_code', 'stock_abbr', 'report_period', 'companies'):
                                _chart_val_key = _ck['key']
                        # 如果有 rank 步骤，用排名指标做图表
                        for _st in reversed(_pipeline['steps']):
                            if _st.get('op') == 'rank':
                                _chart_val_key = _st.get('by', _chart_val_key)
                                break
                        if _chart_val_key and len(_p_rows) >= 2:
                            _chart_labels = [str(r.get('stock_abbr', r.get('stock_code', ''))) for r in _p_rows[:20]]
                            try:
                                _chart_values = [float(r.get(_chart_val_key) or 0) for r in _p_rows[:20]]
                                _p_chart = build_bar_chart(
                                    _p_title or '查询结果', _chart_labels, _chart_values, '#6366f1'
                                )
                            except (TypeError, ValueError):
                                pass
                    # 提取变量信息作为 data_coverage_note
                    _var_lines = [l for l in _p_sql.split('\n') if l.startswith('-- Variable:')]
                    _p_coverage = '; '.join(l.replace('-- Variable: ', '') for l in _var_lines) if _var_lines else None
                    _pre_filter2 = _p_rows[0].get('_total_before_filter') if _p_rows else None
                    if _pre_filter2 and int(_pre_filter2) > len(_p_rows):
                        _filter_note = f'⚠ 查询总共涉及 {int(_pre_filter2)} 家公司，经筛选后有 {len(_p_rows)} 家符合条件。回答时必须先说明"全部 {int(_pre_filter2)} 家公司中，有 {len(_p_rows)} 家符合条件"，不要把筛选后的数量说成全部公司数。'
                        _p_coverage = (_p_coverage + ' ' + _filter_note) if _p_coverage else _filter_note
                    state.record_step('管道计划', 'done', f'返回 {len(_p_rows)} 条结果' + (f' ({_p_coverage})' if _p_coverage else ''))
                    _resolve_kb()
                    return _ensure_meta(_build_done_reply(
                        q, history, _p_sql, _p_chart, _p_cols, _p_rows,
                        _p_tables, 'metric_ranking',
                        {'intent': 'pipeline', 'query_kind': 'ranking', 'analysis_mode': 'ranking'},
                        kb_context, references, emit, deep_think=deep_think,
                        data_coverage_note=_p_coverage, render_mode=render_mode,
                    ), state)
                else:
                    state.record_step('管道计划', 'done', '管道执行无结果，回退')
    except Exception as _pipe_err:
        print(f'[Pipeline] fallback failed: {_pipe_err}', flush=True)
        import traceback; traceback.print_exc()
        state.record_step('管道计划', 'failed', str(_pipe_err))

    # ── Layer 2.5: LLM Query Planner fallback ────────────────────────────────
    try:
        from .llm_query_planner import get_query_plan, execute_query_plan, build_columns_from_plan
        if llm_available() and not _skip_sql_planner:
            _emit('智能查询', 'running', '规则引擎未匹配，尝试 LLM 智能查询计划…')
            _plan = get_query_plan(q)
            if _plan and _plan.get('tables'):
                _plan_rows, _plan_sql, _plan_tables = execute_query_plan(_plan)
                # 0 rows is a valid result for cross-table comparison ("no mismatches")
                _plan_cols = build_columns_from_plan(_plan, _plan_rows) if _plan_rows else []
                _plan_chart = None
                if len(_plan_rows) >= 2:
                    _chart_label_key = next(
                        (k for k in _plan_rows[0] if k not in ('stock_code', 'stock_abbr', 'report_period')), None
                    )
                    if _chart_label_key:
                        _plan_chart = build_bar_chart(
                            '查询结果',
                            [str(r.get('stock_abbr', r.get('stock_code', ''))) for r in _plan_rows[:20]],
                            [float(r.get(_chart_label_key) or 0) for r in _plan_rows[:20]],
                            '#6366f1'
                        )
                if _plan_rows or _is_cross_table_compare:
                    state.record_step('LLM查询计划', 'done', f'返回 {len(_plan_rows)} 条结果')
                    _resolve_kb()
                    return _ensure_meta(_build_done_reply(
                        q, history, _plan_sql, _plan_chart, _plan_cols, _plan_rows,
                        _plan_tables, 'metric_ranking',
                        {'intent': 'llm_planner', 'query_kind': 'ranking', 'analysis_mode': 'ranking',
                         'report_period': ','.join(_plan.get('periods', []))},
                        kb_context, references, emit, deep_think=deep_think, render_mode=render_mode,
                    ), state)
                else:
                    state.record_step('LLM查询计划', 'done', '查询无结果，回退到通用回复')
    except Exception as _plan_err:
        print(f'[LLM-Planner] fallback failed: {_plan_err}', flush=True)
        state.record_step('LLM查询计划', 'failed', str(_plan_err))

    # ── Layer 3: general / KB-based reply ────────────────────────────────────
    _resolve_kb()
    _emit('AI 分析', 'running', '当前问题无需结构化查询，正在生成通用回复…' if not kb_context else '未匹配具体指标，正在基于知识库作答…')
    general = _build_general_llm_reply(q, history, kb_context, references, emit=emit, render_mode=render_mode)
    _emit('AI 分析', 'done', f'回答生成完成，共 {len(general["content"])} 字')
    state.record_step('回答生成', 'done', f'生成内容共 {len(general["content"])} 字')
    refs = references or []
    response: dict[str, Any] = {
        'id': create_id(),
        'role': 'assistant',
        'content': general['content'],
        'followUpHints': general['followUpHints'],
        'sourceTables': [],
        'insights': general['insights'],
        'references': refs,
        'meta': {'query_kind': 'general', 'analysis_mode': 'general'},
        'status': 'done'
    }
    response['attribution'] = build_dynamic_attribution(
        state,
        refs=refs,
        thinking_map=_build_general_thinking_map(q, refs, general['content'], query_kind=query_kind),
    )
    return _ensure_meta(response, state)
