# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import html
import json
import sqlite3
import textwrap
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from config import CONFIG


TABLE_CONFIG = {
    'core_performance_indicators_sheet': {
        'label': '核心业绩指标表',
        'key_fields': [
            'total_operating_revenue',
            'net_profit_10k_yuan',
            'operating_revenue_yoy_growth',
            'net_profit_yoy_growth',
        ],
    },
    'income_sheet': {
        'label': '利润表',
        'key_fields': [
            'total_operating_revenue',
            'net_profit',
            'operating_revenue_yoy_growth',
            'net_profit_yoy_growth',
        ],
    },
    'balance_sheet': {
        'label': '资产负债表',
        'key_fields': [
            'asset_total_assets',
            'liability_total_liabilities',
            'equity_total_equity',
        ],
    },
    'cash_flow_sheet': {
        'label': '现金流量表',
        'key_fields': [
            'net_cash_flow',
            'operating_cf_net_amount',
            'investing_cf_net_amount',
            'financing_cf_net_amount',
        ],
    },
}

EXCLUDED_FIELDS = {'serial_number', 'stock_code', 'stock_abbr', 'report_period', 'report_year'}
METHOD_PRIORITY = {'regex': 3, 'llm': 2, 'calculated': 1}


def pct(value: float | None) -> str:
    if value is None:
        return 'N/A'
    return f'{value * 100:.2f}%'


def number(value, digits: int = 4) -> str:
    if value is None:
        return 'N/A'
    if isinstance(value, float):
        return f'{value:.{digits}f}'
    return str(value)


def print_rule(char: str = '=', width: int = 72) -> None:
    print(char * width)


def print_section(title: str) -> None:
    print()
    print_rule('-')
    print(f'{title}')
    print_rule('-')


def print_kv(label: str, value, indent: int = 0, width: int = 92) -> None:
    prefix = ' ' * indent + f'{label}: '
    wrapped = textwrap.fill(
        str(value),
        width=width,
        initial_indent=prefix,
        subsequent_indent=' ' * len(prefix),
        break_long_words=False,
        break_on_hyphens=False,
    )
    print(wrapped)


def print_item(text: str, indent: int = 2, width: int = 92) -> None:
    prefix = ' ' * indent + '- '
    wrapped = textwrap.fill(
        str(text),
        width=width,
        initial_indent=prefix,
        subsequent_indent=' ' * len(prefix),
        break_long_words=False,
        break_on_hyphens=False,
    )
    print(wrapped)


def short_table_label(table_name: str) -> str:
    return TABLE_CONFIG.get(table_name, {}).get('label', table_name)


def clamp01(value: float | None) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(1.0, value))


def get_method_rate(method_rows: list[dict], method: str) -> float:
    for row in method_rows:
        if row.get('extract_method') == method:
            return float(row.get('rate') or 0.0)
    return 0.0


def get_grade(score: float) -> str:
    if score >= 90:
        return '优秀'
    if score >= 80:
        return '良好'
    if score >= 70:
        return '合格'
    if score >= 60:
        return '待提升'
    return '风险较高'


def make_bar(value: float | None, *, width: int = 18, max_value: float = 1.0) -> str:
    if value is None:
        return '[N/A]'
    ratio = 0.0 if max_value == 0 else clamp01(value / max_value)
    filled = int(round(ratio * width))
    return '[' + '#' * filled + '.' * (width - filled) + ']'


def print_bar_kv(label: str, value_text: str, value: float | None, *, indent: int = 0, width: int = 18, max_value: float = 1.0) -> None:
    print_kv(label, f'{value_text} {make_bar(value, width=width, max_value=max_value)}', indent=indent)


def summarize_component(score: float) -> str:
    if score >= 90:
        return '表现很强'
    if score >= 80:
        return '整体稳健'
    if score >= 70:
        return '基本可用'
    if score >= 60:
        return '存在短板'
    return '需要优先处理'


def fetch_scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def fetch_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def filled_expr(field: str) -> str:
    return f"{field} IS NOT NULL AND TRIM(CAST({field} AS TEXT)) != ''"


def get_business_fields(conn: sqlite3.Connection, table_name: str) -> list[str]:
    rows = fetch_rows(conn, f'PRAGMA table_info({table_name})')
    return [r['name'] for r in rows if r['name'] not in EXCLUDED_FIELDS]


def get_latest_job(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        'SELECT job_id, started_at, finished_at, total_reports, success_reports, '
        'warning_count, error_count, summary_json '
        'FROM import_jobs ORDER BY COALESCE(started_at, finished_at) DESC LIMIT 1'
    ).fetchone()
    if not row:
        return None
    job = dict(row)
    try:
        job['summary'] = json.loads(job.get('summary_json') or '{}')
    except json.JSONDecodeError:
        job['summary'] = {}
    return job


def build_overview(conn: sqlite3.Connection, latest_job: dict | None) -> dict:
    table_rows = {table: fetch_scalar(conn, f'SELECT COUNT(*) FROM {table}') or 0 for table in TABLE_CONFIG}
    company_count = fetch_scalar(
        conn,
        'SELECT COUNT(*) FROM ('
        'SELECT stock_code FROM core_performance_indicators_sheet '
        'UNION SELECT stock_code FROM income_sheet '
        'UNION SELECT stock_code FROM balance_sheet '
        'UNION SELECT stock_code FROM cash_flow_sheet'
        ')',
    ) or 0
    report_group_count = fetch_scalar(
        conn,
        'SELECT COUNT(*) FROM ('
        'SELECT stock_code, report_period FROM core_performance_indicators_sheet '
        'UNION SELECT stock_code, report_period FROM income_sheet '
        'UNION SELECT stock_code, report_period FROM balance_sheet '
        'UNION SELECT stock_code, report_period FROM cash_flow_sheet'
        ')',
    ) or 0
    latest_job_view = None
    if latest_job:
        latest_job_view = {
            'job_id': latest_job.get('job_id'),
            'started_at': latest_job.get('started_at'),
            'finished_at': latest_job.get('finished_at'),
            'total_reports': latest_job.get('total_reports'),
            'success_reports': latest_job.get('success_reports'),
            'warning_count': latest_job.get('warning_count'),
            'error_count': latest_job.get('error_count'),
        }
    return {
        'company_count': company_count,
        'report_group_count': report_group_count,
        'table_rows': table_rows,
        'latest_job': latest_job_view,
    }


