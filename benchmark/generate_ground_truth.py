"""
独立生成 ground_truth.json —— 直接查数据库获取标准答案，不走本系统管道。

用法:
  python -m benchmark.generate_ground_truth          # 生成/覆盖 ground_truth.json
  python -m benchmark.generate_ground_truth --force   # 强制重新生成

生成后可人工审核 key_numbers，将 verified 改为 true，再运行 benchmark 评分。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parent
GT_PATH = _THIS_DIR / 'ground_truth.json'
DB_PATH = _ROOT / 'financial_reports.db'


def load_questions() -> list[dict]:
    with open(_THIS_DIR / 'questions.json', 'r', encoding='utf-8') as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════════
#  针对每道题的 SQL 查询逻辑（直接查数据库，不经过 LLM）
# ═══════════════════════════════════════════════════════════════════════════════

def _query_db(sql: str) -> list[dict]:
    """执行 SQL 查询，返回行字典列表。"""
    if not DB_PATH.exists():
        print(f'  ⚠ 数据库不存在: {DB_PATH}')
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'  ⚠ SQL 执行失败: {e}\n    SQL: {sql}')
        return []
    finally:
        conn.close()


def _extract_numbers_from_rows(rows: list[dict]) -> list[float]:
    """从查询结果行中提取所有非零数值（处理字符串格式的数字）。"""
    nums = []
    skip_cols = {'serial_number', 'stock_code', 'stock_abbr', 'report_period', 'report_year'}
    for row in rows[:20]:
        for k, v in row.items():
            if k in skip_cols:
                continue
            try:
                fv = float(str(v).replace(',', ''))
                if fv != 0:
                    nums.append(fv)
            except (ValueError, TypeError):
                continue
    return nums


# 每道题对应的直接 SQL 查询
# 数据库表: balance_sheet, income_sheet, core_performance_indicators_sheet, cash_flow_sheet
# report_period 格式: 2022FY, 2023Q1, 2023HY, 2023Q3, 2024FY, ...
QUESTION_QUERIES: dict[str, str] = {
    # 片仔癀 2022 全年资产总额、负债总额、股东权益总额（万元）
    'B1030': """
        SELECT asset_total_assets, liability_total_liabilities, equity_total_equity
        FROM balance_sheet
        WHERE stock_code = '600436' AND report_period = '2022FY'
        LIMIT 1
    """,
    # 香雪制药 2024 年研发费用
    'B1008': """
        SELECT operating_expense_rnd_expenses
        FROM income_sheet
        WHERE stock_code = '300147' AND report_period = '2024FY'
        LIMIT 1
    """,
    # 白云山、云南白药、片仔癀 2025Q3 销售毛利率、销售净利率、ROE
    'B1051': """
        SELECT stock_abbr, gross_profit_margin, net_profit_margin, roe
        FROM core_performance_indicators_sheet
        WHERE stock_code IN ('600332','000538','600436')
          AND report_period = '2025Q3'
    """,
    # 白云山、云南白药、片仔癀 2022-2025Q3 净利润
    'B1028': """
        SELECT stock_abbr, report_period, net_profit
        FROM income_sheet
        WHERE stock_code IN ('600332','000538','600436')
          AND report_period IN ('2022Q3','2023Q3','2024Q3','2025Q3')
        ORDER BY stock_code, report_period
    """,
    # 云南白药 2025Q2/Q3 营业总收入及环比增长率
    'B1034': """
        SELECT report_period, total_operating_revenue
        FROM income_sheet
        WHERE stock_code = '000538'
          AND report_period IN ('2025HY','2025Q3')
        ORDER BY report_period
    """,
    # 白云山 2022-2025Q3 营业总收入同比增长率
    'B1010': """
        SELECT report_period, total_operating_revenue, operating_revenue_yoy_growth
        FROM income_sheet
        WHERE stock_code = '600332'
          AND report_period IN ('2022Q3','2023Q3','2024Q3','2025Q3')
        ORDER BY report_period
    """,
    # 云南白药 2022-2025Q3 资产负债率
    'B1045': """
        SELECT report_period, asset_liability_ratio
        FROM balance_sheet
        WHERE stock_code = '000538'
          AND report_period IN ('2022Q3','2023Q3','2024Q3','2025Q3')
        ORDER BY report_period
    """,
    # 香雪制药 2024Q3/2025Q3 收入、成本、费用、净利润
    'B2060': """
        SELECT report_period,
               total_operating_revenue, operating_expense_cost_of_sales,
               operating_expense_selling_expenses, operating_expense_administrative_expenses,
               operating_expense_financial_expenses, net_profit
        FROM income_sheet
        WHERE stock_code = '300147'
          AND report_period IN ('2024Q3','2025Q3')
        ORDER BY report_period
    """,
    # 东阿阿胶 2022-2025Q3 营业总收入
    'B2067': """
        SELECT report_period, total_operating_revenue
        FROM income_sheet
        WHERE stock_code = '000423'
          AND report_period IN ('2022Q3','2023Q3','2024Q3','2025Q3')
        ORDER BY report_period
    """,
    # 三九、白云山、云南白药、片仔癀 2022-2025Q3 营业总收入
    'B2059': """
        SELECT stock_abbr, report_period, total_operating_revenue
        FROM income_sheet
        WHERE stock_code IN ('000999','600332','000538','600436')
          AND report_period IN ('2022Q3','2023Q3','2024Q3','2025Q3')
        ORDER BY stock_code, report_period
    """,
}


def generate_ground_truth(force: bool = False) -> str:
    """为每道题直接查数据库，生成 ground_truth.json。"""
    if GT_PATH.exists() and not force:
        print(f'  ⚠ {GT_PATH} 已存在。使用 --force 覆盖，或直接编辑它。')
        return str(GT_PATH)

    questions = load_questions()

    # 先列出数据库中有哪些表
    tables = []
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        print(f'  📦 数据库表: {tables}')

    payload = {
        '_comment': '标准答案文件。key_numbers 为评分基准，可人工修改。verified=true 表示已审核。',
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'database': str(DB_PATH),
        'questions': {},
    }

    for q in questions:
        qid = q['id']
        sql = QUESTION_QUERIES.get(qid, '')
        rows = []
        key_numbers = []

        if sql:
            # 尝试查询，如果表名不对就自动适配
            rows = _query_db(sql.strip())
            key_numbers = _extract_numbers_from_rows(rows)

        # 将查询行中的数值字符串转为 float，方便审核
        display_rows = []
        for row in rows[:5]:
            dr = {}
            for k, v in row.items():
                try:
                    dr[k] = float(str(v).replace(',', ''))
                except (ValueError, TypeError):
                    dr[k] = v
            display_rows.append(dr)

        entry = {
            'question_text': q['question'],
            'key_entities': q.get('key_entities', []),
            'sql_used': sql.strip() if sql else '',
            'query_rows': display_rows,
            'key_numbers': key_numbers[:20],
            'verified': False,
        }
        payload['questions'][qid] = entry

        status = f'{len(key_numbers)} 个数值' if key_numbers else '⚠ 无数据'
        print(f'  {qid}: {status}')

    with open(GT_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f'\n💾 已保存: {GT_PATH}')
    print(f'📝 请人工审核 key_numbers，确认无误后将 verified 改为 true')
    return str(GT_PATH)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='生成 ground_truth.json')
    parser.add_argument('--force', action='store_true', help='强制覆盖已有文件')
    args = parser.parse_args()
    generate_ground_truth(force=args.force)
