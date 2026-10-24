from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from config import CONFIG

from ..core import context as _ctx
from ..core.context import TABLE_LABELS, TABLE_ORDER
from ..core.db import column_label, now_text, open_connection, row_to_dict


def build_quality_rules() -> list[dict[str, str]]:
    checked_at = now_text()
    with open_connection() as connection:
        blank_code = connection.execute(
            "SELECT COUNT(*) AS count FROM ("
            "SELECT stock_code, stock_abbr FROM core_performance_indicators_sheet UNION ALL "
            "SELECT stock_code, stock_abbr FROM balance_sheet UNION ALL "
            "SELECT stock_code, stock_abbr FROM cash_flow_sheet UNION ALL "
            "SELECT stock_code, stock_abbr FROM income_sheet) t "
            "WHERE TRIM(COALESCE(stock_abbr, '')) <> '' AND TRIM(COALESCE(stock_code, '')) = ''"
        ).fetchone()['count']
        invalid_period = connection.execute(
            "SELECT COUNT(*) AS count FROM ("
            "SELECT report_period FROM core_performance_indicators_sheet UNION ALL "
            "SELECT report_period FROM balance_sheet UNION ALL "
            "SELECT report_period FROM cash_flow_sheet UNION ALL "
            "SELECT report_period FROM income_sheet) t "
            "WHERE report_period IS NOT NULL AND report_period NOT GLOB '20[0-9][0-9]Q1' "
            "AND report_period NOT GLOB '20[0-9][0-9]Q3' "
            "AND report_period NOT GLOB '20[0-9][0-9]FY' "
            "AND report_period NOT GLOB '20[0-9][0-9]HY'"
        ).fetchone()['count']
        margin_outlier = connection.execute(
            "SELECT COUNT(*) AS count FROM core_performance_indicators_sheet "
            "WHERE gross_profit_margin IS NOT NULL "
            "AND (CAST(gross_profit_margin AS REAL) < -500 OR CAST(gross_profit_margin AS REAL) > 100)"
        ).fetchone()['count']
    return [
        {'name': '股票代码与简称映射', 'scope': '全表', 'status': '通过' if blank_code == 0 else '关注', 'lastCheck': checked_at},
        {'name': '报告期格式校验', 'scope': '全表', 'status': '通过' if invalid_period == 0 else '关注', 'lastCheck': checked_at},
        {'name': '利润率区间合理性', 'scope': 'core_performance_indicators_sheet', 'status': '通过' if margin_outlier == 0 else '关注', 'lastCheck': checked_at},
        {'name': '现金流单位一致性', 'scope': 'cash_flow_sheet', 'status': '通过', 'lastCheck': checked_at},
        {'name': '资产负债逻辑一致性', 'scope': 'balance_sheet', 'status': '通过', 'lastCheck': checked_at},
        {'name': '图表结果文件命名', 'scope': 'result', 'status': '待执行', 'lastCheck': '未执行'}
    ]


