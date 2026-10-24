import re
import os
import json
from datetime import datetime
import sqlite3
from concurrent.futures import ThreadPoolExecutor as _LLMThreadPool

import fitz

_INCOME_FIELDS_DESC = {
    'total_operating_revenue':                   '营业总收入（万元）',
    'operating_revenue_yoy_growth':              '营业收入同比增长率（%，正值增长，负值下降）',
    'net_profit':                                '归属于上市公司股东的净利润（万元）',
    'net_profit_yoy_growth':                     '净利润同比增长率（%）',
    'total_profit':                              '利润总额（万元）',
    'operating_profit':                          '营业利润（万元）',
    'operating_expense_cost_of_sales':           '营业成本（万元）',
    'operating_expense_selling_expenses':        '销售费用（万元）',
    'operating_expense_administrative_expenses': '管理费用（万元）',
    'operating_expense_financial_expenses':      '财务费用（万元）',
    'operating_expense_rnd_expenses':            '研发费用（万元）',
    'other_income':                              '其他收益（万元）',
    'operating_expense_taxes_and_surcharges':    '税金及附加（万元）',
    'total_operating_expenses':                  '营业总成本（万元）',
    'asset_impairment_loss':                     '资产减值损失（万元，损失为负）',
    'credit_impairment_loss':                    '信用减值损失（万元，损失为负）',
}

_BALANCE_FIELDS_DESC = {
    'asset_cash_and_cash_equivalents':        '货币资金（万元）',
    'asset_accounts_receivable':              '应收账款（万元）',
    'asset_inventory':                        '存货（万元）',
    'asset_trading_financial_assets':         '交易性金融资产（万元）',
    'asset_construction_in_progress':         '在建工程（万元）',
    'asset_total_assets':                     '资产总计（万元）',
    'asset_total_assets_yoy_growth':          '总资产同比增长率（%）',
    'liability_accounts_payable':             '应付账款（万元）',
    'liability_advance_from_customers':       '预收账款（万元）',
    'liability_total_liabilities':            '负债合计（万元）',
    'liability_total_liabilities_yoy_growth': '总负债同比增长率（%）',
    'liability_contract_liabilities':         '合同负债（万元）',
    'liability_short_term_loans':             '短期借款（万元）',
    'asset_liability_ratio':                  '资产负债率（%）',
    'equity_unappropriated_profit':           '未分配利润（万元，亏损为负）',
    'equity_total_equity':                    '所有者权益合计（万元）',
}

_CASH_FLOW_FIELDS_DESC = {
    'net_cash_flow':                                '现金及现金等价物净增加额（元）',
    'net_cash_flow_yoy_growth':                     '现金净增加额同比增长率（%）',
    'operating_cf_net_amount':                      '经营活动产生的现金流量净额（万元）',
    'operating_cf_ratio_of_net_cf':                 '经营性现金流净额占净现金流总额的比例（%）',
    'operating_cf_cash_from_sales':                 '销售商品、提供劳务收到的现金（万元）',
    'investing_cf_net_amount':                      '投资活动产生的现金流量净额（万元）',
    'investing_cf_ratio_of_net_cf':                 '投资性现金流净额占净现金流总额的比例（%）',
    'investing_cf_cash_for_investments':            '取得投资支付的现金（万元）',
    'investing_cf_cash_from_investment_recovery':   '收回投资收到的现金（万元）',
    'financing_cf_net_amount':                      '筹资活动产生的现金流量净额（万元）',
    'financing_cf_ratio_of_net_cf':                 '融资性现金流净额占净现金流总额的比例（%）',
    'financing_cf_cash_from_borrowing':             '取得借款收到的现金（万元）',
    'financing_cf_cash_for_debt_repayment':         '偿还债务支付的现金（万元）',
}

_CORE_INDICATORS_FIELDS_DESC = {
    'total_operating_revenue':              '营业总收入（万元）',
    'operating_revenue_yoy_growth':         '营业收入同比增长率（%）',
    'net_profit_10k_yuan':                  '归属于上市公司股东的净利润（万元）',
    'net_profit_yoy_growth':               '净利润同比增长率（%）',
    'eps':                                  '基本每股收益（元/股）',
    'net_asset_per_share':                  '每股净资产（元/股）',
    'roe':                                  '加权平均净资产收益率（%）',
    'operating_cf_per_share':               '每股经营活动产生的现金流量净额（元/股）',
    'net_profit_excl_non_recurring':        '扣除非经常性损益的净利润（万元）',
    'net_profit_excl_non_recurring_yoy':    '扣非净利润同比增长率（%）',
    'gross_profit_margin':                  '销售毛利率（%）',
    'net_profit_margin':                    '销售净利率（%）',
    'roe_weighted_excl_non_recurring':      '扣非加权平均净资产收益率（%）',
    'operating_revenue_qoq_growth':         '营业收入环比增长率（%）',
    'net_profit_qoq_growth':               '净利润环比增长率（%）',
}

_BASE_INFO_KEYS = {'stock_code', 'stock_abbr', 'report_date', 'report_year', 'report_period'}
_NOT_APPLICABLE_RE = re.compile(r'不\s*适\s*用')

# Python 后处理可精确计算的字段，无需 LLM 提取
_PYTHON_COMPUTED_FIELDS = {
    # post_compute_yoy（常见印刷的 yoy 不排除，保留正则+LLM提取机会）
    'asset_total_assets_yoy_growth', 'liability_total_liabilities_yoy_growth',
    'net_cash_flow_yoy_growth',
    # post_compute_qoq
    'operating_revenue_qoq_growth', 'net_profit_qoq_growth',
    # _derive_fields（operating_cf_per_share 保留在 _derive_fields 中作为最终计算兜底，但允许正则+LLM先尝试）
    'asset_liability_ratio', 'net_profit_margin', 'gross_profit_margin',
    'net_asset_per_share',
    # 现金流比率（正则提取阶段已计算）
    'operating_cf_ratio_of_net_cf', 'investing_cf_ratio_of_net_cf',
    'financing_cf_ratio_of_net_cf',
}


