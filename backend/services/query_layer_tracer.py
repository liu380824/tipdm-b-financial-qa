"""查询层中间过程追踪器

本模块用于展示查询层（Query Layer）从用户输入到最终回答的完整中间过程，
包括：预处理、意图识别、知识库检索、SQL 生成与执行、LLM 分析、思维导图构建等。

使用方法：
    # 方式一：作为独立脚本运行
    python -m backend.services.query_layer_tracer

    # 方式二：在代码中调用
    from backend.services.query_layer_tracer import trace_query
    report = trace_query("贵州茅台2024年第三季度净利润是多少？")
    print(report['formatted_text'])

    # 方式三：获取结构化中间数据
    from backend.services.query_layer_tracer import QueryLayerTracer
    tracer = QueryLayerTracer()
    result = tracer.run("营收前十的公司有哪些？")
    for step in result['steps']:
        print(step)
"""
from __future__ import annotations

import json
import time
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
#  QueryLayerTracer — 查询层中间过程追踪器
# ═══════════════════════════════════════════════════════════════════════════════

class QueryLayerTracer:
    """拦截 build_assistant_reply 的 emit 回调，完整记录查询层各阶段中间数据。"""

    def __init__(self):
        self.steps: list[dict[str, Any]] = []
        self.thinking: list[str] = []
        self.charts: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self.content_chunks: list[str] = []
        self.start_time: float = 0.0
        self.end_time: float = 0.0

    # ── emit 回调：由 build_assistant_reply 内部各层调用 ──────────────────────
    def _emit_callback(self, event_type: str, data: Any):
        elapsed = round(time.time() - self.start_time, 3)
        if event_type == 'step':
            label = data.get('label', '')
            status = data.get('status', '')
            detail = data.get('detail', '')
            self.steps.append({
                'timestamp': elapsed,
                'label': label,
                'status': status,
                'detail': detail,
            })
            # ── 实时打印到后台控制台 ──
            icon = '✓' if status == 'done' else ('✗' if status == 'error' else '→')
            print(f'  [{elapsed:>7.3f}s] {icon} {label}: {detail}', flush=True)
        elif event_type == 'thinking':
            text = data if isinstance(data, str) else str(data)
            self.thinking.append(text)
            print(f'  [{elapsed:>7.3f}s] 💭 {text}', flush=True)
        elif event_type == 'chart':
            self.charts.append(data)
            title = data.get('title', '未知') if isinstance(data, dict) else '未知'
            type_label = data.get('typeLabel', '') if isinstance(data, dict) else ''
            print(f'  [{elapsed:>7.3f}s] 📊 图表生成: {title} ({type_label})', flush=True)
        elif event_type == 'table':
            self.tables.append(data)
            row_cnt = len(data.get('rows', [])) if isinstance(data, dict) else 0
            col_cnt = len(data.get('columns', [])) if isinstance(data, dict) else 0
            print(f'  [{elapsed:>7.3f}s] 📋 数据表推送: {row_cnt} 行 × {col_cnt} 列', flush=True)
        elif event_type == 'content_chunk':
            text = data if isinstance(data, str) else str(data)
            self.content_chunks.append(text)
        elif event_type == 'done':
            print(f'  [{elapsed:>7.3f}s] ✅ 流程结束', flush=True)
        elif event_type == 'error':
            print(f'  [{elapsed:>7.3f}s] ❌ 错误: {data}', flush=True)
        else:
            print(f'  [{elapsed:>7.3f}s] 📨 事件[{event_type}]', flush=True)

    # ── 主入口 ────────────────────────────────────────────────────────────────
    def run(self, question: str, history: list[dict[str, Any]] | None = None,
            deep_think: bool = True, session_id: str = '') -> dict[str, Any]:
        """执行查询并收集中间过程。

        Returns:
            dict 包含:
                - question: 原始问题
                - steps: 各阶段步骤列表 [{timestamp, label, status, detail}, ...]
                - thinking: 思考过程文本列表
                - reply: build_assistant_reply 的完整返回值
                - elapsed_seconds: 总耗时
                - formatted_text: 可直接打印的格式化中间过程报告
        """
        from .assistant import build_assistant_reply

        self.steps = []
        self.thinking = []
        self.charts = []
        self.tables = []
        self.content_chunks = []
        self.start_time = time.time()

        print(f'\n{"═"*72}', flush=True)
        print(f'  查询层中间过程追踪（完整流程输出）', flush=True)
        print(f'  问题: {question}', flush=True)
        print(f'  开始时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', flush=True)
        print(f'{"═"*72}', flush=True)
        print(f'', flush=True)
        print(f'{"─"*72}', flush=True)
        print(f'  ▶ 阶段一：实时执行追踪', flush=True)
        print(f'{"─"*72}', flush=True)

        reply = build_assistant_reply(
            question,
            history or [],
            emit=self._emit_callback,
            deep_think=deep_think,
            session_id=session_id,
        )

        self.end_time = time.time()
        elapsed = round(self.end_time - self.start_time, 3)

        # ── 完整流程输出 ──────────────────────────────────────────────────────
        meta = reply.get('meta', {})
        rows = reply.get('rows', [])
        columns = reply.get('columns', [])
        refs = reply.get('references', [])
        provenance = reply.get('provenance', [])
        attribution = reply.get('attribution', {})
        thinking_map = attribution.get('thinking_map', {})
        chart = reply.get('chart')
        content = reply.get('content', '')
        follow_ups = reply.get('followUpHints', [])
        sql = reply.get('sql', '')
        insights = reply.get('insights', [])
        source_tables = reply.get('sourceTables', [])
        calc_traces = reply.get('calcTraces', [])

        print(f'', flush=True)
        print(f'{"─"*72}', flush=True)
        print(f'  ▶ 阶段二：查询层完整结果', flush=True)
        print(f'{"─"*72}', flush=True)

        # ① 基本信息
        print(f'', flush=True)
        print(f'  【1. 基本信息】', flush=True)
        print(f'    状态:     {reply.get("status", "unknown")}', flush=True)
        print(f'    总耗时:   {elapsed:.3f} 秒', flush=True)
        print(f'    步骤数:   {len(self.steps)}', flush=True)
        print(f'    思考条数: {len(self.thinking)}', flush=True)
        print(f'    流式片段: {len(self.content_chunks)} 个（共 {"".join(self.content_chunks).__len__()} 字）', flush=True)

        # ② 意图识别
        print(f'', flush=True)
        print(f'  【2. 意图识别结果】', flush=True)
        print(f'    公司:     {meta.get("company", "—") or "—"}', flush=True)
        print(f'    报告期:   {meta.get("report_period", "—") or "—"}', flush=True)
        print(f'    指标列:   {meta.get("metric", "—") or "—"}', flush=True)
        print(f'    查询类型: {meta.get("query_kind", "—") or "—"}', flush=True)
        print(f'    分析模式: {meta.get("analysis_mode", "—") or "—"}', flush=True)
        print(f'    意图概要: {meta.get("intent", "—") or "—"}', flush=True)
        if source_tables:
            print(f'    涉及表:   {", ".join(source_tables)}', flush=True)

        # ③ SQL
        print(f'', flush=True)
        print(f'  【3. 生成的 SQL】', flush=True)
        if sql:
            for sql_line in sql.strip().split('\n'):
                print(f'    {sql_line}', flush=True)
        else:
            print(f'    （无 SQL）', flush=True)

        # ④ 数据查询结果
        print(f'', flush=True)
        print(f'  【4. 数据查询结果】', flush=True)
        print(f'    返回行数: {len(rows)}', flush=True)
        print(f'    列定义:   {", ".join(c.get("label", c.get("key", "")) for c in columns)}', flush=True)
        if rows:
            print(f'    前5行预览:', flush=True)
            for r in rows[:5]:
                items = [f'{k}={v}' for k, v in list(r.items())[:6]]
                print(f'      {", ".join(items)}', flush=True)
            if len(rows) > 5:
                print(f'      ...（共 {len(rows)} 行）', flush=True)

        # ⑤ 知识库检索
        print(f'', flush=True)
        print(f'  【5. 知识库检索结果】', flush=True)
        print(f'    命中片段数: {len(refs)}', flush=True)
        if refs:
            for i, ref in enumerate(refs[:5], 1):
                src = ref.get('source', ref.get('doc_name', ref.get('title', '')))
                doc_type = ref.get('doc_type', '')
                snippet = ref.get('text', ref.get('content', ''))[:100]
                score = ref.get('score', ref.get('relevance_score', ''))
                score_str = f' (分数:{score:.3f})' if isinstance(score, (int, float)) else ''
                print(f'    [{i}] {src} [{doc_type}]{score_str}', flush=True)
                print(f'        {snippet}…', flush=True)

        # ⑥ 数据溯源
        print(f'', flush=True)
        print(f'  【6. 数据溯源记录】', flush=True)
        print(f'    溯源条目数: {len(provenance)}', flush=True)
        if provenance:
            for p in provenance[:5]:
                print(f'    • {p.get("stock_code", "")} | {p.get("report_period", "")} | '
                      f'{p.get("field_name", "")} = {p.get("field_value", "")} '
                      f'(来源: {p.get("source_file", "")}, p.{p.get("source_page", "")})', flush=True)
            if len(provenance) > 5:
                print(f'    ...（共 {len(provenance)} 条）', flush=True)

        # ⑦ 归因分析
        print(f'', flush=True)
        print(f'  【7. 归因分析（Attribution）】', flush=True)
        attr_steps = attribution.get('steps', [])
        if attr_steps:
            for s in attr_steps:
                print(f'    [{s.get("label", "")}] {s.get("detail", "")}', flush=True)
        else:
            print(f'    （无归因数据）', flush=True)
        if attribution.get('data_summary'):
            ds = attribution['data_summary']
            print(f'    数据摘要: 表={ds.get("tables","")}, 行={ds.get("row_count","")}, 列={ds.get("columns","")}', flush=True)

        # ⑧ 思维导图
        print(f'', flush=True)
        print(f'  【8. 思维导图结构（Thinking Map）】', flush=True)
        if thinking_map:
            self._print_tree(thinking_map, indent=4)
        else:
            print(f'    （无思维导图）', flush=True)

        # ⑨ 图表信息
        print(f'', flush=True)
        print(f'  【9. 图表信息】', flush=True)
        if chart:
            print(f'    标题: {chart.get("title", "—")}', flush=True)
            print(f'    类型: {chart.get("typeLabel", "—")}', flush=True)
            option = chart.get('option', {})
            series = option.get('series', [])
            if series:
                print(f'    系列数: {len(series)}', flush=True)
                for si, s in enumerate(series):
                    s_name = s.get('name', f'系列{si+1}')
                    s_type = s.get('type', '')
                    s_data_len = len(s.get('data', []))
                    print(f'      [{si+1}] {s_name} ({s_type}, {s_data_len}个数据点)', flush=True)
        elif self.charts:
            for ci, ch in enumerate(self.charts):
                print(f'    图表{ci+1}: {ch.get("title","—")} ({ch.get("typeLabel","")})', flush=True)
        else:
            print(f'    （无图表）', flush=True)

        # ⑩ LLM 分析内容
        print(f'', flush=True)
        print(f'  【10. LLM 生成的分析内容（摘要）】', flush=True)
        if content:
            # 打印前 300 字
            preview = content[:300]
            for line in preview.split('\n'):
                print(f'    {line}', flush=True)
            if len(content) > 300:
                print(f'    ...（共 {len(content)} 字）', flush=True)
        else:
            print(f'    （无内容）', flush=True)

        # ⑪ 洞察
        if insights:
            print(f'', flush=True)
            print(f'  【11. 数据洞察】', flush=True)
            for ins in insights[:5]:
                if isinstance(ins, str):
                    print(f'    • {ins}', flush=True)
                elif isinstance(ins, dict):
                    print(f'    • {ins.get("text", ins.get("content", str(ins)))}', flush=True)

        # ⑫ 计算溯源链
        if calc_traces:
            print(f'', flush=True)
            print(f'  【12. 计算溯源链】', flush=True)
            print(f'    溯源链条数: {len(calc_traces)}', flush=True)
            for ct in calc_traces[:3]:
                print(f'    • {ct.get("label", "")} = {ct.get("formula", ct.get("value", ""))}', flush=True)

        # ⑬ 后续建议
        print(f'', flush=True)
        print(f'  【13. 后续问题建议】', flush=True)
        if follow_ups:
            for f in follow_ups:
                print(f'    → {f}', flush=True)
        else:
            print(f'    （无建议）', flush=True)

        # ⑭ 完整步骤回顾
        print(f'', flush=True)
        print(f'{"─"*72}', flush=True)
        print(f'  ▶ 阶段三：执行步骤回顾（共 {len(self.steps)} 步）', flush=True)
        print(f'{"─"*72}', flush=True)
        if self.steps:
            for i, step in enumerate(self.steps, 1):
                status_icon = '✓' if step['status'] == 'done' else ('✗' if step['status'] == 'error' else '→')
                print(f'    {i:>2}. [{step["timestamp"]:>6.3f}s] {status_icon} {step["label"]}'
                      + (f' — {step["detail"]}' if step['detail'] else ''), flush=True)
        else:
            print(f'    （无步骤记录）', flush=True)

        # ── 总结 ──
        print(f'', flush=True)
        print(f'{"═"*72}', flush=True)
        print(f'  查询层追踪完成', flush=True)
        print(f'  总耗时: {elapsed:.3f}s | 状态: {reply.get("status", "")} | '
              f'意图: {meta.get("query_kind","")} / {meta.get("analysis_mode","")}', flush=True)
        print(f'  数据: {len(rows)} 行 × {len(columns)} 列 | 知识库: {len(refs)} 条 | '
              f'溯源: {len(provenance)} 条 | 内容: {len(content)} 字', flush=True)
        print(f'{"═"*72}\n', flush=True)

        report = {
            'question': question,
            'steps': self.steps,
            'thinking': self.thinking,
            'reply': reply,
            'elapsed_seconds': elapsed,
            'formatted_text': self._format_report(question, reply, elapsed),
        }
        return report

    # ── 格式化输出 ─────────────────────────────────────────────────────────────
    def _format_report(self, question: str, reply: dict[str, Any],
                       elapsed: float) -> str:
        lines: list[str] = []
        sep = '═' * 72
        thin = '─' * 72

        lines.append(sep)
        lines.append(f'  查询层中间过程报告')
        lines.append(f'  生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
        lines.append(sep)

        # ① 用户输入
        lines.append('')
        lines.append('【1. 用户输入】')
        lines.append(f'  问题: {question}')
        lines.append('')

        # ② 各层步骤
        lines.append('【2. 查询层执行步骤】')
        if self.steps:
            for i, step in enumerate(self.steps, 1):
                status_icon = '✓' if step['status'] == 'done' else '→' if step['status'] == 'running' else '?'
                lines.append(
                    f'  [{step["timestamp"]:>6.3f}s] {status_icon} {step["label"]}'
                    + (f' — {step["detail"]}' if step['detail'] else '')
                )
        else:
            lines.append('  （无步骤记录）')
        lines.append('')

        # ③ 思考过程
        lines.append('【3. LLM 思考过程（Deep Think）】')
        if self.thinking:
            for t in self.thinking:
                lines.append(f'  💭 {t}')
        else:
            lines.append('  （未开启深度思考或无思考输出）')
        lines.append('')

        # ④ 意图识别结果
        meta = reply.get('meta', {})
        lines.append('【4. 意图识别结果】')
        lines.append(f'  公司:       {meta.get("company", "—") or "—"}')
        lines.append(f'  报告期:     {meta.get("report_period", "—") or "—"}')
        lines.append(f'  指标列:     {meta.get("metric", "—") or "—"}')
        lines.append(f'  查询类型:   {meta.get("query_kind", "—") or "—"}')
        lines.append(f'  分析模式:   {meta.get("analysis_mode", "—") or "—"}')
        lines.append(f'  意图概要:   {meta.get("intent", "—") or "—"}')
        lines.append('')

        # ⑤ SQL
        sql = reply.get('sql', '')
        lines.append('【5. 生成的 SQL】')
        if sql:
            for sql_line in sql.strip().split('\n'):
                lines.append(f'  {sql_line}')
        else:
            lines.append('  （无 SQL）')
        lines.append('')

        # ⑥ 数据结果摘要
        rows = reply.get('rows', [])
        columns = reply.get('columns', [])
        lines.append('【6. 数据查询结果】')
        lines.append(f'  返回行数: {len(rows)}')
        lines.append(f'  列定义:   {", ".join(c.get("label", c.get("key", "")) for c in columns)}')
        if rows:
            lines.append(f'  前3行预览:')
            for r in rows[:3]:
                items = [f'{k}={v}' for k, v in list(r.items())[:6]]
                lines.append(f'    {", ".join(items)}')
        lines.append('')

        # ⑦ 知识库检索
        refs = reply.get('references', [])
        lines.append('【7. 知识库检索结果】')
        lines.append(f'  命中片段数: {len(refs)}')
        if refs:
            for i, ref in enumerate(refs[:3], 1):
                src = ref.get('source', ref.get('title', ''))
                snippet = ref.get('text', ref.get('content', ''))[:80]
                lines.append(f'  [{i}] {src}: {snippet}…')
        lines.append('')

        # ⑧ 数据溯源
        provenance = reply.get('provenance', [])
        lines.append('【8. 数据溯源记录】')
        lines.append(f'  溯源条目数: {len(provenance)}')
        if provenance:
            for p in provenance[:3]:
                lines.append(
                    f'  • {p.get("stock_code", "")} | {p.get("report_period", "")} | '
                    f'{p.get("field_name", "")} = {p.get("field_value", "")} '
                    f'(来源: {p.get("source_file", "")}, p.{p.get("source_page", "")})'
                )
        lines.append('')

        # ⑨ 归因分析 / 思维导图
        attribution = reply.get('attribution', {})
        thinking_map = attribution.get('thinking_map', {})
        lines.append('【9. 归因分析（Attribution）】')
        attr_steps = attribution.get('steps', [])
        for s in attr_steps:
            lines.append(f'  [{s.get("label", "")}] {s.get("detail", "")}')
        lines.append('')

        lines.append('【10. 思维导图结构（Thinking Map）】')
        if thinking_map:
            self._format_tree(thinking_map, lines, indent=2)
        else:
            lines.append('  （无思维导图）')
        lines.append('')

        # ⑩ 图表信息
        chart = reply.get('chart')
        lines.append('【11. 图表信息】')
        if chart:
            lines.append(f'  标题: {chart.get("title", "—")}')
            lines.append(f'  类型: {chart.get("typeLabel", "—")}')
        else:
            lines.append('  （无图表）')
        lines.append('')

        # ⑪ LLM 分析摘要
        content = reply.get('content', '')
        lines.append('【12. LLM 生成的分析内容（前 500 字）】')
        lines.append(f'  {content[:500]}')
        if len(content) > 500:
            lines.append(f'  ...（共 {len(content)} 字）')
        lines.append('')

        # ⑫ 后续建议
        follow_ups = reply.get('followUpHints', [])
        lines.append('【13. 后续问题建议】')
        if follow_ups:
            for f in follow_ups:
                lines.append(f'  → {f}')
        else:
            lines.append('  （无建议）')
        lines.append('')

        lines.append(thin)
        lines.append(f'  总耗时: {elapsed:.3f} 秒')
        lines.append(f'  状态: {reply.get("status", "unknown")}')
        lines.append(sep)

        return '\n'.join(lines)

    def _format_tree(self, node: dict, lines: list[str], indent: int = 0):
        """递归格式化思维导图树节点。"""
        prefix = ' ' * indent
        label = node.get('label', '')
        detail = node.get('detail', '')
        edge = node.get('edge_label', '')
        node_type = node.get('type', '')

        type_icon = {
            'question': '❓', 'process': '⚙️', 'data': '📊',
            'evidence': '📚', 'synthesis': '🧠', 'conclusion': '✅',
        }.get(node_type, '•')

        edge_str = f' ──[{edge}]──▸ ' if edge else ''
        lines.append(f'{prefix}{edge_str}{type_icon} {label}')
        if detail:
            detail_short = detail[:100] + ('…' if len(detail) > 100 else '')
            lines.append(f'{prefix}  └ {detail_short}')

        for child in node.get('children', []):
            self._format_tree(child, lines, indent + 4)

    def _print_tree(self, node: dict, indent: int = 4):
        """递归打印思维导图树节点到控制台。"""
        if not node:
            return
        prefix = ' ' * indent
        label = node.get('label', '')
        detail = node.get('detail', '')
        edge = node.get('edge_label', '')
        node_type = node.get('type', '')

        type_icon = {
            'question': '❓', 'process': '⚙️', 'data': '📊',
            'knowledge': '📚', 'source': '📄', 'evidence': '📚',
            'synthesis': '🧠', 'result': '✅', 'conclusion': '✅',
        }.get(node_type, '•')

        edge_str = f'──[{edge}]──▸ ' if edge else ''
        print(f'{prefix}{edge_str}{type_icon} {label}', flush=True)
        if detail:
            detail_short = detail[:120] + ('…' if len(detail) > 120 else '')
            print(f'{prefix}  └ {detail_short}', flush=True)

        for child in node.get('children', []):
            self._print_tree(child, indent + 4)


# ═══════════════════════════════════════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════════════════════════════════════

def trace_query(question: str, history: list[dict[str, Any]] | None = None,
                deep_think: bool = True, session_id: str = '') -> dict[str, Any]:
    """一键追踪查询层中间过程。返回包含 formatted_text 的完整报告。"""
    tracer = QueryLayerTracer()
    return tracer.run(question, history=history, deep_think=deep_think,
                      session_id=session_id)


def trace_query_to_file(question: str, output_path: str | None = None,
                        history: list[dict[str, Any]] | None = None,
                        deep_think: bool = True) -> str:
    """追踪查询并将报告写入文件。返回输出文件路径。"""
    report = trace_query(question, history=history, deep_think=deep_think)

    if output_path is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = os.path.join(_project_root, f'query_trace_{ts}.txt')

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report['formatted_text'])
        f.write('\n\n')
        f.write('=' * 72 + '\n')
        f.write('  结构化 JSON 数据\n')
        f.write('=' * 72 + '\n')
        # 序列化时排除不可序列化的大对象
        slim = {
            'question': report['question'],
            'steps': report['steps'],
            'thinking': report['thinking'],
            'elapsed_seconds': report['elapsed_seconds'],
            'meta': report['reply'].get('meta', {}),
            'sql': report['reply'].get('sql', ''),
            'row_count': len(report['reply'].get('rows', [])),
            'reference_count': len(report['reply'].get('references', [])),
            'provenance_count': len(report['reply'].get('provenance', [])),
            'content_length': len(report['reply'].get('content', '')),
            'status': report['reply'].get('status', ''),
        }
        f.write(json.dumps(slim, ensure_ascii=False, indent=2))

    return output_path


