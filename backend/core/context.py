from __future__ import annotations

from pathlib import Path

from config import CONFIG

BASE_DIR = Path(__file__).resolve().parents[2]
KB_DB_PATH = BASE_DIR / 'knowledge_base.db'
KB_DOCS_DIR = BASE_DIR / 'kb_uploads'
RESULT_DIR = BASE_DIR / 'result'
RESULT_WORKBOOK_PATH = BASE_DIR / 'result_2.xlsx'


def _resolve_db_path() -> Path:
    return BASE_DIR / CONFIG.get('database', {}).get('path', 'financial_reports.db')


def _resolve_attachment5_dir() -> Path:
    rd = CONFIG.get('research_reports_dir', '')
    if isinstance(rd, str) and rd.strip():
        return Path(rd)
    return BASE_DIR / 'B题-示例数据' / '示例数据' / '附件5：研报数据'


DB_PATH: Path = _resolve_db_path()
ATTACHMENT5_DIR: Path = _resolve_attachment5_dir()


def reload_paths() -> None:
    """Re-read DB_PATH and ATTACHMENT5_DIR from CONFIG after settings change."""
    global DB_PATH, ATTACHMENT5_DIR
    DB_PATH = _resolve_db_path()
    ATTACHMENT5_DIR = _resolve_attachment5_dir()

TABLE_ORDER = [
    'core_performance_indicators_sheet',
    'balance_sheet',
    'cash_flow_sheet',
    'income_sheet'
]
TABLE_LABELS = {
    'core_performance_indicators_sheet': '核心业绩指标表',
    'balance_sheet': '资产负债表',
    'cash_flow_sheet': '现金流量表',
    'income_sheet': '利润表'
}
FIELD_LABELS = {
    'serial_number': '序号',
    'stock_code': '股票代码',
    'stock_abbr': '股票简称',
    'report_period': '报告期',
    'report_year': '报告年份',
    # ── 核心业绩指标 ──
    'eps': '每股收益(元)',
    'total_operating_revenue': '营业总收入(万元)',
    'operating_revenue_yoy_growth': '营收同比增长(%)',
    'operating_revenue_qoq_growth': '营收环比增长(%)',
    'net_profit_10k_yuan': '净利润(万元)',
    'net_profit_yoy_growth': '净利润同比增长(%)',
    'net_profit_qoq_growth': '净利润环比增长(%)',
    'net_asset_per_share': '每股净资产(元)',
    'roe': 'ROE(%)',
    'operating_cf_per_share': '每股经营现金流(元)',
    'net_profit_excl_non_recurring': '扣非净利润(万元)',
    'net_profit_excl_non_recurring_yoy': '扣非净利润同比(%)',
    'gross_profit_margin': '毛利率(%)',
    'net_profit_margin': '净利润率(%)',
    'roe_weighted_excl_non_recurring': '加权ROE扣非(%)',
    # ── 利润表 ──
    'net_profit': '净利润(万元)',
    'other_income': '其他收益(万元)',
    'operating_expense_cost_of_sales': '营业成本(万元)',
    'operating_expense_selling_expenses': '销售费用(万元)',
    'operating_expense_administrative_expenses': '管理费用(万元)',
    'operating_expense_financial_expenses': '财务费用(万元)',
    'operating_expense_rnd_expenses': '研发费用(万元)',
    'operating_expense_taxes_and_surcharges': '税金及附加(万元)',
    'total_operating_expenses': '营业总成本(万元)',
    'operating_profit': '营业利润(万元)',
    'total_profit': '利润总额(万元)',
    'asset_impairment_loss': '资产减值损失(万元)',
    'credit_impairment_loss': '信用减值损失(万元)',
    # ── 资产负债表 ──
    'asset_cash_and_cash_equivalents': '货币资金(万元)',
    'asset_accounts_receivable': '应收账款(万元)',
    'asset_inventory': '存货(万元)',
    'asset_trading_financial_assets': '交易性金融资产(万元)',
    'asset_construction_in_progress': '在建工程(万元)',
    'asset_total_assets': '总资产(万元)',
    'asset_total_assets_yoy_growth': '总资产同比增长(%)',
    'liability_accounts_payable': '应付账款(万元)',
    'liability_advance_from_customers': '预收账款(万元)',
    'liability_total_liabilities': '负债合计(万元)',
    'liability_total_liabilities_yoy_growth': '负债同比增长(%)',
    'liability_contract_liabilities': '合同负债(万元)',
    'liability_short_term_loans': '短期借款(万元)',
    'asset_liability_ratio': '资产负债率(%)',
    'equity_unappropriated_profit': '未分配利润(万元)',
    'equity_total_equity': '股东权益合计(万元)',
    # ── 现金流量表 ──
    'net_cash_flow': '净现金流(元)',
    'net_cash_flow_yoy_growth': '净现金流同比增长(%)',
    'operating_cf_net_amount': '经营活动现金流净额(万元)',
    'operating_cf_ratio_of_net_cf': '经营现金流占比(%)',
    'operating_cf_cash_from_sales': '销售商品收到现金(万元)',
    'investing_cf_net_amount': '投资活动现金流净额(万元)',
    'investing_cf_ratio_of_net_cf': '投资现金流占比(%)',
    'investing_cf_cash_for_investments': '购买投资支付现金(万元)',
    'investing_cf_cash_from_investment_recovery': '投资回收现金(万元)',
    'financing_cf_cash_from_borrowing': '借款收到现金(万元)',
    'financing_cf_cash_for_debt_repayment': '偿债支付现金(万元)',
    'financing_cf_net_amount': '筹资活动现金流净额(万元)',
    'financing_cf_ratio_of_net_cf': '筹资现金流占比(%)',
    # ── 表结构 ──
    'cid': '列序号',
    'name': '字段名',
    'type': '字段类型',
    'notnull': '非空',
    'dflt_value': '默认值',
    'pk': '主键',
}
COMPANY_NAMES = sorted(CONFIG.get('stock_abbr_to_code', {}).keys())