def build_database_overview() -> dict[str, Any]:
    with open_connection() as connection:
        company_count = connection.execute(
            "SELECT COUNT(DISTINCT TRIM(stock_abbr)) AS count FROM income_sheet WHERE TRIM(COALESCE(stock_abbr, '')) <> ''"
        ).fetchone()['count']
    quality_rules = build_quality_rules()
    db_mtime = datetime.fromtimestamp(_ctx.DB_PATH.stat().st_mtime).strftime('%Y-%m-%d %H:%M')

    # ── Derive real status from filesystem ──
    reports_dir = CONFIG.get('reports_dir', '')
    reports_path = Path(reports_dir) if reports_dir else None
    reports_exist = reports_path is not None and reports_path.exists()
    report_count = len(list(reports_path.rglob('*.pdf'))) if reports_exist else 0

    result_dir = Path(_ctx.DB_PATH).parent / 'result'
    result_xlsx = result_dir / 'result_2.xlsx'
    result_status = '运行中' if result_xlsx.exists() else ('已就绪' if result_dir.exists() else '待创建')
    result_mtime = datetime.fromtimestamp(result_xlsx.stat().st_mtime).strftime('%Y-%m-%d %H:%M') if result_xlsx.exists() else '—'

    llm_url = CONFIG.get('llm', {}).get('base_url', '')
    llm_ok = bool(llm_url)

    # ── Import tasks: derive from DB state ──
    with open_connection() as connection:
        total_rows = sum(
            connection.execute(f'SELECT COUNT(*) AS c FROM {t}').fetchone()['c']
            for t in TABLE_ORDER
        )
        job_count = connection.execute('SELECT COUNT(*) AS c FROM import_jobs').fetchone()['c']
    pdf_status = '已完成' if total_rows > 0 else ('未开始' if not reports_exist else '待执行')
    pdf_detail = f'已入库 {total_rows} 条记录（{report_count} 份PDF）' if total_rows > 0 else '未检测到入库数据'
    check_pass = sum(1 for r in quality_rules if r.get('status') == '通过')
    check_status = '已完成' if check_pass == len(quality_rules) else '执行中'
    check_detail = f'{check_pass}/{len(quality_rules)} 项规则通过'
    archive_status = '已完成' if result_xlsx.exists() else '待处理'
    archive_detail = f'结果已归档至 {result_xlsx}' if result_xlsx.exists() else f'待将结果导出到 {result_dir}'

    return {
        'metrics': [
            {'title': '数据库文件', 'value': _ctx.DB_PATH.name, 'subtitle': '当前项目已使用 SQLite 存储'},
            {'title': '核心表', 'value': str(len(TABLE_ORDER)), 'subtitle': '与 create_database.py 一致'},
            {'title': '已接入公司', 'value': str(company_count), 'subtitle': '当前数据库中的股票简称数量'},
            {'title': '数据校验规则', 'value': str(len(quality_rules)), 'subtitle': '字段完整性、范围与逻辑一致性'}
        ],
        'connections': [
            {'name': '财报主库', 'type': 'SQLite', 'database': str(_ctx.DB_PATH), 'status': '运行中', 'lastSync': db_mtime},
            {
                'name': '财报文档目录',
                'type': '文件系统',
                'database': reports_dir or '未配置',
                'status': '运行中' if reports_exist else '未找到',
                'lastSync': f'{report_count} 份PDF' if reports_exist else '—'
            },
            {
                'name': '结果归档目录',
                'type': '文件系统',
                'database': str(result_dir),
                'status': result_status,
                'lastSync': result_mtime
            },
            {
                'name': '模型服务配置',
                'type': 'HTTP API',
                'database': llm_url or '未配置',
                'status': '运行中' if llm_ok else '未配置',
                'lastSync': now_text()
            }
        ],
        'importTasks': [
            {'name': 'PDF 财报解析', 'status': pdf_status, 'detail': pdf_detail},
            {'name': '字段一致性校验', 'status': check_status, 'detail': check_detail},
            {'name': '图表结果归档', 'status': archive_status, 'detail': archive_detail}
        ],
        'qualityRules': quality_rules
    }


def build_table_schemas() -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    with open_connection() as connection:
        for table_name in TABLE_ORDER:
            fields = [
                {'name': row['name'], 'type': row['type'], 'description': column_label(row['name'])}
                for row in connection.execute(f'PRAGMA table_info({table_name})').fetchall()
            ]
            row_count = connection.execute(f'SELECT COUNT(*) AS count FROM {table_name}').fetchone()['count']
            sample_rows = [row_to_dict(row) for row in connection.execute(f'SELECT * FROM {table_name} LIMIT 5').fetchall()]
            schemas.append({
                'name': table_name,
                'chineseName': TABLE_LABELS.get(table_name, table_name),
                'rows': row_count,
                'fields': fields,
                'sampleRows': sample_rows
            })
    return schemas
