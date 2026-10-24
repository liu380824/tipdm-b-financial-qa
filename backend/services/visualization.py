from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from typing import Any

from .llm import llm_available, llm_temperature, request_llm_json

_SERIES_COLORS = ['#6366f1', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#14b8a6']
_CHART_LABELS = {
    'line': '折线图',
    'area': '面积图',
    'stacked_area': '堆叠面积图',
    'bar': '柱状图',
    'horizontal_bar': '横向柱状图',
    'grouped_bar': '分组柱状图',
    'stacked_bar': '堆叠柱状图',
    'pie': '饼图',
    'donut': '环形图',
    'scatter': '散点图',
    'bubble': '气泡图',
    'heatmap': '热力图',
    'radar': '雷达图',
    'waterfall': '瀑布图',
    'dual_axis_combo': '双轴组合图',
    'histogram': '直方图',
    'treemap': '矩形树图',
    'boxplot': '箱线图',
}
_EXPLICIT_CHART_PATTERNS = [
    ('dual_axis_combo', re.compile(r'(双轴图|双y轴|双Y轴|组合图|柱线图)', re.IGNORECASE)),
    ('stacked_area', re.compile(r'(堆叠面积图|堆积面积图)', re.IGNORECASE)),
    ('stacked_bar', re.compile(r'(堆叠柱状图|堆积柱状图|堆叠条形图|堆积条形图)', re.IGNORECASE)),
    ('grouped_bar', re.compile(r'(分组柱状图|簇状柱状图|并列柱状图|对比柱状图)', re.IGNORECASE)),
    ('horizontal_bar', re.compile(r'(横向柱状图|横向条形图|条形图)', re.IGNORECASE)),
    ('donut', re.compile(r'(环形图|圆环图)', re.IGNORECASE)),
    ('pie', re.compile(r'(饼图)', re.IGNORECASE)),
    ('bubble', re.compile(r'(气泡图)', re.IGNORECASE)),
    ('scatter', re.compile(r'(散点图)', re.IGNORECASE)),
    ('heatmap', re.compile(r'(热力图|矩阵图)', re.IGNORECASE)),
    ('radar', re.compile(r'(雷达图)', re.IGNORECASE)),
    ('waterfall', re.compile(r'(瀑布图)', re.IGNORECASE)),
    ('treemap', re.compile(r'(树图|矩形树图|treemap)', re.IGNORECASE)),
    ('boxplot', re.compile(r'(箱线图|箱型图|boxplot)', re.IGNORECASE)),
    ('histogram', re.compile(r'(直方图)', re.IGNORECASE)),
    ('area', re.compile(r'(面积图)', re.IGNORECASE)),
    ('line', re.compile(r'(折线图|趋势图|曲线图)', re.IGNORECASE)),
    ('bar', re.compile(r'(柱状图)', re.IGNORECASE)),
]


def _safe_float(value: Any) -> float | None:
    if value is None or value == '' or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        if math.isnan(parsed) or math.isinf(parsed):
            return None
        return parsed
    text = str(value).strip().replace(',', '')
    if not text:
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _base_label(text: str) -> str:
    cleaned = re.sub(r'[（(【\[].*', '', text or '').strip()
    return cleaned or text or ''


def _field_label_map(columns: list[dict[str, str]]) -> dict[str, str]:
    return {str(item.get('key', '')).strip(): _base_label(str(item.get('label', item.get('key', ''))).strip()) for item in columns}


_NON_METRIC_KEYS = {'rank', 'stock_code', 'id', 'index', 'row_num', 'no'}


def _is_numeric_field(rows: list[dict[str, Any]], key: str) -> bool:
    low = key.lower()
    if low in _NON_METRIC_KEYS or low.endswith('_code') or low.endswith('_id'):
        return False
    values = [row.get(key) for row in rows if row.get(key) not in (None, '')]
    if not values:
        return False
    numeric_count = sum(1 for value in values if _safe_float(value) is not None)
    return numeric_count / len(values) >= 0.8


def _looks_like_time_text(value: str) -> bool:
    text = str(value).strip()
    if not text:
        return False
    return bool(re.match(r'^(20\d{2})(Q1|Q2|Q3|Q4|HY|H1|FY)$', text, re.IGNORECASE) or re.match(r'^20\d{2}[-/]\d{1,2}$', text))


def _is_time_field(rows: list[dict[str, Any]], key: str) -> bool:
    lowered = key.lower()
    if lowered in {'report_period', 'date', 'trade_date', 'month', 'year'}:
        return True
    if 'period' in lowered or 'date' in lowered or 'time' in lowered:
        return True
    sample = [str(row.get(key, '')).strip() for row in rows[:8] if row.get(key) not in (None, '')]
    return bool(sample and sum(1 for item in sample if _looks_like_time_text(item)) >= max(1, len(sample) - 1))


def _is_percent_field(key: str, label: str) -> bool:
    text = f'{key} {label}'.lower()
    return '%' in label or any(token in text for token in ('_growth', '_ratio', 'margin', 'yield', 'roe', 'rate'))


def _profile_result(columns: list[dict[str, str]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    label_map = _field_label_map(columns)
    field_keys = [str(item.get('key', '')).strip() for item in columns if str(item.get('key', '')).strip()]
    numeric_fields = [key for key in field_keys if _is_numeric_field(rows, key)]
    time_fields = [key for key in field_keys if key not in numeric_fields and _is_time_field(rows, key)]
    dimension_fields = [key for key in field_keys if key not in numeric_fields]
    category_fields = [key for key in dimension_fields if key not in time_fields]
    return {
        'label_map': label_map,
        'field_keys': field_keys,
        'numeric_fields': numeric_fields,
        'time_fields': time_fields,
        'time_field': time_fields[0] if time_fields else '',
        'dimension_fields': dimension_fields,
        'category_fields': category_fields,
        'category_field': category_fields[0] if category_fields else '',
        'percent_fields': [key for key in numeric_fields if _is_percent_field(key, label_map.get(key, key))],
        'row_count': len(rows),
    }


def _explicit_chart_type(question: str) -> str:
    for chart_type, pattern in _EXPLICIT_CHART_PATTERNS:
        if pattern.search(question):
            return chart_type
    return ''


def _question_contains(question: str, *parts: str) -> bool:
    compact = re.sub(r'\s+', '', question)
    return any(part in compact for part in parts)


def _default_title(chart_type: str, plan: dict[str, Any], profile: dict[str, Any], title: str) -> str:
    if title:
        return title
    y_fields = [field for field in (plan.get('yFields') or []) if field in profile.get('numeric_fields', [])]
    label_map = profile.get('label_map', {})
    if chart_type in {'pie', 'donut', 'radar', 'waterfall'} and y_fields:
        return ' / '.join(label_map.get(field, field) for field in y_fields[:3])
    if y_fields:
        return f"{label_map.get(y_fields[0], y_fields[0])}{_CHART_LABELS.get(chart_type, '图表')}"
    return _CHART_LABELS.get(chart_type, '图表')


def _sanitize_plan(plan: dict[str, Any], profile: dict[str, Any], title: str) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    numeric_fields = profile.get('numeric_fields', [])
    dimension_fields = profile.get('dimension_fields', [])
    category_fields = profile.get('category_fields', [])
    time_field = profile.get('time_field', '')
    raw_type = str(plan.get('chartType') or plan.get('chart_type') or '').strip().lower()
    chart_type = raw_type if raw_type in _CHART_LABELS else ''
    if not chart_type:
        return {}
    raw_y_fields = plan.get('yFields') or plan.get('y_fields') or []
    if isinstance(raw_y_fields, str):
        raw_y_fields = [raw_y_fields]
    y_fields = [str(field).strip() for field in raw_y_fields if str(field).strip() in numeric_fields]
    x_field = str(plan.get('xField') or plan.get('x_field') or '').strip()
    if x_field not in dimension_fields + numeric_fields:
        x_field = ''
    series_field = str(plan.get('seriesField') or plan.get('series_field') or '').strip()
    if series_field not in dimension_fields or series_field == x_field:
        series_field = ''
    if chart_type in {'line', 'area', 'stacked_area', 'bar', 'horizontal_bar', 'grouped_bar', 'stacked_bar', 'dual_axis_combo'}:
        if not x_field:
            if time_field:
                x_field = time_field
            elif category_fields:
                # prefer human-readable label fields (abbr/name) over codes/ids
                _label_pref = [f for f in category_fields if any(k in f.lower() for k in ('abbr', 'name', '简称', '名称'))]
                x_field = _label_pref[0] if _label_pref else category_fields[0]
            elif dimension_fields:
                x_field = dimension_fields[0]
            else:
                x_field = ''
        if chart_type in {'grouped_bar', 'stacked_bar', 'stacked_area'} and len(numeric_fields) == 1 and len(dimension_fields) >= 2 and not series_field:
            series_field = dimension_fields[1] if x_field == dimension_fields[0] and len(dimension_fields) > 1 else dimension_fields[0]
        if not y_fields:
            if chart_type == 'dual_axis_combo' and len(numeric_fields) >= 2:
                y_fields = numeric_fields[:2]
            else:
                y_fields = numeric_fields[: min(3, len(numeric_fields))]
    elif chart_type in {'pie', 'donut'}:
        if not y_fields:
            if len(profile.get('field_keys', [])) == 2 and len(numeric_fields) == 1:
                y_fields = [numeric_fields[0]]
            elif profile.get('row_count', 0) == 1:
                y_fields = numeric_fields[: min(6, len(numeric_fields))]
            elif len(numeric_fields) >= 1:
                y_fields = [numeric_fields[0]]
        if not x_field:
            x_field = category_fields[0] if category_fields else ''
    elif chart_type in {'scatter', 'bubble'}:
        if len(numeric_fields) < 2:
            return {}
        if not x_field or x_field not in numeric_fields:
            x_field = numeric_fields[0]
        y_fields = [field for field in (y_fields or numeric_fields[1:3]) if field in numeric_fields and field != x_field]
        if chart_type == 'scatter' and not y_fields:
            return {}
        if chart_type == 'bubble' and len([x_field] + y_fields) < 3:
            extra = [field for field in numeric_fields if field not in {x_field, *y_fields}]
            y_fields = (y_fields + extra)[:2]
            if len([x_field] + y_fields) < 3:
                return {}
    elif chart_type == 'heatmap':
        if not x_field:
            x_field = time_field or (category_fields[0] if category_fields else '')
        if not series_field:
            if len(category_fields) >= 2:
                series_field = category_fields[1] if x_field == category_fields[0] else category_fields[0]
        if not y_fields:
            if len(numeric_fields) >= 1:
                y_fields = [numeric_fields[0]]
        if not x_field or not y_fields:
            return {}
    elif chart_type in {'radar', 'waterfall'}:
        if not y_fields:
            y_fields = numeric_fields[: min(6, len(numeric_fields))]
        if len(y_fields) < 2:
            return {}
        if not x_field:
            _label_pref = [f for f in category_fields if any(k in f.lower() for k in ('abbr', 'name', '简称', '名称'))]
            x_field = _label_pref[0] if _label_pref else (category_fields[0] if category_fields else '')
    elif chart_type == 'histogram':
        if not y_fields:
            # Prefer percent/rate fields for distribution, then last numeric field
            # (pipeline appends computed columns after raw data columns)
            _pct = profile.get('percent_fields', [])
            y_fields = _pct[-1:] if _pct else numeric_fields[-1:]
        if not y_fields:
            return {}
    elif chart_type == 'treemap':
        if not y_fields:
            y_fields = numeric_fields[:1]
        if not y_fields:
            return {}
        if not x_field:
            x_field = category_fields[0] if category_fields else ''
    if chart_type in {'line', 'area', 'stacked_area', 'bar', 'horizontal_bar', 'grouped_bar', 'stacked_bar', 'dual_axis_combo'} and (not x_field or not y_fields):
        return {}
    if chart_type in {'pie', 'donut'} and not y_fields:
        return {}
    if chart_type == 'treemap' and not x_field and profile.get('row_count', 0) != 1:
        return {}
    return {
        'chartType': chart_type,
        'xField': x_field,
        'yFields': y_fields,
        'seriesField': series_field,
        'title': str(plan.get('title') or '').strip() or _default_title(chart_type, plan, profile, title),
        'reason': str(plan.get('reason') or '').strip(),
    }


_CHART_FALLBACK_MAP: dict[str, list[str]] = {
    'radar': ['bar', 'grouped_bar'],
    'scatter': ['bar', 'grouped_bar'],
    'bubble': ['scatter', 'bar'],
    'heatmap': ['grouped_bar', 'bar'],
    'waterfall': ['bar', 'stacked_bar'],
    'treemap': ['pie', 'bar'],
    'histogram': ['bar', 'horizontal_bar'],
    'donut': ['pie', 'bar'],
    'stacked_area': ['area', 'line'],
    'stacked_bar': ['grouped_bar', 'bar'],
    'grouped_bar': ['bar', 'horizontal_bar'],
    'dual_axis_combo': ['line', 'bar'],
    'pie': ['donut', 'bar'],
}


def _sanitize_with_fallback(chart_type: str, profile: dict[str, Any], title: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Try the requested chart type; if _sanitize_plan rejects it, try fallback types."""
    plan_base = {'chartType': chart_type}
    if extra:
        plan_base.update(extra)
    result = _sanitize_plan(plan_base, profile, title)
    if result:
        return result
    for alt in _CHART_FALLBACK_MAP.get(chart_type, []):
        result = _sanitize_plan({'chartType': alt}, profile, title)
        if result:
            return result
    return {}


def _heuristic_plan(question: str, profile: dict[str, Any], title: str, analysis_mode: str) -> dict[str, Any]:
    numeric_fields = profile.get('numeric_fields', [])
    category_fields = profile.get('category_fields', [])
    dimension_fields = profile.get('dimension_fields', [])
    time_field = profile.get('time_field', '')
    percent_fields = profile.get('percent_fields', [])
    row_count = profile.get('row_count', 0)
    # ---- explicit user request (with fallback cascade) ----
    explicit = _explicit_chart_type(question)
    if explicit:
        return _sanitize_with_fallback(explicit, profile, title)
    # ---- keyword-driven ----
    if _question_contains(question, '\u70ed\u529b', '\u77e9\u9635'):
        return _sanitize_with_fallback('heatmap', profile, title)
    if _question_contains(question, '\u76f8\u5173', '\u5173\u7cfb', '\u5173\u8054') and len(numeric_fields) >= 2:
        return _sanitize_with_fallback('scatter', profile, title)
    if _question_contains(question, '\u6c14\u6ce1') and len(numeric_fields) >= 3:
        return _sanitize_with_fallback('bubble', profile, title)
    if _question_contains(question, '\u96f7\u8fbe') and len(numeric_fields) >= 2:
        return _sanitize_with_fallback('radar', profile, title)
    if _question_contains(question, '\u7011\u5e03', '\u6865\u63a5', '\u62c6\u89e3', '\u8d21\u732e') and len(numeric_fields) >= 2:
        return _sanitize_with_fallback('waterfall', profile, title)
    if _question_contains(question, '\u6811\u56fe', '\u5c42\u7ea7'):
        return _sanitize_with_fallback('treemap', profile, title)
    if _question_contains(question, '\u5206\u5e03', '\u76f4\u65b9') and len(numeric_fields) >= 1 and row_count >= 6:
        return _sanitize_with_fallback('histogram', profile, title)
    if _question_contains(question, '\u7ed3\u6784', '\u6784\u6210', '\u5360\u6bd4'):
        if time_field and len(numeric_fields) >= 2 and row_count >= 2:
            return _sanitize_with_fallback('stacked_area', profile, title)
        if len(numeric_fields) >= 2:
            return _sanitize_with_fallback('donut', profile, title)
    # ---- data-shape driven (smarter defaults) ----
    if analysis_mode == 'trend':
        if time_field and len(numeric_fields) >= 2 and percent_fields and any(f not in percent_fields for f in numeric_fields):
            return _sanitize_with_fallback('dual_axis_combo', profile, title, {'yFields': numeric_fields[:2]})
        if time_field and len(numeric_fields) >= 2:
            return _sanitize_plan({'chartType': 'line', 'yFields': numeric_fields[:min(3, len(numeric_fields))]}, profile, title) or {}
        if time_field and numeric_fields:
            return _sanitize_plan({'chartType': 'area', 'yFields': numeric_fields[:1]}, profile, title) or _sanitize_plan({'chartType': 'line', 'yFields': numeric_fields[:1]}, profile, title) or {}
    if analysis_mode == 'ranking':
        if row_count >= 6:
            return _sanitize_with_fallback('horizontal_bar', profile, title)
        return _sanitize_with_fallback('bar', profile, title)
    # single-row with many metrics: radar (>=3) or donut (>=2)
    if row_count == 1 and len(numeric_fields) >= 3:
        return _sanitize_with_fallback('radar', profile, title, {'yFields': numeric_fields[:min(6, len(numeric_fields))]})
    if row_count == 1 and len(numeric_fields) >= 2:
        return _sanitize_with_fallback('donut', profile, title, {'yFields': numeric_fields[:min(6, len(numeric_fields))]})
    # multi-dimension cross: heatmap or grouped_bar
    if len(dimension_fields) >= 2 and len(numeric_fields) >= 1 and row_count >= 4:
        return _sanitize_with_fallback('heatmap', profile, title)
    # percent + absolute mixed: dual-axis combo
    if time_field and len(numeric_fields) >= 2 and percent_fields and any(f not in percent_fields for f in numeric_fields):
        return _sanitize_with_fallback('dual_axis_combo', profile, title, {'yFields': numeric_fields[:2]})
    # multi-metric time series
    if len(numeric_fields) >= 2 and row_count >= 3:
        if time_field:
            return _sanitize_plan({'chartType': 'line', 'yFields': numeric_fields[:min(3, len(numeric_fields))]}, profile, title) or {}
        if category_fields:
            return _sanitize_plan({'chartType': 'grouped_bar', 'xField': category_fields[0], 'yFields': numeric_fields[:min(3, len(numeric_fields))]}, profile, title) or {}
        return _sanitize_with_fallback('scatter', profile, title)
    # many rows + 1 numeric: pie for small set, horizontal_bar for large
    if category_fields and numeric_fields:
        if row_count <= 8 and row_count >= 3 and len(numeric_fields) == 1:
            plan = _sanitize_plan({'chartType': 'pie', 'xField': category_fields[0], 'yFields': numeric_fields[:1]}, profile, title)
            if plan:
                return plan
        target_type = 'horizontal_bar' if row_count >= 6 else 'bar'
        return _sanitize_plan({'chartType': target_type, 'xField': category_fields[0], 'yFields': numeric_fields[:1]}, profile, title) or {}
    if numeric_fields:
        return _sanitize_plan({'chartType': 'bar', 'yFields': numeric_fields[:1]}, profile, title) or {}
    return {}


def _should_use_llm_planner(question: str, profile: dict[str, Any]) -> bool:
    if not llm_available():
        return False
    # Always use LLM planner when available for better chart diversity
    return True


def _sample_rows(rows: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items()} for row in rows[:limit]]


def _plan_with_llm(question: str, columns: list[dict[str, str]], rows: list[dict[str, Any]], profile: dict[str, Any], title: str, analysis_mode: str) -> dict[str, Any]:
    if not _should_use_llm_planner(question, profile):
        return {}
    field_briefs = [
        {
            'key': key,
            'label': profile.get('label_map', {}).get(key, key),
            'kind': 'numeric' if key in profile.get('numeric_fields', []) else ('time' if key == profile.get('time_field', '') else 'dimension'),
        }
        for key in profile.get('field_keys', [])
    ]
    row_count = len(rows)
    system_prompt = (
        '\u4f60\u662f\u8d22\u52a1\u6570\u636e\u53ef\u89c6\u5316\u89c4\u5212\u5668\u3002\u8bf7\u57fa\u4e8e\u67e5\u8be2\u7ed3\u679c\u5b57\u6bb5\u548c\u7528\u6237\u95ee\u9898\uff0c\u9009\u62e9\u6700\u5408\u9002\u7684\u56fe\u8868\u65b9\u6848\u3002'
        '\u53ea\u80fd\u8f93\u51fa\u7ed9\u5b9a\u652f\u6301\u8303\u56f4\u5185\u7684\u56fe\u8868\u7c7b\u578b\uff0c\u5e76\u4e14\u53ea\u80fd\u4f7f\u7528\u5df2\u6709\u5b57\u6bb5\u3002\u8fd4\u56de JSON\u3002\n'
        '\u91cd\u8981\u89c4\u5219\uff1a\u4e0d\u8981\u603b\u662f\u9009 line \u6216 bar\uff0c\u8bf7\u6839\u636e\u6570\u636e\u5f62\u6001\u9009\u62e9\u6700\u4f73\u56fe\u8868\uff1a\n'
        '- 1\u884c\u591a\u6307\u6807(>=3): \u4f18\u5148 radar \u6216 donut\n'
        '- \u5360\u6bd4/\u7ed3\u6784/\u6784\u6210: pie \u6216 donut\n'
        '- \u6392\u540d/\u524d\u51e0\u540d(>=6\u884c): horizontal_bar\n'
        '- \u591a\u7ef4\u5ea6\u4ea4\u53c9: heatmap \u6216 grouped_bar\n'
        '- \u5206\u5e03/\u7edf\u8ba1: histogram\n'
        '- \u767e\u5206\u6bd4+\u7edd\u5bf9\u503c\u6df7\u5408: dual_axis_combo\n'
        '- \u65f6\u95f4\u5e8f\u5217\u8d8b\u52bf: line \u6216 area\n'
        '- \u5bf9\u6bd4\u5dee\u5f02: grouped_bar \u6216 radar'
    )
    user_prompt = (
        '\u8bf7\u6839\u636e\u4e0b\u9762\u4fe1\u606f\u8fd4\u56de\u4e00\u4e2a JSON \u5bf9\u8c61\u3002\n\n'
        '\u8fd4\u56de\u683c\u5f0f\uff1a'
        '{"chartType":"line|area|stacked_area|bar|horizontal_bar|grouped_bar|stacked_bar|pie|donut|scatter|bubble|heatmap|radar|waterfall|dual_axis_combo|histogram|treemap",'
        '"xField":"\u5b57\u6bb5\u540d\u6216\u7a7a\u5b57\u7b26\u4e32",'
        '"yFields":["\u6570\u503c\u5b57\u6bb51","\u6570\u503c\u5b57\u6bb52"],'
        '"seriesField":"\u5b57\u6bb5\u540d\u6216\u7a7a\u5b57\u7b26\u4e32",'
        '"title":"\u56fe\u8868\u6807\u9898",'
        '"reason":"\u4e00\u53e5\u7b80\u77ed\u7406\u7531"}\n\n'
        '\u8981\u6c42\uff1a\n'
        '1. xField\u3001seriesField\u3001yFields \u53ea\u80fd\u4ece\u7ed9\u5b9a\u5b57\u6bb5\u4e2d\u9009\u62e9\u3002\n'
        '2. yFields \u53ea\u80fd\u9009\u62e9 numeric \u5b57\u6bb5\u3002\n'
        '3. \u5982\u679c\u7528\u6237\u660e\u663e\u6307\u5b9a\u4e86\u67d0\u79cd\u56fe\u8868\uff0c\u4e14\u6570\u636e\u80fd\u652f\u6491\uff0c\u5c31\u4f18\u5148\u9009\u62e9\u3002\n'
        '4. \u907f\u514d\u603b\u662f\u9009 line/bar\uff0c\u5145\u5206\u5229\u7528\u56fe\u8868\u7c7b\u578b\u591a\u6837\u6027\u3002\n'
        '5. \u4ec5\u8fd4\u56de JSON\u3002\n\n'
        f'\u5206\u6790\u6a21\u5f0f\uff1a{analysis_mode}\n'
        f'\u884c\u6570\uff1a{row_count}\n'
        f'\u5019\u9009\u6807\u9898\uff1a{title}\n'
        f'\u5b57\u6bb5\uff1a{json.dumps(field_briefs, ensure_ascii=False)}\n'
        f'\u6837\u4f8b\u6570\u636e\uff1a{json.dumps(_sample_rows(rows), ensure_ascii=False)}\n'
        f'\u7528\u6237\u95ee\u9898\uff1a{question}\n'
    )
    try:
        payload = request_llm_json(system_prompt, user_prompt, temperature=llm_temperature('query_plan_temperature', 0.1))
    except Exception:
        return {}
    return _sanitize_plan(payload, profile, title)


def _base_title(title: str, left: str = 'center') -> dict[str, Any]:
    return {'text': title, 'textStyle': {'fontSize': 14, 'fontWeight': 600, 'color': '#1f2937'}, 'left': left, 'top': 8}


def _axis_formatter(values: list[float], *, percent: bool = False) -> dict[str, Any]:
    if percent:
        return {'axisLabel': {'formatter': '{value}%'}, 'name': '（%）', 'nameTextStyle': {'fontSize': 11, 'color': '#9ca3af'}}
    max_v = max((abs(value) for value in values if value is not None), default=0)
    if max_v >= 1e8:
        return {'axisLabel': {'formatter': '{value}'}, 'name': '（亿元）', 'nameTextStyle': {'fontSize': 11, 'color': '#9ca3af'}}
    if max_v >= 1e4:
        return {'axisLabel': {'formatter': '{value}'}, 'name': '（万元）', 'nameTextStyle': {'fontSize': 11, 'color': '#9ca3af'}}
    return {}


def _build_payload(title: str, chart_type: str, option: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    return {
        'title': title,
        'typeLabel': _CHART_LABELS.get(chart_type, '图表'),
        'option': option,
        'spec': plan,
        'reason': plan.get('reason', ''),
    }


def _series_map_from_rows(rows: list[dict[str, Any]], x_field: str, y_fields: list[str], series_field: str, label_map: dict[str, str]) -> tuple[list[str], list[dict[str, Any]]]:
    if series_field and len(y_fields) == 1:
        x_values: list[str] = []
        series_names: list[str] = []
        bucket: dict[str, dict[str, float | None]] = defaultdict(dict)
        for row in rows:
            x_value = str(row.get(x_field, '')).strip()
            series_name = str(row.get(series_field, '')).strip() or label_map.get(series_field, series_field)
            if x_value and x_value not in x_values:
                x_values.append(x_value)
            if series_name not in series_names:
                series_names.append(series_name)
            bucket[series_name][x_value] = _safe_float(row.get(y_fields[0]))
        series = [
            {
                'name': name,
                'data': [bucket[name].get(x_value) for x_value in x_values],
                'field': y_fields[0],
            }
            for name in series_names
        ]
        return x_values, series
    if len(rows) == 1 and len(y_fields) >= 2:
        row = rows[0]
        categories = [label_map.get(field, field) for field in y_fields]
        series = [{'name': label_map.get(y_fields[0], y_fields[0]), 'data': [_safe_float(row.get(field)) for field in y_fields], 'field': y_fields[0]}]
        return categories, series
    categories = [str(row.get(x_field, '')).strip() for row in rows]
    series = [
        {
            'name': label_map.get(field, field),
            'data': [_safe_float(row.get(field)) for row in rows],
            'field': field,
        }
        for field in y_fields
    ]
    return categories, series


def _render_line_like(plan: dict[str, Any], rows: list[dict[str, Any]], label_map: dict[str, str], *, area: bool = False, stacked: bool = False) -> dict[str, Any] | None:
    categories, series_items = _series_map_from_rows(rows, plan['xField'], plan['yFields'], plan.get('seriesField', ''), label_map)
    if not categories or not series_items:
        return None
    values = [value for item in series_items for value in item['data'] if value is not None]
    option: dict[str, Any] = {
        'title': _base_title(plan['title'], 'center'),
        'tooltip': {'trigger': 'axis'},
        'legend': {'top': 32},
        'grid': {'left': 20, 'right': 24, 'top': 68, 'bottom': 50 if len(categories) <= 8 else 74, 'containLabel': True},
        'xAxis': {'type': 'category', 'data': categories, 'axisLabel': {'rotate': 30 if len(categories) > 6 else 0}},
        'yAxis': dict({'type': 'value'}, **_axis_formatter(values, percent=all(_is_percent_field(item.get('field', ''), item['name']) for item in series_items))),
        'series': [],
        'color': _SERIES_COLORS,
    }
    for index, item in enumerate(series_items):
        color = _SERIES_COLORS[index % len(_SERIES_COLORS)]
        series: dict[str, Any] = {
            'name': item['name'],
            'type': 'line',
            'data': item['data'],
            'smooth': True,
            'symbolSize': 7,
            'lineStyle': {'width': 2.5, 'color': color},
            'itemStyle': {'color': color},
        }
        if area:
            series['areaStyle'] = {'opacity': 0.18}
        if stacked:
            series['stack'] = 'total'
        option['series'].append(series)
    if len(categories) > 8:
        option['dataZoom'] = [
            {'type': 'inside', 'start': max(0, 100 - int(8 / max(len(categories), 1) * 100)), 'end': 100},
            {'type': 'slider', 'start': max(0, 100 - int(8 / max(len(categories), 1) * 100)), 'end': 100, 'height': 20, 'bottom': 8},
        ]
    return _build_payload(plan['title'], plan['chartType'], option, plan)


def _render_bar_like(plan: dict[str, Any], rows: list[dict[str, Any]], label_map: dict[str, str], *, horizontal: bool = False, stacked: bool = False) -> dict[str, Any] | None:
    categories, series_items = _series_map_from_rows(rows, plan['xField'], plan['yFields'], plan.get('seriesField', ''), label_map)
    if not categories or not series_items:
        return None
    values = [value for item in series_items for value in item['data'] if value is not None]
    category_axis = {'type': 'category', 'data': categories, 'axisLabel': {'rotate': 30 if len(categories) > 6 and not horizontal else 0}}
    value_axis = dict({'type': 'value'}, **_axis_formatter(values, percent=all(_is_percent_field(item.get('field', ''), item['name']) for item in series_items)))
    option: dict[str, Any] = {
        'title': _base_title(plan['title'], 'center'),
        'tooltip': {'trigger': 'axis', 'axisPointer': {'type': 'shadow'}},
        'legend': {'top': 32},
        'grid': {'left': 24, 'right': 24, 'top': 68, 'bottom': 52, 'containLabel': True},
        'xAxis': value_axis if horizontal else category_axis,
        'yAxis': category_axis if horizontal else value_axis,
        'series': [],
        'color': _SERIES_COLORS,
    }
    for index, item in enumerate(series_items):
        color = _SERIES_COLORS[index % len(_SERIES_COLORS)]
        series: dict[str, Any] = {
            'name': item['name'],
            'type': 'bar',
            'data': item['data'],
            'itemStyle': {'color': color, 'borderRadius': [4, 4, 0, 0] if not horizontal else [0, 4, 4, 0]},
            'label': {'show': True, 'position': 'right' if horizontal else 'top', 'fontSize': 11, 'color': '#374151'},
        }
        if stacked:
            series['stack'] = 'total'
        option['series'].append(series)
    return _build_payload(plan['title'], plan['chartType'], option, plan)


def _render_pie_like(plan: dict[str, Any], rows: list[dict[str, Any]], label_map: dict[str, str], *, donut: bool = False) -> dict[str, Any] | None:
    items: list[dict[str, Any]] = []
    if len(rows) == 1 and len(plan['yFields']) >= 2:
        row = rows[0]
        items = [
            {'name': label_map.get(field, field), 'value': abs(_safe_float(row.get(field)) or 0)}
            for field in plan['yFields']
            if _safe_float(row.get(field)) is not None
        ]
    elif plan.get('xField') and len(plan['yFields']) == 1:
        metric_field = plan['yFields'][0]
        items = [
            {'name': str(row.get(plan['xField'], '')).strip() or label_map.get(plan['xField'], plan['xField']), 'value': abs(_safe_float(row.get(metric_field)) or 0)}
            for row in rows
            if _safe_float(row.get(metric_field)) is not None
        ]
    if not items:
        return None
    option = {
        'title': _base_title(plan['title'], 'center'),
        'tooltip': {'trigger': 'item', 'formatter': '{b}: {c} ({d}%)'},
        'legend': {'orient': 'vertical', 'right': 16, 'top': 'center'},
        'color': _SERIES_COLORS,
        'series': [{
            'name': plan['title'],
            'type': 'pie',
            'radius': ['42%', '68%'] if donut else ['0%', '68%'],
            'center': ['42%', '55%'],
            'data': items,
            'itemStyle': {'borderRadius': 6, 'borderWidth': 2, 'borderColor': '#fff'},
            'label': {'show': True, 'formatter': '{b}\n{d}%'}
        }]
    }
    return _build_payload(plan['title'], plan['chartType'], option, plan)


def _render_scatter_like(plan: dict[str, Any], rows: list[dict[str, Any]], label_map: dict[str, str], *, bubble: bool = False) -> dict[str, Any] | None:
    numeric_fields = [plan['xField']] + [field for field in plan['yFields'] if field != plan['xField']]
    if len(numeric_fields) < (3 if bubble else 2):
        return None
    x_field = numeric_fields[0]
    y_field = numeric_fields[1]
    size_field = numeric_fields[2] if bubble and len(numeric_fields) >= 3 else ''
    label_field = next((key for key in rows[0].keys() if key not in numeric_fields), '') if rows else ''
    data = []
    sizes = []
    for row in rows:
        x_val = _safe_float(row.get(x_field))
        y_val = _safe_float(row.get(y_field))
        if x_val is None or y_val is None:
            continue
        size_val = _safe_float(row.get(size_field)) if size_field else None
        if size_val is not None:
            sizes.append(abs(size_val))
        point = [x_val, y_val]
        if size_field:
            point.append(abs(size_val or 0))
        point.append(str(row.get(label_field, '')).strip() if label_field else '')
        data.append(point)
    if not data:
        return None
    max_size = max(sizes or [1])
    option = {
        'title': _base_title(plan['title'], 'center'),
        'tooltip': {
            'trigger': 'item',
            'formatter': '{a}<br/>' + f"{label_map.get(x_field, x_field)}: {{c[0]}}<br/>{label_map.get(y_field, y_field)}: {{c[1]}}",
        },
        'grid': {'left': 24, 'right': 24, 'top': 56, 'bottom': 32, 'containLabel': True},
        'xAxis': {'type': 'value', 'name': label_map.get(x_field, x_field)},
        'yAxis': {'type': 'value', 'name': label_map.get(y_field, y_field)},
        'series': [{
            'name': plan['title'],
            'type': 'scatter',
            'data': data,
            'symbolSize': (lambda value: max(10, min(42, 12 + (value[2] / max_size * 24)))) if bubble and size_field else 14,
            'itemStyle': {'color': '#6366f1', 'opacity': 0.82},
            'label': {'show': False},
        }]
    }
    return _build_payload(plan['title'], plan['chartType'], option, plan)


def _render_heatmap(plan: dict[str, Any], rows: list[dict[str, Any]], label_map: dict[str, str]) -> dict[str, Any] | None:
    metric_field = plan['yFields'][0]
    x_field = plan['xField']
    series_field = plan.get('seriesField', '')
    data: list[list[Any]] = []
    x_labels: list[str] = []
    y_labels: list[str] = []
    if series_field:
        for row in rows:
            x_value = str(row.get(x_field, '')).strip()
            y_value = str(row.get(series_field, '')).strip()
            value = _safe_float(row.get(metric_field))
            if not x_value or not y_value or value is None:
                continue
            if x_value not in x_labels:
                x_labels.append(x_value)
            if y_value not in y_labels:
                y_labels.append(y_value)
            data.append([x_value, y_value, value])
    else:
        for row in rows:
            x_value = str(row.get(x_field, '')).strip()
            if not x_value:
                continue
            if x_value not in x_labels:
                x_labels.append(x_value)
            for field in plan['yFields']:
                value = _safe_float(row.get(field))
                y_value = label_map.get(field, field)
                if value is None:
                    continue
                if y_value not in y_labels:
                    y_labels.append(y_value)
                data.append([x_value, y_value, value])
    if not data or not x_labels or not y_labels:
        return None
    x_index = {label: idx for idx, label in enumerate(x_labels)}
    y_index = {label: idx for idx, label in enumerate(y_labels)}
    encoded = [[x_index[item[0]], y_index[item[1]], item[2]] for item in data]
    values = [item[2] for item in encoded]
    option = {
        'title': _base_title(plan['title'], 'center'),
        'tooltip': {'position': 'top'},
        'grid': {'left': 48, 'right': 24, 'top': 60, 'bottom': 40, 'containLabel': True},
        'xAxis': {'type': 'category', 'data': x_labels, 'splitArea': {'show': True}},
        'yAxis': {'type': 'category', 'data': y_labels, 'splitArea': {'show': True}},
        'visualMap': {
            'min': min(values),
            'max': max(values),
            'calculable': True,
            'orient': 'horizontal',
            'left': 'center',
            'bottom': 4,
            'inRange': {'color': ['#e0f2fe', '#60a5fa', '#6366f1']},
        },
        'series': [{
            'name': label_map.get(metric_field, metric_field),
            'type': 'heatmap',
            'data': encoded,
            'label': {'show': len(encoded) <= 36},
            'emphasis': {'itemStyle': {'shadowBlur': 10, 'shadowColor': 'rgba(0, 0, 0, 0.28)'}},
        }]
    }
    return _build_payload(plan['title'], plan['chartType'], option, plan)


def _render_radar(plan: dict[str, Any], rows: list[dict[str, Any]], label_map: dict[str, str]) -> dict[str, Any] | None:
    indicators = [{'name': label_map.get(field, field), 'max': 1} for field in plan['yFields']]
    series_data: list[dict[str, Any]] = []
    values_flat: list[float] = []
    if len(rows) == 1:
        row = rows[0]
        values = [_safe_float(row.get(field)) or 0 for field in plan['yFields']]
        values_flat.extend(values)
        series_data.append({'value': values, 'name': plan['title']})
    else:
        _non_y_keys = [key for key in rows[0].keys() if key not in plan['yFields']]
        _label_keys = [k for k in _non_y_keys if any(s in k.lower() for s in ('abbr', 'name', '简称', '名称'))]
        name_field = plan.get('xField') or (_label_keys[0] if _label_keys else (_non_y_keys[0] if _non_y_keys else ''))
        for row in rows[:6]:
            values = [_safe_float(row.get(field)) or 0 for field in plan['yFields']]
            values_flat.extend(values)
            series_data.append({'value': values, 'name': str(row.get(name_field, '')).strip() or plan['title']})
    if not series_data:
        return None
    max_val = max(values_flat or [1])
    for indicator in indicators:
        indicator['max'] = max(max_val * 1.15, 1)
    option = {
        'title': _base_title(plan['title'], 'center'),
        'tooltip': {'trigger': 'item'},
        'legend': {'top': 32},
        'radar': {'indicator': indicators, 'radius': '62%'},
        'series': [{
            'type': 'radar',
            'data': series_data,
            'areaStyle': {'opacity': 0.12},
        }],
        'color': _SERIES_COLORS,
    }
    return _build_payload(plan['title'], plan['chartType'], option, plan)


def _render_waterfall(plan: dict[str, Any], rows: list[dict[str, Any]], label_map: dict[str, str]) -> dict[str, Any] | None:
    row = rows[0] if rows else {}
    labels = [label_map.get(field, field) for field in plan['yFields']]
    values = [_safe_float(row.get(field)) for field in plan['yFields']]
    if not labels or not any(value is not None for value in values):
        return None
    base: list[Any] = []
    increase: list[Any] = []
    decrease: list[Any] = []
    running = 0.0
    actual_values: list[float] = []
    for value in values:
        current = float(value or 0)
        actual_values.append(current)
        if current >= 0:
            base.append(running)
            increase.append(current)
            decrease.append('-')
            running += current
        else:
            base.append(running + current)
            increase.append('-')
            decrease.append(abs(current))
            running += current
    option = {
        'title': _base_title(plan['title'], 'center'),
        'tooltip': {'trigger': 'axis', 'axisPointer': {'type': 'shadow'}},
        'legend': {'top': 32, 'data': ['增加', '减少']},
        'grid': {'left': 24, 'right': 24, 'top': 68, 'bottom': 52, 'containLabel': True},
        'xAxis': {'type': 'category', 'data': labels, 'axisLabel': {'rotate': 24 if len(labels) > 4 else 0}},
        'yAxis': dict({'type': 'value'}, **_axis_formatter(actual_values)),
        'series': [
            {'type': 'bar', 'stack': 'total', 'itemStyle': {'color': 'rgba(0,0,0,0)'}, 'emphasis': {'itemStyle': {'color': 'rgba(0,0,0,0)'}}, 'data': base},
            {'name': '增加', 'type': 'bar', 'stack': 'total', 'data': increase, 'itemStyle': {'color': '#10b981'}},
            {'name': '减少', 'type': 'bar', 'stack': 'total', 'data': decrease, 'itemStyle': {'color': '#ef4444'}},
        ]
    }
    return _build_payload(plan['title'], plan['chartType'], option, plan)


def _render_dual_axis_combo(plan: dict[str, Any], rows: list[dict[str, Any]], label_map: dict[str, str]) -> dict[str, Any] | None:
    categories = [str(row.get(plan['xField'], '')).strip() for row in rows]
    y_fields = plan['yFields'][:2]
    if len(categories) < 2 or len(y_fields) < 2:
        return None
    left_field = next((field for field in y_fields if not _is_percent_field(field, label_map.get(field, field))), y_fields[0])
    right_field = next((field for field in y_fields if field != left_field), y_fields[1])
    left_values = [_safe_float(row.get(left_field)) for row in rows]
    right_values = [_safe_float(row.get(right_field)) for row in rows]
    option: dict[str, Any] = {
        'title': _base_title(plan['title'], 'center'),
        'tooltip': {'trigger': 'axis'},
        'legend': {'top': 32},
        'grid': {'left': 24, 'right': 34, 'top': 68, 'bottom': 52, 'containLabel': True},
        'xAxis': {'type': 'category', 'data': categories, 'axisLabel': {'rotate': 30 if len(categories) > 6 else 0}},
        'yAxis': [
            dict({'type': 'value', 'name': label_map.get(left_field, left_field)}, **_axis_formatter([value for value in left_values if value is not None], percent=_is_percent_field(left_field, label_map.get(left_field, left_field)))),
            dict({'type': 'value', 'name': label_map.get(right_field, right_field)}, **_axis_formatter([value for value in right_values if value is not None], percent=_is_percent_field(right_field, label_map.get(right_field, right_field)))),
        ],
        'series': [
            {'name': label_map.get(left_field, left_field), 'type': 'bar', 'data': left_values, 'itemStyle': {'color': '#6366f1', 'borderRadius': [4, 4, 0, 0]}},
            {'name': label_map.get(right_field, right_field), 'type': 'line', 'yAxisIndex': 1, 'smooth': True, 'symbolSize': 7, 'data': right_values, 'lineStyle': {'width': 2.5, 'color': '#10b981'}, 'itemStyle': {'color': '#10b981'}},
        ]
    }
    return _build_payload(plan['title'], plan['chartType'], option, plan)


def _render_histogram(plan: dict[str, Any], rows: list[dict[str, Any]], label_map: dict[str, str]) -> dict[str, Any] | None:
    field = plan['yFields'][0]
    values = [_safe_float(row.get(field)) for row in rows]
    values = [value for value in values if value is not None]
    if len(values) < 3:
        return None
    min_val = min(values)
    max_val = max(values)
    if min_val == max_val:
        bins = 1
        step = 1
    else:
        bins = min(max(int(math.sqrt(len(values))), 5), 10)
        step = (max_val - min_val) / bins or 1
    counts = [0 for _ in range(bins)]
    labels: list[str] = []
    for index in range(bins):
        start = min_val + index * step
        end = max_val if index == bins - 1 else start + step
        labels.append(f'{start:.2f}~{end:.2f}')
    for value in values:
        if bins == 1:
            idx = 0
        else:
            idx = min(int((value - min_val) / step), bins - 1)
        counts[idx] += 1
    option = {
        'title': _base_title(plan['title'], 'center'),
        'tooltip': {'trigger': 'axis'},
        'grid': {'left': 24, 'right': 24, 'top': 56, 'bottom': 70, 'containLabel': True},
        'xAxis': {'type': 'category', 'data': labels, 'axisLabel': {'rotate': 30 if len(labels) > 6 else 0, 'interval': 0}},
        'yAxis': {'type': 'value', 'name': '频次'},
        'series': [{'name': label_map.get(field, field), 'type': 'bar', 'data': counts, 'itemStyle': {'color': '#6366f1', 'borderRadius': [4, 4, 0, 0]}}],
    }
    return _build_payload(plan['title'], plan['chartType'], option, plan)


def _render_treemap(plan: dict[str, Any], rows: list[dict[str, Any]], label_map: dict[str, str]) -> dict[str, Any] | None:
    value_field = plan['yFields'][0]
    items: list[dict[str, Any]] = []
    if len(rows) == 1 and len(plan['yFields']) >= 2:
        row = rows[0]
        items = [{'name': label_map.get(field, field), 'value': abs(_safe_float(row.get(field)) or 0)} for field in plan['yFields'] if _safe_float(row.get(field)) is not None]
    elif plan.get('xField'):
        for row in rows:
            name = str(row.get(plan['xField'], '')).strip()
            value = _safe_float(row.get(value_field))
            if not name or value is None:
                continue
            items.append({'name': name, 'value': abs(value)})
    if not items:
        return None
    option = {
        'title': _base_title(plan['title'], 'center'),
        'tooltip': {'formatter': '{b}: {c}'},
        'series': [{
            'type': 'treemap',
            'data': items,
            'top': 48,
            'label': {'show': True, 'formatter': '{b}\n{c}'},
            'itemStyle': {'borderColor': '#fff', 'borderWidth': 2, 'gapWidth': 2},
            'levels': [{}, {'color': _SERIES_COLORS}],
        }]
    }
    return _build_payload(plan['title'], plan['chartType'], option, plan)


def _render_plan(plan: dict[str, Any], rows: list[dict[str, Any]], columns: list[dict[str, str]]) -> dict[str, Any] | None:
    label_map = _field_label_map(columns)
    chart_type = plan.get('chartType', '')
    if chart_type == 'line':
        return _render_line_like(plan, rows, label_map)
    if chart_type == 'area':
        return _render_line_like(plan, rows, label_map, area=True)
    if chart_type == 'stacked_area':
        return _render_line_like(plan, rows, label_map, area=True, stacked=True)
    if chart_type == 'bar':
        return _render_bar_like(plan, rows, label_map)
    if chart_type == 'horizontal_bar':
        return _render_bar_like(plan, rows, label_map, horizontal=True)
    if chart_type == 'grouped_bar':
        return _render_bar_like(plan, rows, label_map)
    if chart_type == 'stacked_bar':
        return _render_bar_like(plan, rows, label_map, stacked=True)
    if chart_type == 'pie':
        return _render_pie_like(plan, rows, label_map)
    if chart_type == 'donut':
        return _render_pie_like(plan, rows, label_map, donut=True)
    if chart_type == 'scatter':
        return _render_scatter_like(plan, rows, label_map)
    if chart_type == 'bubble':
        return _render_scatter_like(plan, rows, label_map, bubble=True)
    if chart_type == 'heatmap':
        return _render_heatmap(plan, rows, label_map)
    if chart_type == 'radar':
        return _render_radar(plan, rows, label_map)
    if chart_type == 'waterfall':
        return _render_waterfall(plan, rows, label_map)
    if chart_type == 'dual_axis_combo':
        return _render_dual_axis_combo(plan, rows, label_map)
    if chart_type == 'histogram':
        return _render_histogram(plan, rows, label_map)
    if chart_type == 'treemap':
        return _render_treemap(plan, rows, label_map)
    return None


def auto_generate_chart_payload(
    question: str,
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
    *,
    title: str = '',
    analysis_mode: str = '',
    existing_chart: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not rows or not columns:
        return existing_chart
    profile = _profile_result(columns, rows)
    if not profile.get('numeric_fields'):
        return existing_chart
    # 单行单指标（单个数据点）不画图，无信息量
    if profile.get('row_count', 0) <= 1 and len(profile.get('numeric_fields', [])) <= 1:
        return existing_chart
    explicit = _explicit_chart_type(question)
    if existing_chart and not explicit:
        return existing_chart
    # if existing_chart already has series and its type is compatible with explicit, keep it
    if existing_chart and explicit:
        _ex_opt = existing_chart.get('option', {}) if isinstance(existing_chart, dict) else {}
        _ex_series = _ex_opt.get('series', []) if isinstance(_ex_opt, dict) else []
        _ex_type = str(existing_chart.get('typeLabel', '')) if isinstance(existing_chart, dict) else ''
        _explicit_cn = _CHART_LABELS.get(explicit, '')
        _bar_types = ('横向柱状图', '柱状图', '分组柱状图')
        if len(_ex_series) >= 1 and (
            _ex_type == _explicit_cn
            or (explicit in ('horizontal_bar', 'bar') and _ex_type in _bar_types)
        ):
            return existing_chart
    # bucket data (range/count from pipeline) already IS a distribution — render as bar chart
    if rows and 'range' in rows[0] and 'count' in rows[0] and explicit in ('histogram', None):
        labels = [str(r.get('range', '')) for r in rows]
        counts = [int(r.get('count', 0)) for r in rows]
        option = {
            'title': _base_title(title or '分布统计', 'center'),
            'tooltip': {'trigger': 'axis'},
            'grid': {'left': 24, 'right': 24, 'top': 56, 'bottom': 70, 'containLabel': True},
            'xAxis': {'type': 'category', 'data': labels, 'axisLabel': {'rotate': 30 if len(labels) > 6 else 0, 'interval': 0}},
            'yAxis': {'type': 'value', 'name': '公司数量'},
            'series': [{'name': '公司数量', 'type': 'bar', 'data': counts,
                        'itemStyle': {'color': '#6366f1', 'borderRadius': [4, 4, 0, 0]}}],
        }
        return _build_payload(title or '分布统计', 'histogram', option, {
            'chartType': 'histogram', 'title': title or '分布统计',
            'yFields': ['count'], 'xField': 'range',
        })
    heuristic_plan = _heuristic_plan(question, profile, title, analysis_mode)
    if explicit and heuristic_plan:
        # user explicitly requested a chart type; heuristic honors it, skip LLM
        plan = heuristic_plan
    else:
        llm_plan = _plan_with_llm(question, columns, rows, profile, title, analysis_mode)
        plan = llm_plan or heuristic_plan
    if explicit and not plan:
        plan = _sanitize_plan({'chartType': explicit}, profile, title)
    if not plan:
        return existing_chart
    chart = _render_plan(plan, rows, columns)
    return chart or existing_chart