def print_score_panel(report: dict) -> None:
    panel = report['score_panel']
    print_section('一、总评分面板')
    print_bar_kv('总评分', f"{number(panel['overall_score'], 2)} / 100", panel['overall_score'], max_value=100.0)
    print_kv('评级', panel['grade'])
    print_kv('说明', panel['note'])
    print()
    print('分项构成:')
    for item in panel['components']:
        print(f"- {item['name']}")
        print_bar_kv('得分', number(item['score'], 2), item['score'], indent=2, max_value=100.0)
        print_kv('权重', pct(item['weight']), indent=2)
        print_kv('贡献分', number(item['contribution'], 2), indent=2)
        print_kv('解读', summarize_component(item['score']), indent=2)
        metrics = item.get('metrics') or {}
        if item['name'] == '完整性':
            print_bar_kv('整体填充率', pct(metrics.get('overall_fill_rate')), metrics.get('overall_fill_rate'), indent=2)
            print_bar_kv('平均关键字段完整率', pct(metrics.get('avg_key_complete_rate')), metrics.get('avg_key_complete_rate'), indent=2)
        elif item['name'] == '可追溯性':
            print_bar_kv('可追溯字段覆盖率', pct(metrics.get('traceability_rate')), metrics.get('traceability_rate'), indent=2)
            print_bar_kv('页码可追溯率', pct(metrics.get('page_traceability_rate')), metrics.get('page_traceability_rate'), indent=2)
            print_kv('平均溯源置信度', number(metrics.get('avg_confidence'), 4), indent=2)
            print_bar_kv('regex占比', pct(metrics.get('regex_rate')), metrics.get('regex_rate'), indent=2)
        else:
            print_kv('每份报告警告数', number(metrics.get('warnings_per_report'), 2), indent=2)
            print_kv('每份报告错误数', number(metrics.get('errors_per_report'), 2), indent=2)
            print_bar_kv('warning健康度', pct(metrics.get('warning_health')), metrics.get('warning_health'), indent=2)
            print_bar_kv('error健康度', pct(metrics.get('error_health')), metrics.get('error_health'), indent=2)


def print_interpretation(report: dict) -> None:
    interpretation = report['interpretation']
    print_section('六、结果解读')
    print_kv('一句话结论', interpretation['overview'])
    if interpretation['strengths']:
        print()
        print('优势:')
        for item in interpretation['strengths']:
            print_item(item, indent=2)
    if interpretation['risks']:
        print()
        print('风险:')
        for item in interpretation['risks']:
            print_item(item, indent=2)
    if interpretation['actions']:
        print()
        print('优先动作:')
        for item in interpretation['actions']:
            print_item(item, indent=2)


def build_completeness_metrics(conn: sqlite3.Connection) -> tuple[list[dict], list[dict], dict]:
    table_summary = []
    field_fill = []
    total_possible_cells = 0
    total_filled_cells = 0

    for table_name, cfg in TABLE_CONFIG.items():
        label = cfg['label']
        row_count = fetch_scalar(conn, f'SELECT COUNT(*) FROM {table_name}') or 0
        business_fields = get_business_fields(conn, table_name)
        key_fields = [f for f in cfg['key_fields'] if f in business_fields]
        table_filled_cells = 0

        for field in business_fields:
            filled = fetch_scalar(conn, f'SELECT COUNT(*) FROM {table_name} WHERE {filled_expr(field)}') or 0
            rate = (filled / row_count) if row_count else None
            table_filled_cells += filled
            field_fill.append({
                'table_name': table_name,
                'table_label': label,
                'field_name': field,
                'row_count': row_count,
                'filled_count': filled,
                'fill_rate': rate,
                'is_key_field': field in key_fields,
            })

        possible_cells = row_count * len(business_fields)
        total_possible_cells += possible_cells
        total_filled_cells += table_filled_cells

        if key_fields and row_count:
            key_complete_sql = ' AND '.join(filled_expr(field) for field in key_fields)
            key_complete_count = fetch_scalar(
                conn,
                f'SELECT COUNT(*) FROM {table_name} WHERE {key_complete_sql}'
            ) or 0
            key_complete_rate = key_complete_count / row_count
        else:
            key_complete_count = 0
            key_complete_rate = None

        table_summary.append({
            'table_name': table_name,
            'table_label': label,
            'row_count': row_count,
            'business_field_count': len(business_fields),
            'filled_business_cells': table_filled_cells,
            'cell_fill_rate': (table_filled_cells / possible_cells) if possible_cells else None,
            'key_field_count': len(key_fields),
            'key_complete_count': key_complete_count,
            'key_complete_rate': key_complete_rate,
        })

    overall = {
        'total_possible_cells': total_possible_cells,
        'total_filled_cells': total_filled_cells,
        'overall_cell_fill_rate': (total_filled_cells / total_possible_cells) if total_possible_cells else None,
    }
    return table_summary, field_fill, overall


def dedupe_provenance(conn: sqlite3.Connection) -> list[dict]:
    rows = fetch_rows(
        conn,
        'SELECT id, stock_code, report_period, table_name, field_name, extract_method, confidence, page_no '
        'FROM field_provenance'
    )
    best = {}
    for row in rows:
        item = dict(row)
        key = (item['stock_code'], item['report_period'], item['table_name'], item['field_name'])
        score = (
            float(item.get('confidence') or 0.0),
            1 if item.get('page_no') is not None else 0,
            METHOD_PRIORITY.get(item.get('extract_method') or '', 0),
            int(item.get('id') or 0),
        )
        existing = best.get(key)
        if existing is None or score > existing['_score']:
            item['_score'] = score
            best[key] = item
    unique = []
    for item in best.values():
        item.pop('_score', None)
        unique.append(item)
    return unique


