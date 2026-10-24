import sqlite3
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pdf_extractor import FinancialReportExtractor
from config import CONFIG

class DatabaseImporter:
    """数据库导入器"""
    


    
    def __init__(self, db_path='financial_reports.db'):
        self.db_path = db_path
        self.conn = None
        self.cursor = None
        self._job_id = None            # 当前任务ID
        self._issue_buffer: list = []  # 批量写入缓冲
        
    def connect(self):
        """连接数据库"""
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        self._ensure_tables()
        self.conn.commit()

    def _ensure_tables(self):
        """确保三张辅助表存在（兼容旧数据库）"""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS field_provenance (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code          VARCHAR(20)  NOT NULL,
                report_period       VARCHAR(20)  NOT NULL,
                table_name          VARCHAR(60)  NOT NULL,
                field_name          VARCHAR(80)  NOT NULL,
                field_value         TEXT,
                source_pdf          TEXT         NOT NULL,
                page_no             INTEGER,
                row_label           VARCHAR(200),
                extract_method      VARCHAR(20)  DEFAULT 'regex',
                confidence          REAL         DEFAULT 1.0,
                raw_text            TEXT,
                created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS import_issues (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id              VARCHAR(40),
                stock_code          VARCHAR(20),
                report_period       VARCHAR(20),
                table_name          VARCHAR(60),
                field_name          VARCHAR(80),
                issue_type          VARCHAR(40)  NOT NULL,
                issue_level         VARCHAR(10)  NOT NULL DEFAULT 'warning',
                original_value      TEXT,
                fixed_value         TEXT,
                message             TEXT,
                created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS import_jobs (
                job_id              VARCHAR(40)  PRIMARY KEY,
                started_at          TIMESTAMP,
                finished_at         TIMESTAMP,
                total_reports       INTEGER      DEFAULT 0,
                success_reports     INTEGER      DEFAULT 0,
                warning_count       INTEGER      DEFAULT 0,
                error_count         INTEGER      DEFAULT 0,
                summary_json        TEXT
            )
        ''')
        self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_fp_lookup  ON field_provenance(stock_code, report_period, table_name)')
        self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_fp_field   ON field_provenance(stock_code, report_period, field_name)')
        self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_issue_job  ON import_issues(job_id)')
        self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_issue_stock ON import_issues(stock_code, report_period)')

    # ─────────────────────────────────────────────────────────────────────
    # 异常日志记录
    # ─────────────────────────────────────────────────────────────────────
    def record_issue(self, issue_type, message, *,
                     stock_code=None, report_period=None, table_name=None,
                     field_name=None, issue_level='warning',
                     original_value=None, fixed_value=None):
        """记录一条导入异常/修复/警告到缓冲区"""
        self._issue_buffer.append((
            self._job_id, stock_code, report_period, table_name, field_name,
            issue_type, issue_level,
            str(original_value) if original_value is not None else None,
            str(fixed_value) if fixed_value is not None else None,
            message,
        ))

    def flush_issues(self):
        """将缓冲区中的 issues 批量写入数据库"""
        if not self._issue_buffer:
            return 0
        sql = (
            'INSERT INTO import_issues '
            '(job_id, stock_code, report_period, table_name, field_name, '
            ' issue_type, issue_level, original_value, fixed_value, message) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)'
        )
        self.cursor.executemany(sql, self._issue_buffer)
        count = len(self._issue_buffer)
        self._issue_buffer.clear()
        return count
        
    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
    
    def get_stock_code(self, stock_abbr):
        """根据股票简称获取股票代码（从 config.json 读取）"""
        return CONFIG.get('stock_abbr_to_code', {}).get(stock_abbr, '')
    
    def validate_data(self, data, table_type):
        """数据校验入口：必填字段 → 范围合理性 → 跨字段一致性"""
        if not data:
            return False

        # 1. 补全股票代码/简称
        if 'stock_code' not in data or not data['stock_code']:
            if 'stock_abbr' in data and data['stock_abbr']:
                data['stock_code'] = self.get_stock_code(data['stock_abbr'])
        if 'stock_abbr' not in data or not data['stock_abbr']:
            if 'stock_code' in data and data['stock_code']:
                _code_to_abbr = {v: k for k, v in CONFIG.get('stock_abbr_to_code', {}).items()}
                data['stock_abbr'] = _code_to_abbr.get(data['stock_code'], '')

        # 2. 必填字段检查
        required_fields = {
            'core_indicators': ['stock_code', 'report_period'],
            'income_sheet':    ['stock_code', 'report_period'],
            'balance_sheet':   ['stock_code', 'report_period'],
            'cash_flow':       ['stock_code', 'report_period']
        }
        for field in required_fields.get(table_type, []):
            if field not in data or not data[field]:
                print(f"  [校验失败] 缺少必填字段 {field}", flush=True)
                return False

        _sc = data.get('stock_code', '')
        _rp = data.get('report_period', '')

        # 3. 单位一致性检查（优先执行，确保后续校验基于正确数量级）
        warnings = self.validate_unit(data, table_type)
        for w in warnings:
            print(f"  [警告] {w}", flush=True)
            self.record_issue('unit_check', w, stock_code=_sc,
                              report_period=_rp, table_name=table_type)

        # 4. 数值字段范围合理性检查
        warnings = self.validate_range(data, table_type)
        for w in warnings:
            print(f"  [警告] {w}", flush=True)
            self.record_issue('range_check', w, stock_code=_sc,
                              report_period=_rp, table_name=table_type)

        # 5. 跨字段逻辑一致性检查（基于修正后的数值）
        warnings = self.validate_consistency(data, table_type)
        for w in warnings:
            print(f"  [警告] {w}", flush=True)
            self.record_issue('consistency_check', w, stock_code=_sc,
                              report_period=_rp, table_name=table_type)

        # 6. 将未提取到的数据设置为 NULL 而不是0
        for key in data:
            if isinstance(data[key], (int, float)) and data[key] == 0:
                data[key] = None

        return True

    # ─────────────────────────────────────────────────────────────────────
    # 校验维度一：数值范围合理性
    # ─────────────────────────────────────────────────────────────────────
    def validate_range(self, data, table_type):
        """检测并清除超出合理范围的数值，返回警告列表"""
        warnings = []

        # 增长率 / 变动率字段：[-16000, 16000] %
        growth_fields = [
            'operating_revenue_yoy_growth', 'operating_revenue_qoq_growth',
            'net_profit_yoy_growth', 'net_profit_qoq_growth',
            'net_profit_excl_non_recurring_yoy', 'net_cash_flow_yoy_growth',
            'asset_total_assets_yoy_growth', 'liability_total_liabilities_yoy_growth',
        ]
        for f in growth_fields:
            v = data.get(f)
            if v is not None and not (-16000 <= v <= 16000):
                warnings.append(f"{f}={v:.4f} 超出增长率合理范围[-16000,16000]%，已清除")
                data[f] = None

        # 资产负债率：[0, 300] %
        v = data.get('asset_liability_ratio')
        if v is not None and not (0 <= v <= 300):
            warnings.append(f"asset_liability_ratio={v:.4f} 超出[0,300]%，已清除")
            data['asset_liability_ratio'] = None

        # ROE / 利润率 / 毛利率：[-500, 100] %（毛利率不超100%）
        for f in ('roe', 'roe_weighted_excl_non_recurring'):
            v = data.get(f)
            if v is not None and not (-500 <= v <= 500):
                warnings.append(f"{f}={v:.4f} 超出[-500,500]%，已清除")
                data[f] = None
        for f in ('net_profit_margin', 'gross_profit_margin'):
            v = data.get(f)
            if v is not None and not (-500 <= v <= 100):
                warnings.append(f"{f}={v:.4f} 超出[-500,100]%，已清除")
                data[f] = None

        # 每股指标合理范围（元/股）：防止误提取年份数字或单位错误
        per_share_ranges = {
            'eps':                    (-100,   100),
            'net_asset_per_share':    (-10000, 10000),
            'operating_cf_per_share': (-10000, 10000),
        }
        for f, (lo, hi) in per_share_ranges.items():
            v = data.get(f)
            if v is not None:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    data[f] = None
                    continue
            if v is not None and not (lo <= v <= hi):
                warnings.append(f"{f}={v} 超出每股合理范围[{lo},{hi}]元/股，已清除")
                data[f] = None

        # 必须为正值的字段（>0）
        positive_map = {
            'balance_sheet':   ['asset_total_assets', 'equity_total_equity'],
            'income_sheet':    ['total_operating_revenue'],
            'core_indicators': ['total_operating_revenue'],
        }
        for f in positive_map.get(table_type, []):
            v = data.get(f)
            if v is not None and v <= 0:
                warnings.append(f"{f}={v:.4f} 应为正值，已清除")
                data[f] = None

        # ④ 减值损失符号：减值损失应≤0（损失计入费用），正值可能是提取符号丢失
        # 注意：部分期间存在转回收益（正值合理），故仅警告不清除
        if table_type == 'income_sheet':
            for f in ('asset_impairment_loss', 'credit_impairment_loss'):
                v = data.get(f)
                if v is not None and v > 0:
                    warnings.append(
                        f"{f}={v:.2f}万元为正值（减值损失通常应≤0，若为转回收益可忽略）"
                    )

        return warnings

    # ─────────────────────────────────────────────────────────────────────
    # 校验维度二：跨字段逻辑一致性
    # ─────────────────────────────────────────────────────────────────────
    def validate_consistency(self, data, table_type):
        """跨字段推算交叉验证，偏差超5%时以推算值修正存储值"""
        warnings = []
        TOL = 0.05  # 5% 相对容忍度

        def _rdiff(calc, stored):
            if calc is None or stored is None or stored == 0:
                return None
            return abs(calc - stored) / abs(stored)

        if table_type == 'core_indicators':
            # 净利润率 = 净利润 / 营业总收入 × 100
            np_ = data.get('net_profit_10k_yuan')
            rev = data.get('total_operating_revenue')
            if np_ is not None and rev and rev != 0:
                calc = round(np_ / rev * 100, 4)
                stored = data.get('net_profit_margin')
                if stored is not None:
                    d = _rdiff(calc, stored)
                    if d is not None and d > TOL:
                        warnings.append(
                            f"净利润率: 计算{calc:.4f}% vs 存储{stored:.4f}%，偏差{d*100:.1f}%，已修正"
                        )
                        data['net_profit_margin'] = calc
                else:
                    data['net_profit_margin'] = calc

        if table_type == 'balance_sheet':
            tl = data.get('liability_total_liabilities')
            ta = data.get('asset_total_assets')
            eq = data.get('equity_total_equity')

            # 资产负债率 = 负债 / 资产 × 100
            if tl is not None and ta and ta != 0:
                calc = round(tl / ta * 100, 4)
                stored = data.get('asset_liability_ratio')
                if stored is not None:
                    d = _rdiff(calc, stored)
                    if d is not None and d > TOL:
                        warnings.append(
                            f"资产负债率: 计算{calc:.4f}% vs 存储{stored:.4f}%，偏差{d*100:.1f}%，已修正"
                        )
                        data['asset_liability_ratio'] = calc
                else:
                    data['asset_liability_ratio'] = calc

            # 资产 = 负债 + 所有者权益（恒等式验证）
            if tl is not None and ta is not None and eq is not None:
                calc_ta = tl + eq
                d = _rdiff(calc_ta, ta)
                if d is not None and d > TOL:
                    warnings.append(
                        f"资产负债权益恒等式: 负债{tl:.0f}+权益{eq:.0f}={calc_ta:.0f} "
                        f"vs 总资产{ta:.0f}，偏差{d*100:.1f}%（警告，未修改）"
                    )

        if table_type == 'income_sheet':
            rev  = data.get('total_operating_revenue')
            cost = data.get('operating_expense_cost_of_sales')
            # 毛利率范围合理性（计算值仅做警告）
            if rev and rev != 0 and cost is not None:
                gpm = (rev - cost) / rev * 100
                if not (-200 <= gpm <= 100):
                    warnings.append(
                        f"毛利率计算值{gpm:.4f}% 异常，请核查营业收入或营业成本字段"
                    )
            # 利润表勾稽：营业利润 ≈ 营业总收入 - 营业总成本
            # 差额来自其他收益、投资收益、减值损失等项目，宽容差设20%
            total_cost = data.get('total_operating_expenses')
            op_profit  = data.get('operating_profit')
            if rev and total_cost is not None and op_profit is not None and rev != 0:
                calc_op = rev - total_cost
                d = _rdiff(calc_op, op_profit)
                if d is not None and d > 0.20:
                    warnings.append(
                        f"营业利润勾稽: 收入{rev:.2f}-总成本{total_cost:.2f}={calc_op:.2f} "
                        f"vs 营业利润{op_profit:.2f}万元，偏差{d*100:.1f}%（含其他收益/减值）"
                    )

        # 现金流量表恒等式：经营+投资+筹资 ≈ 净增加额
        # net_cash_flow单位为元，三大活动单位为万元，统一换算为万元后比较
        if table_type == 'cash_flow':
            op_cf  = data.get('operating_cf_net_amount')
            inv_cf = data.get('investing_cf_net_amount')
            fin_cf = data.get('financing_cf_net_amount')
            net_cf = data.get('net_cash_flow')
            if op_cf is not None and inv_cf is not None and fin_cf is not None and net_cf is not None:
                calc_net_10k   = op_cf + inv_cf + fin_cf   # 万元
                stored_net_10k = net_cf / 10000            # 元→万元
                d = _rdiff(calc_net_10k, stored_net_10k)
                if d is not None and d > TOL:
                    warnings.append(
                        f"现金流恒等式: 三大活动合计{calc_net_10k:.2f}万元 "
                        f"vs 净增加额{stored_net_10k:.2f}万元，偏差{d*100:.1f}%（警告，未修改）"
                    )

        return warnings

    # ─────────────────────────────────────────────────────────────────────
    # 校验维度三：单位一致性
    # ─────────────────────────────────────────────────────────────────────
    # 各表中以"万元"为单位的金额字段（precision=2 且非 net_cash_flow）
    _AMOUNT_FIELDS_10K = {
        'core_indicators': [
            'total_operating_revenue', 'net_profit_10k_yuan',
            'net_profit_excl_non_recurring',
        ],
        'income_sheet': [
            'total_operating_revenue', 'net_profit',
            'total_operating_expenses', 'operating_profit', 'total_profit',
            'other_income', 'asset_impairment_loss', 'credit_impairment_loss',
            'operating_expense_cost_of_sales', 'operating_expense_selling_expenses',
            'operating_expense_administrative_expenses', 'operating_expense_financial_expenses',
            'operating_expense_rnd_expenses', 'operating_expense_taxes_and_surcharges',
        ],
        'balance_sheet': [
            'asset_total_assets', 'liability_total_liabilities', 'equity_total_equity',
            'asset_cash_and_cash_equivalents', 'asset_accounts_receivable',
            'asset_inventory', 'asset_trading_financial_assets',
            'asset_construction_in_progress',
            'liability_accounts_payable', 'liability_advance_from_customers',
            'liability_contract_liabilities', 'liability_short_term_loans',
            'equity_unappropriated_profit',
        ],
        'cash_flow': [
            'operating_cf_net_amount', 'operating_cf_cash_from_sales',
            'investing_cf_net_amount', 'investing_cf_cash_for_investments',
            'investing_cf_cash_from_investment_recovery',
            'financing_cf_net_amount', 'financing_cf_cash_from_borrowing',
            'financing_cf_cash_for_debt_repayment',
        ],
    }

    def validate_unit(self, data, table_type):
        """检测可能存在单位未换算的字段，优先尝试元→万元换算而非直接清除"""
        warnings = []
        # 万元字段若 > 1e9（即超过 1000 亿万元），极可能是元未换算
        for f in self._AMOUNT_FIELDS_10K.get(table_type, []):
            v = data.get(f)
            if v is not None and abs(v) > 1e9:
                converted = round(v / 10000, 2)
                if abs(converted) <= 1e9:
                    warnings.append(
                        f"{f}={v:.0f} 万元，数量级异常（疑似未做元→万元换算），"
                        f"已自动换算为 {converted:.2f} 万元"
                    )
                    data[f] = converted
                else:
                    warnings.append(
                        f"{f}={v:.0f} 万元，数量级异常且换算后仍不合理，已清除"
                    )
                    data[f] = None

        # net_cash_flow 以"元"为单位，阈值更宽（> 1e13 ≈ 10万亿元）
        if table_type == 'cash_flow':
            v = data.get('net_cash_flow')
            if v is not None and abs(v) > 1e13:
                converted = round(v / 10000, 2)
                if abs(converted) <= 1e13:
                    warnings.append(
                        f"net_cash_flow={v:.0f} 元，数量级异常（疑似单位错误），"
                        f"已自动换算为 {converted:.2f} 元"
                    )
                    data['net_cash_flow'] = converted
                else:
                    warnings.append(
                        f"net_cash_flow={v:.0f} 元，数量级异常且换算后仍不合理，已清除"
                    )
                    data['net_cash_flow'] = None

        return warnings

    # ─────────────────────────────────────────────────────────────────────
    # 校验维度四：重复数据检测与合并
    # ─────────────────────────────────────────────────────────────────────
    def merge_with_existing(self, data, table_name, fields):
        """与数据库现有记录合并：新数据非None值优先，以现有值填补新数据的None字段。
        返回 (merged_data, is_duplicate)"""
        stock_code    = data.get('stock_code', '')
        report_period = data.get('report_period', '')
        if not stock_code or not report_period:
            return data, False
        try:
            self.cursor.execute(
                f"SELECT {','.join(fields)} FROM {table_name} "
                f"WHERE stock_code=? AND report_period=?",
                (stock_code, report_period)
            )
            row = self.cursor.fetchone()
            if row is None:
                return data, False
            existing = dict(zip(fields, row))
            na_fields = set(data.get('_na_fields') or [])
            # 合并：新数据为主，现有非None值填补新数据的空缺
            merged = {}
            for k in fields:
                new_v = data.get(k)
                old_v = existing.get(k)
                merged[k] = new_v if (new_v is not None or k in na_fields) else old_v
            return merged, True
        except Exception:
            return data, False
    
    # ─────────────────────────────────────────────────────────────────────
    # 字段精度规范（对应 create_database.py 中 DECIMAL(p,s) 声明）
    # ─────────────────────────────────────────────────────────────────────
    _FIELD_PRECISION = {
        # DECIMAL(10,4)：比率 / 增长率 / 每股指标
        'eps':                                       4,
        'operating_revenue_yoy_growth':              4,
        'operating_revenue_qoq_growth':              4,
        'net_profit_yoy_growth':                     4,
        'net_profit_qoq_growth':                     4,
        'net_asset_per_share':                       4,
        'roe':                                       4,
        'operating_cf_per_share':                    4,
        'net_profit_excl_non_recurring_yoy':         4,
        'gross_profit_margin':                       4,
        'net_profit_margin':                         4,
        'roe_weighted_excl_non_recurring':           4,
        'asset_total_assets_yoy_growth':             4,
        'liability_total_liabilities_yoy_growth':    4,
        'asset_liability_ratio':                     4,
        'net_cash_flow_yoy_growth':                  4,
        'operating_cf_ratio_of_net_cf':              4,
        'investing_cf_ratio_of_net_cf':              4,
        'financing_cf_ratio_of_net_cf':              4,
        # DECIMAL(20,2)：金额类（万元；仅 net_cash_flow 为元）
        'total_operating_revenue':                   2,
        'net_profit_10k_yuan':                       2,
        'net_profit_excl_non_recurring':             2,
        'asset_cash_and_cash_equivalents':           2,
        'asset_accounts_receivable':                 2,
        'asset_inventory':                           2,
        'asset_trading_financial_assets':            2,
        'asset_construction_in_progress':            2,
        'asset_total_assets':                        2,
        'liability_accounts_payable':                2,
        'liability_advance_from_customers':          2,
        'liability_total_liabilities':               2,
        'liability_contract_liabilities':            2,
        'liability_short_term_loans':                2,
        'equity_unappropriated_profit':              2,
        'equity_total_equity':                       2,
        'net_cash_flow':                             2,
        'operating_cf_net_amount':                   2,
        'operating_cf_cash_from_sales':              2,
        'investing_cf_net_amount':                   2,
        'investing_cf_cash_for_investments':         2,
        'investing_cf_cash_from_investment_recovery':2,
        'financing_cf_cash_from_borrowing':          2,
        'financing_cf_cash_for_debt_repayment':      2,
        'financing_cf_net_amount':                   2,
        'net_profit':                                2,
        'other_income':                              2,
        'operating_expense_cost_of_sales':           2,
        'operating_expense_selling_expenses':        2,
        'operating_expense_administrative_expenses': 2,
        'operating_expense_financial_expenses':      2,
        'operating_expense_rnd_expenses':            2,
        'operating_expense_taxes_and_surcharges':    2,
        'total_operating_expenses':                  2,
        'operating_profit':                          2,
        'total_profit':                              2,
        'asset_impairment_loss':                     2,
        'credit_impairment_loss':                    2,
    }

    def _round_data(self, data):
        """按 _FIELD_PRECISION 对数值字段做格式化处理，生成固定小数位的字符串（原地修改并返回 data）"""
        for field, digits in self._FIELD_PRECISION.items():
            v = data.get(field)
            if v is not None:
                try:
                    v_clean = str(v).replace(',', '') if isinstance(v, str) else v
                    data[field] = f"{float(v_clean):.{digits}f}"
                except (TypeError, ValueError):
                    pass
        return data

    def normalize_precision(self):
        """按 _FIELD_PRECISION 批量修正数据库中历史数据的精度。
        
        步骤一：若列类型为 DECIMAL（NUMERIC 亲和性），迁移为 TEXT 亲和性，
                以确保 SQLite 能保留尾部零（如 1.50、2.0000）。
        步骤二：用 printf 格式化所有数值字段，补齐规定小数位的尾部零。
        """
        print("  正在标准化历史数据精度...", flush=True)
        table_names = (
            'core_performance_indicators_sheet',
            'income_sheet',
            'balance_sheet',
            'cash_flow_sheet',
        )

        _INDEX_SQLS = {
            'core_performance_indicators_sheet': [
                'CREATE INDEX IF NOT EXISTS idx_core_stock_code    ON core_performance_indicators_sheet(stock_code)',
                'CREATE INDEX IF NOT EXISTS idx_core_stock_abbr    ON core_performance_indicators_sheet(stock_abbr)',
                'CREATE INDEX IF NOT EXISTS idx_core_report_period ON core_performance_indicators_sheet(report_period)',
            ],
            'balance_sheet': [
                'CREATE INDEX IF NOT EXISTS idx_balance_stock_code    ON balance_sheet(stock_code)',
                'CREATE INDEX IF NOT EXISTS idx_balance_stock_abbr    ON balance_sheet(stock_abbr)',
                'CREATE INDEX IF NOT EXISTS idx_balance_report_period ON balance_sheet(report_period)',
            ],
            'cash_flow_sheet': [
                'CREATE INDEX IF NOT EXISTS idx_cash_stock_code    ON cash_flow_sheet(stock_code)',
                'CREATE INDEX IF NOT EXISTS idx_cash_stock_abbr    ON cash_flow_sheet(stock_abbr)',
                'CREATE INDEX IF NOT EXISTS idx_cash_report_period ON cash_flow_sheet(report_period)',
            ],
            'income_sheet': [
                'CREATE INDEX IF NOT EXISTS idx_income_stock_code    ON income_sheet(stock_code)',
                'CREATE INDEX IF NOT EXISTS idx_income_stock_abbr    ON income_sheet(stock_abbr)',
                'CREATE INDEX IF NOT EXISTS idx_income_report_period ON income_sheet(report_period)',
            ],
        }

        # ── 步骤一：DECIMAL（NUMERIC 亲和性）→ TEXT 迁移 ────────────────────
        for table in table_names:
            self.cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            row = self.cursor.fetchone()
            if not row or 'DECIMAL' not in row[0]:
                continue  # 已是 TEXT 或表不存在，无需迁移

            tmp_name = table + '_migration_tmp'
            new_sql = (row[0]
                       .replace('DECIMAL(20,2)', 'TEXT')
                       .replace('DECIMAL(10,4)', 'TEXT'))
            new_sql = new_sql.replace(f'TABLE {table}', f'TABLE {tmp_name}', 1)

            self.cursor.execute(f'DROP TABLE IF EXISTS {tmp_name}')
            self.cursor.execute(new_sql)

            self.cursor.execute(f'PRAGMA table_info({table})')
            col_names = ', '.join(r[1] for r in self.cursor.fetchall())
            self.cursor.execute(f'INSERT INTO {tmp_name} SELECT {col_names} FROM {table}')
            self.cursor.execute(f'DROP TABLE {table}')
            self.cursor.execute(f'ALTER TABLE {tmp_name} RENAME TO {table}')

            for idx_sql in _INDEX_SQLS.get(table, []):
                self.cursor.execute(idx_sql)

            self.conn.commit()
            print(f"  已迁移 {table}：DECIMAL → TEXT", flush=True)

        # ── 步骤二：printf 格式化，补齐尾部零 ───────────────────────────────
        update_count = 0
        for table in table_names:
            self.cursor.execute(f'PRAGMA table_info({table})')
            table_columns = {row[1] for row in self.cursor.fetchall()}
            precision_fields = [
                (field, digits)
                for field, digits in self._FIELD_PRECISION.items()
                if field in table_columns
            ]
            for field, digits in precision_fields:
                fmt = f'%.{digits}f'
                self.cursor.execute(
                    f"UPDATE {table} "
                    f"SET {field} = printf('{fmt}', CAST({field} AS REAL)) "
                    f"WHERE {field} IS NOT NULL"
                )
                update_count += self.cursor.rowcount

        self.conn.commit()
        print(f"  历史数据精度标准化完成，共更新 {update_count} 条记录", flush=True)
        return update_count

    def insert_core_indicators(self, data):
        """插入核心业绩指标数据"""
        if not self.validate_data(data, 'core_indicators'):
            return False

        fields = [
            'stock_code', 'stock_abbr', 'eps', 'total_operating_revenue',
            'operating_revenue_yoy_growth', 'net_profit_10k_yuan',
            'net_profit_yoy_growth', 'net_asset_per_share', 'roe',
            'operating_cf_per_share', 'net_profit_excl_non_recurring',
            'net_profit_excl_non_recurring_yoy', 'gross_profit_margin',
            'net_profit_margin', 'roe_weighted_excl_non_recurring',
            'operating_revenue_qoq_growth', 'net_profit_qoq_growth',
            'report_period', 'report_year'
        ]

        data, is_dup = self.merge_with_existing(data, 'core_performance_indicators_sheet', fields)
        if is_dup:
            print(f"  [重复合并] {data.get('stock_code')} "
                  f"{data.get('report_period')} 核心业绩指标已存在，合并更新", flush=True)

        self._round_data(data)
        values = [data.get(field) for field in fields]
        placeholders = ','.join(['?' for _ in fields])
        sql = 'INSERT OR REPLACE INTO core_performance_indicators_sheet ({}) VALUES ({})'.format(
            ','.join(fields), placeholders)
        try:
            self.cursor.execute(sql, values)
            return True
        except Exception as e:
            print(f"插入核心业绩指标数据失败: {e}")
            return False

    def insert_income_sheet(self, data):
        """插入利润表数据"""
        if not self.validate_data(data, 'income_sheet'):
            return False

        fields = [
            'stock_code', 'stock_abbr', 'net_profit', 'net_profit_yoy_growth',
            'other_income', 'total_operating_revenue', 'operating_revenue_yoy_growth',
            'operating_expense_cost_of_sales', 'operating_expense_selling_expenses',
            'operating_expense_administrative_expenses', 'operating_expense_financial_expenses',
            'operating_expense_rnd_expenses', 'operating_expense_taxes_and_surcharges',
            'total_operating_expenses', 'operating_profit', 'total_profit',
            'asset_impairment_loss', 'credit_impairment_loss',
            'report_period', 'report_year'
        ]

        data, is_dup = self.merge_with_existing(data, 'income_sheet', fields)
        if is_dup:
            print(f"  [重复合并] {data.get('stock_code')} "
                  f"{data.get('report_period')} 利润表已存在，合并更新", flush=True)

        self._round_data(data)
        values = [data.get(field) for field in fields]
        placeholders = ','.join(['?' for _ in fields])
        sql = 'INSERT OR REPLACE INTO income_sheet ({}) VALUES ({})'.format(
            ','.join(fields), placeholders)
        try:
            self.cursor.execute(sql, values)
            return True
        except Exception as e:
            print(f"插入利润表数据失败: {e}")
            return False
    
    def insert_balance_sheet(self, data):
        """插入资产负债表数据"""
        if not self.validate_data(data, 'balance_sheet'):
            return False

        fields = [
            'stock_code', 'stock_abbr',
            'asset_cash_and_cash_equivalents', 'asset_accounts_receivable',
            'asset_inventory', 'asset_trading_financial_assets',
            'asset_construction_in_progress', 'asset_total_assets',
            'asset_total_assets_yoy_growth', 'liability_accounts_payable',
            'liability_advance_from_customers', 'liability_total_liabilities',
            'liability_total_liabilities_yoy_growth', 'liability_contract_liabilities',
            'liability_short_term_loans', 'asset_liability_ratio',
            'equity_unappropriated_profit', 'equity_total_equity',
            'report_period', 'report_year'
        ]

        data, is_dup = self.merge_with_existing(data, 'balance_sheet', fields)
        if is_dup:
            print(f"  [重复合并] {data.get('stock_code')} "
                  f"{data.get('report_period')} 资产负债表已存在，合并更新", flush=True)

        self._round_data(data)
        values = [data.get(field) for field in fields]
        placeholders = ','.join(['?' for _ in fields])
        sql = 'INSERT OR REPLACE INTO balance_sheet ({}) VALUES ({})'.format(
            ','.join(fields), placeholders)
        try:
            self.cursor.execute(sql, values)
            return True
        except Exception as e:
            print(f"插入资产负债表数据失败: {e}")
            return False
    
    def insert_cash_flow(self, data):
        """插入现金流量表数据"""
        if not self.validate_data(data, 'cash_flow'):
            return False

        fields = [
            'stock_code', 'stock_abbr', 'net_cash_flow', 'net_cash_flow_yoy_growth',
            'operating_cf_net_amount', 'operating_cf_ratio_of_net_cf',
            'operating_cf_cash_from_sales', 'investing_cf_net_amount',
            'investing_cf_ratio_of_net_cf', 'investing_cf_cash_for_investments',
            'investing_cf_cash_from_investment_recovery', 'financing_cf_cash_from_borrowing',
            'financing_cf_cash_for_debt_repayment', 'financing_cf_net_amount',
            'financing_cf_ratio_of_net_cf', 'report_period', 'report_year'
        ]

        data, is_dup = self.merge_with_existing(data, 'cash_flow_sheet', fields)
        if is_dup:
            print(f"  [重复合并] {data.get('stock_code')} "
                  f"{data.get('report_period')} 现金流量表已存在，合并更新", flush=True)

        self._round_data(data)
        values = [data.get(field) for field in fields]
        placeholders = ','.join(['?' for _ in fields])
        sql = 'INSERT OR REPLACE INTO cash_flow_sheet ({}) VALUES ({})'.format(
            ','.join(fields), placeholders)
        try:
            self.cursor.execute(sql, values)
            return True
        except Exception as e:
            print(f"插入现金流量表数据失败: {e}")
            return False
    
    def validate_cross_table(self, results):
        """跨表一致性校验：同一报告中 income_sheet 与 core_indicators 关键字段对比。

        补充说明：
        - net_profit (income_sheet, 万元) 应等于 net_profit_10k_yuan (core_indicators, 万元)
        - total_operating_revenue 两表应一致
        - 来源不同（利润表正文 vs 摘要表），Q3摘要表为单季，利润表正文为累计，
          因此仅对非Q3报告做此校验（Q3两者口径本就不同）。
        """
        income = results.get('income_sheet') or {}
        core   = results.get('core_indicators') or {}
        report_period = core.get('report_period') or income.get('report_period') or ''
        period_suffix = report_period[4:] if len(report_period) > 4 else ''

        # Q3报告：income_sheet部分字段已是单季（来自摘要表），core_indicators也是单季
        # 两者实际上来源相同，不需要跨表对比（否则会产生误报）
        # 对Q1/HY/FY：income_sheet和core_indicators应一致
        if period_suffix == 'Q3':
            return

        report_id = f"{core.get('stock_code', '?')} {report_period}"
        TOL = 0.05

        def _rdiff(a, b):
            if a is None or b is None or b == 0:
                return None
            return abs(a - b) / abs(b)

        # 净利润跨表核对
        np_inc  = income.get('net_profit')
        np_core = core.get('net_profit_10k_yuan')
        d = _rdiff(np_inc, np_core)
        if d is not None and d > TOL:
            print(
                f"  [跨表警告] {report_id} 净利润: "
                f"利润表{np_inc:.2f} vs 核心指标{np_core:.2f}万元，偏差{d*100:.1f}%",
                flush=True
            )

        # 营业总收入跨表核对
        rev_inc  = income.get('total_operating_revenue')
        rev_core = core.get('total_operating_revenue')
        d = _rdiff(rev_inc, rev_core)
        if d is not None and d > TOL:
            print(
                f"  [跨表警告] {report_id} 营业总收入: "
                f"利润表{rev_inc:.2f} vs 核心指标{rev_core:.2f}万元，偏差{d*100:.1f}%",
                flush=True
            )

    def save_provenance(self, provenance_records):
        """将溯源记录批量写入 field_provenance 表"""
        if not provenance_records:
            return 0
        sql = (
            'INSERT INTO field_provenance '
            '(stock_code, report_period, table_name, field_name, field_value, '
            ' source_pdf, page_no, row_label, extract_method, confidence, raw_text) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)'
        )
        count = 0
        for rec in provenance_records:
            try:
                self.cursor.execute(sql, (
                    rec['stock_code'], rec['report_period'], rec['table_name'],
                    rec['field_name'], rec.get('field_value'),
                    rec['source_pdf'], rec.get('page_no'),
                    rec.get('row_label'), rec.get('extract_method', 'regex'),
                    rec.get('confidence', 1.0), rec.get('raw_text'),
                ))
                count += 1
            except Exception as e:
                print(f"  [溯源] 写入失败: {e}", flush=True)
        return count

    def import_report_data(self, results):
        """导入单个报告的所有数据"""
        success_count = 0
        db_table_map = {
            'core_indicators': 'core_performance_indicators_sheet',
            'income_sheet': 'income_sheet',
            'balance_sheet': 'balance_sheet',
            'cash_flow': 'cash_flow_sheet',
        }
        na_locked = set()

        for result_key, db_table in db_table_map.items():
            data = results.get(result_key) or {}
            na_fields = set(data.get('_na_fields') or [])
            for field in na_fields:
                data[field] = None
                na_locked.add((db_table, field))
                self.record_issue(
                    'not_applicable',
                    f'{field} 在PDF中标记为不适用，保持NULL且跳过后续补值',
                    stock_code=data.get('stock_code'),
                    report_period=data.get('report_period'),
                    table_name=db_table,
                    field_name=field,
                    issue_level='info',
                )

        # 先对所有表做单位修正，再做跨表校验（否则跨表校验看到的是未修正的值）
        for result_key in ('core_indicators', 'income_sheet', 'balance_sheet', 'cash_flow'):
            data = results.get(result_key)
            if data:
                _sc = data.get('stock_code', '')
                _rp = data.get('report_period', '')
                unit_warnings = self.validate_unit(data, result_key)
                for w in unit_warnings:
                    print(f"  [预校验-单位] {w}", flush=True)
                    self.record_issue('unit_check', w, stock_code=_sc,
                                      report_period=_rp, table_name=result_key)

        # 跨表一致性校验（此时单位已修正，不会误报）
        self.validate_cross_table(results)

        for table_name, result_key in (('balance_sheet', 'balance_sheet'), ('cash_flow', 'cash_flow')):
            data = results.get(result_key) or {}
            business_values = [
                v for k, v in data.items()
                if k not in ('stock_code', 'stock_abbr', 'report_date', 'report_year', 'report_period')
            ]
            if data and not any(v is not None for v in business_values):
                self.record_issue(
                    'empty_extraction',
                    f'{table_name} 未提取到任何业务字段，请检查 PDF 版式或锚点定位逻辑',
                    stock_code=data.get('stock_code'),
                    report_period=data.get('report_period'),
                    table_name=table_name,
                    issue_level='warning',
                )
        
        if results.get('core_indicators'):
            if self.insert_core_indicators(results['core_indicators']):
                success_count += 1
        
        if results.get('income_sheet'):
            if self.insert_income_sheet(results['income_sheet']):
                success_count += 1
        
        if results.get('balance_sheet'):
            if self.insert_balance_sheet(results['balance_sheet']):
                success_count += 1
        
        if results.get('cash_flow'):
            if self.insert_cash_flow(results['cash_flow']):
                success_count += 1

        # 写入溯源记录
        prov_records = [
            rec for rec in (results.get('_provenance', []) or [])
            if (rec.get('table_name'), rec.get('field_name')) not in na_locked
        ]
        if prov_records:
            prov_count = self.save_provenance(prov_records)
            if prov_count:
                print(f"  [溯源] 已写入 {prov_count} 条溯源记录", flush=True)

        # 刷写异常日志
        self.flush_issues()
        
        return success_count
    
    def commit(self):
        """提交事务"""
        if self.conn:
            self.conn.commit()

    def sync_yoy_data(self):
        """同步 core_performance_indicators_sheet 和 income_sheet 表中的同比数据"""
        print("  正在同步同比数据...", flush=True)
        update_count = 0

        for table_name, field_name in (
            ('core_performance_indicators_sheet', 'operating_revenue_yoy_growth'),
            ('core_performance_indicators_sheet', 'net_profit_yoy_growth'),
            ('income_sheet', 'operating_revenue_yoy_growth'),
            ('income_sheet', 'net_profit_yoy_growth'),
        ):
            self.cursor.execute(
                f'''
                    UPDATE {table_name}
                    SET {field_name} = NULL
                    WHERE {field_name} IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM import_issues
                        WHERE import_issues.stock_code = {table_name}.stock_code
                        AND import_issues.report_period = {table_name}.report_period
                        AND import_issues.table_name = ?
                        AND import_issues.field_name = ?
                        AND import_issues.issue_type = 'not_applicable'
                    )
                ''',
                (table_name, field_name)
            )
            update_count += self.cursor.rowcount
        
        # 从 income_sheet 同步到 core_performance_indicators_sheet
        self.cursor.execute('''
            UPDATE core_performance_indicators_sheet
            SET operating_revenue_yoy_growth = (
                SELECT operating_revenue_yoy_growth
                FROM income_sheet
                WHERE income_sheet.stock_code = core_performance_indicators_sheet.stock_code
                AND income_sheet.report_period = core_performance_indicators_sheet.report_period
            )
            WHERE EXISTS (
                SELECT 1
                FROM income_sheet
                WHERE income_sheet.stock_code = core_performance_indicators_sheet.stock_code
                AND income_sheet.report_period = core_performance_indicators_sheet.report_period
                AND income_sheet.operating_revenue_yoy_growth IS NOT NULL
            )
            AND (core_performance_indicators_sheet.operating_revenue_yoy_growth IS NULL
                 OR core_performance_indicators_sheet.operating_revenue_yoy_growth != (
                     SELECT operating_revenue_yoy_growth
                     FROM income_sheet
                     WHERE income_sheet.stock_code = core_performance_indicators_sheet.stock_code
                     AND income_sheet.report_period = core_performance_indicators_sheet.report_period
                 ))
        ''')
        update_count += self.cursor.rowcount
        
        self.cursor.execute('''
            UPDATE core_performance_indicators_sheet
            SET net_profit_yoy_growth = (
                SELECT net_profit_yoy_growth
                FROM income_sheet
                WHERE income_sheet.stock_code = core_performance_indicators_sheet.stock_code
                AND income_sheet.report_period = core_performance_indicators_sheet.report_period
            )
            WHERE EXISTS (
                SELECT 1
                FROM income_sheet
                WHERE income_sheet.stock_code = core_performance_indicators_sheet.stock_code
                AND income_sheet.report_period = core_performance_indicators_sheet.report_period
                AND income_sheet.net_profit_yoy_growth IS NOT NULL
            )
            AND (core_performance_indicators_sheet.net_profit_yoy_growth IS NULL
                 OR core_performance_indicators_sheet.net_profit_yoy_growth != (
                     SELECT net_profit_yoy_growth
                     FROM income_sheet
                     WHERE income_sheet.stock_code = core_performance_indicators_sheet.stock_code
                     AND income_sheet.report_period = core_performance_indicators_sheet.report_period
                 ))
        ''')
        update_count += self.cursor.rowcount
        
        # 从 core_performance_indicators_sheet 同步到 income_sheet
        self.cursor.execute('''
            UPDATE income_sheet
            SET operating_revenue_yoy_growth = (
                SELECT operating_revenue_yoy_growth
                FROM core_performance_indicators_sheet
                WHERE core_performance_indicators_sheet.stock_code = income_sheet.stock_code
                AND core_performance_indicators_sheet.report_period = income_sheet.report_period
            )
            WHERE EXISTS (
                SELECT 1
                FROM core_performance_indicators_sheet
                WHERE core_performance_indicators_sheet.stock_code = income_sheet.stock_code
                AND core_performance_indicators_sheet.report_period = income_sheet.report_period
                AND core_performance_indicators_sheet.operating_revenue_yoy_growth IS NOT NULL
            )
            AND (income_sheet.operating_revenue_yoy_growth IS NULL
                 OR income_sheet.operating_revenue_yoy_growth != (
                     SELECT operating_revenue_yoy_growth
                     FROM core_performance_indicators_sheet
                     WHERE core_performance_indicators_sheet.stock_code = income_sheet.stock_code
                     AND core_performance_indicators_sheet.report_period = income_sheet.report_period
                 ))
        ''')
        update_count += self.cursor.rowcount
        
        self.cursor.execute('''
            UPDATE income_sheet
            SET net_profit_yoy_growth = (
                SELECT net_profit_yoy_growth
                FROM core_performance_indicators_sheet
                WHERE core_performance_indicators_sheet.stock_code = income_sheet.stock_code
                AND core_performance_indicators_sheet.report_period = income_sheet.report_period
            )
            WHERE EXISTS (
                SELECT 1
                FROM core_performance_indicators_sheet
                WHERE core_performance_indicators_sheet.stock_code = income_sheet.stock_code
                AND core_performance_indicators_sheet.report_period = income_sheet.report_period
                AND core_performance_indicators_sheet.net_profit_yoy_growth IS NOT NULL
            )
            AND (income_sheet.net_profit_yoy_growth IS NULL
                 OR income_sheet.net_profit_yoy_growth != (
                     SELECT net_profit_yoy_growth
                     FROM core_performance_indicators_sheet
                     WHERE core_performance_indicators_sheet.stock_code = income_sheet.stock_code
                     AND core_performance_indicators_sheet.report_period = income_sheet.report_period
                 ))
        ''')
        update_count += self.cursor.rowcount
        
        self.conn.commit()
        print(f"  同比数据同步完成，共更新 {update_count} 条记录", flush=True)
        return update_count

# ── 从 config.json 读取配置 ──────────────────────────────────────────────
_EXTRACT_LLM  = CONFIG.get('extract_llm', CONFIG.get('llm', {}))
_LLM_API_KEY  = _EXTRACT_LLM.get('api_key', '')
_LLM_BASE_URL = _EXTRACT_LLM.get('base_url', '')
_LLM_MODEL    = _EXTRACT_LLM.get('model', '')
_MAX_WORKERS  = CONFIG['parallel']['max_workers']
_PRINT_LOCK   = threading.Lock()   # 保证多线程下控制台输出不乱序

_TABLES = ('core_indicators', 'income_sheet', 'balance_sheet', 'cash_flow')


def _extract_one_pdf(pdf_path, exchange, extractor, abbr_to_code):
    """供 ThreadPoolExecutor 并行调用（LLM I/O 期间释放 GIL，多线程有效）。
    返回 ((stock_code, report_period), results) 或 (None, None/错误字符串)。
    """
    try:
        results = extractor.process_single_report(pdf_path, exchange=exchange)
        if not results:
            return None, None
        # process_single_report 已从PDF文本 / config 映射中填充 stock_code
        key_data = (results.get('core_indicators')
                    or results.get('income_sheet') or {})
        stock_code = key_data.get('stock_code', '')
        report_period = key_data.get('report_period', '')
        if stock_code and report_period:
            return (stock_code, report_period), results
        missing = []
        if not stock_code:
            missing.append('stock_code')
        if not report_period:
            missing.append('report_period')
        with _PRINT_LOCK:
            print(f"  ✗ 缺少 {'+'.join(missing)}，跳过", flush=True)
        return None, None
    except Exception as e:
        with _PRINT_LOCK:
            print(f"  ✗ 处理失败: {e}", flush=True)
        return None, str(e)


def _count_non_null(data):
    """统计dict中非None值的字段数"""
    return sum(1 for k, v in (data or {}).items() if not str(k).startswith('_') and v is not None)


def _merge_report_results(results_list):
    """将同一报告期的多份提取结果在内存中合并，保留最完整的数据。

    策略：
    1. 对每张表，选取非None字段数最多的结果作为主数据。
    2. 用其余结果填补主数据中值为None的字段。
    3. 基础信息（stock_code/report_period/report_year/stock_abbr）取第一个有效值。
    """
    if len(results_list) == 1:
        return results_list[0]

    merged = {}
    for table in _TABLES:
        # 以字段最完整的那份结果为主
        best = max(results_list, key=lambda r: _count_non_null(r.get(table)))
        base = dict(best.get(table) or {})
        na_fields = set()
        for r in results_list:
            na_fields.update((r.get(table) or {}).get('_na_fields') or [])
        # 其余结果仅填补空缺（不覆盖已有值）
        for r in results_list:
            if r is best:
                continue
            for k, v in (r.get(table) or {}).items():
                if str(k).startswith('_') or k in na_fields:
                    continue
                if v is not None and base.get(k) is None:
                    base[k] = v
        if na_fields:
            for field in na_fields:
                base[field] = None
            base['_na_fields'] = sorted(na_fields)
        merged[table] = base

    merged_provenance = []
    seen_provenance = set()
    for result in results_list:
        for rec in result.get('_provenance', []) or []:
            dedupe_key = (
                rec.get('stock_code'),
                rec.get('report_period'),
                rec.get('table_name'),
                rec.get('field_name'),
                rec.get('field_value'),
                rec.get('source_pdf'),
                rec.get('page_no'),
                rec.get('row_label'),
                rec.get('extract_method'),
            )
            if dedupe_key in seen_provenance:
                continue
            seen_provenance.add(dedupe_key)
            merged_provenance.append(rec)
    if merged_provenance:
        merged['_provenance'] = merged_provenance

    return merged


def _safe_float(v):
    """安全地将数据库取出的值转为float，兼容带逗号的字符串。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(',', '').strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def post_convert_q3_to_single(db_path='financial_reports.db'):
    """将 Q3 报告期中仍为累计值的字段转换为单季度值（= Q3累计 - HY累计）。

    字段来源说明：
    ┌─ income_sheet Q3 ────────────────────────────────────────────────────┐
    │ 已是单季度（摘要表"本报告期"列，extract_income_sheet_data lines 639-642）：│
    │   total_operating_revenue, net_profit,                               │
    │   operating_revenue_yoy_growth, net_profit_yoy_growth                │
    │ 仍是累计（合并利润表正文）→ 需转换：                                       │
    │   other_income, operating_expense_*, total_operating_expenses,       │
    │   operating_profit, total_profit, asset_impairment_loss,             │
    │   credit_impairment_loss                                             │
    └──────────────────────────────────────────────────────────────────────┘
    ┌─ cash_flow_sheet Q3 ─────────────────────────────────────────────────┐
    │ 全部金额字段均来自合并现金流量表（累计）→ 全部需转换                           │
    └──────────────────────────────────────────────────────────────────────┘
    ┌─ core_performance_indicators_sheet Q3 ───────────────────────────────┐
    │ 全部来自摘要表"本报告期"列 → 已是单季度，无需处理                             │
    └──────────────────────────────────────────────────────────────────────┘
    """
    conn   = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # income_sheet：仅转换来自利润表正文的累计字段
    # 不转换：total_operating_revenue / net_profit / *_yoy_growth（已是单季度，来自摘要表）
    income_convert_fields = [
        'other_income',
        'operating_expense_cost_of_sales',
        'operating_expense_selling_expenses',
        'operating_expense_administrative_expenses',
        'operating_expense_financial_expenses',
        'operating_expense_rnd_expenses',
        'operating_expense_taxes_and_surcharges',
        'total_operating_expenses',
        'operating_profit',
        'total_profit',
        'asset_impairment_loss',
        'credit_impairment_loss',
    ]

    # cash_flow_sheet：全部金额字段均为累计，全部转换
    cf_convert_fields = [
        'net_cash_flow',
        'operating_cf_net_amount',
        'operating_cf_cash_from_sales',
        'investing_cf_net_amount',
        'investing_cf_cash_for_investments',
        'investing_cf_cash_from_investment_recovery',
        'financing_cf_cash_from_borrowing',
        'financing_cf_cash_for_debt_repayment',
        'financing_cf_net_amount',
    ]
    # 比率字段：原值基于累计口径计算，转换后需清空并从新金额重算
    cf_ratio_fields = [
        'net_cash_flow_yoy_growth',
        'operating_cf_ratio_of_net_cf',
        'investing_cf_ratio_of_net_cf',
        'financing_cf_ratio_of_net_cf',
    ]

    update_count = 0

    # ── income_sheet ─────────────────────────────────────────────────────
    cursor.execute(
        "SELECT serial_number, stock_code, report_year "
        "FROM income_sheet WHERE report_period LIKE '%Q3' AND report_year IS NOT NULL"
    )
    for sn, stock_code, report_year in cursor.fetchall():
        hy_period = f'{int(report_year)}HY'
        cursor.execute(
            f"SELECT {','.join(income_convert_fields)} "
            f"FROM income_sheet WHERE stock_code=? AND report_period=?",
            (stock_code, hy_period)
        )
        hy_row = cursor.fetchone()
        if not hy_row:
            continue
        hy = dict(zip(income_convert_fields, hy_row))

        cursor.execute(
            f"SELECT {','.join(income_convert_fields)} "
            f"FROM income_sheet WHERE serial_number=?",
            (sn,)
        )
        q3_row = cursor.fetchone()
        if not q3_row:
            continue
        q3 = dict(zip(income_convert_fields, q3_row))

        updates = {}
        for f in income_convert_fields:
            q3_val = _safe_float(q3.get(f))
            hy_val = _safe_float(hy.get(f))
            if q3_val is not None and hy_val is not None:
                updates[f] = round(q3_val - hy_val, 2)  # 计算结果为0也保留（真实值）
            else:
                updates[f] = None  # 缺少HY数据，无法换算，置NULL
        # 轻量范围校验：差得超过1e9万元视为量级异常（原始値未换算）
        for f, v in updates.items():
            if v is not None and abs(v) > 1e9:
                print(f"  [警告] Q3单季度换算: income_sheet.{f}={v:.0f}万元 超出量级阈値，已清除", flush=True)
                updates[f] = None

        set_clause = ', '.join(f'{f}=?' for f in updates)
        cursor.execute(
            f'UPDATE income_sheet SET {set_clause} WHERE serial_number=?',
            list(updates.values()) + [sn]
        )
        update_count += cursor.rowcount

    # ── cash_flow_sheet ──────────────────────────────────────────────────
    cursor.execute(
        "SELECT serial_number, stock_code, report_year "
        "FROM cash_flow_sheet WHERE report_period LIKE '%Q3' AND report_year IS NOT NULL"
    )
    for sn, stock_code, report_year in cursor.fetchall():
        hy_period = f'{int(report_year)}HY'
        cursor.execute(
            f"SELECT {','.join(cf_convert_fields)} "
            f"FROM cash_flow_sheet WHERE stock_code=? AND report_period=?",
            (stock_code, hy_period)
        )
        hy_row = cursor.fetchone()
        if not hy_row:
            continue
        hy = dict(zip(cf_convert_fields, hy_row))

        cursor.execute(
            f"SELECT {','.join(cf_convert_fields)} "
            f"FROM cash_flow_sheet WHERE serial_number=?",
            (sn,)
        )
        q3_row = cursor.fetchone()
        if not q3_row:
            continue
        q3 = dict(zip(cf_convert_fields, q3_row))

        updates = {}
        for f in cf_convert_fields:
            q3_val = _safe_float(q3.get(f))
            hy_val = _safe_float(hy.get(f))
            if q3_val is not None and hy_val is not None:
                updates[f] = round(q3_val - hy_val, 2)  # 计算结果为0也保留（真实值）
            else:
                updates[f] = None
        # 轻量范围校验：net_cash_flow单位为元，其余为万元；超限视为量级异常
        for f, v in updates.items():
            if v is None:
                continue
            if f == 'net_cash_flow' and abs(v) > 1e13:
                print(f"  [警告] Q3单季度换算: cash_flow.{f}={v:.0f}元 超出量级阈値，已清除", flush=True)
                updates[f] = None
            elif f != 'net_cash_flow' and abs(v) > 1e9:
                print(f"  [警告] Q3单季度换算: cash_flow.{f}={v:.0f}万元 超出量级阈値，已清除", flush=True)
                updates[f] = None
        # 清空基于累计口径计算的比率/增长率字段
        for f in cf_ratio_fields:
            updates[f] = None
        # 从新单季度金额重算三大活动占净现金流比例
        net_cf = updates.get('net_cash_flow')
        net_cf_f = float(net_cf) if net_cf is not None else None
        net_cf_10k = net_cf_f / 10000 if net_cf_f is not None else None
        if net_cf_10k is not None and net_cf_10k != 0:
            for cf_f, ratio_f in [
                ('operating_cf_net_amount',  'operating_cf_ratio_of_net_cf'),
                ('investing_cf_net_amount',  'investing_cf_ratio_of_net_cf'),
                ('financing_cf_net_amount',  'financing_cf_ratio_of_net_cf'),
            ]:
                cf_val = updates.get(cf_f)
                if cf_val is not None:
                    updates[ratio_f] = round(float(cf_val) / net_cf_10k * 100, 4)

        all_fields = cf_convert_fields + cf_ratio_fields
        set_clause = ', '.join(f'{f}=?' for f in all_fields)
        cursor.execute(
            f'UPDATE cash_flow_sheet SET {set_clause} WHERE serial_number=?',
            [updates.get(f) for f in all_fields] + [sn]
        )
        update_count += cursor.rowcount

    # ── 同步更新 field_provenance 中被转换字段的 field_value ──────────────
    # Q3 转为单季度后，provenance 的 field_value 仍是旧的累计值，
    # 需要从主表重新读取并回写，同时标记 extract_method='calculated'
    all_converted_fields = income_convert_fields + cf_convert_fields
    prov_sync_count = 0
    for f in all_converted_fields:
        # 确定字段所属的主表
        if f in income_convert_fields:
            src_table = 'income_sheet'
        else:
            src_table = 'cash_flow_sheet'
        # 找所有 Q3 的 provenance 记录并用主表的新值更新
        cursor.execute(
            f"UPDATE field_provenance SET "
            f"  field_value = (SELECT CAST({f} AS TEXT) FROM {src_table} "
            f"    WHERE {src_table}.stock_code = field_provenance.stock_code "
            f"    AND {src_table}.report_period = field_provenance.report_period LIMIT 1), "
            f"  extract_method = 'calculated' "
            f"WHERE field_name = ? AND report_period LIKE '%Q3'",
            (f,)
        )
        prov_sync_count += cursor.rowcount

    # ── 补算 Q3 单季度毛利率 ──────────────────────────────────────────────
    # 换算后 income_sheet.operating_expense_cost_of_sales 已为单季度，
    # core_performance_indicators_sheet.total_operating_revenue 也是单季度，
    # 两者口径一致，可计算毛利率 = (收入 - 成本) / 收入 × 100
    gpm_count = 0
    cursor.execute('''
        SELECT c.stock_code, c.report_period,
               CAST(c.total_operating_revenue AS REAL),
               CAST(i.operating_expense_cost_of_sales AS REAL)
        FROM core_performance_indicators_sheet c
        JOIN income_sheet i
          ON i.stock_code = c.stock_code AND i.report_period = c.report_period
        WHERE c.report_period LIKE '%Q3'
          AND c.gross_profit_margin IS NULL
          AND c.total_operating_revenue IS NOT NULL
          AND i.operating_expense_cost_of_sales IS NOT NULL
    ''')
    for sc, period, rev, cost in cursor.fetchall():
        if rev and rev != 0:
            gpm = round((rev - cost) / rev * 100, 4)
            if -100 <= gpm <= 100:
                cursor.execute(
                    'UPDATE core_performance_indicators_sheet '
                    'SET gross_profit_margin = ? '
                    'WHERE stock_code = ? AND report_period = ?',
                    (gpm, sc, period))
                gpm_count += cursor.rowcount

    conn.commit()
    conn.close()
    print(f"  Q3单季度换算完成，共更新 {update_count} 条记录，同步溯源 {prov_sync_count} 条，补算毛利率 {gpm_count} 条", flush=True)
    return update_count


def post_compute_qoq(db_path='financial_reports.db'):
    """全部数据入库后，跨报告计算季度环比增长率并回填 core_performance_indicators_sheet。

    逻辑：
    数据库中存储的是本期值（单季度或累计值），需要处理累计值以计算单季度值：
      Q1 单季度 = Q1 值
      Q2 单季度 = HY 值 - Q1 值
      Q3 单季度 = Q3 值
      Q1 QoQ = (Q1 单季度 - 上年Q4 单季度) / |上年Q4 单季度| × 100
      Q3 QoQ = (Q3 单季度 - Q2 单季度) / |Q2 单季度| × 100
      HY 环比 = (今年H1 - 去年H2) / |去年H2| × 100  （去年H2 = 去年FY - 去年HY）
      FY 环比 = 同比 = (今年FY - 去年FY) / |去年FY| × 100
    """
    from collections import defaultdict

    conn   = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 聚合income_sheet：同一(stock_code, year, period)可能有正文+摘要两条记录，取非NULL优先
    cursor.execute('''
        SELECT stock_code,
               CAST(report_year AS INTEGER)                AS yr,
               report_period,
               MAX(CAST(total_operating_revenue AS REAL))  AS revenue,
               MAX(CAST(net_profit AS REAL))               AS profit
        FROM income_sheet
        WHERE report_year IS NOT NULL AND report_period IS NOT NULL
        GROUP BY stock_code, yr, report_period
    ''')

    # annual[(stock_code, year)][suffix] = (revenue, profit)
    annual = defaultdict(dict)
    for sc, yr, period, rev, prf in cursor.fetchall():
        suffix = period[4:] if len(period) > 4 else ''
        if suffix in ('Q1', 'HY', 'Q3', 'FY'):
            annual[(sc, yr)][suffix] = (rev, prf)

    def _diff(a, b):
        """a - b; None if either operand is None."""
        return (a - b) if (a is not None and b is not None) else None

    def _qoq(curr, prev):
        if curr is not None and prev is not None:
            try:
                c = float(str(curr).replace(',', '')) if isinstance(curr, str) else float(curr)
                p = float(str(prev).replace(',', '')) if isinstance(prev, str) else float(prev)
            except (TypeError, ValueError):
                return None
            if p == 0:
                return None
            v = round((c - p) / abs(p) * 100, 4)
            return v if -16000 <= v <= 16000 else None
        return None

    def _r(d, i): return d[i] if d else None

    update_count = 0
    for (sc, yr), periods in annual.items():
        q1 = periods.get('Q1')
        hy = periods.get('HY')
        q3 = periods.get('Q3')
        fy = periods.get('FY')
        py = annual.get((sc, yr - 1), {})        # prior year

        # ── 当年各季度 本期值 ─────────────────────────────────────────
        q1_rev,  q1_prf  = _r(q1, 0), _r(q1, 1)
        hy_rev  = _r(hy, 0)
        hy_prf  = _r(hy, 1)
        q3_rev  = _r(q3, 0)
        q3_prf  = _r(q3, 1)
        fy_rev  = _r(fy, 0)
        fy_prf  = _r(fy, 1)

        # ── 计算单季度值 ───────────────────────────────────────────────
        q1_standalone_rev, q1_standalone_prf = q1_rev, q1_prf
        q2_standalone_rev = _diff(hy_rev, q1_rev)
        q2_standalone_prf = _diff(hy_prf, q1_prf)
        q3_standalone_rev, q3_standalone_prf = q3_rev, q3_prf

        # ── 上年数据（用于Q1/HY/FY 环比）─────────────────────────────
        py_fy = py.get('FY')
        py_q3 = py.get('Q3')
        py_hy = py.get('HY')
        # 上年Q4单季度（用于Q1环比）
        py_q4_rev = _diff(_diff(_r(py_fy, 0), _r(py_hy, 0)), _r(py_q3, 0)) if _r(py_fy, 0) is not None and _r(py_hy, 0) is not None and _r(py_q3, 0) is not None else None
        py_q4_prf = _diff(_diff(_r(py_fy, 1), _r(py_hy, 1)), _r(py_q3, 1)) if _r(py_fy, 1) is not None and _r(py_hy, 1) is not None and _r(py_q3, 1) is not None else None
        # 上年下半年H2 = 上年FY - 上年HY（用于HY环比）
        py_h2_rev = _diff(_r(py_fy, 0), _r(py_hy, 0))
        py_h2_prf = _diff(_r(py_fy, 1), _r(py_hy, 1))

        # ── QoQ / 环比 计算 ──────────────────────────────────────────────
        qoq_map = {
            f'{yr}Q1': (_qoq(q1_standalone_rev, py_q4_rev), _qoq(q1_standalone_prf, py_q4_prf)),
            f'{yr}HY': (_qoq(hy_rev, py_h2_rev), _qoq(hy_prf, py_h2_prf)),
            f'{yr}Q3': (_qoq(q3_standalone_rev, q2_standalone_rev), _qoq(q3_standalone_prf, q2_standalone_prf)),
            f'{yr}FY': (_qoq(fy_rev, _r(py_fy, 0)), _qoq(fy_prf, _r(py_fy, 1))),
        }

        for period, (rev_qoq, prf_qoq) in qoq_map.items():
            if rev_qoq is not None:
                cursor.execute(
                    'UPDATE core_performance_indicators_sheet '
                    'SET operating_revenue_qoq_growth = ? '
                    'WHERE stock_code = ? AND report_period = ? ',
                    (rev_qoq, sc, period))
                update_count += cursor.rowcount
            if prf_qoq is not None:
                cursor.execute(
                    'UPDATE core_performance_indicators_sheet '
                    'SET net_profit_qoq_growth = ? '
                    'WHERE stock_code = ? AND report_period = ? ',
                    (prf_qoq, sc, period))
                update_count += cursor.rowcount

    conn.commit()
    conn.close()
    print(f"  QoQ跨报告计算完成，共更新 {update_count} 条记录")


def post_compute_yoy(db_path='financial_reports.db'):
    """后处理：基于数据库中已有数据计算同比增长率（仅填充 NULL 字段）。

    计算公式：YoY = (当期值 - 上年同期值) / |上年同期值| × 100
    覆盖表和字段：
      - core_performance_indicators_sheet: operating_revenue_yoy_growth, net_profit_yoy_growth, net_profit_excl_non_recurring_yoy
      - income_sheet: operating_revenue_yoy_growth, net_profit_yoy_growth
      - balance_sheet: asset_total_assets_yoy_growth, liability_total_liabilities_yoy_growth
      - cash_flow_sheet: net_cash_flow_yoy_growth
    """
    conn   = sqlite3.connect(db_path)
    cursor = conn.cursor()
    update_count = 0
    not_applicable_locks = {
        (table_name, stock_code, report_period, field_name)
        for stock_code, report_period, table_name, field_name in cursor.execute(
            "SELECT stock_code, report_period, table_name, field_name "
            "FROM import_issues WHERE issue_type='not_applicable' AND field_name IS NOT NULL"
        ).fetchall()
    }

    def _yoy(curr, prev, field_name=None):
        if curr is not None and prev is not None:
            try:
                c = float(str(curr).replace(',', '')) if isinstance(curr, str) else float(curr)
                p = float(str(prev).replace(',', '')) if isinstance(prev, str) else float(prev)
            except (TypeError, ValueError):
                return None
            if p == 0:
                return None
            if field_name in ('net_profit_yoy_growth', 'net_profit_excl_non_recurring_yoy') and c * p < 0:
                return None
            v = round((c - p) / abs(p) * 100, 4)
            return v if -16000 <= v <= 16000 else None
        return None

    # (表名, [(同比字段, 基准值字段), ...])
    yoy_configs = [
        ('core_performance_indicators_sheet', [
            ('operating_revenue_yoy_growth', 'total_operating_revenue'),
            ('net_profit_yoy_growth',        'net_profit_10k_yuan'),
            ('net_profit_excl_non_recurring_yoy', 'net_profit_excl_non_recurring'),
        ]),
        ('income_sheet', [
            ('operating_revenue_yoy_growth', 'total_operating_revenue'),
            ('net_profit_yoy_growth',        'net_profit'),
        ]),
        ('balance_sheet', [
            ('asset_total_assets_yoy_growth',          'asset_total_assets'),
            ('liability_total_liabilities_yoy_growth', 'liability_total_liabilities'),
        ]),
        ('cash_flow_sheet', [
            ('net_cash_flow_yoy_growth', 'net_cash_flow'),
        ]),
    ]

    for table_name, field_pairs in yoy_configs:
        for yoy_field, _ in field_pairs:
            cursor.execute(
                f'''
                    UPDATE {table_name}
                    SET {yoy_field} = NULL
                    WHERE {yoy_field} IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM import_issues
                        WHERE import_issues.stock_code = {table_name}.stock_code
                        AND import_issues.report_period = {table_name}.report_period
                        AND import_issues.table_name = ?
                        AND import_issues.field_name = ?
                        AND import_issues.issue_type = 'not_applicable'
                    )
                ''',
                (table_name, yoy_field)
            )
            update_count += cursor.rowcount

        base_fields = list({bf for _, bf in field_pairs})
        yoy_fields  = [yf for yf, _ in field_pairs]
        select_cols = ['stock_code', 'report_period'] + base_fields + yoy_fields

        cursor.execute(
            f'SELECT {", ".join(select_cols)} FROM {table_name} '
            f'WHERE report_period IS NOT NULL')

        # (stock_code, report_period) -> {field: value}
        records = {}
        n_base = len(base_fields)
        for row in cursor.fetchall():
            sc, period = row[0], row[1]
            base_vals = {base_fields[i]: row[2 + i]           for i in range(n_base)}
            yoy_vals  = {yoy_fields[i]:  row[2 + n_base + i] for i in range(len(yoy_fields))}
            records[(sc, period)] = (base_vals, yoy_vals)

        for (sc, period), (base_vals, yoy_vals) in records.items():
            if len(period) < 5:
                continue
            try:
                prior_period = str(int(period[:4]) - 1) + period[4:]
            except (TypeError, ValueError):
                continue
            prior = records.get((sc, prior_period))
            if not prior:
                continue
            prior_base, _ = prior

            for yoy_field, base_field in field_pairs:
                if (table_name, sc, period, yoy_field) in not_applicable_locks:
                    continue
                if yoy_vals.get(yoy_field) is not None:
                    continue  # 已有值（正则提取），不覆盖
                yoy_val = _yoy(base_vals.get(base_field), prior_base.get(base_field), yoy_field)
                if yoy_val is not None:
                    cursor.execute(
                        f'UPDATE {table_name} SET {yoy_field} = ? '
                        f'WHERE stock_code = ? AND report_period = ? '
                        f'  AND {yoy_field} IS NULL',
                        (yoy_val, sc, period))
                    update_count += cursor.rowcount

    conn.commit()
    conn.close()
    print(f"  YoY同比计算完成，共更新 {update_count} 条记录")


def batch_import_reports(reports_dir, db_path='financial_reports.db'):
    """两阶段批量导入财报数据：
    阶段一：并行提取PDF数据，按(stock_code, report_period)分组（摘要与正文同组）。
    阶段二：对每组在内存中合并，保留最完整数据，再统一入库。
    """
    import json
    import uuid
    import time
    from datetime import datetime
    from collections import defaultdict

    job_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    job_started = datetime.now().isoformat()

    extractor = FinancialReportExtractor(
        llm_api_key=_LLM_API_KEY,
        llm_base_url=_LLM_BASE_URL,
        llm_model=_LLM_MODEL,
    )
    importer = DatabaseImporter(db_path)
    importer.connect()
    importer._job_id = job_id

    # 写入任务开始记录
    importer.cursor.execute(
        'INSERT INTO import_jobs (job_id, started_at) VALUES (?, ?)',
        (job_id, job_started))
    importer.commit()
    print(f"[任务ID] {job_id}")

    t_total_start = time.time()
    phase_times = {}  # 阶段名 -> 耗时(秒)
    total_files = 0
    failed_files = 0
    # key: (stock_code, report_period) -> list of results
    groups = defaultdict(list)

    # ── 阶段一：并行提取并分组 ──────────────────────────────────────────────
    t1 = time.time()
    for exchange, subdir_name, label in [
        ('sse',  'reports-上交所', '上交所'),
        ('szse', 'reports-深交所', '深交所'),
    ]:
        subdir = os.path.join(reports_dir, subdir_name)
        if not os.path.exists(subdir):
            continue
        _skip_keywords = ('摘要', '英文', 'abstract', 'summary', '更新前', '更正前')
        _all_files = [f for f in os.listdir(subdir) if f.endswith('.pdf')]
        # 找出所有已有"更正后/更新后"版本的基础名，用于排除对应的无标记旧版
        _corrected_bases = set()
        for _f in _all_files:
            if '更正后' in _f or '更新后' in _f:
                _base = _f.replace('（更正后）', '').replace('（更新后）', '').replace(' ', '').replace('.pdf', '')
                _corrected_bases.add(_base)
        def _should_keep(fname):
            if any(kw in fname for kw in _skip_keywords):
                return False
            if '更正后' in fname or '更新后' in fname:
                return True
            _base = fname.replace(' ', '').replace('.pdf', '')
            if _base in _corrected_bases:
                return False  # 无标记旧版，已有更正后版本，跳过
            return True
        pdfs = sorted(f for f in _all_files if _should_keep(f))
        if not pdfs:
            continue
        total_files += len(pdfs)
        print("\n" + "="*80)
        print(f"[阶段一] 并行提取{label}财报（{len(pdfs)} 个文件，{_MAX_WORKERS} 线程）...")
        print("="*80)

        _abbr_to_code = CONFIG.get('stock_abbr_to_code', {})
        futures = {}
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
            for filename in pdfs:
                pdf_path = os.path.join(subdir, filename)
                fut = executor.submit(
                    _extract_one_pdf, pdf_path, exchange,
                    extractor, _abbr_to_code
                )
                futures[fut] = filename

            for fut in as_completed(futures):
                fname = futures[fut]
                try:
                    key, results = fut.result()
                    if key and results:
                        groups[key].append(results)
                    else:
                        failed_files += 1
                except Exception as e:
                    with _PRINT_LOCK:
                        print(f"  ✗ {fname} 意外异常: {e}", flush=True)
                    failed_files += 1

    phase_times['阶段一 并行提取'] = time.time() - t1

    # ── 阶段二：内存合并 + 统一入库 ──────────────────────────────────────────
    t2 = time.time()
    print("\n" + "="*80)
    print("[阶段二] 合并同报告期数据并入库...")
    print("="*80)
    success_groups = 0
    for (stock_code, report_period), results_list in sorted(groups.items()):
        n = len(results_list)
        if n > 1:
            print(f"  [合并] {stock_code} {report_period}: {n} 份文件 → 合并后入库",
                  flush=True)
        merged = _merge_report_results(results_list)
        count = importer.import_report_data(merged)
        if count > 0:
            success_groups += 1
            importer.commit()
        else:
            print(f"  ✗ {stock_code} {report_period} 数据导入失败")

    importer.commit()
    importer.close()
    phase_times['阶段二 合并入库'] = time.time() - t2

    # ── 阶段三：Q3累计→单季度换算 ─────────────────────────────────────────
    print("\n" + "="*80)
    print("[阶段三] Q3报告期数据累计值→单季度值换算（income_sheet / cash_flow_sheet）...")
    print("="*80)
    t3 = time.time()
    post_convert_q3_to_single(db_path)
    phase_times['阶段三 Q3换算'] = time.time() - t3

    # ── 阶段四：Python精确计算同比增长率 ──────────────────────────────────────
    print("\n" + "="*80)
    print("[阶段四] Python精确计算同比增长率（YoY）...")
    print("="*80)
    t4 = time.time()
    post_compute_yoy(db_path)
    phase_times['阶段四 YoY计算'] = time.time() - t4

    # ── 阶段五：跨报告计算季度环比增长率 ──────────────────────────────────────
    print("\n" + "="*80)
    print("[阶段五] 跨报告计算季度环比增长率（QoQ）...")
    print("="*80)
    t5 = time.time()
    post_compute_qoq(db_path)
    phase_times['阶段五 QoQ计算'] = time.time() - t5

    # ── 阶段六：同步同比数据 ──────────────────────────────────────────────
    print("\n" + "="*80)
    print("[阶段六] 同步同比数据...")
    print("="*80)
    t6 = time.time()
    importer.connect()
    importer._job_id = job_id
    importer.sync_yoy_data()
    phase_times['阶段六 同比同步'] = time.time() - t6

    # ── 阶段七：统一精度标准化（覆盖历史数据 + 同步后数据） ─────────────────────
    print("\n" + "="*80)
    print("[阶段七] 统一精度标准化...")
    print("="*80)
    t7 = time.time()
    importer.normalize_precision()
    phase_times['阶段七 精度标准化'] = time.time() - t7

    # ── 写入任务统计 ──────────────────────────────────────────────────────
    importer.cursor.execute(
        'SELECT COUNT(*) FROM import_issues WHERE job_id=? AND issue_level=?',
        (job_id, 'warning'))
    warning_count = importer.cursor.fetchone()[0]
    importer.cursor.execute(
        'SELECT COUNT(*) FROM import_issues WHERE job_id=? AND issue_level=?',
        (job_id, 'error'))
    error_count = importer.cursor.fetchone()[0]
    importer.cursor.execute(
        'SELECT COUNT(*) FROM field_provenance WHERE stock_code != ""')
    prov_total = importer.cursor.fetchone()[0]

    summary = json.dumps({
        'total_files': total_files,
        'report_groups': len(groups),
        'success_groups': success_groups,
        'failed_files': failed_files,
        'provenance_records': prov_total,
    }, ensure_ascii=False)

    from datetime import datetime as _dt
    importer.cursor.execute(
        'UPDATE import_jobs SET finished_at=?, total_reports=?, success_reports=?, '
        'warning_count=?, error_count=?, summary_json=? WHERE job_id=?',
        (_dt.now().isoformat(), total_files, success_groups,
         warning_count, error_count, summary, job_id))
    importer.commit()
    importer.close()

    t_total = time.time() - t_total_start

    print("\n" + "="*80)
    print(f"批量导入完成!  [任务ID: {job_id}]")
    print(f"总文件数:         {total_files}")
    print(f"报告期分组数:     {len(groups)}")
    print(f"成功入库分组数:   {success_groups}")
    print(f"处理失败文件数:   {failed_files}")
    print(f"溯源记录总数:     {prov_total}")
    print(f"校验警告数:       {warning_count}")
    print(f"校验错误数:       {error_count}")
    print("-"*80)
    summary_phase_order = (
        '阶段一 并行提取',
        '阶段二 合并入库',
        '阶段三 Q3换算',
        '阶段四 YoY计算',
        '阶段五 QoQ计算',
        '阶段六 同比同步',
        '阶段七 精度标准化',
    )
    for phase_name in summary_phase_order:
        elapsed = phase_times.get(phase_name)
        if elapsed is not None:
            print(f"  {phase_name}: {elapsed:.2f}s")
    print(f"  总耗时: {t_total:.2f}s")
    print("="*80)

if __name__ == '__main__':
    reports_dir = CONFIG['reports_dir']
    db_path     = CONFIG['database']['path']
    batch_import_reports(reports_dir, db_path)
