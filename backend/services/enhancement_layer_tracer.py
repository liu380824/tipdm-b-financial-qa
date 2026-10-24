"""增强层可视化追踪器

本模块用于展示增强层（Enhancement Layer）的完整处理过程，
通过对比「增强前」和「增强后」的数据，直观呈现增强层为回答增加了哪些维度。

增强层负责将查询层的原始结果（SQL + 数据行 + 知识库命中）转化为：
  ① 自动图表生成        ② LLM 深度分析内容      ③ 数据溯源链
  ④ 归因分析 + 思维导图  ⑤ 计算溯源链           ⑥ 后续建议

使用方法：
    # 方式一：命令行运行
    python -m backend.services.enhancement_layer_tracer

    # 方式二：代码调用
    from backend.services.enhancement_layer_tracer import trace_enhancement
    report = trace_enhancement("2025年第三季度营收前十的公司有哪些？")

    # 方式三：生成 HTML 可视化报告
    from backend.services.enhancement_layer_tracer import trace_enhancement_to_html
    html_path = trace_enhancement_to_html("贵州茅台2025年净利润趋势")
"""
from __future__ import annotations

import json
import time
import copy
import sys
import os
from datetime import datetime
from typing import Any

# ── 保证项目根目录在 sys.path 中 ──────────────────────────────────────────────
_this_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_this_dir, '..', '..'))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ═══════════════════════════════════════════════════════════════════════════════
#  EnhancementLayerTracer — 增强层前后对比追踪器
# ═══════════════════════════════════════════════════════════════════════════════