def build_provenance_metrics(conn: sqlite3.Connection, total_filled_cells: int) -> tuple[dict, list[dict], list[dict]]:
    unique_records = dedupe_provenance(conn)
    total_records = len(unique_records)
    method_counter = Counter(rec.get('extract_method') or 'unknown' for rec in unique_records)
    page_count = sum(1 for rec in unique_records if rec.get('page_no') is not None)
    confidence_values = [float(rec.get('confidence') or 0.0) for rec in unique_records]
    avg_confidence = sum(confidence_values) / len(confidence_values) if confidence_values else None

    method_rows = []
    for method, count in sorted(method_counter.items(), key=lambda x: (-x[1], x[0])):
        method_rows.append({
            'extract_method': method,
            'record_count': count,
            'rate': (count / total_records) if total_records else None,
        })

    field_method = defaultdict(Counter)
    for rec in unique_records:
        key = (rec.get('table_name'), rec.get('field_name'))
        field_method[key][rec.get('extract_method') or 'unknown'] += 1

    hotspot_rows = []
    for (table_name, field_name), counter in field_method.items():
        total = sum(counter.values())
        llm_count = counter.get('llm', 0)
        calc_count = counter.get('calculated', 0)
        non_regex = total - counter.get('regex', 0)
        hotspot_rows.append({
            'table_name': table_name,
            'field_name': field_name,
            'total_records': total,
            'regex_count': counter.get('regex', 0),
            'llm_count': llm_count,
            'calculated_count': calc_count,
            'non_regex_rate': (non_regex / total) if total else None,
            'llm_rate': (llm_count / total) if total else None,
            'calculated_rate': (calc_count / total) if total else None,
        })
    hotspot_rows.sort(key=lambda x: ((x['non_regex_rate'] or 0), x['total_records']), reverse=True)

    summary = {
        'traceable_field_count': total_records,
        'traceability_rate': (total_records / total_filled_cells) if total_filled_cells else None,
        'page_traceability_rate': (page_count / total_records) if total_records else None,
        'avg_confidence': avg_confidence,
    }
    return summary, method_rows, hotspot_rows


def build_issue_metrics(conn: sqlite3.Connection, latest_job: dict | None) -> tuple[dict, list[dict], list[dict]]:
    if latest_job and latest_job.get('job_id'):
        scope_sql = 'WHERE job_id = ?'
        params = (latest_job['job_id'],)
        scope_label = f"最近任务 {latest_job['job_id']}"
    else:
        scope_sql = ''
        params = ()
        scope_label = '全库'

    total_issues = fetch_scalar(conn, f'SELECT COUNT(*) FROM import_issues {scope_sql}', params) or 0
    warning_count = fetch_scalar(
        conn,
        f"SELECT COUNT(*) FROM import_issues {scope_sql + (' AND ' if scope_sql else ' WHERE ')} issue_level='warning'",
        params,
    ) or 0
    error_count = fetch_scalar(
        conn,
        f"SELECT COUNT(*) FROM import_issues {scope_sql + (' AND ' if scope_sql else ' WHERE ')} issue_level='error'",
        params,
    ) or 0

    by_type = [
        dict(row)
        for row in fetch_rows(
            conn,
            f'SELECT issue_type, issue_level, COUNT(*) AS issue_count '
            f'FROM import_issues {scope_sql} '
            f'GROUP BY issue_type, issue_level '
            f'ORDER BY issue_count DESC, issue_type ASC',
            params,
        )
    ]
    by_table = [
        dict(row)
        for row in fetch_rows(
            conn,
            f'SELECT COALESCE(table_name, "未标注") AS table_name, COUNT(*) AS issue_count '
            f'FROM import_issues {scope_sql} '
            f'GROUP BY COALESCE(table_name, "未标注") '
            f'ORDER BY issue_count DESC, table_name ASC',
            params,
        )
    ]

    report_base = None
    if latest_job and latest_job.get('success_reports'):
        report_base = latest_job['success_reports']
    issue_summary = {
        'scope': scope_label,
        'total_issues': total_issues,
        'warning_count': warning_count,
        'error_count': error_count,
        'warnings_per_100_reports': (warning_count / report_base * 100) if report_base else None,
        'errors_per_100_reports': (error_count / report_base * 100) if report_base else None,
    }
    return issue_summary, by_type, by_table


