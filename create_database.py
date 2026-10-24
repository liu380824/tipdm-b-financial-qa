# -*- coding: utf-8 -*-
import sqlite3
import os
from config import CONFIG

def create_financial_database(db_path='financial_reports.db'):
    """
    创建财务报告数据库，严格按照附件3定义的字段创建四个核心表：
    1. 核心业绩指标表 (core_performance_indicators_sheet)
    2. 资产负债表 (balance_sheet)
    3. 现金流量表 (cash_flow_sheet)
    4. 利润表 (income_sheet)
    """

    if os.path.exists(db_path):
        print(f"数据库文件 {db_path} 已存在，将被覆盖...")
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # ------------------------------------------------------------------ #
    # 1. 核心业绩指标表  core_performance_indicators_sheet
    # ------------------------------------------------------------------ #
    print("正在创建核心业绩指标表...")
    cursor.execute('''
    CREATE TABLE core_performance_indicators_sheet (
        serial_number                       INTEGER PRIMARY KEY,
        stock_code                          VARCHAR(20)    NOT NULL,
        stock_abbr                          VARCHAR(50),
        eps                                 TEXT,
        total_operating_revenue             TEXT,
        operating_revenue_yoy_growth        TEXT,
        operating_revenue_qoq_growth        TEXT,
        net_profit_10k_yuan                 TEXT,
        net_profit_yoy_growth               TEXT,
        net_profit_qoq_growth               TEXT,
        net_asset_per_share                 TEXT,
        roe                                 TEXT,
        operating_cf_per_share              TEXT,
        net_profit_excl_non_recurring       TEXT,
        net_profit_excl_non_recurring_yoy   TEXT,
        gross_profit_margin                 TEXT,
        net_profit_margin                   TEXT,
        roe_weighted_excl_non_recurring     TEXT,
        report_period                       VARCHAR(20),
        report_year                         INT
    )
    ''')

    # ------------------------------------------------------------------ #
    # 2. 资产负债表  balance_sheet
    # ------------------------------------------------------------------ #
    print("正在创建资产负债表...")
    cursor.execute('''
    CREATE TABLE balance_sheet (
        serial_number                           INTEGER PRIMARY KEY,
        stock_code                              VARCHAR(20)    NOT NULL,
        stock_abbr                              VARCHAR(50),
        asset_cash_and_cash_equivalents         TEXT,
        asset_accounts_receivable               TEXT,
        asset_inventory                         TEXT,
        asset_trading_financial_assets          TEXT,
        asset_construction_in_progress          TEXT,
        asset_total_assets                      TEXT,
        asset_total_assets_yoy_growth           TEXT,
        liability_accounts_payable              TEXT,
        liability_advance_from_customers        TEXT,
        liability_total_liabilities             TEXT,
        liability_total_liabilities_yoy_growth  TEXT,
        liability_contract_liabilities          TEXT,
        liability_short_term_loans              TEXT,
        asset_liability_ratio                   TEXT,
        equity_unappropriated_profit            TEXT,
        equity_total_equity                     TEXT,
        report_period                           VARCHAR(20),
        report_year                             INT
    )
    ''')

    # ------------------------------------------------------------------ #
    # 3. 现金流量表  cash_flow_sheet
    # ------------------------------------------------------------------ #
    print("正在创建现金流量表...")
    cursor.execute('''
    CREATE TABLE cash_flow_sheet (
        serial_number                               INTEGER PRIMARY KEY,
        stock_code                                  VARCHAR(20)    NOT NULL,
        stock_abbr                                  VARCHAR(50),
        net_cash_flow                               TEXT,
        net_cash_flow_yoy_growth                    TEXT,
        operating_cf_net_amount                     TEXT,
        operating_cf_ratio_of_net_cf                TEXT,
        operating_cf_cash_from_sales                TEXT,
        investing_cf_net_amount                     TEXT,
        investing_cf_ratio_of_net_cf                TEXT,
        investing_cf_cash_for_investments           TEXT,
        investing_cf_cash_from_investment_recovery  TEXT,
        financing_cf_cash_from_borrowing            TEXT,
        financing_cf_cash_for_debt_repayment        TEXT,
        financing_cf_net_amount                     TEXT,
        financing_cf_ratio_of_net_cf                TEXT,
        report_period                               VARCHAR(20),
        report_year                                 INT
    )
    ''')

    # ------------------------------------------------------------------ #
    # 4. 利润表  income_sheet
    # ------------------------------------------------------------------ #
    print("正在创建利润表...")
    cursor.execute('''
    CREATE TABLE income_sheet (
        serial_number                               INTEGER PRIMARY KEY,
        stock_code                                  VARCHAR(20)    NOT NULL,
        stock_abbr                                  VARCHAR(50),
        net_profit                                  TEXT,
        net_profit_yoy_growth                       TEXT,
        other_income                                TEXT,
        total_operating_revenue                     TEXT,
        operating_revenue_yoy_growth                TEXT,
        operating_expense_cost_of_sales             TEXT,
        operating_expense_selling_expenses          TEXT,
        operating_expense_administrative_expenses   TEXT,
        operating_expense_financial_expenses        TEXT,
        operating_expense_rnd_expenses              TEXT,
        operating_expense_taxes_and_surcharges      TEXT,
        total_operating_expenses                    TEXT,
        operating_profit                            TEXT,
        total_profit                                TEXT,
        asset_impairment_loss                       TEXT,
        credit_impairment_loss                      TEXT,
        report_period                               VARCHAR(20),
        report_year                                 INT
    )
    ''')

    # ------------------------------------------------------------------ #
    # 5. 字段级溯源表  field_provenance
    #    记录每一个被写入字段的来源（提取时即刻记录，不做二次搜索）
    # ------------------------------------------------------------------ #
    print("正在创建字段溯源表...")
    cursor.execute('''
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

    # ------------------------------------------------------------------ #
    # 6. 导入异常日志表  import_issues
    #    记录导入过程中的异常、修复和警告
    # ------------------------------------------------------------------ #
    print("正在创建导入异常日志表...")
    cursor.execute('''
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

    # ------------------------------------------------------------------ #
    # 7. 导入任务统计表  import_jobs
    #    记录每一次批量导入任务的统计摘要
    # ------------------------------------------------------------------ #
    print("正在创建导入任务统计表...")
    cursor.execute('''
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

    # ------------------------------------------------------------------ #
    # 索引
    # ------------------------------------------------------------------ #
    print("正在创建索引...")
    for tbl, prefix in [
        ('core_performance_indicators_sheet', 'core'),
        ('balance_sheet',                     'balance'),
        ('cash_flow_sheet',                   'cash'),
        ('income_sheet',                      'income'),
    ]:
        cursor.execute(f'CREATE INDEX idx_{prefix}_stock_code    ON {tbl}(stock_code)')
        cursor.execute(f'CREATE INDEX idx_{prefix}_stock_abbr    ON {tbl}(stock_abbr)')
        cursor.execute(f'CREATE INDEX idx_{prefix}_report_period ON {tbl}(report_period)')

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_fp_lookup ON field_provenance(stock_code, report_period, table_name)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_fp_field  ON field_provenance(stock_code, report_period, field_name)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_issue_job  ON import_issues(job_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_issue_stock ON import_issues(stock_code, report_period)')

    conn.commit()

    # ------------------------------------------------------------------ #
    # 验证
    # ------------------------------------------------------------------ #
    print("\n数据库创建成功！")
    print(f"数据库文件路径: {os.path.abspath(db_path)}")
    print("\n已创建的表及字段数:")
    for tbl in ['core_performance_indicators_sheet', 'balance_sheet',
                'cash_flow_sheet', 'income_sheet',
                'field_provenance', 'import_issues', 'import_jobs']:
        cursor.execute(f"PRAGMA table_info({tbl})")
        cols = cursor.fetchall()
        print(f"  {tbl}: {len(cols)} 个字段")

    conn.close()
    return db_path


if __name__ == '__main__':
    db_path = create_financial_database(CONFIG['database']['path'])
    print(f"\n✓ 数据库已成功创建: {db_path}".encode('utf-8').decode('gbk', 'ignore'))