class EnhancementLayerTracer:
    """拦截 build_assistant_reply 的 emit 回调，分离增强前/后数据，生成对比报告。"""

    def __init__(self):
        self.steps: list[dict[str, Any]] = []
        self.thinking: list[str] = []
        self.charts: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self.content_chunks: list[str] = []
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        # 增强前的原始数据快照
        self._pre_enhancement: dict[str, Any] = {}
        # 增强后的完整数据
        self._post_enhancement: dict[str, Any] = {}
        # 时间戳：各阶段耗时
        self._phase_times: dict[str, float] = {}

    # ── emit 回调 ─────────────────────────────────────────────────────────────
    def _emit_callback(self, event_type: str, data: Any):
        elapsed = round(time.time() - self.start_time, 3)
        if event_type == 'step':
            label = data.get('label', '')
            status = data.get('status', '')
            detail = data.get('detail', '')
            self.steps.append({
                'timestamp': elapsed, 'label': label,
                'status': status, 'detail': detail,
            })
            icon = '✓' if status == 'done' else ('✗' if status == 'error' else '→')
            print(f'  [{elapsed:>7.3f}s] {icon} {label}: {detail}', flush=True)
            # 记录阶段时间
            if status == 'done':
                self._phase_times[label] = elapsed
        elif event_type == 'thinking':
            text = data if isinstance(data, str) else str(data)
            self.thinking.append(text)
            print(f'  [{elapsed:>7.3f}s] 💭 {text}', flush=True)
        elif event_type == 'chart':
            self.charts.append(data)
            title = data.get('title', '未知') if isinstance(data, dict) else '未知'
            print(f'  [{elapsed:>7.3f}s] 📊 图表生成: {title}', flush=True)
        elif event_type == 'table':
            self.tables.append(data)
            row_cnt = len(data.get('rows', [])) if isinstance(data, dict) else 0
            print(f'  [{elapsed:>7.3f}s] 📋 数据表: {row_cnt} 行', flush=True)
        elif event_type == 'content_chunk':
            self.content_chunks.append(data if isinstance(data, str) else str(data))
        else:
            print(f'  [{elapsed:>7.3f}s] 📨 {event_type}', flush=True)

    # ── 主入口 ────────────────────────────────────────────────────────────────
    def run(self, question: str, history: list[dict[str, Any]] | None = None,
            deep_think: bool = True, session_id: str = '') -> dict[str, Any]:
        from .assistant import build_assistant_reply

        self.steps = []
        self.thinking = []
        self.charts = []
        self.tables = []
        self.content_chunks = []
        self._phase_times = {}
        self.start_time = time.time()

        W = 76
        print(f'\n{"═" * W}', flush=True)
        print(f'  增强层可视化追踪器（Enhancement Layer Tracer）', flush=True)
        print(f'  问题: {question}', flush=True)
        print(f'  时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', flush=True)
        print(f'{"═" * W}', flush=True)

        # ── 执行查询 ──────────────────────────────────────────────────────────
        print(f'\n{"─" * W}', flush=True)
        print(f'  ▶ 执行查询流水线', flush=True)
        print(f'{"─" * W}', flush=True)

        reply = build_assistant_reply(
            question, history or [],
            emit=self._emit_callback,
            deep_think=deep_think,
            session_id=session_id,
        )

        self.end_time = time.time()
        elapsed = round(self.end_time - self.start_time, 3)

        # ── 提取增强前/后数据 ─────────────────────────────────────────────────
        self._extract_pre_post(reply)

        # ── 输出增强前 ───────────────────────────────────────────────────────
        pre = self._pre_enhancement
        post = self._post_enhancement
        print(f'\n{"═" * W}', flush=True)
        print(f'  ▼ 增 强 前（查询层原始输出）', flush=True)
        print(f'{"═" * W}', flush=True)
        self._print_pre_enhancement(pre)

        # ── 输出增强过程 ──────────────────────────────────────────────────────
        print(f'\n{"═" * W}', flush=True)
        print(f'  ⚙ 增 强 过 程（Enhancement Pipeline）', flush=True)
        print(f'{"═" * W}', flush=True)
        self._print_enhancement_pipeline(pre, post)

        # ── 输出增强后 ───────────────────────────────────────────────────────
        print(f'\n{"═" * W}', flush=True)
        print(f'  ▲ 增 强 后（最终输出）', flush=True)
        print(f'{"═" * W}', flush=True)
        self._print_post_enhancement(post)

        # ── 增强效果对比 ──────────────────────────────────────────────────────
        print(f'\n{"═" * W}', flush=True)
        print(f'  📊 增强效果对比汇总', flush=True)
        print(f'{"═" * W}', flush=True)
        self._print_comparison_summary(pre, post, elapsed)

        print(f'\n{"═" * W}', flush=True)
        print(f'  增强层追踪完成 | 总耗时: {elapsed:.3f}s', flush=True)
        print(f'{"═" * W}\n', flush=True)

        return {
            'question': question,
            'pre_enhancement': pre,
            'post_enhancement': post,
            'steps': self.steps,
            'thinking': self.thinking,
            'elapsed_seconds': elapsed,
            'reply': reply,
        }

    # ── 从 reply 中分离增强前/后数据 ──────────────────────────────────────────
    def _extract_pre_post(self, reply: dict[str, Any]):
        meta = reply.get('meta', {})
        refs = reply.get('references', [])
        attribution = reply.get('attribution', {})

        # 增强前 = 查询层直接产出的原始数据
        self._pre_enhancement = {
            'status': reply.get('status', ''),
            'meta': meta,
            'sql': reply.get('sql', ''),
            'columns': reply.get('columns', []),
            'rows': reply.get('rows', []),
            'source_tables': reply.get('sourceTables', []),
            'references_raw': refs,
            'chart_basic': bool(reply.get('chart')),
        }

        # 增强后 = 增强层追加的所有维度
        self._post_enhancement = {
            'content': reply.get('content', ''),
            'content_length': len(reply.get('content', '')),
            'chart': reply.get('chart'),
            'chart_title': reply.get('chart', {}).get('title', '') if reply.get('chart') else '',
            'chart_type': reply.get('chart', {}).get('typeLabel', '') if reply.get('chart') else '',
            'chart_series_count': len(reply.get('chart', {}).get('option', {}).get('series', [])) if reply.get('chart') else 0,
            'attribution': attribution,
            'attribution_steps': attribution.get('steps', []),
            'thinking_map': attribution.get('thinking_map', {}),
            'data_summary': attribution.get('data_summary', {}),
            'provenance': reply.get('provenance', []),
            'provenance_count': len(reply.get('provenance', [])),
            'calc_traces': reply.get('calcTraces', []),
            'calc_traces_count': len(reply.get('calcTraces', [])),
            'insights': reply.get('insights', []),
            'follow_up_hints': reply.get('followUpHints', []),
            'citation_refs': reply.get('citationRefs', []),
            'thinking_texts': self.thinking,
            'emit_charts': self.charts,
            'emit_tables': self.tables,
        }

    # ── 打印增强前 ────────────────────────────────────────────────────────────
    def _print_pre_enhancement(self, pre: dict[str, Any]):
        meta = pre['meta']
        rows = pre['rows']
        columns = pre['columns']
        refs = pre['references_raw']
        sql = pre['sql']

        print(f'', flush=True)
        print(f'  【意图识别】', flush=True)
        print(f'    公司:     {meta.get("company", "—") or "—"}', flush=True)
        print(f'    报告期:   {meta.get("report_period", "—") or "—"}', flush=True)
        print(f'    查询类型: {meta.get("query_kind", "—") or "—"}', flush=True)
        print(f'    分析模式: {meta.get("analysis_mode", "—") or "—"}', flush=True)

        print(f'', flush=True)
        print(f'  【原始 SQL】', flush=True)
        if sql:
            for line in sql.strip().split('\n')[:8]:
                print(f'    {line}', flush=True)
        else:
            print(f'    （无 SQL）', flush=True)

        print(f'', flush=True)
        print(f'  【原始数据】 {len(rows)} 行 × {len(columns)} 列', flush=True)
        if columns:
            print(f'    列: {", ".join(c.get("label", c.get("key","")) for c in columns[:8])}', flush=True)
        if rows:
            for r in rows[:3]:
                items = [f'{k}={v}' for k, v in list(r.items())[:5]]
                print(f'    {", ".join(items)}', flush=True)
            if len(rows) > 3:
                print(f'    ...（共 {len(rows)} 行）', flush=True)

        print(f'', flush=True)
        print(f'  【知识库原始命中】 {len(refs)} 条', flush=True)
        for i, ref in enumerate(refs[:3], 1):
            src = ref.get('doc_name', ref.get('source', ''))
            score = ref.get('score', 0)
            print(f'    [{i}] {src} (分数:{score:.4f})', flush=True)

        print(f'', flush=True)
        print(f'  ⚠ 此时尚无：LLM分析内容、归因分析、思维导图、', flush=True)
        print(f'               数据溯源、计算溯源链、自动图表、后续建议', flush=True)

    # ── 打印增强过程 ──────────────────────────────────────────────────────────
    def _print_enhancement_pipeline(self, pre: dict, post: dict):
        pipes = [
            ('① 自动图表生成', 'auto_generate_chart_payload',
             f'输入 {len(pre["rows"])} 行数据 → 输出 {post["chart_type"]} "{post["chart_title"]}"' if post['chart'] else '无数据或无需图表，跳过'),
            ('② LLM 深度分析', '_build_llm_analysis',
             f'输入 SQL+数据+知识库 → 输出 {post["content_length"]} 字分析内容'),
            ('③ 数据溯源查询', 'field_provenance',
             f'查询 field_provenance 表 → 命中 {post["provenance_count"]} 条溯源记录'),
            ('④ 计算溯源链', '_build_calc_traces',
             f'基于溯源记录构建 → {post["calc_traces_count"]} 条计算链'),
            ('⑤ 归因分析构建', 'attribution',
             f'生成 {len(post["attribution_steps"])} 步归因链路'),
            ('⑥ 思维导图构建', '_build_thinking_map',
             f'构建推理树 → {"有" if post["thinking_map"] else "无"}思维导图'),
            ('⑦ 后续问题建议', 'followUpHints',
             f'生成 {len(post["follow_up_hints"])} 条建议'),
        ]
        print(f'', flush=True)
        for label, func, detail in pipes:
            has_output = not ('跳过' in detail or '0 条' in detail or '0 字' in detail)
            icon = '✅' if has_output else '⬜'
            print(f'  {icon} {label}  [{func}]', flush=True)
            print(f'       {detail}', flush=True)

        # 各阶段耗时
        if self._phase_times:
            print(f'', flush=True)
            print(f'  ⏱ 各阶段耗时:', flush=True)
            prev = 0.0
            for label, ts in sorted(self._phase_times.items(), key=lambda x: x[1]):
                delta = ts - prev
                print(f'    {label}: {ts:.3f}s (+{delta:.3f}s)', flush=True)
                prev = ts

    # ── 打印增强后 ────────────────────────────────────────────────────────────
    def _print_post_enhancement(self, post: dict[str, Any]):
        # ① LLM 分析内容
        print(f'', flush=True)
        print(f'  【LLM 分析内容】 {post["content_length"]} 字', flush=True)
        content = post['content']
        if content:
            preview = content[:400]
            for line in preview.split('\n'):
                print(f'    {line}', flush=True)
            if len(content) > 400:
                print(f'    ...（共 {len(content)} 字）', flush=True)
        else:
            print(f'    （无内容）', flush=True)

        # ② 图表
        print(f'', flush=True)
        print(f'  【自动图表】', flush=True)
        if post['chart']:
            print(f'    标题:   {post["chart_title"]}', flush=True)
            print(f'    类型:   {post["chart_type"]}', flush=True)
            print(f'    系列数: {post["chart_series_count"]}', flush=True)
            chart = post['chart']
            series = chart.get('option', {}).get('series', [])
            for si, s in enumerate(series):
                s_name = s.get('name', f'系列{si+1}')
                s_type = s.get('type', '')
                data_len = len(s.get('data', []))
                print(f'      [{si+1}] {s_name} ({s_type}, {data_len}点)', flush=True)
        else:
            print(f'    （无图表）', flush=True)

        # ③ 归因分析
        print(f'', flush=True)
        print(f'  【归因分析】 {len(post["attribution_steps"])} 步', flush=True)
        for s in post['attribution_steps']:
            print(f'    [{s.get("label","")}] {s.get("detail","")}', flush=True)
        ds = post.get('data_summary', {})
        if ds:
            print(f'    数据摘要: 表={ds.get("tables","")}, 行={ds.get("row_count","")}, 列={ds.get("columns","")}', flush=True)

        # ④ 思维导图
        print(f'', flush=True)
        print(f'  【思维导图】', flush=True)
        if post['thinking_map']:
            self._print_tree(post['thinking_map'], indent=4)
        else:
            print(f'    （无思维导图）', flush=True)

        # ⑤ 数据溯源
        print(f'', flush=True)
        print(f'  【数据溯源】 {post["provenance_count"]} 条', flush=True)
        for p in post['provenance'][:5]:
            print(f'    • {p.get("stock_code","")} | {p.get("report_period","")} | '
                  f'{p.get("field_name","")} = {p.get("field_value","")}', flush=True)
        if post['provenance_count'] > 5:
            print(f'    ...（共 {post["provenance_count"]} 条）', flush=True)

        # ⑥ 计算溯源链
        print(f'', flush=True)
        print(f'  【计算溯源链】 {post["calc_traces_count"]} 条', flush=True)
        for ct in post['calc_traces'][:5]:
            print(f'    • {ct.get("label","")} = {ct.get("formula", ct.get("value",""))}', flush=True)
        if post['calc_traces_count'] > 5:
            print(f'    ...（共 {post["calc_traces_count"]} 条）', flush=True)

        # ⑦ 后续建议
        print(f'', flush=True)
        print(f'  【后续问题建议】 {len(post["follow_up_hints"])} 条', flush=True)
        for f in post['follow_up_hints']:
            print(f'    → {f}', flush=True)

        # ⑧ 深度思考
        print(f'', flush=True)
        print(f'  【深度思考过程】 {len(post["thinking_texts"])} 条', flush=True)
        for t in post['thinking_texts']:
            print(f'    💭 {t}', flush=True)

    # ── 对比汇总 ──────────────────────────────────────────────────────────────
    def _print_comparison_summary(self, pre: dict, post: dict, elapsed: float):
        rows = [
            ('维度', '增强前', '增强后', '增量'),
            ('─' * 20, '─' * 18, '─' * 18, '─' * 14),
            ('SQL',
             '有' if pre['sql'] else '无',
             '有' if pre['sql'] else '无',
             '—'),
            ('数据行数',
             str(len(pre['rows'])),
             str(len(pre['rows'])),
             '—'),
            ('知识库命中',
             f'{len(pre["references_raw"])} 条',
             f'{len(pre["references_raw"])} 条',
             '—'),
            ('LLM 分析内容',
             '无',
             f'{post["content_length"]} 字',
             f'+{post["content_length"]} 字'),
            ('自动图表',
             '基础/无',
             f'"{post["chart_title"][:20]}"' if post['chart'] else '无',
             '✅ 生成' if post['chart'] else '—'),
            ('归因分析',
             '无',
             f'{len(post["attribution_steps"])} 步',
             f'+{len(post["attribution_steps"])} 步'),
            ('思维导图',
             '无',
             '有' if post['thinking_map'] else '无',
             '✅ 生成' if post['thinking_map'] else '—'),
            ('数据溯源',
             '无',
             f'{post["provenance_count"]} 条',
             f'+{post["provenance_count"]} 条'),
            ('计算溯源链',
             '无',
             f'{post["calc_traces_count"]} 条',
             f'+{post["calc_traces_count"]} 条'),
            ('深度思考',
             '无',
             f'{len(post["thinking_texts"])} 条',
             f'+{len(post["thinking_texts"])} 条'),
            ('后续建议',
             '无',
             f'{len(post["follow_up_hints"])} 条',
             f'+{len(post["follow_up_hints"])} 条'),
        ]
        print(f'', flush=True)
        for dim, before, after, delta in rows:
            print(f'    {dim:<20s} {before:<18s} {after:<18s} {delta}', flush=True)
        print(f'', flush=True)
        print(f'    总耗时: {elapsed:.3f}s', flush=True)

    # ── 树打印 ────────────────────────────────────────────────────────────────
    def _print_tree(self, node: dict, indent: int = 4):
        if not node:
            return
        prefix = ' ' * indent
        label = node.get('label', '')
        detail = node.get('detail', '')
        edge = node.get('edge_label', '')
        node_type = node.get('type', '')
        icon = {
            'question': '❓', 'process': '⚙️', 'data': '📊',
            'knowledge': '📚', 'source': '📄', 'evidence': '📚',
            'synthesis': '🧠', 'result': '✅', 'conclusion': '✅',
        }.get(node_type, '•')
        edge_str = f'──[{edge}]──▸ ' if edge else ''
        print(f'{prefix}{edge_str}{icon} {label}', flush=True)
        if detail:
            print(f'{prefix}  └ {detail[:120]}{"…" if len(detail) > 120 else ""}', flush=True)
        for child in node.get('children', []):
            self._print_tree(child, indent + 4)


