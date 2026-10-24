#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
4.1 模型选择与性能对比实验
===========================
在本系统中更换对话大模型 API，评测不同模型在财报智能问数场景下的表现。
意图识别模型与对话模型使用同一个大模型（同步切换）。

评测指标（与论文一致）:
  1. 平均响应时间 (秒)       — 自动测量
  2. 回答质量 (1-5 分)       — 人工评分（准确性 + 流畅度）
  3. 成本 (元/10万token)     — 手动填入各模型官方定价
  4. 安全合规性 (1-5 分)     — 人工评分（隐私、本地化、内容安全）
  5. 综合得分                — 加权计算

使用方法:
  # 1) 配置待测模型列表（见 MODELS_TO_TEST）
  # 2) 运行实验
  python -m benchmark.model_compare

  # 3) 自动运行完成后，打开生成的 HTML 报告
  #    手动填入 回答质量 和 安全合规性 评分到 CSV 文件
  # 4) 重新运行以生成最终报告（含人工评分）
  python -m benchmark.model_compare --finalize
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

# ── 路径设置 ────────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════════════════════════
#  待测模型配置
# ═══════════════════════════════════════════════════════════════════════════════
# 每个模型需要: name, base_url, model, api_key, cost_per_100k_tokens
# cost_per_100k_tokens: 元/10万token，用于成本对比（API 模型填官方定价，
#                       本地模型填硬件折旧估算）
#
# ⚠ 请根据实际情况修改以下配置。API 密钥请通过环境变量注入，不要写进源码：
#   SILICONFLOW_API_KEY  — SiliconFlow 平台密钥
#   BIGMODEL_API_KEY     — 智谱（open.bigmodel.cn）平台密钥