def build_score_panel(
    overview: dict,
    completeness_overall: dict,
    table_summary: list[dict],
    prov_summary: dict,
    method_breakdown: list[dict],
    issue_summary: dict,
) -> dict:
    overall_fill_rate = float(completeness_overall.get('overall_cell_fill_rate') or 0.0)
    key_rates = [float(row['key_complete_rate']) for row in table_summary if row.get('key_complete_rate') is not None]
    avg_key_complete_rate = sum(key_rates) / len(key_rates) if key_rates else 0.0
    completeness_score = 100 * (0.65 * overall_fill_rate + 0.35 * avg_key_complete_rate)

    traceability_rate = float(prov_summary.get('traceability_rate') or 0.0)
    page_traceability_rate = float(prov_summary.get('page_traceability_rate') or 0.0)
    avg_confidence = clamp01(float(prov_summary.get('avg_confidence') or 0.0))
    regex_rate = get_method_rate(method_breakdown, 'regex')
    provenance_score = 100 * (
        0.40 * traceability_rate +
        0.15 * page_traceability_rate +
        0.15 * avg_confidence +
        0.30 * regex_rate
    )

    success_reports = None
    latest_job = overview.get('latest_job') or {}
    if latest_job.get('success_reports'):
        success_reports = float(latest_job['success_reports'])
    warning_count = float(issue_summary.get('warning_count') or 0.0)
    error_count = float(issue_summary.get('error_count') or 0.0)
    warnings_per_report = (warning_count / success_reports) if success_reports else None
    errors_per_report = (error_count / success_reports) if success_reports else None
    warning_health = 1.0 if warnings_per_report is None else clamp01(1 - warnings_per_report / 5.0)
    error_health = 1.0 if errors_per_report is None else clamp01(1 - errors_per_report / 0.5)
    issue_health_score = 100 * (0.70 * warning_health + 0.30 * error_health)

    weights = {
        '完整性': 0.45,
        '可追溯性': 0.30,
        '质量告警健康度': 0.25,
    }
    components = [
        {
            'name': '完整性',
            'score': round(completeness_score, 2),
            'weight': weights['完整性'],
            'contribution': round(completeness_score * weights['完整性'], 2),
            'metrics': {
                'overall_fill_rate': overall_fill_rate,
                'avg_key_complete_rate': avg_key_complete_rate,
            },
        },
        {
            'name': '可追溯性',
            'score': round(provenance_score, 2),
            'weight': weights['可追溯性'],
            'contribution': round(provenance_score * weights['可追溯性'], 2),
            'metrics': {
                'traceability_rate': traceability_rate,
                'page_traceability_rate': page_traceability_rate,
                'avg_confidence': avg_confidence,
                'regex_rate': regex_rate,
            },
        },
        {
            'name': '质量告警健康度',
            'score': round(issue_health_score, 2),
            'weight': weights['质量告警健康度'],
            'contribution': round(issue_health_score * weights['质量告警健康度'], 2),
            'metrics': {
                'warnings_per_report': warnings_per_report,
                'errors_per_report': errors_per_report,
                'warning_health': warning_health,
                'error_health': error_health,
            },
        },
    ]
    overall_score = round(sum(item['contribution'] for item in components), 2)
    return {
        'overall_score': overall_score,
        'grade': get_grade(overall_score),
        'components': components,
        'formula': {
            '完整性': '0.65 × 整体业务字段填充率 + 0.35 × 平均关键字段完整率',
            '可追溯性': '0.40 × 可追溯字段覆盖率 + 0.15 × 页码可追溯率 + 0.15 × 平均溯源置信度 + 0.30 × regex占比',
            '质量告警健康度': '0.70 × warning健康度 + 0.30 × error健康度',
            '总分': '0.45 × 完整性 + 0.30 × 可追溯性 + 0.25 × 质量告警健康度',
        },
        'note': '总分用于横向比较任务质量，不等同于人工标注准确率。',
    }