def trace_query_to_html(question: str, output_path: str | None = None,
                        history: list[dict[str, Any]] | None = None,
                        deep_think: bool = True) -> str:
    """追踪查询并生成可视化 HTML 报告。返回输出文件路径。"""
    report = trace_query(question, history=history, deep_think=deep_think)

    if output_path is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = os.path.join(_project_root, f'query_trace_{ts}.html')

    reply = report['reply']
    meta = reply.get('meta', {})
    steps = report['steps']
    thinking = report['thinking']
    rows = reply.get('rows', [])
    columns = reply.get('columns', [])
    refs = reply.get('references', [])
    provenance = reply.get('provenance', [])
    attribution = reply.get('attribution', {})
    thinking_map = attribution.get('thinking_map', {})
    chart = reply.get('chart')
    content = reply.get('content', '')
    follow_ups = reply.get('followUpHints', [])
    sql = reply.get('sql', '')
    elapsed = report['elapsed_seconds']

    def _esc(s):
        """HTML escape."""
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    def _tree_html(node, depth=0):
        """递归生成思维导图 HTML。"""
        if not node:
            return ''
        icon_map = {
            'question': '❓', 'process': '⚙️', 'data': '📊',
            'evidence': '📚', 'synthesis': '🧠', 'conclusion': '✅',
        }
        icon = icon_map.get(node.get('type', ''), '•')
        label = _esc(node.get('label', ''))
        detail = _esc(node.get('detail', ''))
        edge = _esc(node.get('edge_label', ''))
        ntype = node.get('type', '')
        type_cls = f'tm-{ntype}' if ntype else 'tm-default'

        h = f'<div class="tm-node {type_cls}" style="margin-left:{depth*28}px">'
        if edge:
            h += f'<span class="tm-edge">{edge}</span>'
        h += f'<span class="tm-icon">{icon}</span>'
        h += f'<span class="tm-label">{label}</span>'
        if detail:
            h += f'<div class="tm-detail">{detail[:200]}{"…" if len(detail)>200 else ""}</div>'
        h += '</div>'
        for child in node.get('children', []):
            h += _tree_html(child, depth + 1)
        return h

    # ── 步骤时间线 HTML ──
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

    # ── 思考过程 HTML ──
    think_html = ''
    for t in thinking:
        think_html += f'<div class="think-item">💭 {_esc(t)}</div>\n'
    if not thinking:
        think_html = '<div class="empty">（未开启深度思考或无思考输出）</div>'

    # ── 数据表格 HTML ──
    table_html = ''
    if rows and columns:
        table_html = '<table class="data-table"><thead><tr>'
        for c in columns:
            table_html += f'<th>{_esc(c.get("label", c.get("key", "")))}</th>'
        table_html += '</tr></thead><tbody>'
        for r in rows[:20]:
            table_html += '<tr>'
            for c in columns:
                val = r.get(c.get('key', ''), '')
                table_html += f'<td>{_esc(str(val))}</td>'
            table_html += '</tr>'
        table_html += '</tbody></table>'
        if len(rows) > 20:
            table_html += f'<div class="table-note">显示前 20 行，共 {len(rows)} 行</div>'
    else:
        table_html = '<div class="empty">（无数据返回）</div>'

    # ── 知识库 HTML ──
    kb_html = ''
    if refs:
        for i, ref in enumerate(refs[:5], 1):
            src = _esc(ref.get('source', ref.get('title', '')))
            snippet = _esc(ref.get('text', ref.get('content', ''))[:120])
            kb_html += f'<div class="ref-item"><b>[{i}]</b> <span class="ref-src">{src}</span><br>{snippet}…</div>\n'
    else:
        kb_html = '<div class="empty">（未命中知识库）</div>'

    # ── 溯源 HTML ──
    prov_html = ''
    if provenance:
        for p in provenance[:8]:
            prov_html += f'''<div class="prov-item">
                <b>{_esc(p.get("stock_code",""))}</b> | {_esc(p.get("report_period",""))} |
                {_esc(p.get("field_name",""))} = {_esc(str(p.get("field_value","")))}
                <span class="prov-src">（{_esc(p.get("source_file",""))}, p.{p.get("source_page","")}）</span>
            </div>\n'''
    else:
        prov_html = '<div class="empty">（无溯源记录）</div>'

    # ── 组装完整 HTML（仅展示查询层过程） ──
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>查询层过程展示 — {_esc(question[:30])}</title>
<style>
:root {{
    --blue: #023047; --light-blue: #90C9E6; --yellow: #FFB703;
    --orange: #FB8402; --teal: #219EBD; --gray: #D0D0D0; --dark: #404040;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    background: #f0f4f8; color: var(--dark); line-height: 1.6;
}}
.container {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px; }}
h1 {{
    background: var(--blue); color: #fff; padding: 20px 28px;
    border-radius: 12px 12px 0 0; font-size: 20px; letter-spacing: 1px;
}}
h1 small {{ display: block; font-weight: 400; font-size: 13px; color: var(--light-blue); margin-top: 4px; }}
.card {{
    background: #fff; border-radius: 10px; margin-bottom: 16px;
    box-shadow: 0 2px 8px rgba(0,0,0,.06); overflow: hidden;
}}
.card-header {{
    padding: 12px 20px; font-weight: 700; font-size: 15px;
    border-bottom: 2px solid var(--light-blue); color: var(--blue);
    display: flex; align-items: center; gap: 8px;
}}
.card-header .badge {{
    background: var(--teal); color: #fff; font-size: 11px;
    padding: 2px 8px; border-radius: 10px; font-weight: 600;
}}
.card-body {{ padding: 16px 20px; }}
/* 用户输入 */
.question-box {{
    background: linear-gradient(135deg, var(--blue), #035a7a);
    color: #fff; padding: 18px 24px; border-radius: 8px;
    font-size: 17px; font-weight: 600;
}}
/* 步骤时间线 */
.step-item {{
    display: flex; align-items: center; gap: 10px;
    padding: 6px 0; border-left: 3px solid var(--gray);
    padding-left: 14px; margin-left: 8px;
}}
.step-item.step-done {{ border-left-color: #10b981; }}
.step-item.step-running {{ border-left-color: var(--yellow); }}
.step-time {{ font-size: 12px; color: #888; min-width: 56px; font-family: "Consolas", monospace; }}
.step-icon {{ font-size: 16px; }}
.step-label {{ font-weight: 600; min-width: 90px; }}
.step-detail {{ color: #555; font-size: 13px; }}
/* 思考 */
.think-item {{ padding: 6px 0; font-size: 14px; color: #444; }}
/* 意图 */
.meta-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 8px; }}
.meta-item {{ padding: 8px 12px; background: #f8fafc; border-radius: 6px; border-left: 3px solid var(--teal); }}
.meta-item b {{ color: var(--blue); }}
/* SQL */
.sql-box {{
    background: #1e293b; color: #e2e8f0; padding: 16px 20px;
    border-radius: 8px; font-family: "Consolas", "Courier New", monospace;
    font-size: 13px; white-space: pre-wrap; word-break: break-all;
    overflow-x: auto; line-height: 1.8;
}}
.sql-box .kw {{ color: #7dd3fc; font-weight: 700; }}
.sql-box .str {{ color: #fbbf24; }}
.sql-box .comment {{ color: #6b7280; font-style: italic; }}
/* 数据表格 */
.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.data-table th {{
    background: var(--blue); color: #fff; padding: 8px 10px;
    text-align: left; font-weight: 600; white-space: nowrap;
}}
.data-table td {{ padding: 6px 10px; border-bottom: 1px solid #e5e7eb; }}
.data-table tr:hover {{ background: #f0f9ff; }}
.table-note {{ font-size: 12px; color: #888; padding: 6px 0; text-align: right; }}
/* 知识库 */
.ref-item {{ padding: 8px 0; border-bottom: 1px dashed #e5e7eb; font-size: 13px; }}
.ref-src {{ color: var(--teal); font-weight: 600; }}
/* 溯源 */
.prov-item {{ padding: 4px 0; font-size: 13px; }}
.prov-src {{ color: #888; font-size: 12px; }}
/* 思维导图 */
.tm-node {{
    padding: 5px 0; transition: background .2s;
}}
.tm-node:hover {{ background: #f0f9ff; border-radius: 4px; }}
.tm-edge {{
    display: inline-block; background: var(--yellow); color: var(--blue);
    font-size: 11px; padding: 1px 6px; border-radius: 8px;
    margin-right: 4px; font-weight: 600;
}}
.tm-icon {{ font-size: 15px; margin-right: 4px; }}
.tm-label {{ font-weight: 600; color: var(--blue); }}
.tm-detail {{ color: #666; font-size: 12px; margin: 2px 0 4px 28px; line-height: 1.5; }}
/* 图表 */
.chart-info {{ padding: 8px 12px; background: #f0f9ff; border-radius: 6px; font-size: 13px; }}
/* 通用 */
.empty {{ color: #999; font-style: italic; padding: 8px 0; }}
.summary-bar {{
    display: flex; gap: 16px; padding: 12px 20px; background: var(--blue);
    color: #fff; border-radius: 0 0 12px 12px; font-size: 13px;
    flex-wrap: wrap;
}}
.summary-bar span b {{ color: var(--yellow); }}
</style>
</head>
<body>
<div class="container">

<h1>查询层过程展示
    <small>生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} | 耗时: {elapsed:.3f}s | 状态: {_esc(reply.get("status",""))}</small>
</h1>
<div class="summary-bar">
    <span>公司: <b>{_esc(meta.get("company","—") or "—")}</b></span>
    <span>报告期: <b>{_esc(meta.get("report_period","—") or "—")}</b></span>
    <span>查询类型: <b>{_esc(meta.get("query_kind","—") or "—")}</b></span>
    <span>分析模式: <b>{_esc(meta.get("analysis_mode","—") or "—")}</b></span>
    <span>数据行: <b>{len(rows)}</b></span>
    <span>知识库: <b>{len(refs)}条</b></span>
</div>

<!-- 1. 用户输入 -->
<div class="card" style="margin-top:16px;">
    <div class="card-header">1. 用户输入</div>
    <div class="card-body">
        <div class="question-box">{_esc(question)}</div>
    </div>
</div>

<!-- 2. 执行步骤时间线 -->
<div class="card">
    <div class="card-header">2. 查询层执行步骤 <span class="badge">{len(steps)} 步</span></div>
    <div class="card-body">{steps_html or '<div class="empty">（无步骤）</div>'}</div>
</div>

<!-- 3. 思考过程 -->
<div class="card">
    <div class="card-header">3. LLM 思考过程</div>
    <div class="card-body">{think_html}</div>
</div>

<!-- 4. 意图识别 -->
<div class="card">
    <div class="card-header">4. 意图识别结果</div>
    <div class="card-body">
        <div class="meta-grid">
            <div class="meta-item"><b>公司:</b> {_esc(meta.get("company","—") or "—")}</div>
            <div class="meta-item"><b>报告期:</b> {_esc(meta.get("report_period","—") or "—")}</div>
            <div class="meta-item"><b>指标列:</b> {_esc(meta.get("metric","—") or "—")}</div>
            <div class="meta-item"><b>查询类型:</b> {_esc(meta.get("query_kind","—") or "—")}</div>
            <div class="meta-item"><b>分析模式:</b> {_esc(meta.get("analysis_mode","—") or "—")}</div>
            <div class="meta-item"><b>意图概要:</b> {_esc(meta.get("intent","—") or "—")}</div>
        </div>
    </div>
</div>

<!-- 5. SQL -->
<div class="card">
    <div class="card-header">5. 生成的 SQL</div>
    <div class="card-body">
        {"<div class='sql-box'>" + _highlight_sql(_esc(sql)) + "</div>" if sql else '<div class="empty">（无 SQL）</div>'}
    </div>
</div>

<!-- 6. 数据结果 -->
<div class="card">
    <div class="card-header">6. 数据查询结果 <span class="badge">{len(rows)} 行 × {len(columns)} 列</span></div>
    <div class="card-body">{table_html}</div>
</div>

<!-- 7. 知识库 -->
<div class="card">
    <div class="card-header">7. 知识库检索 <span class="badge">{len(refs)} 条</span></div>
    <div class="card-body">{kb_html}</div>
</div>

<!-- 8. 数据溯源 -->
<div class="card">
    <div class="card-header">8. 数据溯源 <span class="badge">{len(provenance)} 条</span></div>
    <div class="card-body">{prov_html}</div>
</div>

<!-- 9. 思维导图 -->
<div class="card">
    <div class="card-header">9. 思维导图（Thinking Map）</div>
    <div class="card-body">
        {_tree_html(thinking_map) if thinking_map else '<div class="empty">（无思维导图）</div>'}
    </div>
</div>

<!-- 10. 归因分析 -->
<div class="card">
    <div class="card-header">10. 归因分析（Attribution）<span class="badge">{len(attribution.get("steps",[]))} 步</span></div>
    <div class="card-body">
        {''.join(f'<div class="step-item step-done"><span class="step-label">{_esc(s.get("label",""))}</span><span class="step-detail">{_esc(s.get("detail",""))}</span></div>' for s in attribution.get("steps",[])) or '<div class="empty">（无归因数据）</div>'}
    </div>
</div>

<!-- 11. 图表信息 -->
<div class="card">
    <div class="card-header">11. 图表生成</div>
    <div class="card-body">
        {"<div class='chart-info'><b>标题:</b> " + _esc(chart.get("title","")) + " | <b>类型:</b> " + _esc(chart.get("typeLabel","")) + "</div>" if chart else '<div class="empty">（无图表）</div>'}
    </div>
</div>

<!-- 12. LLM 分析内容 -->
<div class="card">
    <div class="card-header">12. LLM 生成的分析内容 <span class="badge">{len(content)} 字</span></div>
    <div class="card-body">
        {"<div style='white-space:pre-wrap;font-size:14px;line-height:1.8;color:#333;'>" + _esc(content[:800]) + ("…<br><i style='color:#888;'>（共 " + str(len(content)) + " 字，此处截断显示）</i>" if len(content) > 800 else "") + "</div>" if content else '<div class="empty">（无内容）</div>'}
    </div>
</div>

<!-- 13. 后续问题建议 -->
<div class="card">
    <div class="card-header">13. 后续问题建议</div>
    <div class="card-body">
        {''.join(f'<div style="padding:4px 0;font-size:14px;">→ {_esc(f)}</div>' for f in follow_ups) or '<div class="empty">（无建议）</div>'}
    </div>
</div>

</div>
</body>
</html>'''

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return output_path


def _highlight_sql(escaped_sql: str) -> str:
    """简单的 SQL 关键字高亮（输入已 HTML escaped）。"""
    import re as _re
    keywords = [
        'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'ORDER', 'BY', 'ASC',
        'DESC', 'LIMIT', 'JOIN', 'ON', 'IN', 'AS', 'GROUP', 'HAVING',
        'DISTINCT', 'COUNT', 'SUM', 'AVG', 'ROUND', 'CAST', 'REAL',
        'TRIM', 'IS', 'NOT', 'NULL', 'LIKE', 'BETWEEN', 'UNION',
    ]
    for kw in keywords:
        escaped_sql = _re.sub(
            rf'\b({kw})\b',
            r'<span class="kw">\1</span>',
            escaped_sql,
            flags=_re.IGNORECASE,
        )
    # 字符串 'xxx'
    escaped_sql = _re.sub(
        r"(&apos;|&#39;|&#x27;|'([^']*)')",
        r'<span class="str">\1</span>',
        escaped_sql,
    )
    # 注释 --
    escaped_sql = _re.sub(
        r'(--.*?)(\n|$)',
        r'<span class="comment">\1</span>\2',
        escaped_sql,
    )
    return escaped_sql


def trace_batch(questions: list[str], output_dir: str | None = None) -> list[str]:
    """批量追踪多个问题，每个问题生成一份报告文件。"""
    if output_dir is None:
        output_dir = os.path.join(_project_root, 'trace_reports')
    os.makedirs(output_dir, exist_ok=True)

    paths = []
    for i, q in enumerate(questions, 1):
        fname = f'trace_{i:03d}.txt'
        fpath = os.path.join(output_dir, fname)
        print(f'[{i}/{len(questions)}] 正在追踪: {q[:40]}…')
        try:
            trace_query_to_file(q, output_path=fpath)
            paths.append(fpath)
            print(f'  → 完成: {fpath}')
        except Exception as e:
            print(f'  → 失败: {e}')
            paths.append('')
    return paths


# ═══════════════════════════════════════════════════════════════════════════════
#  独立运行入口
# ═══════════════════════════════════════════════════════════════════════════════

DEMO_QUESTIONS = [
    '贵州茅台2024年第三季度净利润是多少？',
    '2025年第三季度营收前十的公司有哪些？',
    '比亚迪和宁德时代2023年研发费用对比',
    '哪些公司2024年ROE超过20%？',
]


def main():
    """交互式或演示运行，默认生成 HTML 可视化报告并在浏览器中打开。"""
    print('=' * 60)
    print('  查询层中间过程追踪器')
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

    print(f'\n▶ 开始追踪: {question}\n')

    # 生成 HTML 可视化报告
    html_path = trace_query_to_html(question, deep_think=True)
    print(f'\n📄 HTML 可视化报告已保存至: {html_path}')

    # 自动在浏览器中打开
    import webbrowser
    webbrowser.open(f'file:///{html_path.replace(os.sep, "/")}')
    print('🌐 已在浏览器中打开')


if __name__ == '__main__':
    main()