MODELS_TO_TEST: list[dict[str, Any]] = [
    # ── 从弱到强排列 ──
    {
        'name': 'GLM-4-Flash',
        'base_url': 'https://open.bigmodel.cn/api/paas/v4',
        'model': 'glm-4-flash',
        'api_key': os.environ.get('BIGMODEL_API_KEY', ''),
        'cost_per_100k_tokens': 0, 
        'safety_score': 3.5,
        'deploy_type': 'API',
    },
    {
        'name': 'DeepSeek-V3',
        'base_url': 'https://api.siliconflow.cn/v1',
        'model': 'deepseek-ai/DeepSeek-V3',
        'api_key': os.environ.get('SILICONFLOW_API_KEY', ''),
        'cost_per_100k_tokens': 0.5,
        'safety_score': 4.0,
        'deploy_type': 'API',
    },
    {
        'name': 'Qwen2.5-72B-Instruct',
        'base_url': 'https://api.siliconflow.cn/v1',
        'model': 'Qwen/Qwen2.5-72B-Instruct',
        'api_key': os.environ.get('SILICONFLOW_API_KEY', ''),
        'cost_per_100k_tokens': 0.413,
        'safety_score': 4.2,
        'deploy_type': 'API',
    },
    {
        'name': 'GLM-5.1',
        'base_url': 'https://api.siliconflow.cn/v1',
        'model': 'Pro/zai-org/GLM-5.1',
        'api_key': os.environ.get('SILICONFLOW_API_KEY', ''),
        'cost_per_100k_tokens': 1.5,  
        'safety_score': 4.5,
        'deploy_type': 'API',
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
#  评分权重（综合得分计算）
# ═══════════════════════════════════════════════════════════════════════════════
# 各指标归一化后加权求和，满分 10 分
WEIGHTS = {
    'response_time': 0.15,    # 响应时间（越短越好）
    'quality':       0.40,    # 回答质量（1-5，人工）
    'cost':          0.20,    # 成本效益（越低越好）
    'safety':        0.25,    # 安全合规性（1-5，人工）
}


# ═══════════════════════════════════════════════════════════════════════════════
#  输出路径
# ═══════════════════════════════════════════════════════════════════════════════
OUTPUT_DIR = _THIS_DIR / 'model_compare_results'


# ═══════════════════════════════════════════════════════════════════════════════
#  核心：切换模型并运行测试
# ═══════════════════════════════════════════════════════════════════════════════

def _switch_model(model_cfg: dict[str, Any]) -> dict[str, Any]:
    """临时切换 CONFIG 中的 llm 和 intent_llm 为指定模型，返回旧配置用于恢复。"""
    from config import CONFIG

    old_llm = dict(CONFIG.get('llm', {}))
    old_intent = dict(CONFIG.get('intent_llm', {}))

    # 同时切换对话模型和意图模型
    for section_key in ('llm', 'intent_llm'):
        section = CONFIG.setdefault(section_key, {})
        section['api_key'] = model_cfg['api_key']
        section['base_url'] = model_cfg['base_url'].rstrip('/')
        section['model'] = model_cfg['model']
        section['request_timeout'] = 300   # 放宽到 5 分钟，避免慢模型超时

    return {'llm': old_llm, 'intent_llm': old_intent}


def _restore_model(old_cfg: dict[str, Any]) -> None:
    """恢复 CONFIG 中的 llm 和 intent_llm 到旧配置。"""
    from config import CONFIG
    CONFIG['llm'].update(old_cfg['llm'])
    CONFIG['intent_llm'].update(old_cfg['intent_llm'])


def load_questions(limit: int | None = None) -> list[dict]:
    """加载题库。"""
    qpath = _THIS_DIR / 'questions.json'
    if not qpath.exists():
        print(f'❌ 题库文件不存在: {qpath}')
        sys.exit(1)
    with open(qpath, 'r', encoding='utf-8') as f:
        questions = json.load(f)
    if limit:
        questions = questions[:limit]
    return questions


def _auto_score_quality(result: dict[str, Any]) -> float:
    """根据自动化指标为单条回答打分 (1-5)，作为人工评分前的基线。"""
    if result.get('status') == 'error':
        return 1.0
    score = 2.0  # 基础分：能返回结果
    content_len = result.get('response_length', 0)
    row_count = result.get('row_count', 0)
    has_chart = result.get('has_chart', 0)
    has_table = result.get('has_table', 0)
    # 内容丰富度 (+0~1.5)
    if content_len >= 500:
        score += 1.0
    elif content_len >= 200:
        score += 0.5
    # 数据质量 (+0~1.0)
    if row_count >= 3:
        score += 1.0
    elif row_count >= 1:
        score += 0.5
    # 可视化 (+0~0.5)
    if has_chart:
        score += 0.3
    if has_table:
        score += 0.2
    return min(round(score, 1), 5.0)


def run_single_model(
    model_cfg: dict[str, Any],
    questions: list[dict],
) -> list[dict[str, Any]]:
    """对单个模型运行所有测试题，返回每题的结果。"""
    from backend.services.assistant import build_assistant_reply

    model_name = model_cfg['name']
    old = _switch_model(model_cfg)

    results: list[dict[str, Any]] = []
    total = len(questions)
    success_count = 0
    error_count = 0
    timeout_count = 0

    print(f'\n{"=" * 60}')
    print(f'  模型: {model_name} ({model_cfg["model"]})')
    print(f'  题目数: {total}')
    print(f'{"=" * 60}')

    for qi, q in enumerate(questions, 1):
        qid = q['id']
        question_text = q['question']
        print(f'  [{qi}/{total}] {qid}: {question_text[:50]}…', end='', flush=True)

        t0 = time.time()
        status = 'done'
        reply_content = ''
        reply_obj: dict[str, Any] = {}
        error_msg = ''

        try:
            reply_obj = build_assistant_reply(
                question_text, [],
                emit=None,
                deep_think=False,   # 关闭深度思考以加速
                session_id='',
            )
            reply_content = reply_obj.get('content', '')
            status = reply_obj.get('status', 'done')
        except Exception as e:
            status = 'error'
            error_msg = str(e)
            reply_content = f'[错误] {e}'

        elapsed = round(time.time() - t0, 2)

        if status == 'error':
            error_count += 1
            print(f'\r  [{qi}/{total}] {qid}: ❌ {elapsed:.1f}s | {error_msg[:40]}', flush=True)
        elif elapsed > 120:
            timeout_count += 1
            print(f'\r  [{qi}/{total}] {qid}: ⏱ {elapsed:.1f}s | 超时', flush=True)
        else:
            success_count += 1
            print(f'\r  [{qi}/{total}] {qid}: ✅ {elapsed:.1f}s | {len(reply_content)}字', flush=True)

        results.append({
            'question_id': qid,
            'question': question_text,
            'category': q.get('category', ''),
            'difficulty': q.get('difficulty', ''),
            'model_name': model_name,
            'response_time': elapsed,
            'response_length': len(reply_content),
            'response_text': reply_content[:2000],   # 截断保存
            'status': status,
            'error': error_msg,
            'has_chart': 1 if reply_obj.get('chart') else 0,
            'has_table': 1 if reply_obj.get('rows') else 0,
            'row_count': len(reply_obj.get('rows', [])),
            'quality_score': None,    # 人工评分 1-5
        })
        # 自动评分作为基线（人工可覆盖）
        results[-1]['quality_score'] = _auto_score_quality(results[-1])

    _restore_model(old)

    print(f'\n  📊 {model_name} 完成: 成功={success_count}, 错误={error_count}, 超时={timeout_count}')
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  聚合统计
# ═══════════════════════════════════════════════════════════════════════════════

def compute_model_summary(
    model_cfg: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """计算单个模型的汇总指标。"""
    valid_times = [r['response_time'] for r in results
                   if r['status'] != 'error' and r['response_time'] <= 120]
    quality_scores = [r['quality_score'] for r in results
                      if r.get('quality_score') is not None]

    avg_time = round(sum(valid_times) / len(valid_times), 2) if valid_times else -1
    avg_quality = round(sum(quality_scores) / len(quality_scores), 1) if quality_scores else None
    success_rate = round(
        sum(1 for r in results if r['status'] == 'done') / len(results) * 100, 1
    ) if results else 0

    cost = model_cfg.get('cost_per_100k_tokens')
    safety = model_cfg.get('safety_score')
    cost_str = f'{cost:.2f}' if cost is not None and cost > 0 else '免费' if cost == 0 else '-'

    return {
        'model_name': model_cfg['name'],
        'deploy_type': model_cfg.get('deploy_type', ''),
        'avg_response_time': avg_time,
        'avg_quality': avg_quality,
        'cost_per_100k_tokens': cost,
        'cost_display': cost_str,
        'safety_score': safety,
        'success_rate': success_rate,
        'total_questions': len(results),
        'error_count': sum(1 for r in results if r['status'] == 'error'),
    }


def compute_composite_score(summary: dict[str, Any], all_summaries: list[dict[str, Any]]) -> float | None:
    """计算综合得分 (0-10)。需要人工评分完成后才能计算。"""
    quality = summary.get('avg_quality')
    safety = summary.get('safety_score')
    if quality is None or safety is None:
        return None

    avg_time = summary['avg_response_time']
    cost = summary.get('cost_per_100k_tokens')
    if cost is None:
        cost = 0

    # 归一化响应时间: 最快的得 10 分，最慢的得 2 分
    all_times = [s['avg_response_time'] for s in all_summaries if s['avg_response_time'] > 0]
    if all_times:
        t_min, t_max = min(all_times), max(all_times)
        if t_max > t_min:
            time_score = 10 - 8 * (avg_time - t_min) / (t_max - t_min)
        else:
            time_score = 10
    else:
        time_score = 5

    # 归一化质量: 直接 * 2 (1-5 → 2-10)
    quality_score = quality * 2

    # 归一化成本: 最低的得 10 分（免费=0 → 10分）
    all_costs = [s['cost_per_100k_tokens'] for s in all_summaries
                 if s.get('cost_per_100k_tokens') is not None]
    if all_costs:
        c_max = max(all_costs)
        if c_max > 0:
            cost_score = 10 * (1 - cost / c_max)
        else:
            cost_score = 10
    else:
        cost_score = 5

    # 归一化安全: 直接 * 2
    safety_score = safety * 2

    composite = (
        WEIGHTS['response_time'] * time_score
        + WEIGHTS['quality'] * quality_score
        + WEIGHTS['cost'] * cost_score
        + WEIGHTS['safety'] * safety_score
    )
    return round(composite, 1)


# ═══════════════════════════════════════════════════════════════════════════════
#  CSV 导出（供人工评分）
# ═══════════════════════════════════════════════════════════════════════════════

def export_scoring_csv(all_results: dict[str, list[dict]], output_path: Path) -> None:
    """导出 CSV 文件供人工评分。"""
    rows = []
    for model_name, results in all_results.items():
        for r in results:
            rows.append({
                '模型': model_name,
                '题目ID': r['question_id'],
                '题目': r['question'][:80],
                '类别': r['category'],
                '难度': r['difficulty'],
                '响应时间(秒)': r['response_time'],
                '回答长度': r['response_length'],
                '状态': r['status'],
                '回答摘要': r['response_text'][:300],
                '回答质量(1-5)': r.get('quality_score', ''),
            })

    with open(output_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys() if rows else [])
        writer.writeheader()
        writer.writerows(rows)

    print(f'  📝 评分CSV已导出: {output_path}')


def import_scoring_csv(csv_path: Path) -> dict[str, dict[str, float]]:
    """读取人工评分CSV，返回 {model_name: {question_id: score}}。"""
    scores: dict[str, dict[str, float]] = {}
    if not csv_path.exists():
        return scores
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            model = row.get('模型', '')
            qid = row.get('题目ID', '')
            raw = row.get('回答质量(1-5)', '')
            if model and qid and raw:
                try:
                    score = float(raw)
                    scores.setdefault(model, {})[qid] = score
                except ValueError:
                    pass
    return scores


# ═══════════════════════════════════════════════════════════════════════════════
#  HTML 报告生成（含表格 + 雷达图）
# ═══════════════════════════════════════════════════════════════════════════════

def generate_html_report(
    summaries: list[dict[str, Any]],
    output_path: Path,
    env_info: dict[str, str] | None = None,
) -> None:
    """生成包含表格和雷达图的 HTML 报告。"""

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ── 表格行 HTML ──
    table_rows = ''
    for s in summaries:
        composite = s.get('composite_score')
        composite_str = f'{composite}' if composite is not None else '待评分'
        quality_str = f'{s["avg_quality"]}' if s.get('avg_quality') is not None else '待评分'
        safety_str = f'{s["safety_score"]}' if s.get('safety_score') is not None else '待评分'
        time_str = f'{s["avg_response_time"]}' if s['avg_response_time'] > 0 else '部署失败'

        table_rows += f'''
        <tr>
          <td><strong>{s["model_name"]}</strong>{"（"+s["deploy_type"]+"）" if s.get("deploy_type") else ""}</td>
          <td>{time_str}</td>
          <td>{quality_str}</td>
          <td>{s["cost_display"]}</td>
          <td>{safety_str}</td>
          <td><strong>{composite_str}</strong></td>
          <td>{s["success_rate"]}%</td>
        </tr>'''

    # ── 雷达图数据 (Chart.js) ──
    radar_datasets = []
    colors = [
        'rgba(54, 162, 235, 0.7)',   # 蓝
        'rgba(255, 99, 132, 0.7)',   # 红
        'rgba(75, 192, 192, 0.7)',   # 青
        'rgba(255, 159, 64, 0.7)',   # 橙
        'rgba(153, 102, 255, 0.7)',  # 紫
        'rgba(255, 205, 86, 0.7)',   # 黄
    ]
    bg_colors = [c.replace('0.7', '0.15') for c in colors]

    for i, s in enumerate(summaries):
        composite = s.get('composite_score')
        if composite is None:
            continue
        # 归一化到 0-5 范围用于雷达图
        time_val = s['avg_response_time']
        all_times = [x['avg_response_time'] for x in summaries if x['avg_response_time'] > 0]
        if all_times and max(all_times) > 0:
            t_norm = 5 * (1 - (time_val / max(max(all_times), 1)))
            t_norm = max(0, min(5, t_norm))
        else:
            t_norm = 2.5
        quality_val = s.get('avg_quality') or 0
        cost_val = s.get('cost_per_100k_tokens') or 0
        all_costs = [x.get('cost_per_100k_tokens') or 0 for x in summaries]
        max_cost = max(all_costs) if all_costs else 1
        cost_norm = 5 * (1 - cost_val / max(max_cost, 0.01)) if max_cost > 0 else 5
        cost_norm = max(0, min(5, cost_norm))
        safety_val = s.get('safety_score') or 0
        composite_norm = composite / 2  # 0-10 → 0-5

        ci = i % len(colors)
        radar_datasets.append(
            f'''{{
              label: '{s["model_name"]}',
              data: [{round(t_norm,1)}, {round(quality_val,1)}, {round(cost_norm,1)}, {round(safety_val,1)}, {round(composite_norm,1)}],
              borderColor: '{colors[ci]}',
              backgroundColor: '{bg_colors[ci]}',
              pointBackgroundColor: '{colors[ci]}',
            }}'''
        )

    radar_js = ',\n            '.join(radar_datasets)

    # ── 每个模型单独的雷达图 ──
    individual_radars = ''
    for i, s in enumerate(summaries):
        composite = s.get('composite_score')
        if composite is None:
            continue
        time_val = s['avg_response_time']
        all_times = [x['avg_response_time'] for x in summaries if x['avg_response_time'] > 0]
        if all_times and max(all_times) > 0:
            t_norm = 5 * (1 - (time_val / max(max(all_times), 1)))
            t_norm = max(0, min(5, t_norm))
        else:
            t_norm = 2.5
        quality_val = s.get('avg_quality') or 0
        cost_val = s.get('cost_per_100k_tokens') or 0
        all_costs = [x.get('cost_per_100k_tokens') or 0 for x in summaries]
        max_cost = max(all_costs) if all_costs else 1
        cost_norm = 5 * (1 - cost_val / max(max_cost, 0.01)) if max_cost > 0 else 5
        cost_norm = max(0, min(5, cost_norm))
        safety_val = s.get('safety_score') or 0
        composite_norm = composite / 2

        ci = i % len(colors)
        individual_radars += f'''
    <div class="radar-card">
      <h4>综合能力雷达图 - {s["model_name"]}</h4>
      <canvas id="radar_{i}" width="220" height="220"></canvas>
      <script>
        new Chart(document.getElementById('radar_{i}'), {{
          type: 'radar',
          data: {{
            labels: ['响应速度', '质量', '成本', '安全', '综合'],
            datasets: [{{
              label: '{s["model_name"]}',
              data: [{round(t_norm,1)}, {round(quality_val,1)}, {round(cost_norm,1)}, {round(safety_val,1)}, {round(composite_norm,1)}],
              borderColor: '{colors[ci]}',
              backgroundColor: '{bg_colors[ci]}',
              pointBackgroundColor: '{colors[ci]}',
            }}]
          }},
          options: {{
            scales: {{ r: {{ min: 0, max: 5, ticks: {{ stepSize: 1 }} }} }},
            plugins: {{ legend: {{ display: false }} }},
          }}
        }});
      </script>
    </div>'''

    # ── 环境信息 ──
    env_html = ''
    if env_info:
        env_items = ''.join(f'<li><strong>{k}</strong>: {v}</li>' for k, v in env_info.items())
        env_html = f'<div class="env-info"><h3>4.1.1 实验设计与环境说明</h3><ul>{env_items}</ul></div>'

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>4.1 模型选择与性能对比实验</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f8f9fa; }}
  h1 {{ text-align: center; color: #333; }}
  h2 {{ color: #444; border-bottom: 2px solid #ddd; padding-bottom: 8px; }}
  h3 {{ color: #555; }}
  table {{ width: 100%; border-collapse: collapse; margin: 20px 0; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  th {{ background: #4a90d9; color: #fff; padding: 12px 8px; text-align: center; }}
  td {{ padding: 10px 8px; text-align: center; border-bottom: 1px solid #eee; }}
  tr:hover {{ background: #f0f7ff; }}
  .env-info {{ background: #fff; padding: 15px 20px; border-radius: 8px; margin: 15px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .env-info ul {{ line-height: 2; }}
  .radar-container {{ display: flex; flex-wrap: nowrap; gap: 12px; justify-content: center; margin: 30px 0; }}
  .radar-card {{ background: #fff; padding: 10px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; flex: 1 1 0; min-width: 0; }}
  .radar-card h4 {{ margin: 0 0 10px; color: #555; font-size: 14px; }}
  .note {{ background: #fff3cd; padding: 12px 16px; border-left: 4px solid #ffc107; margin: 15px 0; border-radius: 4px; font-size: 14px; }}
  .timestamp {{ text-align: center; color: #999; font-size: 13px; margin-top: 30px; }}
  .weights {{ background: #e8f4fd; padding: 12px 16px; border-radius: 6px; margin: 10px 0; font-size: 14px; }}
</style>
</head>
<body>

<h1>4.1 模型选择与性能对比实验</h1>

{env_html}

<h2>4.1.2 详细实验数据与结果分析</h2>
<h3>表 4-1 模型对比</h3>
<table>
  <thead>
    <tr>
      <th>模型类型</th>
      <th>平均响应时间<br>(秒)</th>
      <th>回答质量<br>(1-5分)</th>
      <th>成本或硬件折旧<br>(元/10万token)</th>
      <th>安全合规性<br>(1-5分)</th>
      <th>综合得分</th>
      <th>成功率</th>
    </tr>
  </thead>
  <tbody>
    {table_rows}
  </tbody>
</table>

<div class="weights">
  <strong>综合得分权重：</strong>
  响应时间 {WEIGHTS["response_time"]*100:.0f}% +
  回答质量 {WEIGHTS["quality"]*100:.0f}% +
  成本效益 {WEIGHTS["cost"]*100:.0f}% +
  安全合规 {WEIGHTS["safety"]*100:.0f}%
</div>

<div class="note">
  <strong>注：</strong>
  <ul style="margin:5px 0;">
    <li>回答质量和安全合规性为人工评分，请在导出的 CSV 文件中填写后重新运行 <code>--finalize</code></li>
    <li>成本计算：API 模型采用官方定价；本地模型考虑硬件折旧</li>
    <li>响应时间中剔除了超时（>120s）和错误的请求</li>
  </ul>
</div>

<h2>图 4-1 综合能力对比图</h2>
<div class="radar-container">
  {individual_radars}
</div>

<h3>全模型综合对比雷达图</h3>
<div style="max-width:500px; margin:0 auto; background:#fff; padding:20px; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,0.1);">
  <canvas id="radarAll" width="480" height="480"></canvas>
</div>
<script>
  new Chart(document.getElementById('radarAll'), {{
    type: 'radar',
    data: {{
      labels: ['响应速度', '质量', '成本', '安全', '综合'],
      datasets: [
        {radar_js}
      ]
    }},
    options: {{
      scales: {{ r: {{ min: 0, max: 5, ticks: {{ stepSize: 1 }} }} }},
      plugins: {{ legend: {{ position: 'bottom' }} }},
    }}
  }});
</script>

<p class="timestamp">报告生成时间: {timestamp}</p>
</body>
</html>'''

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'  📄 HTML报告已生成: {output_path}')


# ═══════════════════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='4.1 模型选择与性能对比实验')
    parser.add_argument('--questions', type=int, default=None,
                        help='测试题目数量（默认全部）')
    parser.add_argument('--models', nargs='*', default=None,
                        help='指定运行的模型名称（默认全部）')
    parser.add_argument('--finalize', action='store_true',
                        help='读取人工评分CSV并生成最终报告')
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    questions = load_questions(args.questions)
    results_json_path = OUTPUT_DIR / 'raw_results.json'
    scoring_csv_path = OUTPUT_DIR / 'scoring.csv'
    summary_json_path = OUTPUT_DIR / 'summary.json'
    report_path = OUTPUT_DIR / f'model_compare_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.html'

    # 过滤待测模型
    models = MODELS_TO_TEST
    if args.models:
        models = [m for m in models if m['name'] in args.models]
        if not models:
            print(f'❌ 未找到指定模型，可选: {[m["name"] for m in MODELS_TO_TEST]}')
            sys.exit(1)

    # 过滤掉没有 API Key 的模型
    active_models = []
    for m in models:
        if not m.get('api_key'):
            print(f'  ⚠ 跳过 {m["name"]}: 未配置 api_key')
        else:
            active_models.append(m)

    if not active_models:
        print('❌ 没有可用的模型（请配置 api_key）')
        sys.exit(1)

    # ── finalize 模式：读取人工评分，生成最终报告 ──
    if args.finalize:
        print('\n📋 Finalize 模式：读取人工评分…')
        if not results_json_path.exists():
            print(f'❌ 未找到原始结果: {results_json_path}，请先运行实验')
            sys.exit(1)

        with open(results_json_path, 'r', encoding='utf-8') as f:
            all_results: dict[str, list[dict]] = json.load(f)

        # 读取人工评分
        manual_scores = import_scoring_csv(scoring_csv_path)
        if manual_scores:
            for model_name, qscores in manual_scores.items():
                if model_name in all_results:
                    for r in all_results[model_name]:
                        if r['question_id'] in qscores:
                            r['quality_score'] = qscores[r['question_id']]
            print(f'  ✅ 已导入 {sum(len(v) for v in manual_scores.values())} 条人工评分')
        else:
            print(f'  ⚠ 未找到评分数据，请在 {scoring_csv_path} 中填写后重试')

        # 读取安全合规性评分（从 MODELS_TO_TEST 配置中获取）
        model_cfgs = {m['name']: m for m in MODELS_TO_TEST}

        # 计算汇总
        summaries = []
        for model_name, results in all_results.items():
            cfg = model_cfgs.get(model_name, {'name': model_name})
            s = compute_model_summary(cfg, results)
            summaries.append(s)

        # 综合得分
        for s in summaries:
            s['composite_score'] = compute_composite_score(s, summaries)

        # 生成报告
        generate_html_report(summaries, report_path)

        # 保存汇总
        with open(summary_json_path, 'w', encoding='utf-8') as f:
            json.dump(summaries, f, ensure_ascii=False, indent=2)

        webbrowser.open(str(report_path))
        print(f'\n✅ 最终报告已生成: {report_path}')
        return

    # ── 正常模式：运行实验 ──
    print(f'\n🚀 开始模型对比实验')
    print(f'  📝 题目数: {len(questions)}')
    print(f'  🤖 待测模型: {[m["name"] for m in active_models]}')

    all_results: dict[str, list[dict]] = {}

    for model_cfg in active_models:
        try:
            results = run_single_model(model_cfg, questions)
            all_results[model_cfg['name']] = results
        except Exception as e:
            print(f'\n  ❌ {model_cfg["name"]} 运行失败: {e}')
            import traceback; traceback.print_exc()
            continue

    if not all_results:
        print('❌ 所有模型都运行失败')
        sys.exit(1)

    # 保存原始结果
    with open(results_json_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f'\n  💾 原始结果已保存: {results_json_path}')

    # 导出评分 CSV
    export_scoring_csv(all_results, scoring_csv_path)

    # 计算汇总（不含人工评分）
    model_cfgs = {m['name']: m for m in MODELS_TO_TEST}
    summaries = []
    for model_name, results in all_results.items():
        cfg = model_cfgs.get(model_name, {'name': model_name})
        s = compute_model_summary(cfg, results)
        summaries.append(s)

    for s in summaries:
        s['composite_score'] = compute_composite_score(s, summaries)

    # 保存汇总
    with open(summary_json_path, 'w', encoding='utf-8') as f:
        json.dump(summaries, f, ensure_ascii=False, indent=2)

    # 环境信息
    import platform
    env_info = {
        '实验主机': platform.node(),
        '操作系统': f'{platform.system()} {platform.release()}',
        'CPU': platform.processor() or 'N/A',
        'Python': platform.python_version(),
        '测试题目数': str(len(questions)),
        '测试模型数': str(len(active_models)),
    }

    # 生成报告
    generate_html_report(summaries, report_path, env_info=env_info)

    # 打印汇总表
    print(f'\n{"=" * 80}')
    print(f'  {"模型":<20} {"平均响应(s)":<14} {"成功率":<10} {"成本(元/10万token)":<20}')
    print(f'  {"-"*20} {"-"*14} {"-"*10} {"-"*20}')
    for s in summaries:
        t = f'{s["avg_response_time"]:.2f}' if s['avg_response_time'] > 0 else '失败'
        print(f'  {s["model_name"]:<20} {t:<14} {s["success_rate"]:<10}% {s["cost_display"]:<20}')
    print(f'{"=" * 80}')

    print(f'\n📋 下一步:')
    print(f'  1. 打开 {scoring_csv_path}')
    print(f'  2. 填写「回答质量(1-5)」列')
    print(f'  3. 在 MODELS_TO_TEST 中填写 safety_score')
    print(f'  4. 运行: python -m benchmark.model_compare --finalize')

    webbrowser.open(str(report_path))


if __name__ == '__main__':
    main()
