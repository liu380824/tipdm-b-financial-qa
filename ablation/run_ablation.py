"""消融实验运行器（Ablation Study Runner）

本模块对增强层的 7 大组件逐一"消融"（禁用），
通过对比 Baseline（全量开启）与各消融条件下的输出指标，
量化每个组件对最终回答质量的贡献。

消融条件：
  ┌───────────────┬────────────────────────────────────────────┐
  │  条件标识      │  描述                                      │
  ├───────────────┼────────────────────────────────────────────┤
  │  full         │  Baseline — 所有组件开启                    │
  │  -chart       │  禁用自动图表生成                           │
  │  -llm         │  禁用 LLM 分析（内容/洞察/建议）            │
  │  -provenance  │  禁用数据溯源查询                           │
  │  -thinking_map│  禁用思维导图构建                           │
  │  -calc_traces │  禁用计算溯源链                             │
  │  -deep_think  │  禁用深度思考（thinking emit）              │
  │  -kb          │  禁用知识库检索                             │
  └───────────────┴────────────────────────────────────────────┘

使用方法：
    python -m ablation.run_ablation
    python -m ablation.run_ablation "贵州茅台2025年净利润趋势"
    python -m ablation.run_ablation --questions 3
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from statistics import mean, stdev
from typing import Any
from unittest.mock import patch

# ── 保证项目根目录在 sys.path 中 ──────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.services.assistant import build_assistant_reply


# ═══════════════════════════════════════════════════════════════════════════════
#  指标采集
# ═══════════════════════════════════════════════════════════════════════════════

def _collect_metrics(reply: dict[str, Any], thinking_count: int, elapsed: float) -> dict[str, Any]:
    """从一次 build_assistant_reply 的结果中提取所有可量化指标。"""
    attribution = reply.get('attribution', {})
    chart = reply.get('chart')
    content = reply.get('content', '')
    return {
        'status': reply.get('status', ''),
        'content_length': len(content),
        'has_chart': 1 if chart else 0,
        'chart_title': chart.get('title', '') if chart else '',
        'chart_type': chart.get('typeLabel', '') if chart else '',
        'chart_series': len(chart.get('option', {}).get('series', [])) if chart else 0,
        'row_count': len(reply.get('rows', [])),
        'col_count': len(reply.get('columns', [])),
        'provenance_count': len(reply.get('provenance', [])),
        'calc_traces_count': len(reply.get('calcTraces', [])),
        'attribution_steps': len(attribution.get('steps', [])),
        'has_thinking_map': 1 if attribution.get('thinking_map') else 0,
        'insights_count': len(reply.get('insights', [])),
        'follow_up_count': len(reply.get('followUpHints', [])),
        'kb_refs_count': len(reply.get('references', [])),
        'thinking_count': thinking_count,
        'elapsed': round(elapsed, 3),
        # 详细日志字段（不参与评分）
        '_sql': reply.get('sql', ''),
        '_content_preview': content[:200].replace('\n', ' '),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  单次实验执行
# ═══════════════════════════════════════════════════════════════════════════════

# ── 所有条件均为 LIVE 实测（monkey-patch 实际禁用组件）──────────────────────
LIVE_CONDITIONS = {'full', '-llm', '-calc_traces', '-deep_think', '-chart', '-kb', '-thinking_map', '-provenance'}


def _build_patches(condition: str) -> tuple[list, bool]:
    """根据条件标识构建 monkey-patch 列表。支持组合条件（如 -llm-kb）。"""
    # 拆分组合条件为单个子条件
    sub_conds: list[str] = []
    if condition == 'full':
        return [], True
    for part in condition.split('-'):
        if part:
            sub_conds.append(f'-{part}')

    patches = []
    deep_think = True

    for sc in sub_conds:
        if sc == '-llm':
            patches.append(patch(
                'backend.services.assistant._build_llm_analysis',
                return_value={'content': '（LLM 分析已禁用）', 'insights': [], 'followUpHints': []},
            ))
        elif sc == '-calc_traces':
            patches.append(patch(
                'backend.services.assistant._build_calc_traces',
                return_value=([], []),
            ))
        elif sc == '-deep_think':
            deep_think = False
        elif sc == '-chart':
            patches.append(patch(
                'backend.services.visualization.auto_generate_chart_payload',
                return_value=None,
            ))
        elif sc == '-kb':
            patches.append(patch(
                'backend.services.assistant._retrieve_kb_context',
                return_value=('', []),
            ))
        elif sc == '-thinking_map':
            patches.append(patch('backend.services.assistant._build_thinking_map', return_value={}))
            patches.append(patch('backend.services.assistant._build_clarify_thinking_map', return_value={}))
            patches.append(patch('backend.services.assistant._build_general_thinking_map', return_value={}))
        elif sc == '-provenance':
            patches.append(patch(
                'backend.services.assistant._fetch_support_provenance_record',
                return_value=None,
            ))
    return patches, deep_think


def _run_live(question: str, condition: str, deep_think: bool = True) -> dict[str, Any]:
    """通过实际 monkey-patch 运行消融实验。支持单组件和组合条件。"""

    thinking_list: list[str] = []

    def _emit(event_type: str, data: Any):
        if event_type == 'thinking':
            thinking_list.append(data if isinstance(data, str) else str(data))

    patches, dt = _build_patches(condition)
    deep_think = deep_think and dt  # 组合条件中若含 -deep_think 则禁用

    t0 = time.time()
    for p in patches:
        p.start()
    try:
        reply = build_assistant_reply(
            question, [],
            emit=_emit,
            deep_think=deep_think,
            session_id='',
        )
    finally:
        for p in patches:
            p.stop()
    elapsed = time.time() - t0

    # ── 后处理：清除无法完全通过 patch 禁用的输出字段 ──────────────
    if '-chart' in condition:
        reply['chart'] = None  # 管道路径生成的图表也需清除
    if '-provenance' in condition:
        reply['provenance'] = []  # 主查询直接用 SQL，patch 不到
        reply['citationRefs'] = []
        # 注意：不清除 calcTraces —— 计算溯源链是独立组件，与数据溯源无关

    metrics = _collect_metrics(reply, len(thinking_list), elapsed)
    metrics['condition'] = condition
    metrics['mode'] = 'live'
    return metrics


# ═══════════════════════════════════════════════════════════════════════════════
#  批量消融实验
# ═══════════════════════════════════════════════════════════════════════════════

# ── 单组件消融条件 ───────────────────────────────────────────────────────
SINGLE_CONDITIONS = [
    ('full',           '全量 Baseline'),
    ('-chart',         '禁用自动图表'),
    ('-llm',           '禁用 LLM 分析'),
    ('-provenance',    '禁用数据溯源'),
    ('-thinking_map',  '禁用思维导图'),
    ('-calc_traces',   '禁用计算溯源链'),
    ('-deep_think',    '禁用深度思考'),
    ('-kb',            '禁用知识库检索'),
]

# ── 组合消融条件（测试组件间交互效应）──────────────────────────────
PAIRWISE_CONDITIONS = [
    ('-llm-kb',           '禁用 LLM+知识库'),
    ('-chart-thinking_map', '禁用图表+思维导图'),
    ('-provenance-calc_traces', '禁用溯源+计算链'),
]

CONDITIONS = SINGLE_CONDITIONS  # 默认只跑单组件

DEMO_QUESTIONS = [
    # ① 简单查询 — B1044
    '2025年第三季度营业总收入排名前五的中药公司有哪些？',
    # ② 计算类 — B1034
    '计算云南白药（000538）2025年第三季度营业总收入的环比增长率（环比增长率=(本季度值-上季度值)/上季度值*100），并展示2025年第二季度和第三季度的营业总收入（万元）',
    # ③ 跨表关联 — B1033
    '结合现金流量表和利润表，查询2024年全年所有公司的股票代码、简称、净利润（利润表）、经营性现金流量净额（现金流量表），筛选出经营性现金流量净额大于净利润的公司',
    # ④ 趋势+可视化 — B1050
    '分析云南白药2022-2025年第三季度净利润的变化趋势，用折线图展示',
    # ⑤ 多条件筛选 — B1038
    '查询2025年第三季度满足以下条件的公司：营业总收入超过10亿元（即100000万元）、资产负债率低于40%、经营性现金流量净额为正，列出股票代码、简称、营业总收入、资产负债率、经营性现金流量净额',
    # ⑥ 开放性问题 — B1066
    '哪些公司的业绩比较好？',
    # ⑦ 综合分析 — B2001 (result_3)
    '2024年利润最高的top10企业是哪些？这些企业的利润、销售额年同比是多少？年同比上涨幅度最大的是哪家企业？',
]

# 每个指标的中文名和"满分"含义（用于归一化计算）
METRIC_LABELS = {
    'content_length':   ('内容长度（字）', True),
    'has_chart':        ('是否有图表', True),
    'provenance_count': ('溯源条目数', True),
    'calc_traces_count':('计算溯源链', True),
    'attribution_steps':('归因步骤数', True),
    'has_thinking_map': ('是否有思维导图', True),
    'insights_count':   ('数据洞察数', True),
    'follow_up_count':  ('后续建议数', True),
    'kb_refs_count':    ('知识库命中数', True),
    'thinking_count':   ('深度思考条数', True),
    'elapsed':          ('响应耗时（s）', False),  # False = 越小越好
}

# ── 综合质量评分权重 ──────────────────────────────────────────────────────────
# 每个指标对最终回答质量的贡献权重（满分 100 分）
QUALITY_WEIGHTS = {
    'content_length':    20,   # LLM 分析内容（降权：避免单一指标主导）
    'has_chart':         12,   # 可视化图表
    'provenance_count':  12,   # 数据溯源
    'calc_traces_count': 10,   # 计算溯源链
    'attribution_steps':  5,   # 归因分析（各条件差异小，降权）
    'has_thinking_map':   8,   # 思维导图
    'insights_count':     8,   # 数据洞察
    'follow_up_count':    5,   # 后续建议
    'kb_refs_count':     12,   # 知识库命中
    'thinking_count':     8,   # 深度思考
}


def compute_quality_score(metrics: dict[str, Any], baseline: dict[str, Any]) -> float:
    """基于加权保留率计算综合质量评分（0~100）。"""
    score = 0.0
    for mk, weight in QUALITY_WEIGHTS.items():
        base_val = baseline.get(mk, 0)
        cur_val = metrics.get(mk, 0)
        if base_val > 0:
            ratio = min(cur_val / base_val, 1.2)  # 允许小幅超越 baseline，上限 1.2
        else:
            ratio = 1.0  # baseline 本身也没有的指标，不扣分
        score += weight * ratio
    return round(score, 1)


def _llm_judge(question: str, content: str) -> dict[str, float]:
    """用 LLM 对回答进行语义质量打分（1~10）。需要 LLM 服务可用。"""
    try:
        from backend.services.llm import request_llm_text
        system = "你是一个严格的回答质量评审员。只输出 JSON，不要其他内容。"
        user = (
            "请对以下问答对的回答质量打分（1-10 分），从三个维度评分：\n"
            "1. correctness（正确性）：数据和事实是否准确\n"
            "2. completeness（完整性）：是否充分回答了问题\n"
            "3. expression（表达质量）：语言是否流畅、有逻辑\n\n"
            f"【问题】{question}\n\n"
            f"【回答】{content[:1500]}\n\n"
            '请严格按 JSON 格式输出：{"correctness": X, "completeness": X, "expression": X}'
        )
        text = request_llm_text(system, user, temperature=0)
        if not text:
            return {'judge_correctness': 0, 'judge_completeness': 0, 'judge_expression': 0, 'judge_avg': 0, 'judge_error': 'empty response'}
        text = text.strip()
        # 提取 JSON
        if '{' in text:
            text = text[text.index('{'):text.rindex('}') + 1]
        scores = json.loads(text)
        return {
            'judge_correctness': float(scores.get('correctness', 0)),
            'judge_completeness': float(scores.get('completeness', 0)),
            'judge_expression': float(scores.get('expression', 0)),
            'judge_avg': round((float(scores.get('correctness', 0)) + float(scores.get('completeness', 0)) + float(scores.get('expression', 0))) / 3, 2),
        }
    except Exception as e:
        return {'judge_correctness': 0, 'judge_completeness': 0, 'judge_expression': 0, 'judge_avg': 0, 'judge_error': str(e)}


def _aggregate_results(raw_results: list[dict[str, Any]], questions: list[str],
                       conditions: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """将多次重复实验的原始结果聚合为 mean±std。"""
    aggregated = []
    metric_keys = list(METRIC_LABELS.keys()) + ['quality_score', 'judge_avg']
    for q in questions:
        for cond, label in conditions:
            runs = [r for r in raw_results if r['question'] == q and r['condition'] == cond]
            if not runs:
                continue
            agg: dict[str, Any] = {
                'question': q, 'condition': cond, 'condition_label': label,
                'mode': 'live', 'n_runs': len(runs),
            }
            for mk in metric_keys:
                vals = [r.get(mk, 0) for r in runs if isinstance(r.get(mk, 0), (int, float))]
                if vals:
                    agg[mk] = round(mean(vals), 2)
                    agg[f'{mk}_std'] = round(stdev(vals), 2) if len(vals) >= 2 else 0.0
                else:
                    agg[mk] = 0
                    agg[f'{mk}_std'] = 0.0
            # 一致性指标：所有 run 都比 baseline 差的指标数量
            aggregated.append(agg)
    return aggregated


def _compute_significance(raw_results: list[dict[str, Any]], questions: list[str],
                          conditions: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    """基于重复实验判断每个消融条件的显著性。
    返回 {condition: {significance: '***'/'**'/'*'/'ns', direction: 'down'/'up'/'same'}}
    使用简单的 Cohen's d 效应量 + 一致性检查。
    """
    sig_map: dict[str, dict[str, str]] = {}
    metric_keys = ['quality_score']
    for cond, label in conditions:
        if cond == 'full':
            continue
        base_scores: list[float] = []
        cond_scores: list[float] = []
        for q in questions:
            bvals = [r.get('quality_score', 0) for r in raw_results if r['question'] == q and r['condition'] == 'full']
            cvals = [r.get('quality_score', 0) for r in raw_results if r['question'] == q and r['condition'] == cond]
            base_scores.extend(bvals)
            cond_scores.extend(cvals)

        if len(base_scores) < 2 or len(cond_scores) < 2:
            sig_map[cond] = {'significance': 'n/a', 'direction': 'unknown'}
            continue

        m_base, m_cond = mean(base_scores), mean(cond_scores)
        pooled_std = math.sqrt((stdev(base_scores)**2 + stdev(cond_scores)**2) / 2) if stdev(base_scores) + stdev(cond_scores) > 0 else 1e-9
        cohens_d = (m_base - m_cond) / max(pooled_std, 1e-9)
        direction = 'down' if m_cond < m_base else ('up' if m_cond > m_base else 'same')

        # |d| >= 0.8 → large (***), >= 0.5 → medium (**), >= 0.2 → small (*), else ns
        if abs(cohens_d) >= 0.8:
            sig = '***'
        elif abs(cohens_d) >= 0.5:
            sig = '**'
        elif abs(cohens_d) >= 0.2:
            sig = '*'
        else:
            sig = 'ns'
        sig_map[cond] = {'significance': sig, 'direction': direction, 'cohens_d': round(cohens_d, 2)}
    return sig_map


def _run_single_task(question: str, cond: str, cond_label: str, ri: int) -> dict[str, Any]:
    """单次消融任务（可被进程池调用）。"""
    metrics = _run_live(question, cond)
    metrics['question'] = question
    metrics['condition_label'] = cond_label
    metrics['run_idx'] = ri
    return metrics


def run_ablation(questions: list[str] | None = None,
                 n_repeats: int = 1,
                 include_pairwise: bool = False,
                 use_judge: bool = False,
                 max_workers: int = 1) -> dict[str, Any]:
    """对给定问题列表运行全部消融条件，返回结构化结果。

    Args:
        questions: 测试问题列表
        n_repeats: 每个条件重复运行次数（≥2 可计算 std 和显著性）
        include_pairwise: 是否包含组合消融条件
        use_judge: 是否使用 LLM-as-Judge 打分（额外 LLM 调用）
        max_workers: 并行工作线程数（1=串行，>1=进程级并行）
    """
    questions = questions or DEMO_QUESTIONS[:1]
    active_conditions = list(SINGLE_CONDITIONS)
    if include_pairwise:
        active_conditions.extend(PAIRWISE_CONDITIONS)

    all_results: list[dict[str, Any]] = []
    W = 76
    total_runs = len(questions) * len(active_conditions) * n_repeats

    print(f'\n{"═" * W}', flush=True)
    print(f'  消融实验（Ablation Study）', flush=True)
    print(f'  时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', flush=True)
    print(f'  问题数: {len(questions)} | 条件数: {len(active_conditions)} | 重复: {n_repeats}', flush=True)
    print(f'  总运行次数: {total_runs}', flush=True)
    if use_judge:
        print(f'  LLM-as-Judge: 启用（pipeline 模式，与下次 run 重叠）', flush=True)
    if max_workers > 1:
        print(f'  并行进程: {max_workers}', flush=True)
    print(f'{"═" * W}', flush=True)

    # ── Judge pipeline: 后台线程池异步处理 judge 调用 ─────────────────
    judge_pool = ThreadPoolExecutor(max_workers=min(max_workers, 4)) if use_judge else None
    judge_futures: dict = {}  # future -> run index

    def _submit_judge(idx: int, m: dict):
        """提交 judge 任务到后台线程池。"""
        if judge_pool is None:
            return
        q_text = m['question']
        preview = m.get('_content_preview', '')
        fut = judge_pool.submit(_llm_judge, q_text, preview)
        judge_futures[fut] = idx

    def _collect_judge_results():
        """收集所有已完成的 judge 结果，写回 all_results。"""
        done_keys = [f for f in judge_futures if f.done()]
        for fut in done_keys:
            idx = judge_futures.pop(fut)
            try:
                scores = fut.result(timeout=0)
                all_results[idx].update(scores)
            except Exception as e:
                all_results[idx]['judge_error'] = str(e)

    # ── 主运行循环（串行，保证 patch 安全）────────────────────────────
    run_idx = 0
    for qi, question in enumerate(questions, 1):
        print(f'\n{"─" * W}', flush=True)
        print(f'  问题 {qi}/{len(questions)}: {question}', flush=True)
        print(f'{"─" * W}', flush=True)

        for ci, (cond, cond_label) in enumerate(active_conditions, 1):
            for ri in range(n_repeats):
                run_idx += 1
                rep_tag = f'R{ri+1}' if n_repeats > 1 else ''
                tag = f'[{run_idx}/{total_runs}]'
                print(f'\n  {tag} 🔬 {cond_label} ({cond}) {rep_tag}', flush=True)
                print(f'  {tag} 运行中…', flush=True, end='')

                # 顺便收集已完成的 judge 结果
                _collect_judge_results()

                try:
                    metrics = _run_single_task(question, cond, cond_label, ri)
                    all_results.append(metrics)
                    result_idx = len(all_results) - 1

                    # 异步提交 judge（不等待，与下一次 run 重叠）
                    _submit_judge(result_idx, metrics)

                    print(f'\r  {tag} ✅ {metrics["elapsed"]:.1f}s | '
                          f'内容:{metrics["content_length"]}字 | '
                          f'图表:{"有" if metrics["has_chart"] else "无"} | '
                          f'溯源:{metrics["provenance_count"]} | '
                          f'KB:{metrics["kb_refs_count"]}',
                          flush=True)
                except Exception as e:
                    print(f'\r  {tag} ❌ 失败: {e}', flush=True)
                    metrics = {
                        'question': question, 'condition': cond, 'condition_label': cond_label,
                        'status': 'error', 'mode': 'live', 'run_idx': ri, 'error': str(e),
                        **{k: 0 for k in METRIC_LABELS},
                    }
                    all_results.append(metrics)

    # ── 等待所有 judge 完成 ────────────────────────────────────────────
    if judge_pool:
        remaining = len(judge_futures)
        if remaining:
            print(f'\n  ⏳ 等待 {remaining} 个 Judge 评分完成…', flush=True, end='')
        for fut in as_completed(judge_futures):
            idx = judge_futures[fut]
            try:
                scores = fut.result(timeout=120)
                all_results[idx].update(scores)
            except Exception as e:
                all_results[idx]['judge_error'] = str(e)
        judge_futures.clear()
        judge_pool.shutdown(wait=False)
        if remaining:
            print(f'\r  ✅ 全部 Judge 评分完成', flush=True)

    # ── 计算质量评分（每个 run 独立计算）──────────────────────────────────
    for q in questions:
        baselines = [r for r in all_results if r['question'] == q and r['condition'] == 'full']
        if not baselines:
            continue
        # 用 baseline 各 run 的平均值作为基准
        avg_baseline: dict[str, Any] = {}
        for mk in QUALITY_WEIGHTS:
            vals = [r.get(mk, 0) for r in baselines]
            avg_baseline[mk] = mean(vals) if vals else 0
        for r in all_results:
            if r['question'] == q:
                r['quality_score'] = compute_quality_score(r, avg_baseline)

    # ── 聚合统计 ──────────────────────────────────────────────────────────
    aggregated = _aggregate_results(all_results, questions, active_conditions)
    significance = _compute_significance(all_results, questions, active_conditions) if n_repeats >= 2 else {}

    # ── 终端汇总 ──────────────────────────────────────────────────────────
    print(f'\n{"═" * W}', flush=True)
    print(f'  消融实验完成 — 结果汇总', flush=True)
    print(f'{"═" * W}', flush=True)

    _print_summary_table(aggregated, questions, significance, n_repeats)

    return {
        'questions': questions, 'conditions': active_conditions,
        'results': all_results, 'aggregated': aggregated,
        'significance': significance, 'n_repeats': n_repeats,
        'use_judge': use_judge,
    }


def _print_summary_table(results: list[dict], questions: list[str],
                         significance: dict | None = None, n_repeats: int = 1):
    """在终端打印消融结果对比表（含 mean±std 和显著性标记）。"""
    key_metrics = ['content_length', 'has_chart', 'provenance_count',
                   'calc_traces_count', 'has_thinking_map', 'kb_refs_count',
                   'thinking_count', 'elapsed']
    show_std = n_repeats >= 2
    significance = significance or {}

    for qi, q in enumerate(questions):
        q_results = [r for r in results if r['question'] == q]
        baseline = next((r for r in q_results if r['condition'] == 'full'), None)
        if not baseline:
            continue

        print(f'\n  问题: {q[:60]}', flush=True)
        if show_std:
            print(f'  （每条件重复 {n_repeats} 次，显示 mean±std）', flush=True)
        header = f'    {"条件":<16s}'
        for mk in key_metrics:
            label = METRIC_LABELS[mk][0][:6]
            header += f' {label:>10s}'
        header += f' {"评分":>8s}'
        if significance:
            header += f' {"显著性":>6s}'
        print(header, flush=True)
        print(f'    {"─" * 16}' + (' ' + '─' * 10) * len(key_metrics) + ' ' + '─' * 8
              + (' ' + '─' * 6 if significance else ''), flush=True)

        for r in q_results:
            line = f'    {r["condition_label"]:<16s}'
            for mk in key_metrics:
                val = r.get(mk, 0)
                std_val = r.get(f'{mk}_std', 0)
                base_val = baseline.get(mk, 0)
                if isinstance(val, float):
                    cell = f'{val:.1f}'
                else:
                    cell = str(val)
                if show_std and std_val > 0:
                    cell += f'±{std_val:.1f}'
                if r['condition'] != 'full' and isinstance(val, (int, float)) and val < base_val:
                    cell += '↓'
                line += f' {cell:>10s}'
            score = r.get('quality_score', 0)
            std_score = r.get('quality_score_std', 0)
            score_cell = f'{score:.1f}'
            if show_std and std_score > 0:
                score_cell += f'±{std_score:.1f}'
            line += f' {score_cell:>8s}'
            if significance and r['condition'] != 'full':
                sig_info = significance.get(r['condition'], {})
                sig_mark = sig_info.get('significance', '')
                line += f' {sig_mark:>6s}'
            print(line, flush=True)

        # 组件重要度排名
        print(f'\n  📊 组件重要度排名（质量损失 = 100 - 消融后评分）:', flush=True)
        ranked = sorted(
            [(r['condition_label'], 100 - r.get('quality_score', 100), r['condition'])
             for r in q_results if r['condition'] != 'full'],
            key=lambda x: x[1], reverse=True,
        )
        for i, (label, loss, cond) in enumerate(ranked, 1):
            sig_info = significance.get(cond, {})
            sig_mark = sig_info.get('significance', '')
            bar = '█' * int(loss / 2) + '░' * (50 - int(loss / 2))
            print(f'    {i}. {label:<16s} 损失 {loss:>5.1f}分  {bar}  {sig_mark}', flush=True)

    if significance:
        print(f'\n  显著性标记: *** Cohen\'s d≥0.8(大)  ** d≥0.5(中)  * d≥0.2(小)  ns 不显著', flush=True)
    print(flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  HTML 可视化报告生成
# ═══════════════════════════════════════════════════════════════════════════════

def generate_html_report(ablation_data: dict[str, Any], output_path: str | None = None) -> str:
    """根据消融实验数据生成 HTML 可视化报告。"""

    # 优先使用聚合数据（含 mean±std），回退到原始数据
    results = ablation_data.get('aggregated') or ablation_data['results']
    questions = ablation_data['questions']
    conditions = ablation_data['conditions']
    n_repeats = ablation_data.get('n_repeats', 1)
    significance = ablation_data.get('significance', {})
    use_judge = ablation_data.get('use_judge', False)

    if output_path is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = os.path.join(_THIS_DIR, f'ablation_report_{ts}.html')

    def _esc(s):
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # ── 每个问题的对比数据 ────────────────────────────────────────────────
    key_metrics = [
        ('content_length',    '内容长度'),
        ('has_chart',         '图表'),
        ('provenance_count',  '溯源'),
        ('calc_traces_count', '计算链'),
        ('attribution_steps', '归因'),
        ('has_thinking_map',  '思维导图'),
        ('insights_count',    '洞察'),
        ('follow_up_count',   '建议'),
        ('kb_refs_count',     '知识库'),
        ('thinking_count',    '思考'),
    ]

    # ── 构建 per-question 表格和柱状图 HTML ───────────────────────────────
    question_sections = ''
    for qi, q in enumerate(questions):
        q_results = [r for r in results if r['question'] == q]
        baseline = next((r for r in q_results if r['condition'] == 'full'), None)
        if not baseline:
            continue

        # 对比表（含质量评分 + mean±std + 显著性）
        show_std = n_repeats >= 2
        table_html = '<table class="abl-table"><thead><tr><th>条件</th>'
        if show_std:
            table_html += '<th>N</th>'
        for _, label in key_metrics:
            table_html += f'<th>{_esc(label)}</th>'
        table_html += '<th>耗时(s)</th><th>质量评分</th>'
        if use_judge:
            table_html += '<th>Judge</th>'
        if significance:
            table_html += '<th>显著性</th>'
        table_html += '</tr></thead><tbody>'

        for r in q_results:
            is_base = r['condition'] == 'full'
            cls = 'row-baseline' if is_base else ''
            table_html += f'<tr class="{cls}"><td><b>{_esc(r.get("condition_label", r["condition"]))}</b></td>'
            if show_std:
                table_html += f'<td>{r.get("n_runs", 1)}</td>'
            for mk, _ in key_metrics:
                val = r.get(mk, 0)
                std_val = r.get(f'{mk}_std', 0)
                base_val = baseline.get(mk, 0)
                cell_text = f'{val:.1f}' if isinstance(val, float) else str(val)
                if show_std and std_val > 0:
                    cell_text += f'<small style="color:#888;">±{std_val:.1f}</small>'
                if is_base:
                    table_html += f'<td class="val-base">{cell_text}</td>'
                elif isinstance(val, (int, float)) and val < base_val:
                    table_html += f'<td class="val-down">{cell_text} ↓</td>'
                else:
                    table_html += f'<td>{cell_text}</td>'
            # 质量评分
            score = r.get('quality_score', 0)
            std_score = r.get('quality_score_std', 0)
            score_cls = 'val-base' if is_base else ('val-down' if score < 90 else '')
            elapsed = r.get('elapsed', 0)
            std_elapsed = r.get('elapsed_std', 0)
            elapsed_text = f'{elapsed:.1f}'
            if show_std and std_elapsed > 0:
                elapsed_text += f'<small style="color:#888;">±{std_elapsed:.1f}</small>'
            table_html += f'<td>{elapsed_text}</td>'
            score_text = f'{score:.1f}'
            if show_std and std_score > 0:
                score_text += f'<small style="color:#888;">±{std_score:.1f}</small>'
            table_html += f'<td class="{score_cls}" style="font-weight:700;font-size:15px;">{score_text}</td>'
            if use_judge:
                judge = r.get('judge_avg', 0)
                std_judge = r.get('judge_avg_std', 0)
                jtext = f'{judge:.1f}'
                if show_std and std_judge > 0:
                    jtext += f'<small>±{std_judge:.1f}</small>'
                table_html += f'<td>{jtext}</td>'
            if significance and not is_base:
                sig_info = significance.get(r['condition'], {})
                sig_mark = sig_info.get('significance', '')
                d_val = sig_info.get('cohens_d', '')
                sig_cls = 'sig-high' if sig_mark == '***' else 'sig-mid' if sig_mark == '**' else 'sig-low'
                table_html += f'<td class="{sig_cls}">{sig_mark}'
                if d_val:
                    table_html += f' <small>(d={d_val})</small>'
                table_html += '</td>'
            elif significance and is_base:
                table_html += '<td>—</td>'
            table_html += '</tr>'
        table_html += '</tbody></table>'

        # 质量评分柱状图
        bar_html = '<div class="bar-chart-container">'
        sorted_results = sorted(
            [r for r in q_results if r['condition'] != 'full'],
            key=lambda x: x.get('quality_score', 0),
        )
        for r in sorted_results:
            label = r.get('condition_label', r['condition'])
            score = r.get('quality_score', 0)
            loss = round(100 - score, 1)
            mode = r.get('mode', '?')

            # 具体损失的维度
            lost = []
            for mk, ml in key_metrics:
                bv = baseline.get(mk, 0)
                cv = r.get(mk, 0)
                if bv > 0 and cv < bv:
                    if isinstance(bv, int) and bv > 1:
                        lost.append(f'{ml} ({cv}/{bv})')
                    else:
                        lost.append(ml)
            lost_str = ', '.join(lost) if lost else '无损失'

            sig_info = significance.get(r['condition'], {})
            sig_mark = sig_info.get('significance', '')
            std_score = r.get('quality_score_std', 0)
            std_text = f' ±{std_score:.1f}' if show_std and std_score > 0 else ''

            bar_cls = 'bar-low' if score < 50 else 'bar-partial' if score < 85 else 'bar-full'
            bar_html += f'''<div class="bar-row">
                <div class="bar-label">{_esc(label)}</div>
                <div class="bar-track">
                    <div class="bar-fill {bar_cls}" style="width:{score}%">{score}{std_text}分 (损失{loss}) {sig_mark}</div>
                </div>
                <div class="bar-lost">{_esc(lost_str)}</div>
            </div>\n'''
        bar_html += '</div>'

        # 雷达图数据（用 CSS 模拟的表格热力图）
        heat_html = '<div class="heatmap"><table class="heat-table"><thead><tr><th></th>'
        for _, ml in key_metrics:
            heat_html += f'<th>{_esc(ml)}</th>'
        heat_html += '</tr></thead><tbody>'
        for r in q_results:
            heat_html += f'<tr><td><b>{_esc(r.get("condition_label", r["condition"]))}</b></td>'
            for mk, _ in key_metrics:
                val = r.get(mk, 0)
                base_val = baseline.get(mk, 0)
                if base_val == 0:
                    ratio = 1.0
                else:
                    ratio = val / base_val
                # 颜色映射
                if ratio >= 1.0:
                    bg = '#d1fae5'  # green
                elif ratio >= 0.5:
                    bg = '#fef3c7'  # yellow
                elif ratio > 0:
                    bg = '#fed7aa'  # orange
                else:
                    bg = '#fecaca'  # red
                heat_html += f'<td style="background:{bg};text-align:center;font-weight:600;">{val}</td>'
            heat_html += '</tr>'
        heat_html += '</tbody></table></div>'

        question_sections += f'''
        <div class="card">
            <div class="card-header">问题 {qi+1}: {_esc(q[:60])}</div>
            <div class="card-body">
                <h3>对比表</h3>
                {table_html}
                <h3 style="margin-top:20px;">质量评分排名（Quality Score Ranking）</h3>
                {bar_html}
                <h3 style="margin-top:20px;">热力图（Heatmap）</h3>
                {heat_html}
            </div>
        </div>
        '''

    # ── 全局特征重要度：基于 quality_score 计算每个组件的贡献度 ─────────
    importance_html = ''
    if questions:
        component_losses: dict[str, list[float]] = {}
        for cond, cond_label in conditions:
            if cond == 'full':
                continue
            losses = []
            for q in questions:
                ablated = next((r for r in results if r['question'] == q and r['condition'] == cond), None)
                if ablated:
                    losses.append(100 - ablated.get('quality_score', 100))
            if losses:
                component_losses[cond_label] = losses

        # 按平均损失排序
        sorted_impacts = sorted(
            [(label, round(sum(ls) / len(ls), 1), ls) for label, ls in component_losses.items()],
            key=lambda x: x[1], reverse=True,
        )
        importance_html = '<div class="importance-chart">'
        for label, avg_loss, _ in sorted_impacts:
            # 找到模式
            cond_id = next((c for c, l in conditions if l == label), '')
            mode = 'live' if cond_id in LIVE_CONDITIONS else 'posthoc'
            bar_cls = 'imp-high' if avg_loss >= 20 else 'imp-mid' if avg_loss >= 8 else 'imp-low'
            importance_html += f'''<div class="imp-row">
                <div class="imp-label">{_esc(label)} <small style="color:#888;">[{mode}]</small></div>
                <div class="imp-track">
                    <div class="imp-fill {bar_cls}" style="width:{min(avg_loss * 2, 100)}%">损失 {avg_loss} 分</div>
                </div>
            </div>\n'''
        importance_html += '</div>'

    # ── 完整 HTML ─────────────────────────────────────────────────────────
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>消融实验报告 — Ablation Study</title>
<style>
:root {{
    --blue: #023047; --light-blue: #90C9E6; --yellow: #FFB703;
    --orange: #FB8402; --teal: #219EBD; --gray: #D0D0D0; --dark: #404040;
    --green: #10b981; --red: #ef4444;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; background: #f0f4f8; color: var(--dark); line-height: 1.6; }}
.container {{ max-width: 1280px; margin: 0 auto; padding: 24px 16px; }}
h1 {{ background: linear-gradient(135deg, var(--blue), #035a7a); color: #fff; padding: 24px 32px; border-radius: 12px; font-size: 22px; margin-bottom: 20px; }}
h1 small {{ display: block; font-weight: 400; font-size: 13px; color: var(--light-blue); margin-top: 6px; }}
h3 {{ color: var(--blue); font-size: 15px; margin-bottom: 10px; }}
.card {{ background: #fff; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,.06); overflow: hidden; }}
.card-header {{ padding: 14px 22px; font-weight: 700; font-size: 16px; border-bottom: 2px solid var(--light-blue); color: var(--blue); }}
.card-body {{ padding: 18px 22px; }}
/* 表格 */
.abl-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.abl-table th {{ background: var(--blue); color: #fff; padding: 10px 8px; text-align: center; font-weight: 600; white-space: nowrap; }}
.abl-table td {{ padding: 8px; text-align: center; border-bottom: 1px solid #e5e7eb; }}
.abl-table tr:hover {{ background: #f0f9ff; }}
.row-baseline {{ background: #ecfdf5 !important; }}
.row-baseline:hover {{ background: #d1fae5 !important; }}
.val-base {{ font-weight: 700; color: var(--green); }}
.val-down {{ color: var(--red); font-weight: 600; }}
/* 模式标签 */
.mode-badge {{ display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 8px; font-weight: 600; }}
.mode-live {{ background: #dbeafe; color: #1e40af; }}
.mode-posthoc {{ background: #fef3c7; color: #92400e; }}
/* 柱状图 */
.bar-chart-container {{ padding: 8px 0; }}
.bar-row {{ display: flex; align-items: center; margin-bottom: 8px; gap: 10px; }}
.bar-label {{ width: 130px; font-size: 13px; font-weight: 600; text-align: right; flex-shrink: 0; }}
.bar-track {{ flex: 1; background: #e5e7eb; border-radius: 6px; height: 28px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 6px; display: flex; align-items: center; padding: 0 10px; font-size: 12px; font-weight: 700; color: #fff; transition: width 0.5s; min-width: 40px; }}
.bar-full {{ background: var(--green); }}
.bar-partial {{ background: var(--yellow); color: var(--blue); }}
.bar-low {{ background: var(--orange); }}
.bar-lost {{ font-size: 12px; color: #888; min-width: 180px; }}
/* 热力图 */
.heat-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
.heat-table th {{ background: var(--blue); color: #fff; padding: 8px 6px; text-align: center; font-size: 11px; }}
.heat-table td {{ padding: 6px; border: 1px solid #e5e7eb; font-size: 13px; }}
/* 特征重要度 */
.importance-chart {{ padding: 8px 0; }}
.imp-row {{ display: flex; align-items: center; margin-bottom: 8px; gap: 10px; }}
.imp-label {{ width: 160px; font-size: 14px; font-weight: 600; text-align: right; flex-shrink: 0; }}
.imp-track {{ flex: 1; background: #e5e7eb; border-radius: 6px; height: 32px; overflow: hidden; }}
.imp-fill {{ height: 100%; border-radius: 6px; display: flex; align-items: center; padding: 0 12px; font-size: 13px; font-weight: 700; color: #fff; min-width: 50px; }}
.imp-high {{ background: linear-gradient(90deg, var(--red), #f97316); }}
.imp-mid {{ background: linear-gradient(90deg, var(--orange), var(--yellow)); color: var(--blue); }}
.imp-low {{ background: linear-gradient(90deg, var(--teal), var(--light-blue)); }}
/* 实验设计说明 */
.design-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }}
.design-item {{ background: #f8fafc; border-radius: 8px; padding: 12px 16px; border-left: 4px solid var(--teal); }}
.design-item b {{ color: var(--blue); }}
.design-item .tag {{ display: inline-block; background: var(--blue); color: #fff; font-size: 11px; padding: 1px 8px; border-radius: 8px; margin-right: 6px; }}
/* 显著性标记 */
.sig-high {{ color: var(--red); font-weight: 800; }}
.sig-mid {{ color: var(--orange); font-weight: 700; }}
.sig-low {{ color: #a3a3a3; font-weight: 500; }}
.config-badges {{ display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }}
.config-badge {{ display: inline-block; font-size: 12px; padding: 3px 10px; border-radius: 6px; font-weight: 600; }}
.config-badge-blue {{ background: #dbeafe; color: #1e40af; }}
.config-badge-green {{ background: #d1fae5; color: #065f46; }}
.config-badge-amber {{ background: #fef3c7; color: #92400e; }}
</style>
</head>
<body>
<div class="container">

<h1>消融实验报告（Ablation Study）
    <small>生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} | 问题: {len(questions)} | 条件: {len(conditions)} | 重复: {n_repeats}x | 共 {len(ablation_data.get('results', results))} 次实验</small>
    <div class="config-badges">
        <span class="config-badge config-badge-blue">重复 {n_repeats}x</span>
        {'<span class="config-badge config-badge-green">LLM-as-Judge ✓</span>' if use_judge else ''}
        {'<span class="config-badge config-badge-amber">含组合消融</span>' if any(c for c, _ in conditions if c.count('-') > 1) else ''}
        {'<span class="config-badge config-badge-green">Cohen\'s d 显著性</span>' if significance else ''}
    </div>
</h1>

<!-- 实验设计 -->
<div class="card">
    <div class="card-header">实验设计</div>
    <div class="card-body">
        <p style="margin-bottom:12px;font-size:14px;">
            对增强层的 <b>7 大组件</b>逐一禁用，与 Baseline（全量开启）对比，量化每个组件对最终回答质量的贡献。
        </p>
        <div class="design-grid">
            {''.join(f'<div class="design-item"><span class="tag">{_esc(c)}</span><b>{_esc(l)}</b></div>' for c, l in conditions)}
        </div>
    </div>
</div>

<!-- 特征重要度 -->
<div class="card">
    <div class="card-header">组件重要度排名（Feature Importance）</div>
    <div class="card-body">
        <p style="margin-bottom:12px;font-size:13px;color:#666;">
            数值 = 禁用该组件后，各指标平均损失百分比。越高表示该组件越关键。
        </p>
        {importance_html or '<div style="color:#999;font-style:italic;">（数据不足）</div>'}
    </div>
</div>

<!-- 各问题详细结果 -->
{question_sections}

</div>
</body>
</html>'''

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
#  JSON 结果保存/加载
# ═══════════════════════════════════════════════════════════════════════════════

def save_results_json(ablation_data: dict[str, Any], path: str | None = None) -> str:
    """将消融实验结果保存为 JSON（方便后续分析或重新可视化）。"""
    if path is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = os.path.join(_THIS_DIR, f'ablation_results_{ts}.json')

    serializable = {
        'questions': ablation_data['questions'],
        'conditions': ablation_data['conditions'],
        'results': ablation_data['results'],
        'aggregated': ablation_data.get('aggregated', []),
        'significance': ablation_data.get('significance', {}),
        'n_repeats': ablation_data.get('n_repeats', 1),
        'use_judge': ablation_data.get('use_judge', False),
        'timestamp': datetime.now().isoformat(),
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)
    return path


def load_and_visualize(json_path: str, html_path: str | None = None) -> str:
    """从已保存的 JSON 重新生成 HTML 报告。"""
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return generate_html_report(data, html_path)


# ═══════════════════════════════════════════════════════════════════════════════
#  命令行入口
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description='增强层消融实验')
    parser.add_argument('question', nargs='?', default=None, help='自定义问题')
    parser.add_argument('--questions', '-n', type=int, default=1,
                        help='使用前 N 个默认问题（1-7）')
    parser.add_argument('--repeats', '-r', type=int, default=1,
                        help='每个条件重复次数（≥2 启用 mean±std + 显著性）')
    parser.add_argument('--pairwise', '-p', action='store_true',
                        help='包含组合消融条件（测试组件交互效应）')
    parser.add_argument('--judge', action='store_true',
                        help='启用 LLM-as-Judge 语义质量打分')
    parser.add_argument('--workers', '-w', type=int, default=1,
                        help='Judge 并行线程数（加速 Judge 调用，默认 1）')
    parser.add_argument('--output', '-o', default=None, help='HTML 输出路径')
    parser.add_argument('--json', '-j', action='store_true', help='同时保存 JSON')
    args = parser.parse_args()

    if args.question:
        questions = [args.question]
    else:
        n = min(max(args.questions, 1), len(DEMO_QUESTIONS))
        questions = DEMO_QUESTIONS[:n]

    # 运行消融实验
    data = run_ablation(
        questions,
        n_repeats=args.repeats,
        include_pairwise=args.pairwise,
        use_judge=args.judge,
        max_workers=args.workers,
    )

    # 生成 HTML
    html_path = generate_html_report(data, args.output)
    print(f'\n📊 HTML 可视化报告: {html_path}', flush=True)

    # 可选保存 JSON
    if args.json:
        json_path = save_results_json(data)
        print(f'💾 JSON 数据: {json_path}', flush=True)

    # 打开浏览器
    try:
        import subprocess
        subprocess.Popen(f'start "" "{html_path}"', shell=True)
        print('🌐 已在浏览器中打开', flush=True)
    except Exception:
        pass


if __name__ == '__main__':
    main()