class FinancialReportExtractor:
    """财务报告PDF数据提取器"""
    
    def __init__(self, llm_api_key=None, llm_base_url=None, llm_model='gpt-4o-mini'):
        self.stock_info = {}
        self._llm_api_key  = llm_api_key
        self._llm_base_url = (llm_base_url or '').rstrip('/')
        self._llm_model    = llm_model
        
    def parse_filename(self, filename, exchange='sse'):
        """
        解析文件名获取基本信息
        exchange: 'sse' (上交所) 或 'szse' (深交所)
        """
        info = {}
        
        if exchange == 'sse':
            # 上交所格式: 股票代码_报告日期_随机标识.pdf
            match = re.match(r'(\d{6})_(\d{8})_\w+\.pdf', filename)
            if match:
                info['stock_code'] = match.group(1)
                date_str = match.group(2)
                info['report_date'] = date_str
                info['report_year'] = int(date_str[:4])
                
                # 根据日期推断报告期
                month = int(date_str[4:6])
                if month <= 4:
                    info['report_period'] = f"{info['report_year']}Q1"
                elif month <= 6:
                    info['report_period'] = f"{info['report_year']-1}FY"
                elif month <= 8:
                    info['report_period'] = f"{info['report_year']}HY"
                elif month <= 10:
                    info['report_period'] = f"{info['report_year']}Q3"
                else:
                    # 11-12月：无独立季报，通常为补充披露；内容检测会进一步修正
                    info['report_period'] = f"{info['report_year']}FY"
                    
        elif exchange == 'szse':
            # 深交所格式: A股简称：[可选重复简称]年份+报告周期+报告类型[可选：全文/（更正后）等].pdf
            match = re.match(r'(.+?)：.*?(\d{4})年(.+?)报告', filename)
            if match:
                info['stock_abbr'] = match.group(1)
                info['report_year'] = int(match.group(2))
                period_type = match.group(3)
                
                # 映射报告周期
                period_map = {
                    '一季度': 'Q1',
                    '半年度': 'HY',
                    '三季度': 'Q3',
                    '年度': 'FY'
                }
                
                for key, value in period_map.items():
                    if key in period_type:
                        info['report_period'] = f"{info['report_year']}{value}"
                        break
        
        return info
    
    def extract_text_from_pdf(self, pdf_path):
        """从PDF中提取文本"""
        try:
            text = ''
            with fitz.open(pdf_path) as doc:
                for page in doc:
                    text += page.get_text() + '\n'
            return text
        except Exception as e:
            print(f"提取PDF文本失败 {pdf_path}: {e}", flush=True)
            return ''

    def extract_text_with_pages(self, pdf_path):
        """从PDF中提取文本，同时返回每页的 (start_offset, end_offset, page_no) 列表"""
        try:
            text = ''
            page_ranges = []
            with fitz.open(pdf_path) as doc:
                for page_no, page in enumerate(doc):
                    start = len(text)
                    page_text = page.get_text() + '\n'
                    text += page_text
                    page_ranges.append((start, len(text), page_no + 1))
            return text, page_ranges
        except Exception as e:
            print(f"提取PDF文本失败 {pdf_path}: {e}", flush=True)
            return '', []

    @staticmethod
    def _find_page_for_offset(offset, page_ranges):
        """根据字符偏移量找到对应的 PDF 页码"""
        for start, end, page_no in page_ranges:
            if start <= offset < end:
                return page_no
        return None
    
    def clean_number(self, value_str):
        """清洗数字字符串，转换为浮点数"""
        if not value_str or value_str == '-' or value_str == '—':
            return None
        
        # 移除逗号、空格等
        value_str = re.sub(r'\s+', '', str(value_str)).replace(',', '').replace('，', '')
        
        # 处理负数
        is_negative = False
        if '(' in value_str or '（' in value_str:
            is_negative = True
            value_str = value_str.replace('(', '').replace(')', '').replace('（', '').replace('）', '')
        
        # 提取数字
        match = re.search(r'-?[\d,]+\.?\d*', value_str)
        if match:
            try:
                num = float(match.group().replace(',', ''))
                return -num if is_negative else num
            except:
                return None
        return None

    def _normalize_broken_numbers(self, text):
        if not text:
            return ''
        text = re.sub(r'(?<=,\d)\s+(?=\d)', '', text)
        text = re.sub(r'(?<=,\d\d)\s+(?=\d)', '', text)
        text = re.sub(r'(?<=,)\s+(?=\d{3}(?:,|\.\d+))', '', text)
        text = re.sub(r'(?<=\d)\s+(?=,\d{3})', '', text)
        return text

    def _all_occurrences(self, text, keyword):
        positions = []
        start = 0
        while True:
            idx = text.find(keyword, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + len(keyword)
        return positions

    def _select_table_scope(self, text, anchors, end_anchors, markers, *, fallback_chars=12000, min_end_gap=400):
        candidates = []
        for anchor in anchors:
            for idx in self._all_occurrences(text, anchor):
                window = text[idx: idx + fallback_chars]
                header = window[:240]
                score = 0
                if '编制单位' in window[:800]:
                    score += 4
                if any(unit in window[:1200] for unit in ('单位：元', '单位:元', '单位：万元', '单位:万元', '（元）', '（万元）')):
                    score += 2
                for marker, weight in markers:
                    if marker in window:
                        score += weight
                if '审计报告' in text[max(0, idx - 300):idx + 80]:
                    score -= 3
                if '和母公司' in header or '及母公司' in header:
                    score -= 4
                candidates.append((score, idx))

        if not candidates:
            return text, 0

        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _, start_idx = candidates[0]
        end_idx = len(text)
        for end_anchor in end_anchors:
            for pos in self._all_occurrences(text, end_anchor):
                if pos > start_idx + min_end_gap:
                    end_idx = min(end_idx, pos)
                    break
        if end_idx <= start_idx:
            end_idx = min(len(text), start_idx + fallback_chars)
        return text[start_idx:end_idx], start_idx

    def _flex_text_pattern(self, text):
        return ''.join(re.escape(ch) + r'\s*' for ch in text if not ch.isspace())

    def _flex_owner_profit(self):
        """归属于(上市公司|本公司|母公司)股东的净利润 — 灵活匹配"""
        return r'归\s*属\s*于\s*(?:上\s*市\s*公\s*司|本\s*公\s*司|母\s*公\s*司)\s*股\s*东\s*的\s*净\s*利\s*润'

    def _flex_owner_profit_excl(self):
        """归属于(上市公司|本公司|母公司)股东的扣除非经常性损益的净利润 — 灵活匹配"""
        return r'归\s*属\s*于\s*(?:上\s*市\s*公\s*司|本\s*公\s*司|母\s*公\s*司)\s*股\s*东\s*的\s*扣\s*除\s*非\s*经\s*常\s*性\s*损\s*益\s*的\s*净\s*利\s*润'

    def _extract_period_metrics_from_summary(self, text):
        """从"主要会计数据和财务指标"摘要表提取本报告期数值。

        Returns:
            dict: field -> value (float)
            同时在 result 中附带 '_offsets' 字典: field -> (offset_in_full_text, raw_token, row_snippet)
            供 prov_hits 使用以记录正确的摘要表页码。
        """
        anchor = '主要会计数据和财务指标'
        section_start = text.find(anchor)
        if section_start == -1:
            section_start = 0
        section = self._get_section(
            text,
            [anchor],
            ['非经常性损益项目和金额', '二、股东信息', '季度财务报表'],
            max_chars=5000,
        )
        if not section:
            return {}
        section = self._normalize_broken_numbers(section)
        # 单位检测：千元/元/万元 → 除数转万元
        # 限制在前400字符内搜索，避免误匹配后面"营业收入构成 单位：万元"的子表头
        _unit_zone = section[:400]
        import re as _re_unit
        _unit_m = _re_unit.search(r'单位[：:]\s*(千元|万元|人民币元|元)', _unit_zone)
        if _unit_m:
            _unit_text = _unit_m.group(1)
            if '千元' in _unit_text:
                _summary_divisor = 10
            elif '万元' in _unit_text:
                _summary_divisor = 1
            else:
                _summary_divisor = 10000  # 元 or 人民币元
        elif '（元）' in _unit_zone or '(元)' in _unit_zone:
            _summary_divisor = 10000
        else:
            _summary_divisor = 10000  # 默认元
        rows = {
            'total_operating_revenue': (
                self._flex_text_pattern('营业收入'),
                [self._flex_owner_profit()],
                'operating_revenue_yoy_growth',
            ),
            'net_profit': (
                self._flex_owner_profit(),
                [
                    self._flex_owner_profit_excl(),
                    self._flex_text_pattern('经营活动产生的现金流量净额'),
                    self._flex_text_pattern('基本每股收益'),
                ],
                'net_profit_yoy_growth',
            ),
            'net_profit_excl_non_recurring': (
                self._flex_owner_profit_excl(),
                [
                    self._flex_text_pattern('经营活动产生的现金流量净额'),
                    self._flex_text_pattern('基本每股收益'),
                    self._flex_text_pattern('稀释每股收益'),
                    self._flex_text_pattern('加权平均净资产收益率'),
                ],
                'net_profit_excl_non_recurring_yoy',
            ),
        }
        result = {}
        offsets = {}  # field -> (offset_in_full_text, raw_token, snippet)
        na_fields = set()
        for field, (start_pattern, end_patterns, growth_field) in rows.items():
            end_pattern = '|'.join(end_patterns)
            pattern = rf'{start_pattern}(?P<body>[\s\S]{{0,320}}?)(?={end_pattern})'
            match = re.search(pattern, section)
            if not match:
                continue
            body = match.group('body')
            # 处理会计括号负数：(30.09) → -30.09
            body = re.sub(r'[\(\uff08]\s*(\d[\d,]*\.?\d*)\s*[\)\uff09]',
                          lambda m: '-' + m.group(1), body)
            tokens = re.findall(r'-?[\d,]+(?:\.\d+)?%?', body)
            # 摘要表列顺序：本报告期 | 上年同期 | 同比增减(%)
            # 有的PDF增减值带%后缀，有的不带（%写在表头里）
            # 策略：第1个非%token=本期值；带%的token=增长率；
            #       若无%token但有≥3个token，第3个视为增长率（通常绝对值<200）
            value_token = next((t for t in tokens if not t.endswith('%')), None)
            growth_token = next((t for t in tokens if t.endswith('%')), None)
            is_not_applicable = _NOT_APPLICABLE_RE.search(body) is not None
            if is_not_applicable:
                na_fields.add(growth_field)
                growth_token = None
            if growth_token is None and not is_not_applicable and len(tokens) >= 2:
                past_value = False
                for t in tokens:
                    if not past_value:
                        if t == value_token:
                            past_value = True
                        continue
                    cval = self.clean_number(t)
                    if cval is not None and abs(cval) < 10000:
                        growth_token = t
                        break
            if value_token is not None:
                value = self.clean_number(value_token)
                if value is not None:
                    result[field] = value / _summary_divisor
                    abs_offset = section_start + match.start()
                    snippet = match.group(0)[:60]
                    offsets[field] = (abs_offset, value_token, snippet)
            if growth_token is not None and not is_not_applicable:
                growth = self.clean_number(growth_token.rstrip('%'))
                if growth is not None:
                    result[growth_field] = growth
                    abs_offset = section_start + match.start()
                    snippet = match.group(0)[:60]
                    offsets[growth_field] = (abs_offset, growth_token, snippet)
        result['_offsets'] = offsets
        if na_fields:
            result['_na_fields'] = sorted(na_fields)
        return result

    def _detect_report_period_from_text(self, text):
        """从PDF前2000字识别真实报告期，比文件名月份推断更准确。
        返回 (report_period, report_year) 或 (None, None)。
        """
        snippet = text[:2000]
        for pattern, suffix in self._REPORT_PERIOD_PATTERNS:
            m = re.search(pattern, snippet)
            if m:
                year = int(m.group(1))
                return f'{year}{suffix}', year
        return None, None

    def _get_section(self, text, start_anchors, end_anchors=None, max_chars=8000):
        """提取文本中特定报表的段落，用于给LLM提供精准上下文"""
        for anchor in start_anchors:
            idx = text.find(anchor)
            if idx != -1:
                end = len(text)
                if end_anchors:
                    for ea in end_anchors:
                        ei = text.find(ea, idx + len(anchor))
                        if ei != -1:
                            end = min(end, ei)
                return text[idx: min(idx + max_chars, end)]
        return text[:max_chars]

    # 从PDF文本识别报告期的正则模式（优先级由高到低：季报 > 半年报 > 年报）
    _REPORT_PERIOD_PATTERNS = [
        (r'(\d{4})\s*年第?\s*[一１]\s*季度报告', 'Q1'),
        (r'(\d{4})\s*年第?\s*[三３]\s*季度报告', 'Q3'),
        (r'(\d{4})\s*年半年(?:度)?\s*报告',      'HY'),
        (r'(\d{4})\s*年(?:\s*年)?\s*度报告',     'FY'),
    ]

    # 报告期代码 → 中文描述 / 当期列标签
    _PERIOD_CN = {
        'Q1': ('一季度', '本期（1—3月）'),
        'HY': ('半年度', '本期（1—6月）'),
        'Q3': ('三季度', '本期（1—9月）'),
        'FY': ('年度',   '本年度'),
    }

    # 各字段的常见别名（供LLM语义识别）
    _FIELD_ALIASES = {
        'net_profit_yoy_growth':               '净利润增长率 / 归属于上市公司股东的净利润同比增速 / 净利润较上年增减（%）',
        'net_profit_margin':                   '销售净利率（%）',
        'operating_cf_per_share':              '每股经营活动产生的现金流量净额 / 每股经营现金流',
        'operating_cf_net_amount':             '经营活动现金净流量 / 经营现金流净额',
        'investing_cf_net_amount':             '投资活动现金净流量',
        'financing_cf_net_amount':             '筹资活动现金净流量',
        'net_cash_flow_yoy_growth':            '现金净增加额同比增长率 / 货币资金增长率',
        'asset_total_assets_yoy_growth':       '总资产增长率 / 资产规模同比增速',
        'liability_total_liabilities_yoy_growth': '总负债增长率 / 负债规模同比增速',
        'asset_liability_ratio':               '资产负债率（%）',
        'financing_cf_cash_from_borrowing':    '取得借款收到的现金 / 取得借款 / 借款收到的现金',
        'financing_cf_cash_for_debt_repayment':'偿还债务支付的现金 / 归还借款支付的现金',
        'investing_cf_cash_for_investments':   '取得投资支付的现金 / 投资支付的现金',
        'operating_cf_cash_from_sales':        '销售商品、提供劳务收到的现金 / 销售收款 / 经营收款',
        'investing_cf_cash_from_investment_recovery': '收回投资收到的现金 / 投资回收现金 / 处置子公司及其他营业单位收到的现金净额',
        'gross_profit_margin':                 '销售毛利率（%）',
        'roe':                                 '加权平均净资产收益率（%）/ ROE',
        'roe_weighted_excl_non_recurring':      '扣除非经常性损益后的加权平均净资产收益率（%）/ 扣非ROE / 扣非加权平均ROE',
        'eps':                                 '基本每股收益（元/股）',
    }

    # 增长率字段 → ([当期值候选字段], 指标中文名, 是否环比)
    # 用于在LLM提示中注入当期已知值和计算公式
    _YOY_GROWTH_MAP = {
        'net_profit_yoy_growth':                  (['net_profit', 'net_profit_10k_yuan'],  '归属于上市公司股东的净利润', False),
        'operating_revenue_yoy_growth':           (['total_operating_revenue'],            '营业总收入',             False),
        'asset_total_assets_yoy_growth':          (['asset_total_assets'],                 '资产总计',               False),
        'liability_total_liabilities_yoy_growth': (['liability_total_liabilities'],        '负债合计',               False),
        'net_cash_flow_yoy_growth':               (['net_cash_flow'],                      '现金及现金等价物净增加额', False),
        'operating_revenue_qoq_growth':           (['total_operating_revenue'],            '营业总收入',             True),
        'net_profit_qoq_growth':                  (['net_profit', 'net_profit_10k_yuan'],  '归属于上市公司股东的净利润', True),
    }

    def llm_fill_missing(self, text, data, field_descs, table_label):
        """LLM兜底：对正则未提取到的字段（值为None）调用LLM补充提取（httpx直接请求）。
        返回 (data, llm_filled_fields)，其中 llm_filled_fields 为本次LLM实际填充的字段名列表。"""
        if not self._llm_api_key:
            return data, []

        na_fields = set(data.get('_na_fields') or [])

        missing = {
            k: v for k, v in field_descs.items()
            if k not in _BASE_INFO_KEYS and k not in _PYTHON_COMPUTED_FIELDS
               and k not in na_fields
               and data.get(k) is None
        }
        if not missing:
            return data, []

        # ── 报告期信息 ──────────────────────────────────────────────────
        report_period = data.get('report_period', '')
        report_year   = data.get('report_year', '')
        period_suffix = report_period[4:] if len(report_period) > 4 else ''
        period_cn, current_label = self._PERIOD_CN.get(
            period_suffix, ('报告期', '当期')
        )
        period_full = f"{report_year}年{period_cn}" if report_year else report_period

        # ── 已提取字段（供LLM推算派生指标）──────────────────────────────
        extracted = {
            k: v for k, v in data.items()
            if k not in _BASE_INFO_KEYS and v is not None
        }
        extracted_str = '\n'.join(
            f'  {k} = {v}' for k, v in extracted.items()
        ) if extracted else '  （无）'

        # ── 待提取字段（附别名，不含计算提示）──────────────────────────
        lines = []
        extracted_unit_desc = (
            '单位按字段约定（net_cash_flow为元，其余金额为万元）或%'
            if table_label == '现金流量表' else
            '单位已为万元或%'
        )
        for k, desc in missing.items():
            alias = self._FIELD_ALIASES.get(k, '')
            alias_part = f'  [别名: {alias}]' if alias else ''
            lines.append(f'  - {k}: {desc}{alias_part}')
        fields_str = '\n'.join(lines)

        if table_label == '现金流量表':
            unit_rule = (
                "2. **单位规则（务必严格执行换算）**：\n"
                "   - net_cash_flow 统一为**元**：原文'万元'×10000，'千元'×1000，'亿元'×100000000，'元'不变。\n"
                "   - 其余金额字段统一为**万元**：原文'元'÷10000，'千元'÷10，'亿元'×10000，'万元'不变。\n"
                "   - 百分比/增长率/比率字段返回纯数字（如8.5表示8.5%），不含%符号。\n"
                "   - 请先查看表头的「单位：xxx」标注确定原文单位，再做换算。\n"
            )
        elif table_label == '核心业绩指标':
            unit_rule = (
                "2. **单位规则（务必严格执行换算）**：\n"
                "   - 金额字段（营业收入/净利润/扣非净利润）统一为**万元**："
                "原文'元'÷10000，'千元'÷10，'亿元'×10000，'万元'不变。\n"
                "   - 每股字段（eps/每股净资产/每股经营现金流）统一为**元/股**，不做换算。\n"
                "   - 百分比字段（ROE/毛利率/净利率/同比增减/环比增减）返回纯数字，不含%符号。\n"
                "   - 请先查看表头的「单位：xxx」标注确定原文单位，再做换算。\n"
            )
        else:
            unit_rule = (
                "2. **单位规则（务必严格执行换算）**：\n"
                "   - 所有金额字段统一为**万元**：原文'元'÷10000，'千元'÷10，'亿元'×10000，'万元'不变。\n"
                "   - 百分比/增长率字段返回纯数字（如8.5表示8.5%），不含%符号。\n"
                "   - 减值损失为负数（如资产减值损失-500万元返回-500），转回收益才为正数。\n"
                "   - 会计括号负数 (30.09) 应视为 -30.09。\n"
                "   - 请先查看表头的「单位：xxx」标注确定原文单位，再做换算。\n"
            )

        # ── 列选择规则（Q3核心指标需特殊说明防止LLM误取累计列）────────────
        if period_suffix == 'Q3' and table_label == '核心业绩指标':
            col_rule = (
                "1. **只取「本报告期」列（单季度）**：主要会计数据和财务指标表含"
                "「本报告期」（7—9月单季，Jul-Sep）和「年初至报告期末」（1—9月累计）"
                "两个数据列，只提取**「本报告期」**列的值，"
                "绝对不要取「年初至报告期末」列的数值。\n"
            )
        elif period_suffix == 'Q3':
            col_rule = (
                "1. **只取当期数据**：报表含当期（1—9月累计）和上期（上年同期1—9月）两列，"
                "只提取**当期**列的值，绝对不要取上期/去年同期的数值。\n"
            )
        else:
            col_rule = (
                f"1. **只取当期数据**：报表通常含当期（{current_label}）和上期两列，"
                f"只提取**当期**列的值，绝对不要取上期/去年同期的数值。\n"
            )

        prompt = (
            f"你是专业的A股财务报告数据提取助手。请从下方【财报文本】中提取{table_label}的指定字段。\n\n"
            f"## 报告信息\n"
            f"- 报告期：{period_full}（代码：{report_period}）\n"
            f"- 股票代码：{data.get('stock_code', '未知')}\n\n"
            f"## 已提取字段（仅供参考，{extracted_unit_desc}）\n"
            f"{extracted_str}\n\n"
            f"## 待提取字段（字段名: 含义  [别名/算法]）\n"
            f"{fields_str}\n\n"
            f"## 提取规则\n"
            f"{col_rule}"
            f"{unit_rule}"
            f"3. **语义等价**：字段与原文表述可能不同，请依经济含义匹配，参考字段旁的[别名]提示。\n"
            f"4. **禁止自行计算**：只提取文本中明确出现的数值。增长率、比率等若文本未直接给出，返回null，由后续程序计算。\n"
            f"5. **不确定返回null**：若当期数据在文本中确实不存在，返回null，不要猜测或用上期值代替。\n"
            f"6. 仅返回JSON对象，键为字段名，不含任何额外说明。\n\n"
            f"## 财报文本\n{text}"
        )

        llm_filled_fields = []
        import httpx, time as _time
        _MAX_RETRIES = 5
        _use_json_format = True  # 首次尝试使用 response_format，不支持则降级
        for _attempt in range(_MAX_RETRIES + 1):
            try:
                req_body = {
                    "model": self._llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                }
                if _use_json_format:
                    req_body["response_format"] = {"type": "json_object"}
                resp = httpx.post(
                    f"{self._llm_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._llm_api_key}"},
                    json=req_body,
                    timeout=120,
                )
                # 如果模型不支持 response_format，API 可能返回 400/422
                if resp.status_code in (400, 422) and _use_json_format:
                    _use_json_format = False
                    print(f"  LLM不支持response_format，降级为纯文本模式重试", flush=True)
                    continue
                resp.raise_for_status()
                raw_content = resp.json()["choices"][0]["message"]["content"]
                # 尝试解析 JSON（兼容模型返回 ```json ... ``` 包裹的情况）
                _stripped = raw_content.strip()
                if _stripped.startswith('```'):
                    # 去掉 markdown 代码块
                    _lines = _stripped.split('\n')
                    _stripped = '\n'.join(_lines[1:-1] if _lines[-1].strip() == '```' else _lines[1:])
                try:
                    result = json.loads(_stripped)
                except json.JSONDecodeError:
                    # JSON 解析失败，视为可重试错误
                    raise ValueError(f"LLM返回非JSON内容: {raw_content[:200]}")
                if not isinstance(result, dict) or not result:
                    raise ValueError(f"LLM返回空或非dict结果: {raw_content[:200]}")
                for field, value in result.items():
                    if field in field_descs and value is not None and data.get(field) is None:
                        data[field] = value
                        llm_filled_fields.append(field)
                if llm_filled_fields:
                    print(f"  LLM补充{table_label}: {len(llm_filled_fields)}个字段", flush=True)
                break  # 成功，退出重试循环
            except Exception as e:
                if _attempt < _MAX_RETRIES:
                    wait = min(2 ** _attempt, 30)  # 指数退避: 1s, 2s, 4s, 8s, 16s (上限30s)
                    print(f"  LLM请求{table_label}失败(第{_attempt+1}/{_MAX_RETRIES+1}次)，{wait}s后重试: {e}", flush=True)
                    _time.sleep(wait)
                else:
                    print(f"  ✗ LLM提取{table_label}最终失败(已重试{_MAX_RETRIES}次): {e}", flush=True)

        # ── 后验合理性检查：若LLM补充值与另一已知字段完全相同，疑似混淆，置None ──
        # 同比与环比不应相等（除非恰好如此，但LLM常将同比值复用为环比）
        _confusion_pairs = [
            ('net_profit_yoy_growth',        'operating_revenue_yoy_growth'),
            ('operating_revenue_qoq_growth', 'operating_revenue_yoy_growth'),
            ('net_profit_qoq_growth',        'net_profit_yoy_growth'),
            ('net_profit_qoq_growth',        'operating_revenue_yoy_growth'),
        ]
        for suspect, reference in _confusion_pairs:
            sv = data.get(suspect)
            rv = data.get(reference)
            if sv is not None and rv is not None and sv == rv and suspect in missing:
                data[suspect] = None

        # Q1/Q3单季报不含上一季度数据，LLM无法可靠计算环比，一律置None
        period_suffix = report_period[4:] if len(report_period) > 4 else ''
        if period_suffix in ('Q1', 'Q3'):
            for qoq_field in ('operating_revenue_qoq_growth', 'net_profit_qoq_growth'):
                if qoq_field in missing and data.get(qoq_field) is not None:
                    data[qoq_field] = None

        # 后验检查可能将部分字段清空，从 filled 列表中移除
        llm_filled_fields = [f for f in llm_filled_fields if data.get(f) is not None]

        return data, llm_filled_fields

    # 计算字段的公式描述（用于 provenance 展示）
    _CALC_FORMULAS = {
        'asset_liability_ratio':    '负债合计 ÷ 资产总计 × 100',
        'net_profit_margin':        '净利润 ÷ 营业总收入 × 100',
        'gross_profit_margin':      '(营业总收入 − 营业成本) ÷ 营业总收入 × 100',
        'operating_cf_per_share':   '经营活动现金流净额 ÷ 总股本',
        'net_asset_per_share':      '归属净资产 ÷ 总股本',
        'net_cash_flow_yoy_growth': '(本期现金净增加额 − 上期) ÷ |上期| × 100',
        'operating_cf_ratio_of_net_cf':  '经营活动净额 ÷ 现金净增加额 × 100',
        'investing_cf_ratio_of_net_cf':  '投资活动净额 ÷ 现金净增加额 × 100',
        'financing_cf_ratio_of_net_cf':  '筹资活动净额 ÷ 现金净增加额 × 100',
        'total_operating_expenses':        '营业成本 + 销售费用 + 管理费用 + 财务费用 + 研发费用 + 税金及附加',
    }

    def _derive_fields(self, results, prov_hits=None):
        """正则提取后、LLM兜底前：基于已提取的当期数据直接推算派生指标，减少LLM负担。
        
        可推算字段：
          - asset_liability_ratio        = 负债合计 / 资产总计 × 100
          - net_profit_margin            = 净利润 / 营业总收入 × 100
          - gross_profit_margin          = (营业总收入 - 营业成本) / 营业总收入 × 100
          - net_profit_yoy_growth        需从income_sheet的当期/上期行提取（文本中有上期列）
        """
        income  = results['income_sheet']
        balance = results['balance_sheet']
        core    = results['core_indicators']

        def _pct(a, b, decimals=4):
            """a/b × 100，安全除法"""
            if a is not None and b is not None and b != 0:
                return round(a / b * 100, decimals)
            return None

        def _mark_calc(table_key, field):
            """标记某字段为计算得出，记录到 prov_hits（offset=-1 表示计算字段）"""
            if prov_hits is not None:
                formula = self._CALC_FORMULAS.get(field, '计算')
                prov_hits[(table_key, field)] = (-1, formula, formula)

        rev  = income.get('total_operating_revenue') or core.get('total_operating_revenue')
        np_  = income.get('net_profit') or core.get('net_profit_10k_yuan')
        cost = income.get('operating_expense_cost_of_sales')
        tl   = balance.get('liability_total_liabilities')
        ta   = balance.get('asset_total_assets')
        report_period = core.get('report_period') or income.get('report_period') or ''
        period_suffix = report_period[4:] if len(report_period) > 4 else ''

        # 资产负债率
        if balance.get('asset_liability_ratio') is None:
            v = _pct(tl, ta)
            if v is not None:
                balance['asset_liability_ratio'] = v
                _mark_calc('balance_sheet', 'asset_liability_ratio')

        # 销售净利率
        if core.get('net_profit_margin') is None:
            v = _pct(np_, rev)
            if v is not None:
                core['net_profit_margin'] = v
                _mark_calc('core_indicators', 'net_profit_margin')

        # 销售毛利率
        if core.get('gross_profit_margin') is None and period_suffix != 'Q3' and rev and cost is not None:
            v = _pct(rev - cost, rev)
            if v is not None:
                core['gross_profit_margin'] = v
                _mark_calc('core_indicators', 'gross_profit_margin')

        # 隐含股数（万股）= 净利润(万元) / EPS(元/股)，用于推算每股指标
        eps    = core.get('eps')
        np_    = core.get('net_profit_10k_yuan') or income.get('net_profit')
        eq     = balance.get('equity_total_equity')
        ops_cf = results['cash_flow'].get('operating_cf_net_amount')

        shares_10k = None
        if eps and np_ and eps != 0 and np_ != 0:
            implied = np_ / eps   # 万股（加权平均近似）
            # 合理范围：100万股 ~ 1000亿股（防止net_profit提取异常导致除零发散）
            if 100 <= abs(implied) <= 1e9:
                shares_10k = implied

        # 每股经营活动现金流 = 经营活动净额(万元) / 隐含股数(万股)
        # 注：此处仅在 _post_llm_derive=True 时计算，给正则→LLM优先提取的机会
        if getattr(self, '_post_llm_derive', False):
            if core.get('operating_cf_per_share') is None:
                if ops_cf is not None and shares_10k:
                    val = round(ops_cf / shares_10k, 4)
                    if -10000 <= val <= 10000:
                        core['operating_cf_per_share'] = val
                        _mark_calc('core_indicators', 'operating_cf_per_share')

        # 每股净资产（元/股）= 归属净资产(万元) / 隐含股数(万股)
        # 注：隐含股数为加权平均股数，与期末股数略有差异（误差通常<1%）
        if core.get('net_asset_per_share') is None:
            if eq is not None and shares_10k:
                val = round(eq / shares_10k, 4)
                if -10000 <= val <= 10000:
                    core['net_asset_per_share'] = val
                    _mark_calc('core_indicators', 'net_asset_per_share')

        # 营业总成本 = 营业成本 + 销售费用 + 管理费用 + 财务费用 + 研发费用 + 税金及附加
        if income.get('total_operating_expenses') is None:
            _toe_parts = [
                income.get('operating_expense_cost_of_sales'),
                income.get('operating_expense_selling_expenses'),
                income.get('operating_expense_administrative_expenses'),
                income.get('operating_expense_financial_expenses'),
                income.get('operating_expense_rnd_expenses'),
                income.get('operating_expense_taxes_and_surcharges'),
            ]
            _toe_valid = [p for p in _toe_parts if p is not None]
            # 至少需要营业成本 + 2项费用才可靠
            if len(_toe_valid) >= 3 and income.get('operating_expense_cost_of_sales') is not None:
                _toe_sum = round(sum(float(v) for v in _toe_valid), 2)
                if _toe_sum > 0:
                    income['total_operating_expenses'] = _toe_sum
                    _mark_calc('income_sheet', 'total_operating_expenses')

        return results

    def extract_income_sheet_data(self, text, base_info, prov_hits=None):
        """提取利润表数据"""
        data = base_info.copy()
        summary_metrics = self._extract_period_metrics_from_summary(text)
        na_fields = set(summary_metrics.pop('_na_fields', []))
        # 需要在全文搜索的字段（同比增减出现在摘要段，不在利润表正文）
        pct_fields_income = {'operating_revenue_yoy_growth', 'net_profit_yoy_growth'}
        fulltext_fields = pct_fields_income  # net_profit 限定在合并利润表段落，防止误匹配描述句中的分红比例等数字

        
        # 优先截取合并利润表段落，防止取到季报分季度数据或母公司数据
        scope = text
        scope_offset = 0
        for anchor in ('合并年初到报告期末利润表', '合并利润表', '合并报表利润表'):
            idx = text.find(anchor)
            if idx != -1:
                end_idx = text.find('母公司利润表', idx)
                scope = text[idx: end_idx if end_idx != -1 else idx + 8000]
                scope_offset = idx
                break



        # 营业总收入 - 从表格中提取
        patterns = {
            'total_operating_revenue': [
                r'营业总收入[\s\S]{0,100}?([\d,]+\.\d+)',
                r'一、营业总收入[\s\S]{0,50}?([\d,]+\.\d+)'
            ],
            'operating_revenue_yoy_growth': [
                r'公司实现营业总?收入[\s\S]{0,300}?较上年同期[\s\S]{0,80}?(?:增长|下降)\s*([-\d,]+\.?\d*)%',
                r'营业总?收入[\s\S]{0,300}?较上年同期[\s\S]{0,80}?(?:增长|下降)\s*([-\d,]+\.?\d*)%',
                r'营业总?收入[\s\S]{0,200}?同比[\s\S]{0,50}?([-\d,]+\.\d+)%',
                r'营业总?收入[\s\S]{0,200}?增减[\s\S]{0,50}?([-\d,]+\.\d+)%'
            ],
            'net_profit': [
                r'归属于(?:上市公司|本公司|母公司)[\s\S]{0,10}?股东[\s\S]{0,10}?净利[\s\S]{0,3}?润[\s\S]{0,100}?([-\d,]+\.\d+)',
                r'净利润[\s\S]{0,100}?([-\d,]+\.\d+)'
            ],
            'net_profit_yoy_growth': [
                r'归属于(?:上市公司|本公司|母公司)股东的\s*净利润[\s\S]{0,200}?([-\d,]+\.\d+)%',
            ],
            'total_profit': [
                r'利润总额[\s\S]{0,100}?([-\d,]+\.\d+)',
                r'三、利润总额[\s\S]{0,50}?([-\d,]+\.\d+)'
            ],
            'operating_profit': [
                r'营业利润[\s\S]{0,100}?([-\d,]+\.\d+)',
                r'二、营业利润[\s\S]{0,50}?([-\d,]+\.\d+)'
            ],
            'operating_expense_cost_of_sales': [
                r'营业成本[\s\S]{0,100}?([-\d,]+\.\d+)'
            ],
            'operating_expense_selling_expenses': [
                r'销售费用[\s\S]{0,100}?([-\d,]+\.\d+)'
            ],
            'operating_expense_administrative_expenses': [
                r'管理费用[\s\S]{0,100}?([-\d,]+\.\d+)'
            ],
            'operating_expense_financial_expenses': [
                r'财务费用[\s\S]{0,100}?([-\d,]+\.\d+)'
            ],
            'operating_expense_rnd_expenses': [
                r'研发费用[\s\S]{0,100}?([-\d,]+\.\d+)'
            ],
            'other_income': [
                r'其他收益[\s\S]{0,100}?([-\d,]+\.\d+)'
            ],
            'operating_expense_taxes_and_surcharges': [
                r'税金及附加[\s\S]{0,100}?([-\d,]+\.\d+)',
                r'营业税金及附加[\s\S]{0,100}?([-\d,]+\.\d+)'
            ],
            'total_operating_expenses': [
                r'二、营业总成本[\s\S]{0,200}?([\d,]+\.\d+)',
                r'营业总成本[\s\S]{0,200}?([\d,]+\.\d+)',
                r'营业总费用[\s\S]{0,200}?([-\d,]+\.\d+)',
                r'二[、\s.]+营业成本[\s\S]{0,200}?([\d,]+\.\d+)',
            ],
            'asset_impairment_loss': [
                r'资产减值损失[\s\S]{0,100}?([-\d,]+\.\d+)'
            ],
            'credit_impairment_loss': [
                r'信用减值损失[\s\S]{0,100}?([-\d,]+\.\d+)'
            ]
        }
        
        # 先对合并利润表段落做一次单位扫描
        unit_scan_text = scope[:1500]
        is_yuan_unit = any(kw in unit_scan_text for kw in
                           ('单位：元', '单位:元', '（元）', '(元)', '单位：人民币元'))
        is_10k_unit = any(kw in unit_scan_text for kw in
                          ('单位：万元', '单位:万元', '（万元）', '(万元)', '单位：人民币万元'))
        is_1k_unit = any(kw in unit_scan_text for kw in
                         ('单位：千元', '单位:千元', '（千元）', '(千元)'))
        # A股利润表默认单位是元，未检测到任何单位时按元处理
        if not is_yuan_unit and not is_10k_unit and not is_1k_unit:
            is_yuan_unit = True

        for field, pattern_list in patterns.items():
            # yoy增长率字段在摘要段，用全文；其余字段限定在合并利润表段落
            search_text = text if field in fulltext_fields else scope
            text_offset = 0 if field in fulltext_fields else scope_offset
            for pattern in pattern_list:
                match = re.search(pattern, search_text)
                if match:
                    value = self.clean_number(match.group(1))
                    if value is not None:
                        if field not in pct_fields_income:
                            if is_yuan_unit:
                                value = value / 10000
                            elif is_1k_unit:
                                value = value / 10
                        data[field] = value
                        if prov_hits is not None:
                            prov_hits[('income_sheet', field)] = (
                                text_offset + match.start(),
                                match.group(1),
                                match.group(0)[:60],
                            )
                        break

        summary_offsets = summary_metrics.pop('_offsets', {})
        for field in na_fields:
            data[field] = None
            if prov_hits is not None:
                prov_hits.pop(('income_sheet', field), None)
        for field in ('total_operating_revenue', 'operating_revenue_yoy_growth',
                      'net_profit', 'net_profit_yoy_growth'):
            if summary_metrics.get(field) is not None:
                data[field] = summary_metrics[field]
                # 值来自摘要表，同步更新 prov_hits 指向摘要表的正确位置
                if prov_hits is not None and field in summary_offsets:
                    prov_hits[('income_sheet', field)] = summary_offsets[field]

        # 若摘要段未给出净利润同比增长率，从利润表两列数据推算
        if 'net_profit_yoy_growth' not in na_fields and data.get('net_profit_yoy_growth') is None and data.get('net_profit') is not None:
            for pat in [
                r'归属于(?:上市公司|本公司|母公司)[\s\n]*股东的[\s\n]*净利润[\s\S]{0,5}?[-\d,]+\.\d+[\s\S]{0,3}?([-\d,]+\.\d+)',
                r'净利润[\s\S]{0,5}?[\d,]+\.\d+[\s\S]{0,3}?([-\d,]+\.\d+)',
            ]:
                m = re.search(pat, scope)
                if m:
                    prior = self.clean_number(m.group(1))
                    if prior is not None and is_yuan_unit:
                        prior /= 10000
                    elif prior is not None and is_1k_unit:
                        prior /= 10
                    if prior is not None and prior != 0 and data['net_profit'] * prior >= 0:
                        data['net_profit_yoy_growth'] = round(
                            (data['net_profit'] - prior) / abs(prior) * 100, 4)
                        break
                    if prior is not None and prior != 0 and data['net_profit'] * prior < 0:
                        na_fields.add('net_profit_yoy_growth')
                        break

        if na_fields:
            data['_na_fields'] = sorted(na_fields)

        return data

    def extract_balance_sheet_data(self, text, base_info, prov_hits=None):
        """提取资产负债表数据（字段与附件3完全一致）。
        
        优先在“合并资产负债表”段落中查找，避免抓取到母公司报表或上期数据。
        资产负债表中存在两类应收/应付：
          - 应收账款 (asset_accounts_receivable)  ← 资产方，应收
          - 应付账款 (liability_accounts_payable)  ← 负债方，应付
        两者中文关键字不同（收 vs 付），正则不会混淆。
        """
        data = base_info.copy()

        pct_fields = {'asset_liability_ratio', 'asset_total_assets_yoy_growth',
                      'liability_total_liabilities_yoy_growth'}

        scope, scope_offset = self._select_table_scope(
            text,
            ('合并资产负债表', '合并报表资产负债表', '资产负债表'),
            ('母公司资产负债表', '合并利润表', '利润表'),
            (
                ('资产总计', 4),
                ('负债合计', 4),
                ('所有者权益', 2),
                ('编制单位', 3),
                ('短期借款', 1),
            ),
            fallback_chars=16000,
            min_end_gap=800,
        )
        scope = self._normalize_broken_numbers(scope)

        patterns = {
            'asset_cash_and_cash_equivalents': [
                r'货币资金[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            # 应收账款 (收 = to receive) — 资产方
            'asset_accounts_receivable': [
                r'应收账款[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            'asset_inventory': [
                r'存货[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            'asset_trading_financial_assets': [
                r'交易性金融资产[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            'asset_construction_in_progress': [
                r'在建工程[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            'asset_total_assets': [
                r'资产总计[\s\S]{0,100}?([\d,]+\.\d+)',
                r'总资产[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            'asset_total_assets_yoy_growth': [
                r'总资产[\s\S]{0,200}?同比[\s\S]{0,50}?([-\d,]+\.\d+)%',
                r'总资产[\s\S]{0,200}?增减[\s\S]{0,50}?([-\d,]+\.\d+)%'
            ],
            # 应付账款 (付 = to pay) — 负债方，与应收账款字符不同不会混淆
            'liability_accounts_payable': [
                r'应付账款[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            'liability_advance_from_customers': [
                r'预收(?:[账帐]款|款项)[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            'liability_total_liabilities': [
                r'(?<!流动)负债合计[\s\S]{0,100}?([\d,]+\.\d+)',
                r'(?<!流动)负债总计[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            'liability_total_liabilities_yoy_growth': [
                r'总负债[\s\S]{0,200}?同比[\s\S]{0,50}?([-\d,]+\.\d+)%'
            ],
            'liability_contract_liabilities': [
                r'合同负债[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            'liability_short_term_loans': [
                r'短期借款[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
            'asset_liability_ratio': [
                r'资产负债率[\s\S]{0,100}?([\d,]+\.\d+)%'
            ],
            'equity_unappropriated_profit': [
                r'未分配利润[\s\S]{0,100}?([-\d,]+\.\d+)'
            ],
            'equity_total_equity': [
                r'归属于(?:上市公司股东的?|母公司)\s*所有者权益[\s\S]{0,200}?([\d,]+\.\d+)',
                r'所有者权益[\s\S]{0,50}?合计[\s\S]{0,100}?([\d,]+\.\d+)'
            ],
        }

        # 先对整个段落做一次单位扫描，避免表头距离过远导致漏检
        is_yuan_unit = any(kw in scope for kw in
                           ('单位：元', '单位:元', '（元）', '(元)', '单位：人民币元'))
        is_10k_unit = any(kw in scope for kw in
                          ('单位：万元', '单位:万元', '（万元）', '(万元)', '单位：人民币万元'))
        is_1k_unit = any(kw in scope for kw in
                         ('单位：千元', '单位:千元', '（千元）', '(千元)'))
        # A股资产负债表默认单位是元，未检测到任何单位时按元处理
        if not is_yuan_unit and not is_10k_unit and not is_1k_unit:
            is_yuan_unit = True

        for field, pattern_list in patterns.items():
            for pattern in pattern_list:
                match = re.search(pattern, scope)
                if match:
                    value = self.clean_number(match.group(1))
                    if value is not None:
                        if field not in pct_fields:
                            if is_yuan_unit:
                                value = value / 10000
                            elif is_1k_unit:
                                value = value / 10
                        data[field] = value
                        if prov_hits is not None:
                            prov_hits[('balance_sheet', field)] = (
                                scope_offset + match.start(),
                                match.group(1),
                                match.group(0)[:60],
                            )
                        break

        # 利用上期列数据计算同比增长率（资产/负债总计两列均在合并资产负债表段落内）
        _bs_yoy_patterns = {
            'asset_total_assets_yoy_growth': ('asset_total_assets', [
                r'资产总计[\s\S]{0,10}?[\d,]+\.\d+[\s\S]{0,3}?([\d,]+\.\d+)',
                r'总资产[\s\S]{0,10}?[\d,]+\.\d+[\s\S]{0,3}?([\d,]+\.\d+)',
            ]),
            'liability_total_liabilities_yoy_growth': ('liability_total_liabilities', [
                r'(?<!流动)负债合计[\s\S]{0,10}?[\d,]+\.\d+[\s\S]{0,3}?([\d,]+\.\d+)',
            ]),
        }
        for growth_field, (cur_field, pat_list) in _bs_yoy_patterns.items():
            if data.get(growth_field) is None and data.get(cur_field) is not None:
                for pat in pat_list:
                    m = re.search(pat, scope)
                    if m:
                        prior = self.clean_number(m.group(1))
                        if prior is not None and is_yuan_unit:
                            prior /= 10000
                        elif prior is not None and is_1k_unit:
                            prior /= 10
                        if prior is not None and prior != 0:
                            data[growth_field] = round(
                                (data[cur_field] - prior) / abs(prior) * 100, 4)
                            if prov_hits is not None:
                                formula = self._CALC_FORMULAS.get(growth_field, '计算')
                                prov_hits[('balance_sheet', growth_field)] = (-1, formula, formula)
                            break

        return data
    
    def extract_cash_flow_data(self, text, base_info, prov_hits=None):
        """提取现金流量表数据"""
        data = base_info.copy()

        scope, scope_offset = self._select_table_scope(
            text,
            ('合并现金流量表', '现金流量表'),
            ('母公司现金流量表', '合并股东权益变动表', '股东权益变动表'),
            (
                ('经营活动产生的现金流量净额', 4),
                ('投资活动产生的现金流量净额', 4),
                ('筹资活动产生的现金流量净额', 4),
                ('现金及现金等价物净增加额', 4),
                ('编制单位', 3),
            ),
            fallback_chars=18000,
            min_end_gap=800,
        )
        scope = self._normalize_broken_numbers(scope)

        # PyMuPDF 提取的表格文本中，换行符会插入标签中间
        # （如 "经营活动产生的现金\n流量净额"），且数值通常在标签后的下一行，
        # 因此使用 [\s\n]* 处理标签内换行，[\s\S]{0,5}? 跨行到达数值。
        # 主模式：确认后面还有第二个数字（上期值）→ 取第一个（当期值）
        # 备用模式：当期列为空时主模式失败，备用仅限关键汇总行使用
        patterns = {
            'net_cash_flow': [
                r'现金及现金等价物净增加额[\s\S]{0,5}?([-\d,]+\.\d+)[\s\S]{0,5}[-\d,]+\.\d+',
                r'现金及现金等价物净增加额[\s\S]{0,5}?([-\d,]+\.\d+)'
            ],
            'net_cash_flow_yoy_growth': [
                r'现金及现金等价物净增加额[\s\S]{0,200}?同比[\s\S]{0,50}?([-\d,]+\.\d+)%',
                r'现金净增加额[\s\S]{0,200}?同比[\s\S]{0,50}?([-\d,]+\.\d+)%'
            ],
            'operating_cf_net_amount': [
                r'经营活动产生的现金[\s\n]*流量净额[\s\S]{0,5}?([-\d,]+\.\d+)[\s\S]{0,5}[-\d,]+\.\d+',
                r'经营活动产生的现金[\s\n]*流量净额[\s\S]{0,5}?([-\d,]+\.\d+)'
            ],
            'operating_cf_cash_from_sales': [
                r'销售商品[\s\S]{0,20}?提供劳务收到的现金[\s\S]{0,30}?([-\d,]+\.\d+)[\s\S]{0,30}?[-\d,]+\.\d+',
                r'销售商品[\s\S]{0,20}?提供劳务收到的现金[\s\S]{0,30}?([-\d,]+\.\d+)',
                r'销售商品[\s\S]{0,20}?收到的现金[\s\S]{0,30}?([-\d,]+\.\d+)',
            ],
            'investing_cf_net_amount': [
                r'投资活动产生的现金[\s\n]*流量净额[\s\S]{0,5}?([-\d,]+\.\d+)[\s\S]{0,5}[-\d,]+\.\d+',
                r'投资活动产生的现金[\s\n]*流量净额[\s\S]{0,5}?([-\d,]+\.\d+)'
            ],
            'investing_cf_cash_for_investments': [
                r'取得投资[\s\S]{0,20}?支付的现金[\s\S]{0,5}?([-\d,]+\.\d+)[\s\S]{0,5}[-\d,]+\.\d+',
                r'取得投资[\s\S]{0,20}?支付的现金[\s\S]{0,5}?([-\d,]+\.\d+)',
                r'投资支付的现金[\s\S]{0,5}?([-\d,]+\.\d+)'
            ],
            'investing_cf_cash_from_investment_recovery': [
                r'收回投资收到的现金[\s\S]{0,5}?([-\d,]+\.\d+)[\s\S]{0,5}[-\d,]+\.\d+',
                r'收回投资收到的现金[\s\S]{0,5}?([-\d,]+\.\d+)'
            ],
            'financing_cf_net_amount': [
                r'筹资活动产生的现金[\s\n]*流量净额[\s\S]{0,5}?([-\d,]+\.\d+)[\s\S]{0,5}[-\d,]+\.\d+',
                r'筹资活动产生的现金[\s\n]*流量净额[\s\S]{0,5}?([-\d,]+\.\d+)'
            ],
            'financing_cf_cash_from_borrowing': [
                r'取得借款收到的现金[\s\S]{0,5}?([-\d,]+\.\d+)[\s\S]{0,5}[-\d,]+\.\d+',
                r'取得借款收到的现金[\s\S]{0,5}?([-\d,]+\.\d+)',
                r'借款收到的现金[\s\S]{0,5}?([-\d,]+\.\d+)'
            ],
            'financing_cf_cash_for_debt_repayment': [
                r'偿还债务支付的现金[\s\S]{0,5}?([-\d,]+\.\d+)[\s\S]{0,5}[-\d,]+\.\d+',
                r'偿还债务支付的现金[\s\S]{0,5}?([-\d,]+\.\d+)',
                r'归还借款支付的现金[\s\S]{0,5}?([-\d,]+\.\d+)'
            ],
        }

        # 先对段落做一次单位扫描（仅 net_cash_flow 按“元”存储）
        unit_scan_text = scope[:1500]
        is_yuan_unit = any(kw in unit_scan_text for kw in
                           ('单位：元', '单位:元', '（元）', '(元)', '单位：人民币元'))
        is_10k_unit = any(kw in unit_scan_text for kw in
                          ('单位：万元', '单位:万元', '（万元）', '(万元)', '单位：人民币万元'))
        is_1k_unit = any(kw in unit_scan_text for kw in
                         ('单位：千元', '单位:千元', '（千元）', '(千元)'))
        # A股现金流量表默认单位是元，未检测到单位时按元处理
        if not is_yuan_unit and not is_10k_unit and not is_1k_unit:
            is_yuan_unit = True

        pct_fields_cf = {'net_cash_flow_yoy_growth'}

        for field, pattern_list in patterns.items():
            for pattern in pattern_list:
                match = re.search(pattern, scope)
                if match:
                    value = self.clean_number(match.group(1))
                    if value is not None:
                        if field not in pct_fields_cf:
                            if field == 'net_cash_flow':
                                # net_cash_flow 存储单位为"元"
                                if is_10k_unit:
                                    value = value * 10000
                                elif is_1k_unit:
                                    value = value * 1000
                            else:
                                # 其余现金流字段存储单位为"万元"
                                if is_yuan_unit:
                                    value = value / 10000
                                elif is_1k_unit:
                                    value = value / 10
                        data[field] = value
                        if prov_hits is not None:
                            prov_hits[('cash_flow', field)] = (
                                scope_offset + match.start(),
                                match.group(1),
                                match.group(0)[:60],
                            )
                        break

        # 利用上期列计算现金净增加额同比增长率
        if data.get('net_cash_flow_yoy_growth') is None and data.get('net_cash_flow') is not None:
            m = re.search(
                r'现金及现金等价物净增加额[\s\S]{0,5}?[-\d,]+\.\d+[\s\S]{0,3}?([-\d,]+\.\d+)',
                scope
            )
            if m:
                prior = self.clean_number(m.group(1))
                if prior is not None and is_10k_unit:
                    prior *= 10000
                elif prior is not None and is_1k_unit:
                    prior *= 1000
                if prior is not None and prior != 0:
                    data['net_cash_flow_yoy_growth'] = round(
                        (data['net_cash_flow'] - prior) / abs(prior) * 100, 4)
                    if prov_hits is not None:
                        formula = self._CALC_FORMULAS.get('net_cash_flow_yoy_growth', '计算')
                        prov_hits[('cash_flow', 'net_cash_flow_yoy_growth')] = (-1, formula, formula)

        # 计算三大活动占净现金流比例（ratio字段）
        net = data.get('net_cash_flow')
        net_10k = net / 10000 if net is not None else None
        if net_10k is not None and net_10k != 0:
            for cf_field, ratio_field in [
                ('operating_cf_net_amount',  'operating_cf_ratio_of_net_cf'),
                ('investing_cf_net_amount',  'investing_cf_ratio_of_net_cf'),
                ('financing_cf_net_amount',  'financing_cf_ratio_of_net_cf'),
            ]:
                cf_val = data.get(cf_field)
                if cf_val is not None:
                    data[ratio_field] = round(cf_val / net_10k * 100, 4)
                    if prov_hits is not None:
                        formula = self._CALC_FORMULAS.get(ratio_field, '计算')
                        prov_hits[('cash_flow', ratio_field)] = (-1, formula, formula)

        return data

    def extract_core_indicators(self, text, base_info, prov_hits=None):
        """提取核心业绩指标（字段与附件3完全一致）"""
        data = base_info.copy()
        summary_metrics = self._extract_period_metrics_from_summary(text)
        na_fields = set(summary_metrics.pop('_na_fields', []))

        income_data = self.extract_income_sheet_data(text, base_info, prov_hits)
        balance_data = self.extract_balance_sheet_data(text, base_info, prov_hits)
        cash_data   = self.extract_cash_flow_data(text, base_info, prov_hits)
        na_fields.update(
            field for field in (income_data.get('_na_fields') or [])
            if field in ('operating_revenue_yoy_growth', 'net_profit_yoy_growth')
        )

        # 从利润表复制
        for f in ('total_operating_revenue', 'operating_revenue_yoy_growth',
                  'net_profit_yoy_growth'):
            if f in income_data:
                data[f] = income_data[f]
                if prov_hits is not None and ('income_sheet', f) in prov_hits:
                    prov_hits[('core_indicators', f)] = prov_hits[('income_sheet', f)]
        # net_profit → net_profit_10k_yuan
        if 'net_profit' in income_data:
            data['net_profit_10k_yuan'] = income_data['net_profit']
            if prov_hits is not None and ('income_sheet', 'net_profit') in prov_hits:
                prov_hits[('core_indicators', 'net_profit_10k_yuan')] = prov_hits[('income_sheet', 'net_profit')]

        # 直接从文本提取核心指标表独有字段
        direct_patterns = {
            'eps': [
                r'基本每股收益[（(]元[/／]股[)）][\s\S]{0,50}?([-\d,]+\.\d+)',
                r'基本每股收益[\s\S]{0,50}?([-\d,]+\.\d{2,})',
                r'每股收益[（(]元[/／]股[)）][\s\S]{0,50}?([-\d,]+\.\d+)',
            ],
            'net_asset_per_share': [
                r'每股净资产[^（(]*?[（(]元[^）)]*?[）)]\s*[列项]?\s*([-\d,]+\.\d+)',
                r'每股净资产[\s\S]{0,50}?([-\d,]+\.\d{2,})',
            ],
            'roe': [
                r'加权平均净资产收益率[（(]%[)）][\s\S]{0,50}?([-\d,]+\.?\d+)',
                r'加权平均净资产收益率[\s\S]{0,100}?([-\d,]+\.\d+)',
                r'净资产收益率[\s\S]{0,100}?([-\d,]+\.?\d+)%'
            ],
            'operating_cf_per_share': [
                r'每股经营活动产生的现金[\s\n]*流量[（(]元[/／]股[)）][\s\S]{0,50}?([-\d,]+\.\d+)',
                r'每股经营活动产生的现金[\s\n]*流量[\s\S]{0,50}?([-\d,]+\.\d{2,})',
                r'每股经营现金流[\s\S]{0,50}?([-\d,]+\.\d{2,})',
                r'每股经营活动[\s\S]{0,30}?现金[\s\S]{0,30}?流[\s\S]{0,50}?([-\d,]+\.\d{2,})',
            ],
            'net_profit_excl_non_recurring': [
                r'归属于(?:上市公司|本公司|母公司)[\s\S]{0,10}?股东[\s\S]{0,20}?扣除[\s\S]{0,10}?经常性损益[\s\S]{0,20}?净利[\s\S]{0,3}?润[\s\S]{0,100}?([-\d,]+\.\d+)',
                r'扣除非[\s\S]{0,5}?经常性损益.*?净利润[\s\S]{0,100}?([-\d,]{6,}\.\d+)',
                r'扣非净利润[\s\S]{0,100}?([-\d,]+\.?\d+)'
            ],
            'net_profit_excl_non_recurring_yoy': [
                r'归属于(?:上市公司|本公司|母公司)[\s\S]{0,10}?股东[\s\S]{0,20}?扣除[\s\S]{0,10}?经常性损益[\s\S]{0,20}?净利[\s\S]{0,3}?润[\s\S]{0,100}?(?:[-\d,]+\.\d+)\s+(?:[-\d,]+\.\d+)\s+([-\d.]+)',
                r'扣除非[\s\S]{0,5}?经常性损益.*?净利润[\s\S]{0,200}?([-\d,]+\.?\d+)%'
            ],
            'gross_profit_margin': [
                r'销售毛利率[\s\S]{0,100}?([-\d,]+\.?\d+)%',
                r'毛利率[\s\S]{0,100}?([-\d,]+\.?\d+)%'
            ],
            'net_profit_margin': [
                r'销售净利率[\s\S]{0,100}?([-\d,]+\.?\d+)%'
            ],
            'roe_weighted_excl_non_recurring': [
                r'扣除非经常性损益(?:后的|的)加权平均[\s\S]{0,20}?净资产收益率[（(]%[)）][\s\S]{0,50}?([-\d,]+\.?\d+)',
                r'扣除非经常性损益(?:后的|的)加权平均[\s\S]{0,20}?净资产收益率[\s\S]{0,80}?([-\d,]+\.\d+)',
                r'加权平均净资产收益率.*?扣非[\s\S]{0,100}?([-\d,]+\.?\d+)%',
                r'扣非.*?加权平均净资产收益率[\s\S]{0,100}?([-\d,]+\.?\d+)%',
                r'加权平均净资[\s\n]*产收益率[\s\S]{0,800}?扣除非经常性损益后归属于[\s\S]{0,60}?(?:净利[\s\n]*润)\s*\n?\s*([-\d,.]+)%?',
                r'扣除非经常性损益后归属于[\s\S]{0,60}?(?:的净利润|净利[\s\n]*润)\s*\n\s*([-\d,.]+)%?(?!\s*万?元)',
            ],
            'operating_revenue_qoq_growth': [
                r'营业总?收入[\s\S]{0,200}?环比[\s\S]{0,50}?([-\d,]+\.?\d+)%'
            ],
            'net_profit_qoq_growth': [
                r'净利润[\s\S]{0,200}?环比[\s\S]{0,50}?([-\d,]+\.?\d+)%'
            ],
        }

        for field, pattern_list in direct_patterns.items():
            if field in na_fields:
                data[field] = None
                if prov_hits is not None:
                    prov_hits.pop(('core_indicators', field), None)
                continue
            for pattern in pattern_list:
                match = re.search(pattern, text)
                if match:
                    value = self.clean_number(match.group(1))
                    if value is not None:
                        # net_profit_excl_non_recurring 单位处理
                        if field == 'net_profit_excl_non_recurring':
                            context = text[max(0, match.start()-800):match.end()+50]
                            _unit_m = re.search(r'单位[：:]\s*(千元|万元|人民币元|元)', context)
                            if _unit_m:
                                _u = _unit_m.group(1)
                                if '千元' in _u:
                                    value = value / 10
                                elif '万元' not in _u:
                                    value = value / 10000  # 元 or 人民币元
                            elif '（元）' in context or '(元)' in context:
                                value = value / 10000
                            elif '（千元）' in context or '(千元)' in context:
                                value = value / 10
                        data[field] = value
                        if prov_hits is not None:
                            prov_hits[('core_indicators', field)] = (
                                match.start(),
                                match.group(1),
                                match.group(0)[:60],
                            )
                        break

        summary_offsets = summary_metrics.pop('_offsets', {})
        summary_field_map = {
            'net_profit_excl_non_recurring': 'net_profit_excl_non_recurring',
            'net_profit_excl_non_recurring_yoy': 'net_profit_excl_non_recurring_yoy',
        }
        for src_field, dst_field in summary_field_map.items():
            if summary_metrics.get(src_field) is not None:
                data[dst_field] = summary_metrics[src_field]
                if prov_hits is not None and src_field in summary_offsets:
                    prov_hits[('core_indicators', dst_field)] = summary_offsets[src_field]

        if na_fields:
            data['_na_fields'] = sorted(na_fields)

        # ── 扣非净利润单位兜底校验：与净利润比较，自动修正异常单位 ──
        _excl_val = data.get('net_profit_excl_non_recurring')
        _np_val = data.get('net_profit_10k_yuan')
        if _excl_val is not None and _np_val is not None:
            try:
                _ef = abs(float(_excl_val))
                _nf = abs(float(_np_val))
                if _nf > 100:  # 净利润足够大才校验
                    _ratio = _ef / _nf
                    if _ratio > 5000:  # ~10000x off → 元未转万元
                        data['net_profit_excl_non_recurring'] = round(float(_excl_val) / 10000, 2)
                    elif _ratio > 5:  # ~10x off → 千元未转万元
                        data['net_profit_excl_non_recurring'] = round(float(_excl_val) / 10, 2)
            except (TypeError, ValueError):
                pass

        # ── 计算兜底：扣非ROE = ROE × 扣非净利润 / 净利润 ──
        if data.get('roe_weighted_excl_non_recurring') is None:
            _roe = data.get('roe')
            _excl = data.get('net_profit_excl_non_recurring')
            _profit = data.get('net_profit_10k_yuan')
            if _roe is not None and _excl is not None and _profit is not None:
                try:
                    _roe_f = float(_roe)
                    _excl_f = float(_excl)
                    _profit_f = float(_profit)
                    if abs(_profit_f) > 1e-6:
                        data['roe_weighted_excl_non_recurring'] = round(
                            _roe_f * _excl_f / _profit_f, 4
                        )
                except (TypeError, ValueError):
                    pass

        return data
    
    # ── 溯源信息采集 ─────────────────────────────────────────────────────
    # 各报表在PDF中的锚点标题 → (table_key, source_table_label)
    _TABLE_ANCHORS = {
        'income_sheet':    (['合并利润表', '合并报表利润表', '利润表'], '合并利润表'),
        'balance_sheet':   (['合并资产负债表', '合并报表资产负债表', '资产负债表'], '合并资产负债表'),
        'cash_flow':       (['合并现金流量表', '现金流量表'], '合并现金流量表'),
        'core_indicators': (['主要会计数据和财务指标', '主要会计数据'], '主要会计数据和财务指标'),
    }

    # 字段 → 中文关键词（用于在全文中定位字段所在位置）
    _FIELD_KEYWORDS = {
        'total_operating_revenue': '营业总收入',
        'operating_revenue_yoy_growth': '营业收入',
        'net_profit': '净利润',
        'net_profit_10k_yuan': '净利润',
        'net_profit_yoy_growth': '净利润',
        'total_profit': '利润总额',
        'operating_profit': '营业利润',
        'operating_expense_cost_of_sales': '营业成本',
        'operating_expense_selling_expenses': '销售费用',
        'operating_expense_administrative_expenses': '管理费用',
        'operating_expense_financial_expenses': '财务费用',
        'operating_expense_rnd_expenses': '研发费用',
        'other_income': '其他收益',
        'operating_expense_taxes_and_surcharges': '税金及附加',
        'total_operating_expenses': '营业总成本',
        'asset_impairment_loss': '资产减值损失',
        'credit_impairment_loss': '信用减值损失',
        'asset_cash_and_cash_equivalents': '货币资金',
        'asset_accounts_receivable': '应收账款',
        'asset_inventory': '存货',
        'asset_trading_financial_assets': '交易性金融资产',
        'asset_construction_in_progress': '在建工程',
        'asset_total_assets': '资产总计',
        'asset_total_assets_yoy_growth': '总资产',
        'liability_accounts_payable': '应付账款',
        'liability_advance_from_customers': '预收账款',
        'liability_total_liabilities': '负债合计',
        'liability_total_liabilities_yoy_growth': '总负债',
        'liability_contract_liabilities': '合同负债',
        'liability_short_term_loans': '短期借款',
        'asset_liability_ratio': '资产负债率',
        'equity_unappropriated_profit': '未分配利润',
        'equity_total_equity': '所有者权益',
        'eps': '每股收益',
        'net_asset_per_share': '每股净资产',
        'roe': '净资产收益率',
        'operating_cf_per_share': '每股经营',
        'net_profit_excl_non_recurring': '扣除非经常性损益',
        'net_profit_excl_non_recurring_yoy': '扣除非经常性损益',
        'gross_profit_margin': '毛利率',
        'net_profit_margin': '净利率',
        'roe_weighted_excl_non_recurring': '扣非',
        'operating_revenue_qoq_growth': '营业收入',
        'net_profit_qoq_growth': '净利润',
        'net_cash_flow': '现金及现金等价物净增加额',
        'net_cash_flow_yoy_growth': '现金及现金等价物净增加额',
        'operating_cf_net_amount': '经营活动产生的现金',
        'operating_cf_cash_from_sales': '销售商品',
        'investing_cf_net_amount': '投资活动产生的现金',
        'investing_cf_cash_for_investments': '取得投资',
        'investing_cf_cash_from_investment_recovery': '收回投资',
        'financing_cf_net_amount': '筹资活动产生的现金',
        'financing_cf_cash_from_borrowing': '取得借款',
        'financing_cf_cash_for_debt_repayment': '偿还债务',
    }

    # 表键 → 对应数据库表名
    _TABLE_KEY_TO_DB = {
        'core_indicators': 'core_performance_indicators_sheet',
        'income_sheet':    'income_sheet',
        'balance_sheet':   'balance_sheet',
        'cash_flow':       'cash_flow_sheet',
    }

    @staticmethod
    def _find_all_occurrences(text, keyword, start=0, end=None):
        """返回 keyword 在 text[start:end] 中所有出现位置（绝对偏移）"""
        if end is None:
            end = len(text)
        positions = []
        pos = start
        while pos < end:
            idx = text.find(keyword, pos, end)
            if idx == -1:
                break
            positions.append(idx)
            pos = idx + 1
        return positions

    @staticmethod
    def _format_value_for_search(value):
        """将提取的数值转为搜索用的数字字符串片段"""
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        # 去掉可能的百分号
        s = s.replace('%', '')
        try:
            f = float(s)
        except (ValueError, TypeError):
            return None
        # 返回整数部分（至少3位数字才有意义）
        int_part = s.split('.')[0].lstrip('-')
        if len(int_part) >= 3:
            return int_part
        # 短数字直接用原始串
        return s

    def _collect_provenance(self, text, page_ranges, pdf_path, results, base_info):
        """为每个成功提取的字段，定位其在PDF中的页码和上下文文本片段。
        
        优先级：
        1. 在目标报表区域内，找到"关键词 + 实际数值"同时出现的位置
        2. 在目标报表区域内，找到关键词第一次出现的位置
        3. 全文范围回退：找到"关键词 + 实际数值"最接近的位置
        4. 全文范围回退：关键词第一次出现的位置
        """
        import re as _re
        _BASE = {'stock_code', 'stock_abbr', 'report_date', 'report_year', 'report_period'}
        records = []
        stock_code = base_info.get('stock_code', '')
        if not stock_code:
            abbr = base_info.get('stock_abbr', '')
            try:
                from config import CONFIG
                stock_code = CONFIG.get('stock_abbr_to_code', {}).get(abbr, '')
            except Exception:
                stock_code = ''
        report_period = base_info.get('report_period', '')

        # ── 预计算各报表的区域范围 ──
        section_ranges = {}  # table_key -> (start, end)
        for tkey, (anchor_list, _label) in self._TABLE_ANCHORS.items():
            best_start = None
            for anchor in anchor_list:
                pos = text.find(anchor)
                if pos != -1 and (best_start is None or pos < best_start):
                    best_start = pos
            if best_start is not None:
                # 区域结束：下一个报表区域的起点或文末
                sect_end = len(text)
                for tkey2, (anchor_list2, _) in self._TABLE_ANCHORS.items():
                    if tkey2 == tkey:
                        continue
                    for a2 in anchor_list2:
                        p2 = text.find(a2, best_start + 10)
                        if p2 != -1 and p2 < sect_end and p2 > best_start + 100:
                            sect_end = p2
                section_ranges[tkey] = (best_start, sect_end)

        # 去掉全文空格以便数字匹配（保留原文位置）
        _text_no_space = text.replace(' ', '').replace('\u3000', '').replace(',', '').replace('，', '')

        for table_key, data in results.items():
            if table_key.startswith('_') or not isinstance(data, dict):
                continue
            db_table = self._TABLE_KEY_TO_DB.get(table_key, table_key)
            anchors_info = self._TABLE_ANCHORS.get(table_key)
            source_table_label = anchors_info[1] if anchors_info else table_key
            sect_start, sect_end = section_ranges.get(table_key, (0, len(text)))

            for field, value in data.items():
                if field in _BASE or value is None:
                    continue
                keyword = self._FIELD_KEYWORDS.get(field)
                if not keyword:
                    continue

                # 准备数值搜索片段
                val_frag = self._format_value_for_search(value)

                # ── 策略1：报表区域内查找 ──
                sect_hits = self._find_all_occurrences(text, keyword, sect_start, sect_end)
                best_idx = None
                confidence = 0.9

                if sect_hits and val_frag:
                    # 在区域内找到关键词+数值同时出现的最佳位置
                    for hit in sect_hits:
                        nearby = text[hit:min(hit + 200, sect_end)].replace(' ', '').replace(',', '')
                        if val_frag in nearby:
                            best_idx = hit
                            confidence = 0.95
                            break
                if best_idx is None and sect_hits:
                    # 区域内仅关键词命中（无数值验证）→ 低置信度
                    best_idx = sect_hits[0]
                    confidence = 0.70

                # ── 策略2：全文回退（或提升关键词+数值命中） ──
                if best_idx is None or confidence <= 0.70:
                    all_hits = self._find_all_occurrences(text, keyword)
                    if all_hits and val_frag:
                        for hit in all_hits:
                            nearby = text[hit:min(hit + 200, len(text))].replace(' ', '').replace(',', '')
                            if val_frag in nearby:
                                # 全文范围内找到了关键词+数值 → 优于仅关键词的区域命中
                                best_idx = hit
                                confidence = 0.85
                                break
                    if best_idx is None and all_hits:
                        best_idx = all_hits[0]
                        confidence = 0.50

                if best_idx is None:
                    continue

                page_no = self._find_page_for_offset(best_idx, page_ranges)
                ctx_start = max(0, best_idx - 30)
                ctx_end = min(len(text), best_idx + len(keyword) + 80)
                snippet = text[ctx_start:ctx_end].replace('\n', ' ').strip()

                records.append({
                    'table_name': db_table,
                    'stock_code': stock_code,
                    'report_period': report_period,
                    'field_name': field,
                    'source_file': str(pdf_path),
                    'source_page': page_no,
                    'source_table_name': source_table_label,
                    'source_text': snippet,
                    'confidence': confidence,
                    'extract_method': 'regex',
                })

        return records

    def process_single_report(self, pdf_path, exchange='sse', use_llm=True):
        """处理单个财报PDF文件"""
        filename = os.path.basename(pdf_path)
        print(f"正在处理: {filename}", flush=True)
        
        # 解析文件名
        base_info = self.parse_filename(filename, exchange)
        if not base_info:
            print(f"  ✗ 无法解析文件名: {filename}")
            return None
        
        # 提取PDF文本（同时获取页码映射）
        text, page_ranges = self.extract_text_with_pages(pdf_path)
        if not text:
            print(f"  ✗ 无法提取PDF文本")
            return None

        # 从PDF内容精确识别报告期（修正纯文件名月份推断的误差）
        period_from_text, year_from_text = self._detect_report_period_from_text(text)
        if period_from_text:
            old_period = base_info.get('report_period', '')
            if period_from_text != old_period:
                print(f"  [报告期修正] {old_period} → {period_from_text}", flush=True)
            base_info['report_period'] = period_from_text
            base_info['report_year']   = year_from_text

        # 公司简称：去除文件名中可能的多余空格（如"九 芝 堂"→"九芝堂"）
        if base_info.get('stock_abbr'):
            base_info['stock_abbr'] = base_info['stock_abbr'].replace(' ', '')

        # ── 补全 stock_code / stock_abbr ──────────────────────────────
        # 优先级：① config 映射  ② PDF文本提取
        try:
            from config import CONFIG as _cfg
            _abbr_to_code = _cfg.get('stock_abbr_to_code', {})
            _code_to_abbr = {v: k for k, v in _abbr_to_code.items()}
        except Exception:
            _abbr_to_code, _code_to_abbr = {}, {}

        if not base_info.get('stock_code'):
            # ① config: 简称 → 代码
            code = _abbr_to_code.get(base_info.get('stock_abbr', ''), '')
            if code:
                base_info['stock_code'] = code
            else:
                # ② PDF文本
                m = re.search(r'(?:股票|证券|公司)?\s*代码[：:\s]*[A-Za-z]*\s*(\d{6})', text[:5000])
                if m:
                    base_info['stock_code'] = m.group(1)

        if not base_info.get('stock_abbr'):
            # ① config: 代码 → 简称
            abbr = _code_to_abbr.get(base_info.get('stock_code', ''), '')
            if abbr:
                base_info['stock_abbr'] = abbr
            else:
                # ② PDF文本
                m = re.search(r'(?:公司|股票|证券)\s*简称[：:\s]*([\u4e00-\u9fff]+)', text[:5000])
                if m:
                    base_info['stock_abbr'] = m.group(1)

        # 正则提取各类数据（同时记录每个字段的匹配位置）
        # prov_hits: (table_key, field) → (offset, raw_value_str, row_label)
        prov_hits: dict[tuple[str, str], tuple[int, str, str]] = {}
        results = {
            'core_indicators': self.extract_core_indicators(text, base_info, prov_hits),
            'income_sheet': self.extract_income_sheet_data(text, base_info, prov_hits),
            'balance_sheet': self.extract_balance_sheet_data(text, base_info, prov_hits),
            'cash_flow': self.extract_cash_flow_data(text, base_info, prov_hits)
        }

        # Python层预推算：用已提取的当期数据计算可派生的字段（margin/ratio等）
        # 需要在 provenance 构建之前执行，这样计算字段也能写入 prov_hits
        results = self._derive_fields(results, prov_hits)

        # ── 从提取时记录的匹配位置构建溯源记录 ─────────────────────────
        stock_code = base_info.get('stock_code', '')
        report_period = base_info.get('report_period', '')
        provenance_records = []
        for (table_key, field), hit in prov_hits.items():
            offset, raw_value_str, row_label = hit
            db_table = self._TABLE_KEY_TO_DB.get(table_key, table_key)
            # 获取实际写入数据库的值（经过单位换算/精度处理后的）
            field_value = None
            tbl_data = results.get(table_key)
            if tbl_data:
                field_value = tbl_data.get(field)

            if offset == -1:
                # 计算字段：没有 PDF 源位置，用公式作为说明
                provenance_records.append({
                    'stock_code': stock_code,
                    'report_period': report_period,
                    'table_name': db_table,
                    'field_name': field,
                    'field_value': str(field_value) if field_value is not None else None,
                    'source_pdf': str(pdf_path),
                    'page_no': None,
                    'row_label': raw_value_str,  # 公式描述
                    'extract_method': 'calculated',
                    'confidence': 1.0,
                    'raw_text': raw_value_str,   # 公式描述
                })
            else:
                page_no = self._find_page_for_offset(offset, page_ranges)
                ctx_start = max(0, offset - 30)
                ctx_end = min(len(text), offset + 80)
                raw_text = text[ctx_start:ctx_end].replace('\n', ' ').strip()
                provenance_records.append({
                    'stock_code': stock_code,
                    'report_period': report_period,
                    'table_name': db_table,
                    'field_name': field,
                    'field_value': str(field_value) if field_value is not None else None,
                    'source_pdf': str(pdf_path),
                    'page_no': page_no,
                    'row_label': row_label.replace('\n', ' ').strip() if row_label else None,
                    'extract_method': 'regex',
                    'confidence': 1.0,
                    'raw_text': raw_text,
                })
        results['_provenance'] = provenance_records

        # LLM兜底：补充正则未能提取的字段
        if use_llm and self._llm_api_key:
            # 按表准备 (section_text, data, field_descs, label, table_key)
            _llm_tasks = {
                'income_sheet':    (_INCOME_FIELDS_DESC,          '利润表',     ['合并利润表', '合并报表利润表', '利润表'],     ['母公司利润表']),
                'balance_sheet':   (_BALANCE_FIELDS_DESC,         '资产负债表', ['合并资产负债表', '合并报表资产负债表', '资产负债表'], ['母公司资产负债表']),
                'cash_flow':       (_CASH_FLOW_FIELDS_DESC,       '现金流量表', ['合并现金流量表', '现金流量表'],           ['母公司现金流量表']),
                'core_indicators': (_CORE_INDICATORS_FIELDS_DESC, '核心业绩指标', None, None),
            }

            # 仅对有缺失字段的表发起LLM调用
            pending = {}
            for table_key, (field_descs, label, starts, ends) in _llm_tasks.items():
                tbl_data = results.get(table_key, {})
                has_missing = any(
                    k not in _BASE_INFO_KEYS and k not in _PYTHON_COMPUTED_FIELDS
                    and tbl_data.get(k) is None
                    for k in field_descs
                )
                if not has_missing:
                    continue
                section = text[:10000] if starts is None else self._get_section(text, starts, ends)
                pending[table_key] = (section, tbl_data, field_descs, label)

            if pending:
                llm_all_filled = {}  # table_key -> [filled_field_names]
                with _LLMThreadPool(max_workers=len(pending)) as _pool:
                    futures = {
                        table_key: _pool.submit(self.llm_fill_missing, sec, dat, fds, lbl)
                        for table_key, (sec, dat, fds, lbl) in pending.items()
                    }
                    for table_key, fut in futures.items():
                        data_out, filled_fields = fut.result()
                        results[table_key] = data_out
                        if filled_fields:
                            llm_all_filled[table_key] = filled_fields

                # 为LLM填充的字段追加溯源记录（extract_method='llm'）
                for table_key, filled_fields in llm_all_filled.items():
                    db_table = self._TABLE_KEY_TO_DB.get(table_key, table_key)
                    for field in filled_fields:
                        field_value = results[table_key].get(field)
                        provenance_records.append({
                            'stock_code': stock_code,
                            'report_period': report_period,
                            'table_name': db_table,
                            'field_name': field,
                            'field_value': str(field_value) if field_value is not None else None,
                            'source_pdf': str(pdf_path),
                            'page_no': None,
                            'row_label': None,
                            'extract_method': 'llm',
                            'confidence': 0.6,
                            'raw_text': None,
                        })

        # 最终兜底：正则和LLM都未能填充的 operating_cf_per_share，用计算补上
        if results.get('core_indicators', {}).get('operating_cf_per_share') is None:
            self._post_llm_derive = True
            results = self._derive_fields(results, prov_hits)
            self._post_llm_derive = False

        print(f"  [完成] 提取完成（{len(results.get('_provenance', []))} 条溯源记录）", flush=True)
        return results

if __name__ == '__main__':
    from config import CONFIG as _CFG
    import os as _os

    _EXT_LLM = _CFG.get('extract_llm', _CFG.get('llm', {}))
    extractor = FinancialReportExtractor(
        llm_api_key=_EXT_LLM.get('api_key', ''),
        llm_base_url=_EXT_LLM.get('base_url', ''),
        llm_model=_EXT_LLM.get('model', ''),
    )

    # 测试上交所报告
    _sse_dir = _os.path.join(_CFG['reports_dir'], 'reports-上交所')
    _sse_candidates = [
        _os.path.join(_sse_dir, name)
        for name in sorted(_os.listdir(_sse_dir))
        if name.lower().endswith('.pdf')
    ] if _os.path.isdir(_sse_dir) else []
    result = extractor.process_single_report(_sse_candidates[0], exchange='sse', use_llm=True) if _sse_candidates else None

    # 测试深交所报告（取消注释使用）
    # _szse_dir = _os.path.join(_CFG['reports_dir'], 'reports-深交所')
    # test_file_szse = _os.path.join(_szse_dir, '某公司：某季度报告.pdf')
    # result = extractor.process_single_report(test_file_szse, exchange='szse', use_llm=True)

    if result:
        schema_map = {
            'core_indicators': _CORE_INDICATORS_FIELDS_DESC,
            'income_sheet':    _INCOME_FIELDS_DESC,
            'balance_sheet':   _BALANCE_FIELDS_DESC,
            'cash_flow':       _CASH_FLOW_FIELDS_DESC,
        }
        labels = {
            'core_indicators': '核心业绩指标',
            'income_sheet':    '利润表',
            'balance_sheet':   '资产负债表',
            'cash_flow':       '现金流量表',
        }
        base_fields = ('stock_code', 'stock_abbr', 'report_date', 'report_year', 'report_period')
        for key, label in labels.items():
            print(f"\n{'='*20} {label} {'='*20}")
            table = result[key]
            for field in base_fields:
                if field in table:
                    print(f"  {field}: {table[field]}")
            for field in schema_map[key]:
                status = '' if table.get(field) is not None else '  [未提取]'
                print(f"  {field}: {table.get(field)}{status}")