# ── 公司别名/昵称 → 官方简称映射 ─────────────────────────────────────────────
# 包括：股票代码、口语简称、数字代号、常见缩写等
_STOCK_ABBR_TO_CODE = CONFIG.get('stock_abbr_to_code', {})
_COMPANY_ALIAS_MANUAL: dict[str, str] = {
    # 数字代号 / 品牌名
    '999': '华润三九', '三九': '华润三九', '华润': '华润三九', '三九药业': '华润三九',
    '白药': '云南白药', '云白': '云南白药',
    '同仁': '同仁堂',
    '太极': '太极集团',
    '步长': '步长制药',
    '以岭': '以岭药业', '岭药': '以岭药业',
    '片仔': '片仔癀', '片仔黄': '片仔癀',
    '济川': '济川药业',
    '天士': '天士力', '天士力医药': '天士力',
    '达仁': '达仁堂',
    '瑞康': '瑞康医药',
    '昆药': '昆药集团',
    '康恩': '康恩贝',
    '信邦': '信邦制药',
    '红日': '红日药业',
    '葵花': '葵花药业',
    '仁和': '仁和药业',
    '康美': '康美药业',
    '康缘': '康缘药业',
    '东阿': '东阿阿胶', '阿胶': '东阿阿胶',
    '江中': '江中药业',
    '健民': '健民集团',
    '康弘': '康弘药业',
    '千金': '千金药业',
    '振东': '振东制药',
    '羚锐': '羚锐制药',
    '珍宝': '珍宝岛',
    '中恒': '中恒集团',
    '马应龙': '马应龙',
    '亚宝': '亚宝药业',
    '九芝': '九芝堂',
    '益佰': '益佰制药',
    '众生': '众生药业',
    '金花': '金花股份',
    '神奇': '神奇制药',
    '桂林三金': '桂林三金', '三金': '桂林三金',
    '太龙': '太龙药业',
    '奇正': '奇正藏药', '藏药': '奇正藏药',
    '佐力': '佐力药业',
    '精华': '精华制药',
    '万邦': '万邦德',
    '通化': '通化金马', '金马': '通化金马',
    '康泰': '康泰医学',
    '盘龙': '盘龙药业',
    '华神': '华神科技',
    '新天': '新天药业',
    '恩威': '恩威医药',
    '华森': '华森制药',
    '沃华': '沃华医药',
    '寿仙': '寿仙谷',
    '特一': '特一药业',
    '益盛': '益盛药业',
    '康惠': '康惠制药',
    '嘉应': '嘉应制药',
    '维康': '维康药业',
    '启迪': '启迪药业',
    '粤万年青': '粤万年青', '万年青': '粤万年青',
    '新光': '新光药业',
    '天目': '天目药业',
    '莱茵': '莱茵生物',
    '广誉': '广誉远', '广誉远药业': '广誉远',
    '贵州三力': '贵州三力',
    '方盛': '方盛制药',
    '陇神': '陇神戎发', '戎发': '陇神戎发',
    '贵州百灵': '贵州百灵', '百灵': '贵州百灵',
    '赛隆': '赛隆药业',
    '长药': '长药控股',
    '香雪': '香雪制药',
    '新里程': '新里程',
}

