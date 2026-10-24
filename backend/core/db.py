from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from . import context as _ctx
from .context import FIELD_LABELS

_NUMERIC_TEXT_FIELDS: frozenset[str] = frozenset({
    'eps', 'total_operating_revenue', 'operating_revenue_yoy_growth',
    'operating_revenue_qoq_growth', 'net_profit_10k_yuan', 'net_profit_yoy_growth',
    'net_profit_qoq_growth', 'net_asset_per_share', 'roe', 'operating_cf_per_share',
    'net_profit_excl_non_recurring', 'net_profit_excl_non_recurring_yoy',
    'gross_profit_margin', 'net_profit_margin', 'roe_weighted_excl_non_recurring',
    'asset_cash_and_cash_equivalents', 'asset_accounts_receivable', 'asset_inventory',
    'asset_trading_financial_assets', 'asset_construction_in_progress', 'asset_total_assets',
    'asset_total_assets_yoy_growth', 'liability_accounts_payable',
    'liability_advance_from_customers', 'liability_total_liabilities',
    'liability_total_liabilities_yoy_growth', 'liability_contract_liabilities',
    'liability_short_term_loans', 'asset_liability_ratio',
    'equity_unappropriated_profit', 'equity_total_equity',
    'net_cash_flow', 'net_cash_flow_yoy_growth', 'operating_cf_net_amount',
    'operating_cf_ratio_of_net_cf', 'operating_cf_cash_from_sales',
    'investing_cf_net_amount', 'investing_cf_ratio_of_net_cf',
    'investing_cf_cash_for_investments', 'investing_cf_cash_from_investment_recovery',
    'financing_cf_cash_from_borrowing', 'financing_cf_cash_for_debt_repayment',
    'financing_cf_net_amount', 'financing_cf_ratio_of_net_cf',
    'net_profit', 'other_income', 'operating_expense_cost_of_sales',
    'operating_expense_selling_expenses', 'operating_expense_administrative_expenses',
    'operating_expense_financial_expenses', 'operating_expense_rnd_expenses',
    'operating_expense_taxes_and_surcharges', 'total_operating_expenses',
    'operating_profit', 'total_profit', 'asset_impairment_loss', 'credit_impairment_loss',
})

_SQL_KW = frozenset({
    'ASC', 'DESC', 'AS', 'BY', 'NULL', 'IS', 'NOT', 'AND', 'OR', 'IN',
    'LIKE', 'BETWEEN', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
    'REAL', 'INTEGER', 'TEXT', 'BLOB', 'NUMERIC', 'CAST',
})

_RE_OB_CLAUSE = re.compile(
    r'\bORDER\s+BY\s+([\w\s,.()\'"]+?)(?=\s+(?:LIMIT|HAVING|UNION|EXCEPT|INTERSECT)\b|\s*;|\s*$)',
    re.IGNORECASE | re.DOTALL,
)
_RE_OB_ITEM = re.compile(r'(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)(\s+(?:ASC|DESC)\b)?', re.IGNORECASE)
_RE_AGG = re.compile(r'\b(MAX|MIN)\s*\(\s*([A-Za-z_]\w*)\s*\)', re.IGNORECASE)
_RE_CMP = re.compile(r'\b([A-Za-z_]\w*)\s*([<>]=?|<>)\s*(-?\d+(?:\.\d+)?)\b', re.IGNORECASE)


def rewrite_numeric_sql(sql: str) -> str:
    """对 TEXT 存储的数值列在 ORDER BY/MAX/MIN/数值比较 中自动加 CAST(x AS REAL)。"""

    def _fix_ob(m: re.Match) -> str:
        # Skip if already wrapped in CAST()
        if 'CAST(' in m.group(0).upper():
            return m.group(0)
        def _fix_item(fm: re.Match) -> str:
            full_match = fm.group(0)
            field, direction = fm.group(1), (fm.group(2) or '')
            if field.upper() not in _SQL_KW and field in _NUMERIC_TEXT_FIELDS:
                # Preserve table prefix if present (e.g. t1.field)
                prefix = full_match[:full_match.rfind(field)].rstrip('.')
                if prefix and prefix[-1:].isalnum():
                    return f'CAST({prefix}.{field} AS REAL){direction}'
                return f'CAST({field} AS REAL){direction}'
            return full_match
        new_list = ', '.join(
            _RE_OB_ITEM.sub(_fix_item, item.strip())
            for item in m.group(1).split(',')
        )
        return f'ORDER BY {new_list}'

    sql = _RE_OB_CLAUSE.sub(_fix_ob, sql)

    def _fix_agg(m: re.Match) -> str:
        func, field = m.group(1), m.group(2)
        if field in _NUMERIC_TEXT_FIELDS:
            return f'{func}(CAST({field} AS REAL))'
        return m.group(0)

    sql = _RE_AGG.sub(_fix_agg, sql)

    def _fix_cmp(m: re.Match) -> str:
        field, op, val = m.group(1), m.group(2), m.group(3)
        if field in _NUMERIC_TEXT_FIELDS:
            return f'CAST({field} AS REAL) {op} {val}'
        return m.group(0)

    sql = _RE_CMP.sub(_fix_cmp, sql)
    return sql


def now_text() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M')


def create_id() -> str:
    return f"{int(time.time() * 1000)}"


def open_connection() -> sqlite3.Connection:
    if not _ctx.DB_PATH.exists():
        raise HTTPException(status_code=500, detail=f'数据库文件不存在: {_ctx.DB_PATH}')
    connection = sqlite3.connect(_ctx.DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def serialize_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: serialize_value(row[key]) for key in row.keys()}


def column_label(name: str) -> str:
    return FIELD_LABELS.get(name, name)


def build_columns_from_row(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not rows:
        return []
    return [{'key': key, 'label': column_label(key)} for key in rows[0].keys()]


def validate_read_only_sql(sql: str) -> str:
    text = sql.strip()
    lowered = text.lower()
    if not text:
        raise HTTPException(status_code=400, detail='SQL 不能为空')
    if ';' in text.rstrip(';'):
        raise HTTPException(status_code=400, detail='仅允许执行单条只读 SQL')
    if not re.match(r'^(select|with|pragma)\b', lowered):
        raise HTTPException(status_code=400, detail='仅允许执行 SELECT / WITH / PRAGMA 只读 SQL')
    for token in ['insert ', 'update ', 'delete ', 'drop ', 'alter ', 'create ', 'replace ', 'truncate ', 'attach ', 'detach ']:
        if token in lowered:
            raise HTTPException(status_code=400, detail='检测到非只读 SQL，已拒绝执行')
    return text.rstrip(';')


def query_rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    sql = rewrite_numeric_sql(sql)
    with open_connection() as connection:
        rows = connection.execute(sql, params).fetchall()
    return [row_to_dict(row) for row in rows]