# ═══════════════════════════════════════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════════════════════════════════════

def trace_enhancement(question: str, history: list[dict[str, Any]] | None = None,
                      deep_think: bool = True, session_id: str = '') -> dict[str, Any]:
    """一键追踪增强层前后变化。"""
    tracer = EnhancementLayerTracer()
    return tracer.run(question, history=history, deep_think=deep_think,
                      session_id=session_id)


def trace_enhancement_to_html(question: str, output_path: str | None = None,
                               history: list[dict[str, Any]] | None = None,
                               deep_think: bool = True) -> str:
    """追踪增强层并生成 HTML 可视化对比报告。"""
    report = trace_enhancement(question, history=history, deep_think=deep_think)

    if output_path is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = os.path.join(_project_root, f'enhancement_trace_{ts}.html')

    pre = report['pre_enhancement']
    post = report['post_enhancement']
    reply = report['reply']
    steps = report['steps']
    thinking = report['thinking']
    elapsed = report['elapsed_seconds']
    meta = pre['meta']

    def _esc(s):
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    def _tree_html(node, depth=0):
        if not node:
            return ''
        icon_map = {
            'question': '❓', 'process': '⚙️', 'data': '📊',
            'knowledge': '📚', 'source': '📄', 'evidence': '📚',
            'synthesis': '🧠', 'result': '✅', 'conclusion': '✅',
        }
        icon = icon_map.get(node.get('type', ''), '•')
        label = _esc(node.get('label', ''))
        detail = _esc(node.get('detail', ''))
        edge = _esc(node.get('edge_label', ''))
        h = f'<div class="tm-node" style="margin-left:{depth * 24}px">'
        if edge:
            h += f'<span class="tm-edge">{edge}</span>'
        h += f'<span class="tm-icon">{icon}</span><span class="tm-label">{label}</span>'
        if detail:
            h += f'<div class="tm-detail">{detail[:200]}{"…" if len(detail) > 200 else ""}</div>'
        h += '</div>'
        for child in node.get('children', []):
            h += _tree_html(child, depth + 1)
        return h

    # 步骤 HTML
    steps_html = ''
    for s in steps:
        icon = '✅' if s['status'] == 'done' else '🔄' if s['status'] == 'running' else '❔'
        cls = 'step-done' if s['status'] == 'done' else 'step-running'
        steps_html += f'''<div class="step-item {cls}">
            <span class="step-time">{s["timestamp"]:.3f}s</span>
            <span class="step-icon">{icon}</span>
            <span class="step-label">{_esc(s["label"])}</span>
            <span class="step-detail">{_esc(s["detail"])}</span>
        </div>\n'''

    # 增强前数据表
    pre_table_html = ''
    if pre['rows'] and pre['columns']:
        pre_table_html = '<table class="data-table"><thead><tr>'
        for c in pre['columns']:
            pre_table_html += f'<th>{_esc(c.get("label", c.get("key", "")))}</th>'
        pre_table_html += '</tr></thead><tbody>'
        for r in pre['rows'][:10]:
            pre_table_html += '<tr>'
            for c in pre['columns']:
                val = r.get(c.get('key', ''), '')
                pre_table_html += f'<td>{_esc(str(val))}</td>'
            pre_table_html += '</tr>'
        pre_table_html += '</tbody></table>'
        if len(pre['rows']) > 10:
            pre_table_html += f'<div class="table-note">显示前 10 行，共 {len(pre["rows"])} 行</div>'
    else:
        pre_table_html = '<div class="empty">（无数据返回）</div>'

    # 知识库 HTML
    kb_html = ''
    for i, ref in enumerate(pre['references_raw'][:5], 1):
        src = _esc(ref.get('doc_name', ref.get('source', '')))
        score = ref.get('score', 0)
        snippet = _esc(ref.get('text', ref.get('content', ''))[:120])
        kb_html += f'<div class="ref-item"><b>[{i}]</b> <span class="ref-src">{src}</span> (分数:{score:.4f})<br>{snippet}…</div>\n'
    if not pre['references_raw']:
        kb_html = '<div class="empty">（未命中知识库）</div>'

    # 增强管道 HTML
    pipes = [
        ('① 自动图表生成', post['chart_type'] or '—', '✅' if post['chart'] else '⬜'),
        ('② LLM 深度分析', f'{post["content_length"]} 字', '✅' if post['content_length'] else '⬜'),
        ('③ 数据溯源查询', f'{post["provenance_count"]} 条', '✅' if post['provenance_count'] else '⬜'),
        ('④ 计算溯源链', f'{post["calc_traces_count"]} 条', '✅' if post['calc_traces_count'] else '⬜'),
        ('⑤ 归因分析', f'{len(post["attribution_steps"])} 步', '✅' if post['attribution_steps'] else '⬜'),
        ('⑥ 思维导图', '有' if post['thinking_map'] else '无', '✅' if post['thinking_map'] else '⬜'),
        ('⑦ 后续建议', f'{len(post["follow_up_hints"])} 条', '✅' if post['follow_up_hints'] else '⬜'),
    ]
    pipe_html = ''
    for label, detail, icon in pipes:
        active = icon == '✅'
        cls = 'pipe-active' if active else 'pipe-skip'
        pipe_html += f'<div class="pipe-item {cls}"><span class="pipe-icon">{icon}</span><b>{_esc(label)}</b><span class="pipe-detail">{_esc(detail)}</span></div>\n'

    # 对比表 HTML
    comparison_rows = [
        ('SQL', '有' if pre['sql'] else '无', '有' if pre['sql'] else '无'),
        ('数据行数', str(len(pre['rows'])), str(len(pre['rows']))),
        ('知识库命中', f'{len(pre["references_raw"])} 条', f'{len(pre["references_raw"])} 条'),
        ('LLM 分析内容', '❌ 无', f'✅ {post["content_length"]} 字'),
        ('自动图表', '❌ 无/基础', f'✅ {post["chart_type"]}' if post['chart'] else '❌ 无'),
        ('归因分析', '❌ 无', f'✅ {len(post["attribution_steps"])} 步'),
        ('思维导图', '❌ 无', '✅ 有' if post['thinking_map'] else '❌ 无'),
        ('数据溯源', '❌ 无', f'✅ {post["provenance_count"]} 条'),
        ('计算溯源链', '❌ 无', f'✅ {post["calc_traces_count"]} 条'),
        ('深度思考', '❌ 无', f'✅ {len(post["thinking_texts"])} 条'),
        ('后续建议', '❌ 无', f'✅ {len(post["follow_up_hints"])} 条'),
    ]
    comp_html = '<table class="comp-table"><thead><tr><th>维度</th><th>增强前</th><th>增强后</th></tr></thead><tbody>'
    for dim, before, after in comparison_rows:
        comp_html += f'<tr><td><b>{_esc(dim)}</b></td><td>{_esc(before)}</td><td>{_esc(after)}</td></tr>'
    comp_html += '</tbody></table>'

    # 归因 HTML
    attr_html = ''
    for s in post['attribution_steps']:
        attr_html += f'<div class="attr-step"><b>{_esc(s.get("label",""))}</b>: {_esc(s.get("detail",""))}</div>\n'
    if not post['attribution_steps']:
        attr_html = '<div class="empty">（无归因数据）</div>'

    # 溯源 HTML
    prov_html = ''
    for p in post['provenance'][:8]:
        prov_html += f'<div class="prov-item">• {_esc(p.get("stock_code",""))} | {_esc(p.get("report_period",""))} | {_esc(p.get("field_name",""))} = {_esc(str(p.get("field_value","")))}</div>\n'
    if not post['provenance']:
        prov_html = '<div class="empty">（无溯源记录）</div>'
    elif post['provenance_count'] > 8:
        prov_html += f'<div class="table-note">显示前 8 条，共 {post["provenance_count"]} 条</div>'

    # LLM 内容 HTML
    content = post['content']
    content_html = f'<div class="content-box">{_esc(content[:1000])}{"…" if len(content)>1000 else ""}</div>' if content else '<div class="empty">（无内容）</div>'

    # 后续建议 HTML
    hints_html = ''.join(f'<div class="hint-item">→ {_esc(h)}</div>' for h in post['follow_up_hints']) or '<div class="empty">（无建议）</div>'

    # 深度思考 HTML
    think_html = ''.join(f'<div class="think-item">💭 {_esc(t)}</div>' for t in thinking) or '<div class="empty">（无思考输出）</div>'

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>增强层可视化 — {_esc(question[:30])}</title>
<style>
:root {{
    --blue: #023047; --light-blue: #90C9E6; --yellow: #FFB703;
    --orange: #FB8402; --teal: #219EBD; --gray: #D0D0D0; --dark: #404040;
    --green: #10b981; --red: #ef4444;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; background: #f0f4f8; color: var(--dark); line-height: 1.6; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px 16px; }}
h1 {{ background: linear-gradient(135deg, var(--blue), #035a7a); color: #fff; padding: 20px 28px; border-radius: 12px 12px 0 0; font-size: 20px; }}
h1 small {{ display: block; font-weight: 400; font-size: 13px; color: var(--light-blue); margin-top: 4px; }}
.summary-bar {{ display: flex; gap: 16px; padding: 12px 20px; background: var(--blue); color: #fff; border-radius: 0 0 12px 12px; font-size: 13px; flex-wrap: wrap; }}
.summary-bar b {{ color: var(--yellow); }}
.card {{ background: #fff; border-radius: 10px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,.06); overflow: hidden; }}
.card-header {{ padding: 12px 20px; font-weight: 700; font-size: 15px; border-bottom: 2px solid var(--light-blue); color: var(--blue); display: flex; align-items: center; gap: 8px; }}
.card-header.pre {{ border-bottom-color: var(--orange); color: #b45309; }}
.card-header.enhance {{ border-bottom-color: var(--yellow); color: #92400e; }}
.card-header.post {{ border-bottom-color: var(--green); color: #065f46; }}
.badge {{ background: var(--teal); color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }}
.badge-orange {{ background: var(--orange); }}
.badge-green {{ background: var(--green); }}
.card-body {{ padding: 16px 20px; }}
.question-box {{ background: linear-gradient(135deg, var(--blue), #035a7a); color: #fff; padding: 18px 24px; border-radius: 8px; font-size: 17px; font-weight: 600; }}
/* 步骤 */
.step-item {{ display: flex; align-items: center; gap: 10px; padding: 6px 0; border-left: 3px solid var(--gray); padding-left: 14px; margin-left: 8px; }}
.step-item.step-done {{ border-left-color: var(--green); }}
.step-item.step-running {{ border-left-color: var(--yellow); }}
.step-time {{ font-size: 12px; color: #888; min-width: 56px; font-family: "Consolas", monospace; }}
.step-icon {{ font-size: 16px; }}
.step-label {{ font-weight: 600; min-width: 90px; }}
.step-detail {{ color: #555; font-size: 13px; }}
/* SQL */
.sql-box {{ background: #1e293b; color: #e2e8f0; padding: 16px 20px; border-radius: 8px; font-family: "Consolas", monospace; font-size: 13px; white-space: pre-wrap; word-break: break-all; line-height: 1.8; }}
/* 表格 */
.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.data-table th {{ background: var(--blue); color: #fff; padding: 8px 10px; text-align: left; font-weight: 600; white-space: nowrap; }}
.data-table td {{ padding: 6px 10px; border-bottom: 1px solid #e5e7eb; }}
.data-table tr:hover {{ background: #f0f9ff; }}
.table-note {{ font-size: 12px; color: #888; padding: 6px 0; text-align: right; }}
/* 对比表 */
.comp-table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
.comp-table th {{ background: var(--blue); color: #fff; padding: 10px 14px; text-align: left; }}
.comp-table td {{ padding: 8px 14px; border-bottom: 1px solid #e5e7eb; }}
.comp-table tr:hover {{ background: #f0f9ff; }}
/* 管道 */
.pipe-item {{ display: flex; align-items: center; gap: 10px; padding: 8px 12px; margin-bottom: 4px; border-radius: 6px; font-size: 14px; }}
.pipe-active {{ background: #ecfdf5; border-left: 3px solid var(--green); }}
.pipe-skip {{ background: #fef3c7; border-left: 3px solid #d97706; opacity: 0.7; }}
.pipe-icon {{ font-size: 18px; }}
.pipe-detail {{ color: #555; font-size: 13px; margin-left: auto; }}
/* 知识库 */
.ref-item {{ padding: 8px 0; border-bottom: 1px dashed #e5e7eb; font-size: 13px; }}
.ref-src {{ color: var(--teal); font-weight: 600; }}
/* 归因 */
.attr-step {{ padding: 6px 0; font-size: 14px; border-left: 3px solid var(--green); padding-left: 12px; margin-bottom: 4px; }}
/* 溯源 */
.prov-item {{ padding: 3px 0; font-size: 13px; }}
/* 内容 */
.content-box {{ white-space: pre-wrap; font-size: 14px; line-height: 1.8; color: #333; padding: 12px; background: #f8fafc; border-radius: 6px; }}
/* 思考 */
.think-item {{ padding: 6px 0; font-size: 14px; color: #444; }}
/* 建议 */
.hint-item {{ padding: 4px 0; font-size: 14px; }}
/* 思维导图 */
.tm-node {{ padding: 4px 0; }}
.tm-node:hover {{ background: #f0f9ff; border-radius: 4px; }}
.tm-edge {{ display: inline-block; background: var(--yellow); color: var(--blue); font-size: 11px; padding: 1px 6px; border-radius: 8px; margin-right: 4px; font-weight: 600; }}
.tm-icon {{ font-size: 14px; margin-right: 4px; }}
.tm-label {{ font-weight: 600; color: var(--blue); }}
.tm-detail {{ color: #666; font-size: 12px; margin: 2px 0 4px 28px; line-height: 1.5; }}
.empty {{ color: #999; font-style: italic; padding: 8px 0; }}
.section-label {{ display: inline-block; padding: 2px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; margin-bottom: 8px; }}
.label-pre {{ background: #fff7ed; color: #c2410c; border: 1px solid #fb923c; }}
.label-post {{ background: #ecfdf5; color: #065f46; border: 1px solid #34d399; }}
.two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
@media (max-width: 800px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<div class="container">

<h1>增强层可视化对比报告
    <small>生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} | 耗时: {elapsed:.3f}s | 状态: {_esc(reply.get("status",""))}</small>
</h1>
<div class="summary-bar">
    <span>问题: <b>{_esc(question[:50])}</b></span>
    <span>查询类型: <b>{_esc(meta.get("query_kind","—"))}</b></span>
    <span>数据: <b>{len(pre["rows"])} 行</b></span>
    <span>内容: <b>{post["content_length"]} 字</b></span>
    <span>溯源: <b>{post["provenance_count"]} 条</b></span>
</div>

<!-- 用户输入 -->
<div class="card" style="margin-top:16px;">
    <div class="card-header">用户输入</div>
    <div class="card-body"><div class="question-box">{_esc(question)}</div></div>
</div>

<!-- 执行步骤 -->
<div class="card">
    <div class="card-header">执行步骤时间线 <span class="badge">{len(steps)} 步</span></div>
    <div class="card-body">{steps_html or '<div class="empty">（无步骤）</div>'}</div>
</div>

<!-- ═══ 增强前 ═══ -->
<div class="card">
    <div class="card-header pre">▼ 增强前 — 查询层原始输出 <span class="badge badge-orange">RAW</span></div>
    <div class="card-body">
        <div class="two-col">
            <div>
                <span class="section-label label-pre">意图识别</span>
                <div style="font-size:14px;">
                    <div><b>公司:</b> {_esc(meta.get("company","—") or "—")}</div>
                    <div><b>报告期:</b> {_esc(meta.get("report_period","—") or "—")}</div>
                    <div><b>查询类型:</b> {_esc(meta.get("query_kind","—") or "—")}</div>
                    <div><b>分析模式:</b> {_esc(meta.get("analysis_mode","—") or "—")}</div>
                </div>
            </div>
            <div>
                <span class="section-label label-pre">原始 SQL</span>
                {"<div class='sql-box'>" + _esc(pre["sql"][:500]) + "</div>" if pre["sql"] else '<div class="empty">（无 SQL）</div>'}
            </div>
        </div>
        <div style="margin-top:16px;">
            <span class="section-label label-pre">原始数据 ({len(pre["rows"])} 行 × {len(pre["columns"])} 列)</span>
            {pre_table_html}
        </div>
        <div style="margin-top:16px;">
            <span class="section-label label-pre">知识库原始命中 ({len(pre["references_raw"])} 条)</span>
            {kb_html}
        </div>
        <div style="margin-top:12px; padding:10px; background:#fff7ed; border-radius:6px; font-size:13px; color:#c2410c;">
            ⚠ 此时尚无：LLM分析内容、归因分析、思维导图、数据溯源、计算溯源链、自动图表、后续建议
        </div>
    </div>
</div>

<!-- ═══ 增强过程 ═══ -->
<div class="card">
    <div class="card-header enhance">⚙ 增强管道 — Enhancement Pipeline <span class="badge" style="background:var(--yellow);color:var(--blue);">7 步</span></div>
    <div class="card-body">{pipe_html}</div>
</div>

<!-- ═══ 增强后 ═══ -->
<div class="card">
    <div class="card-header post">▲ 增强后 — 最终输出 <span class="badge badge-green">ENHANCED</span></div>
    <div class="card-body">
        <div class="two-col">
            <div>
                <span class="section-label label-post">LLM 分析内容 ({post["content_length"]} 字)</span>
                {content_html}
            </div>
            <div>
                <span class="section-label label-post">自动图表</span>
                {"<div style='padding:8px;background:#f0f9ff;border-radius:6px;'><b>标题:</b> " + _esc(post["chart_title"]) + "<br><b>类型:</b> " + _esc(post["chart_type"]) + "<br><b>系列:</b> " + str(post["chart_series_count"]) + "</div>" if post["chart"] else '<div class="empty">（无图表）</div>'}
            </div>
        </div>
        <div style="margin-top:16px;">
            <span class="section-label label-post">归因分析 ({len(post["attribution_steps"])} 步)</span>
            {attr_html}
        </div>
        <div style="margin-top:16px;">
            <span class="section-label label-post">思维导图</span>
            {_tree_html(post["thinking_map"]) if post["thinking_map"] else '<div class="empty">（无思维导图）</div>'}
        </div>
        <div class="two-col" style="margin-top:16px;">
            <div>
                <span class="section-label label-post">数据溯源 ({post["provenance_count"]} 条)</span>
                {prov_html}
            </div>
            <div>
                <span class="section-label label-post">深度思考 ({len(thinking)} 条)</span>
                {think_html}
            </div>
        </div>
        <div style="margin-top:16px;">
            <span class="section-label label-post">后续问题建议</span>
            {hints_html}
        </div>
    </div>
</div>

<!-- ═══ 效果对比 ═══ -->
<div class="card">
    <div class="card-header">📊 增强效果对比汇总</div>
    <div class="card-body">{comp_html}</div>
</div>

</div>
</body>
</html>'''

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
#  独立运行入口
# ═══════════════════════════════════════════════════════════════════════════════

DEMO_QUESTIONS = [
    '贵州茅台2025年第三季度净利润是多少？',
    '2025年第三季度营收前十的公司有哪些？',
    '比亚迪和宁德时代2023年研发费用对比',
    '哪些公司2024年ROE超过20%？',
]


def main():
    print('=' * 60)
    print('  增强层可视化追踪器')
    print('=' * 60)

    if len(sys.argv) > 1:
        question = ' '.join(sys.argv[1:])
    else:
        print('\n示例问题:')
        for i, q in enumerate(DEMO_QUESTIONS, 1):
            print(f'  {i}. {q}')
        print()
        user_input = input('请输入问题（或序号，回车使用第1个示例）: ').strip()
        if not user_input:
            question = DEMO_QUESTIONS[0]
        elif user_input.isdigit() and 1 <= int(user_input) <= len(DEMO_QUESTIONS):
            question = DEMO_QUESTIONS[int(user_input) - 1]
        else:
            question = user_input

    print(f'\n▶ 开始追踪增强层: {question}\n')

    html_path = trace_enhancement_to_html(question, deep_think=True)
    print(f'\n📄 HTML 可视化报告已保存至: {html_path}')

    import webbrowser
    webbrowser.open(f'file:///{html_path.replace(os.sep, "/")}')
    print('🌐 已在浏览器中打开')


if __name__ == '__main__':
    main()