# 自动添加：股票代码 → 公司简称
COMPANY_ALIAS: dict[str, str] = {}
for _abbr, _code in _STOCK_ABBR_TO_CODE.items():
    COMPANY_ALIAS[_code] = _abbr           # "000999" → "华润三九"
    COMPANY_ALIAS[_code.lstrip('0')] = _abbr  # "999" → "华润三九" (数字简写)
    COMPANY_ALIAS[_abbr] = _abbr           # 自身
COMPANY_ALIAS.update(_COMPANY_ALIAS_MANUAL)


def resolve_company_alias(text: str) -> str:
    """从文本中识别公司名（支持别名），返回官方简称。找不到返回空字符串。"""
    # 1. 精确匹配官方名称（最优先，按长度降序以免"三九"抢先于"华润三九"）
    for name in sorted(COMPANY_NAMES, key=len, reverse=True):
        if name in text:
            return name
    # 2. 别名匹配（按长度降序避免短别名误匹配）
    for alias in sorted(COMPANY_ALIAS.keys(), key=len, reverse=True):
        if alias in text:
            resolved = COMPANY_ALIAS[alias]
            if resolved in COMPANY_NAMES:
                return resolved
    return ''


def resolve_all_companies_in_text(text: str) -> list[str]:
    """从文本中识别所有出现的公司名（支持别名），返回去重列表。"""
    found: list[str] = []
    seen: set[str] = set()
    remaining = text
    for name in sorted(COMPANY_NAMES, key=len, reverse=True):
        if name in remaining:
            if name not in seen:
                found.append(name)
                seen.add(name)
            remaining = remaining.replace(name, ' ')
    for alias in sorted(COMPANY_ALIAS.keys(), key=len, reverse=True):
        if alias in remaining:
            resolved = COMPANY_ALIAS[alias]
            if resolved in COMPANY_NAMES and resolved not in seen:
                found.append(resolved)
                seen.add(resolved)
            remaining = remaining.replace(alias, ' ')
    return found


PERIOD_SORT_ORDER = {'Q1': 1, 'HY': 2, 'H1': 2, 'Q3': 3, 'Q3C': 3, 'FY': 4}
MAX_SQL_ROWS = 200
CORS_ORIGINS = [
    'http://127.0.0.1:5173',
    'http://localhost:5173',
    'http://127.0.0.1:5174',
    'http://localhost:5174',
    'http://127.0.0.1:5175',
    'http://localhost:5175',
    'http://127.0.0.1:4173',
    'http://localhost:4173'
]

RESULT_DIR.mkdir(parents=True, exist_ok=True)