def build_interpretation(report: dict, top_n: int) -> dict:
    score_panel = report['score_panel']
    completeness = report['completeness']
    provenance = report['provenance']
    issues = report['issues']

    low_fill_fields = sorted(
        [row for row in completeness['field_fill'] if row['row_count'] > 0 and (row['fill_rate'] or 0) < 1.0],
        key=lambda x: (x['fill_rate'] if x['fill_rate'] is not None else 1.0, x['filled_count'])
    )
    hotspot_fields = [row for row in provenance['non_regex_hotspots'] if (row['non_regex_rate'] or 0) > 0]
    method_rows = provenance['method_breakdown']
    regex_rate = get_method_rate(method_rows, 'regex')
    llm_rate = get_method_rate(method_rows, 'llm')
    calc_rate = get_method_rate(method_rows, 'calculated')

    strengths = []
    risks = []
    actions = []

    overall_fill_rate = completeness['overall']['overall_cell_fill_rate'] or 0.0
    if overall_fill_rate >= 0.98:
        strengths.append(f"整体字段填充率达到 {pct(overall_fill_rate)}，说明提取完整性较高。")
    if all((row.get('key_complete_rate') or 0) >= 0.99 for row in completeness['table_summary'] if row.get('key_complete_rate') is not None):
        strengths.append('四张表的关键字段完整率均接近或达到 100%，主流程可用性较强。')
    if regex_rate >= 0.6:
        strengths.append(f"regex 提取占比为 {pct(regex_rate)}，多数值来自更稳定的显式提取。")
    if (issues['summary'].get('error_count') or 0) == 0:
        strengths.append('最近一次导入没有 error 级问题，当前流程没有出现明显硬失败。')

    traceability_rate = provenance['summary'].get('traceability_rate') or 0.0
    if traceability_rate < 0.8:
        risks.append(f"可追溯字段覆盖率仅为 {pct(traceability_rate)}，仍有部分入库值缺少来源链路。")
    if calc_rate >= 0.2:
        risks.append(f"计算字段占比达到 {pct(calc_rate)}，说明部分指标依赖后处理而非原文显式提取。")
    if llm_rate >= 0.05:
        risks.append(f"LLM 占比为 {pct(llm_rate)}，部分字段存在模型波动风险。")
    warning_count = issues['summary'].get('warning_count') or 0
    if warning_count > 0:
        risks.append(f"最近任务共有 {warning_count} 条 warning，主要问题类型需要持续压降。")
    if low_fill_fields:
        top_field = low_fill_fields[0]
        risks.append(
            f"低覆盖最明显的字段是 {top_field['table_label']}.{top_field['field_name']}，"
            f"当前填充率为 {pct(top_field['fill_rate'])}。"
        )

    if low_fill_fields:
        actions.append(
            '优先补齐低覆盖字段：' + '、'.join(
                f"{row['table_label']}.{row['field_name']}({pct(row['fill_rate'])})"
                for row in low_fill_fields[:min(3, len(low_fill_fields))]
            )
        )
    if hotspot_fields:
        actions.append(
            '优先复核高依赖后处理字段：' + '、'.join(
                f"{row['table_name']}.{row['field_name']}"
                for row in hotspot_fields[:min(3, len(hotspot_fields))]
            )
        )
    by_type = issues['by_type']
    if by_type:
        actions.append(
            '优先压降告警类型：' + '、'.join(
                f"{row['issue_type']}({row['issue_count']})"
                for row in by_type[:min(2, len(by_type))]
            )
        )
    if traceability_rate < 0.85:
        actions.append('补强 provenance 记录覆盖率，优先保证关键字段都能追溯到页码与来源文本。')

    weakest_component = min(score_panel['components'], key=lambda x: x['score'])
    overview = (
        f"本次任务总体评级为{score_panel['grade']}，总分 {number(score_panel['overall_score'], 2)}。"
        f"当前最强维度是 {max(score_panel['components'], key=lambda x: x['score'])['name']}，"
        f"最需要提升的是 {weakest_component['name']}。"
    )
    return {
        'overview': overview,
        'strengths': strengths[:top_n],
        'risks': risks[:top_n],
        'actions': actions[:top_n],
        'weakest_component': weakest_component['name'],
    }


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def svg_bar_chart(title: str, items: list[dict], label_key: str, value_key: str, *, width: int = 900, row_height: int = 44, percent: bool = False, max_value: float | None = None) -> str:
    safe_title = html.escape(title)
    labels = [str(item.get(label_key, '')) for item in items]
    values = [float(item.get(value_key) or 0.0) for item in items]
    max_val = max_value if max_value is not None else (max(values) if values else 1.0)
    if max_val <= 0:
        max_val = 1.0
    height = 90 + max(len(items), 1) * row_height
    left = 240
    right = 40
    top = 56
    bar_width = width - left - right
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<defs><linearGradient id="grad" x1="0" x2="1"><stop offset="0%" stop-color="#7c3aed"/><stop offset="100%" stop-color="#06b6d4"/></linearGradient></defs>',
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="18" fill="#0f172a"/>',
        f'<text x="28" y="36" fill="#e2e8f0" font-size="22" font-family="Segoe UI, Arial">{safe_title}</text>',
    ]
    for idx, (label, value) in enumerate(zip(labels, values)):
        y = top + idx * row_height
        bar_len = 0 if max_val == 0 else max(4, int(bar_width * max(0.0, value) / max_val))
        display = f'{value * 100:.2f}%' if percent else f'{value:.2f}'
        parts.append(f'<text x="28" y="{y + 24}" fill="#cbd5e1" font-size="14" font-family="Segoe UI, Arial">{html.escape(label)}</text>')
        parts.append(f'<rect x="{left}" y="{y + 6}" width="{bar_width}" height="18" rx="9" fill="#1e293b"/>')
        parts.append(f'<rect x="{left}" y="{y + 6}" width="{bar_len}" height="18" rx="9" fill="url(#grad)"/>')
        parts.append(f'<text x="{left + bar_width + 10}" y="{y + 21}" fill="#e2e8f0" font-size="13" font-family="Segoe UI, Arial">{display}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def save_visual_report(output_dir: Path, report: dict) -> dict:
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_dir = output_dir / f'quality_visual_report_{ts}'
    charts_dir = report_dir / 'charts'
    report_dir.mkdir(parents=True, exist_ok=True)
    charts_dir.mkdir(parents=True, exist_ok=True)

    score_items = [
        {'label': item['name'], 'value': item['score']}
        for item in report['score_panel']['components']
    ]
    completeness_items = [
        {'label': row['table_label'], 'value': row['cell_fill_rate'] or 0.0}
        for row in report['completeness']['table_summary']
    ]
    method_items = [
        {'label': row['extract_method'], 'value': row['rate'] or 0.0}
        for row in report['provenance']['method_breakdown']
    ]
    issue_items = [
        {'label': row['issue_type'], 'value': row['issue_count']}
        for row in report['issues']['by_type'][:5]
    ]

    charts = {
        'score_panel.svg': svg_bar_chart('总评分分项', score_items, 'label', 'value', max_value=100.0),
        'table_fill_rate.svg': svg_bar_chart('各表填充率', completeness_items, 'label', 'value', percent=True, max_value=1.0),
        'extract_methods.svg': svg_bar_chart('提取方式占比', method_items, 'label', 'value', percent=True, max_value=1.0),
        'issue_breakdown.svg': svg_bar_chart('问题类型分布', issue_items, 'label', 'value'),
    }
    for filename, content in charts.items():
        (charts_dir / filename).write_text(content, encoding='utf-8')

    interpretation = report['interpretation']
    score_panel = report['score_panel']
    html_content = f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>财务报告数据质量可视化报告</title>
  <style>
    :root {{
      --bg: #020617;
      --panel: rgba(15, 23, 42, 0.78);
      --panel-border: rgba(148, 163, 184, 0.16);
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #8b5cf6;
      --accent-2: #06b6d4;
      --good: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
      --shadow: 0 20px 50px rgba(2, 6, 23, 0.45);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: 'Segoe UI', Arial, sans-serif;
      background: radial-gradient(circle at top, #0f172a 0%, #020617 45%, #01030a 100%);
      color: var(--text);
    }}
    .container {{ max-width: 1380px; margin: 0 auto; padding: 36px 24px 64px; }}
    .hero {{
      display: grid;
      grid-template-columns: 1.5fr 1fr;
      gap: 24px;
      align-items: stretch;
      margin-bottom: 28px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
      padding: 24px;
    }}
    .title {{ font-size: 34px; font-weight: 700; margin: 0 0 12px; }}
    .subtitle {{ color: var(--muted); font-size: 15px; line-height: 1.7; }}
    .score-ring {{
      display: flex; align-items: center; justify-content: center;
      width: 220px; height: 220px; margin: 12px auto 18px;
      border-radius: 50%;
      background: conic-gradient(var(--accent) 0 {score_panel['overall_score']}%, rgba(148,163,184,0.12) 0 100%);
      position: relative;
    }}
    .score-ring::after {{
      content: ''; position: absolute; inset: 16px; border-radius: 50%; background: #081120;
      border: 1px solid rgba(148,163,184,0.12);
    }}
    .score-ring-content {{ position: relative; z-index: 1; text-align: center; }}
    .score-ring-content .score {{ font-size: 48px; font-weight: 700; }}
    .score-ring-content .grade {{ color: #c4b5fd; margin-top: 6px; font-size: 18px; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 28px; }}
    .stat-card {{ padding: 18px; border-radius: 20px; background: rgba(15,23,42,0.72); border: 1px solid rgba(148,163,184,0.12); }}
    .stat-label {{ color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
    .stat-value {{ font-size: 28px; font-weight: 700; }}
    .stat-sub {{ color: #cbd5e1; font-size: 13px; margin-top: 6px; }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 24px; }}
    .grid-3 {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 24px; }}
    h2 {{ margin: 0 0 18px; font-size: 22px; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }}
    .chip {{ padding: 10px 14px; border-radius: 999px; background: rgba(124,58,237,0.14); color: #ddd6fe; font-size: 13px; border: 1px solid rgba(167,139,250,0.18); }}
    .list {{ margin: 0; padding-left: 18px; color: #e5e7eb; line-height: 1.8; }}
    .list li {{ margin-bottom: 8px; }}
    .section-note {{ color: var(--muted); font-size: 14px; line-height: 1.8; }}
    .chart {{ width: 100%; border-radius: 18px; overflow: hidden; border: 1px solid rgba(148,163,184,0.12); background: rgba(15,23,42,0.64); }}
    .metric-row {{ display: grid; grid-template-columns: 150px 1fr 70px; gap: 12px; align-items: center; margin-bottom: 12px; }}
    .metric-row .bar {{ height: 12px; border-radius: 999px; background: rgba(148,163,184,0.14); overflow: hidden; }}
    .metric-row .bar span {{ display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }}
    .footer {{ color: var(--muted); text-align: center; margin-top: 28px; font-size: 13px; }}
    @media (max-width: 1100px) {{
      .hero, .grid-2, .grid-3, .stats {{ grid-template-columns: 1fr; }}
      .score-ring {{ width: 180px; height: 180px; }}
    }}
  </style>
</head>
<body>
  <div class=\"container\">
    <section class=\"hero\">
      <div class=\"panel\">
        <div class=\"title\">财务报告数据质量可视化报告</div>
        <div class=\"subtitle\">生成时间：{html.escape(report['generated_at'])}<br />数据库：{html.escape(report['db_path'])}</div>
        <div class=\"chips\">
          <div class=\"chip\">公司数量 {report['overview']['company_count']}</div>
          <div class=\"chip\">报告期组合 {report['overview']['report_group_count']}</div>
          <div class=\"chip\">总评分 {number(score_panel['overall_score'], 2)}</div>
          <div class=\"chip\">评级 {html.escape(score_panel['grade'])}</div>
        </div>
        <div style=\"margin-top: 20px;\" class=\"section-note\">{html.escape(interpretation['overview'])}</div>
      </div>
      <div class=\"panel\" style=\"text-align:center;\">
        <div class=\"score-ring\">
          <div class=\"score-ring-content\">
            <div class=\"score\">{number(score_panel['overall_score'], 2)}</div>
            <div class=\"grade\">{html.escape(score_panel['grade'])}</div>
          </div>
        </div>
        <div class=\"section-note\">{html.escape(score_panel['note'])}</div>
      </div>
    </section>

    <section class=\"stats\">
      <div class=\"stat-card\"><div class=\"stat-label\">完整性得分</div><div class=\"stat-value\">{number(score_panel['components'][0]['score'], 2)}</div><div class=\"stat-sub\">整体填充率 {pct(score_panel['components'][0]['metrics']['overall_fill_rate'])}</div></div>
      <div class=\"stat-card\"><div class=\"stat-label\">可追溯性得分</div><div class=\"stat-value\">{number(score_panel['components'][1]['score'], 2)}</div><div class=\"stat-sub\">追溯覆盖率 {pct(score_panel['components'][1]['metrics']['traceability_rate'])}</div></div>
      <div class=\"stat-card\"><div class=\"stat-label\">告警健康度</div><div class=\"stat-value\">{number(score_panel['components'][2]['score'], 2)}</div><div class=\"stat-sub\">Warning {report['issues']['summary']['warning_count']} / Error {report['issues']['summary']['error_count']}</div></div>
      <div class=\"stat-card\"><div class=\"stat-label\">Regex占比</div><div class=\"stat-value\">{pct(score_panel['components'][1]['metrics']['regex_rate'])}</div><div class=\"stat-sub\">LLM {pct(get_method_rate(report['provenance']['method_breakdown'], 'llm'))} / Calculated {pct(get_method_rate(report['provenance']['method_breakdown'], 'calculated'))}</div></div>
    </section>

    <section class=\"grid-2\">
      <div class=\"panel\">
        <h2>评分维度</h2>
        <div class=\"section-note\">分数越高越稳定，可用于比较不同导入批次和规则版本。</div>
        {''.join(f'<div class="metric-row"><div>{html.escape(item["name"])}</div><div class="bar"><span style="width:{item["score"]}%"></span></div><div>{number(item["score"], 2)}</div></div>' for item in score_panel['components'])}
      </div>
      <div class=\"panel\">
        <h2>总体解读</h2>
        <ul class=\"list\">{''.join(f'<li>{html.escape(text)}</li>' for text in interpretation['strengths'])}</ul>
      </div>
    </section>

    <section class=\"grid-2\">
      <div class=\"panel\"><h2>分项评分图</h2><img class=\"chart\" src=\"charts/score_panel.svg\" alt=\"score_panel\" /></div>
      <div class=\"panel\"><h2>各表填充率</h2><img class=\"chart\" src=\"charts/table_fill_rate.svg\" alt=\"table_fill_rate\" /></div>
    </section>

    <section class=\"grid-2\">
      <div class=\"panel\"><h2>提取方式占比</h2><img class=\"chart\" src=\"charts/extract_methods.svg\" alt=\"extract_methods\" /></div>
      <div class=\"panel\"><h2>问题类型分布</h2><img class=\"chart\" src=\"charts/issue_breakdown.svg\" alt=\"issue_breakdown\" /></div>
    </section>

    <section class=\"grid-3\">
      <div class=\"panel\">
        <h2>优势</h2>
        <ul class=\"list\">{''.join(f'<li>{html.escape(text)}</li>' for text in interpretation['strengths'])}</ul>
      </div>
      <div class=\"panel\">
        <h2>风险</h2>
        <ul class=\"list\">{''.join(f'<li>{html.escape(text)}</li>' for text in interpretation['risks'])}</ul>
      </div>
      <div class=\"panel\">
        <h2>优先动作</h2>
        <ul class=\"list\">{''.join(f'<li>{html.escape(text)}</li>' for text in interpretation['actions'])}</ul>
      </div>
    </section>

    <div class=\"footer\">报告目录：{html.escape(str(report_dir.resolve()))}</div>
  </div>
</body>
</html>
"""
    html_path = report_dir / 'index.html'
    html_path.write_text(html_content, encoding='utf-8')
    return {
        'report_dir': str(report_dir),
        'html': str(html_path),
        'charts_dir': str(charts_dir),
        'chart_files': [str(charts_dir / name) for name in charts],
    }


def save_outputs(output_dir: Path, report: dict) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    json_path = output_dir / f'quality_report_{ts}.json'
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    summary_csv = output_dir / f'quality_summary_{ts}.csv'
    summary_row = [{
        'generated_at': report['generated_at'],
        'db_path': report['db_path'],
        'overall_score': report['score_panel']['overall_score'],
        'grade': report['score_panel']['grade'],
        'completeness_score': report['score_panel']['components'][0]['score'],
        'provenance_score': report['score_panel']['components'][1]['score'],
        'issue_health_score': report['score_panel']['components'][2]['score'],
        'company_count': report['overview']['company_count'],
        'report_group_count': report['overview']['report_group_count'],
        'overall_cell_fill_rate': report['completeness']['overall']['overall_cell_fill_rate'],
        'traceability_rate': report['provenance']['summary']['traceability_rate'],
        'page_traceability_rate': report['provenance']['summary']['page_traceability_rate'],
        'avg_confidence': report['provenance']['summary']['avg_confidence'],
        'issue_scope': report['issues']['summary']['scope'],
        'warning_count': report['issues']['summary']['warning_count'],
        'error_count': report['issues']['summary']['error_count'],
    }]
    write_csv(summary_csv, summary_row, list(summary_row[0].keys()))

    score_csv = output_dir / f'quality_scorecard_{ts}.csv'
    write_csv(score_csv, report['score_panel']['components'], [
        'name', 'score', 'weight', 'contribution', 'metrics',
    ])

    table_csv = output_dir / f'quality_table_completeness_{ts}.csv'
    write_csv(table_csv, report['completeness']['table_summary'], [
        'table_name', 'table_label', 'row_count', 'business_field_count',
        'filled_business_cells', 'cell_fill_rate', 'key_field_count',
        'key_complete_count', 'key_complete_rate',
    ])

    field_csv = output_dir / f'quality_field_fill_{ts}.csv'
    write_csv(field_csv, report['completeness']['field_fill'], [
        'table_name', 'table_label', 'field_name', 'row_count',
        'filled_count', 'fill_rate', 'is_key_field',
    ])

    prov_csv = output_dir / f'quality_provenance_methods_{ts}.csv'
    write_csv(prov_csv, report['provenance']['method_breakdown'], [
        'extract_method', 'record_count', 'rate',
    ])

    hotspot_csv = output_dir / f'quality_provenance_hotspots_{ts}.csv'
    write_csv(hotspot_csv, report['provenance']['non_regex_hotspots'], [
        'table_name', 'field_name', 'total_records', 'regex_count',
        'llm_count', 'calculated_count', 'non_regex_rate', 'llm_rate', 'calculated_rate',
    ])

    issue_csv = output_dir / f'quality_issue_breakdown_{ts}.csv'
    write_csv(issue_csv, report['issues']['by_type'], [
        'issue_type', 'issue_level', 'issue_count',
    ])

    issue_table_csv = output_dir / f'quality_issue_tables_{ts}.csv'
    write_csv(issue_table_csv, report['issues']['by_table'], [
        'table_name', 'issue_count',
    ])

    return {
        'json': str(json_path),
        'csv': [
            str(summary_csv),
            str(score_csv),
            str(table_csv),
            str(field_csv),
            str(prov_csv),
            str(hotspot_csv),
            str(issue_csv),
            str(issue_table_csv),
        ],
    }


def print_overview(report: dict) -> None:
    overview = report['overview']
    print_section('二、总体概览')
    print_kv('数据库路径', report['db_path'])
    print_kv('生成时间', report['generated_at'])
    print_kv('公司数量', overview['company_count'])
    print_kv('报告期组合数', overview['report_group_count'])
    print()
    print('表记录数:')
    for table_name, row_count in overview['table_rows'].items():
        print_kv(short_table_label(table_name), row_count, indent=2)
    latest_job = overview.get('latest_job')
    if latest_job:
        print()
        print('最近导入任务:')
        print_kv('任务ID', latest_job.get('job_id'), indent=2)
        print_kv('开始时间', latest_job.get('started_at'), indent=2)
        print_kv('结束时间', latest_job.get('finished_at'), indent=2)
        print_kv('报告总数', latest_job.get('total_reports'), indent=2)
        print_kv('成功报告数', latest_job.get('success_reports'), indent=2)
        print_kv('警告数', latest_job.get('warning_count'), indent=2)
        print_kv('错误数', latest_job.get('error_count'), indent=2)


def print_completeness(report: dict, top_n: int) -> None:
    summary = report['completeness']['table_summary']
    overall = report['completeness']['overall']
    low_fill_fields = sorted(
        [
            row for row in report['completeness']['field_fill']
            if row['row_count'] > 0 and (row['fill_rate'] is None or row['fill_rate'] < 1.0)
        ],
        key=lambda x: (x['fill_rate'] if x['fill_rate'] is not None else 1.0, x['filled_count'])
    )[:top_n]

    print_section('三、完整性 / 覆盖率')
    print_bar_kv('整体业务字段填充率', pct(overall['overall_cell_fill_rate']), overall['overall_cell_fill_rate'])
    print()
    for row in summary:
        print(f"- {row['table_label']}")
        print_kv('行数', row['row_count'], indent=2)
        print_bar_kv('单元填充率', pct(row['cell_fill_rate']), row['cell_fill_rate'], indent=2)
        print_bar_kv('关键字段完整率', pct(row['key_complete_rate']), row['key_complete_rate'], indent=2)

    if low_fill_fields:
        print()
        print(f'低覆盖字段 Top {min(top_n, len(low_fill_fields))}:')
        for idx, row in enumerate(low_fill_fields, start=1):
            print(f"  {idx:>2}. {row['table_label']}.{row['field_name']}")
            print_kv('填充率', f"{pct(row['fill_rate'])} ({row['filled_count']}/{row['row_count']})", indent=6)


def print_provenance(report: dict, top_n: int) -> None:
    summary = report['provenance']['summary']
    methods = report['provenance']['method_breakdown']
    hotspots = [row for row in report['provenance']['non_regex_hotspots'] if (row['non_regex_rate'] or 0) > 0][:top_n]

    print_section('四、来源质量 / 可追溯性')
    print_bar_kv('可追溯字段覆盖率', pct(summary['traceability_rate']), summary['traceability_rate'])
    print_bar_kv('页码可追溯率', pct(summary['page_traceability_rate']), summary['page_traceability_rate'])
    print_kv('平均溯源置信度', number(summary['avg_confidence'], 4))
    print()
    print('提取方式占比:')
    for row in methods:
        print(f"  - {row['extract_method']}: {row['record_count']} / {pct(row['rate'])}")

    if hotspots:
        print()
        print(f'非正则依赖字段 Top {min(top_n, len(hotspots))}:')
        for idx, row in enumerate(hotspots, start=1):
            print(f"  {idx:>2}. {row['table_name']}.{row['field_name']}")
            print_kv('非正则占比', pct(row['non_regex_rate']), indent=6)
            print_kv('LLM占比', pct(row['llm_rate']), indent=6)
            print_kv('计算占比', pct(row['calculated_rate']), indent=6)


def print_issues(report: dict, top_n: int) -> None:
    summary = report['issues']['summary']
    by_type = report['issues']['by_type'][:top_n]
    by_table = report['issues']['by_table'][:top_n]

    print_section('五、规则校验 / 质量告警')
    print_kv('统计范围', summary['scope'])
    print_kv('告警总数', summary['warning_count'])
    print_kv('错误总数', summary['error_count'])
    print_kv('每100份报告警告数', number(summary['warnings_per_100_reports'], 2))
    print_kv('每100份报告错误数', number(summary['errors_per_100_reports'], 2))
    print()
    print(f'问题类型 Top {min(top_n, len(by_type))}:')
    for idx, row in enumerate(by_type, start=1):
        print(f"  {idx:>2}. {row['issue_type']} ({row['issue_level']})")
        print_kv('数量', row['issue_count'], indent=6)
    print()
    print(f'问题表分布 Top {min(top_n, len(by_table))}:')
    for idx, row in enumerate(by_table, start=1):
        print(f"  {idx:>2}. {row['table_name']}")
        print_kv('数量', row['issue_count'], indent=6)


def build_report(db_path: str, top_n: int) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        latest_job = get_latest_job(conn)
        overview = build_overview(conn, latest_job)
        table_summary, field_fill, completeness_overall = build_completeness_metrics(conn)
        prov_summary, method_breakdown, hotspot_rows = build_provenance_metrics(
            conn,
            completeness_overall['total_filled_cells'],
        )
        issue_summary, by_type, by_table = build_issue_metrics(conn, latest_job)

        report = {
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'db_path': str(Path(db_path).resolve()),
            'overview': overview,
            'completeness': {
                'overall': completeness_overall,
                'table_summary': table_summary,
                'field_fill': field_fill,
                'low_fill_fields_top_n': sorted(
                    [row for row in field_fill if row['row_count'] > 0],
                    key=lambda x: (x['fill_rate'] if x['fill_rate'] is not None else 1.0, x['filled_count'])
                )[:top_n],
            },
            'provenance': {
                'summary': prov_summary,
                'method_breakdown': method_breakdown,
                'non_regex_hotspots': hotspot_rows,
            },
            'issues': {
                'summary': issue_summary,
                'by_type': by_type,
                'by_table': by_table,
            },
        }
        report['score_panel'] = build_score_panel(
            overview,
            completeness_overall,
            table_summary,
            prov_summary,
            method_breakdown,
            issue_summary,
        )
        report['interpretation'] = build_interpretation(report, top_n)
        return report
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='生成财报提取数据质量报告')
    parser.add_argument('--db-path', default=CONFIG['database']['path'], help='SQLite 数据库路径')
    parser.add_argument('--output-dir', default='quality_reports', help='报告输出目录')
    parser.add_argument('--visual-output-dir', default='quality_visual_reports', help='可视化报告输出目录')
    parser.add_argument('--top-n', type=int, default=10, help='控制台展示的 Top N 数量')
    parser.add_argument('--save-files', action='store_true', help='保存 JSON 和 CSV 报告文件')
    parser.add_argument('--no-open-browser', action='store_true', help='生成可视化报告但不自动打开浏览器')
    args = parser.parse_args()

    report = build_report(args.db_path, args.top_n)
    visual_outputs = save_visual_report(Path(args.visual_output_dir), report)
    report['visual_report'] = visual_outputs
    outputs = None
    if args.save_files:
        outputs = save_outputs(Path(args.output_dir), report)
        report['output_files'] = outputs
        Path(outputs['json']).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if not args.no_open_browser:
        try:
            webbrowser.open(Path(visual_outputs['html']).resolve().as_uri())
        except Exception:
            pass

    print_rule()
    print('财务报告数据质量报告')
    print_rule()
    print_score_panel(report)
    print_overview(report)
    print_completeness(report, args.top_n)
    print_provenance(report, args.top_n)
    print_issues(report, args.top_n)
    print_interpretation(report)
    print_section('七、可视化报告')
    print_kv('HTML 页面', visual_outputs['html'])
    print_kv('图表目录', visual_outputs['charts_dir'])
    print_kv('报告目录', visual_outputs['report_dir'])
    if outputs:
        print_section('八、报告文件')
        print_kv('JSON', outputs['json'])
        for path in outputs['csv']:
            print_kv('CSV', path)
        print()
    print_rule()
    print('质量报告生成完成')
    print_rule()


if __name__ == '__main__':
    main()
