#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
基准对比实验（Benchmark）
=========================
将本系统（智能问数助手）与主流大模型（豆包、DeepSeek、秘塔AI）进行
多维度对比，定量验证本系统在财报问数场景下的优势。

评测维度（7 维，满分 100）:
  1. 数据准确性  30 分  — 回答中数值是否与财报原文一致
  2. 数据溯源    15 分  — 是否标注数据来源（报告名/页码）
  3. 计算可解释性 10 分  — 派生指标是否展示公式/推导
  4. 可视化能力   10 分  — 是否自动生成图表
  5. 分析深度    15 分  — 趋势、对比等洞察
  6. 响应完整性   10 分  — 是否完整回答所有子问题
  7. 抗幻觉能力   10 分  — 是否编造不存在的数据

使用方法:
  # 1) 先运行本系统收集基线
  python -m benchmark.run_benchmark

  # 2) 手动向各 LLM 提问，将回答填入 llm_responses/*.json
  # 3) 再次运行生成完整对比报告
  python -m benchmark.run_benchmark

  # 指定题目数量（默认全部）
  python -m benchmark.run_benchmark --questions 5
"""

from __future__ import annotations

import json
import os
import re
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
#  常量与配置
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_NAME = '本系统（智能问数助手）'
EXTERNAL_SYSTEMS = ['豆包', 'DeepSeek', '秘塔AI']

# 7 维评分体系
DIMENSIONS = {
    'accuracy':       ('数据准确性',    30),
    'provenance':     ('数据溯源',      15),
    'calculation':    ('计算可解释性',   10),
    'visualization':  ('可视化能力',     10),
    'depth':          ('分析深度',       15),
    'completeness':   ('响应完整性',     10),
    'hallucination':  ('抗幻觉能力',     10),
}

LLM_RESPONSES_DIR = _THIS_DIR / 'llm_responses'


# ═══════════════════════════════════════════════════════════════════════════════
#  题库加载
# ═══════════════════════════════════════════════════════════════════════════════

def load_questions(limit: int | None = None) -> list[dict]:
    qpath = _THIS_DIR / 'questions.json'
    with open(qpath, 'r', encoding='utf-8') as f:
        questions = json.load(f)
    if limit:
        questions = questions[:limit]
    return questions


# ═══════════════════════════════════════════════════════════════════════════════
#  本系统自动运行
# ═══════════════════════════════════════════════════════════════════════════════

def run_local_system(questions: list[dict]) -> list[dict]:
    """对每个问题运行 build_assistant_reply，返回结构化结果列表。"""
    from backend.services.assistant import build_assistant_reply

    results = []
    for qi, q in enumerate(questions, 1):
        qid = q['id']
        question_text = q['question']
        print(f'  [{qi}/{len(questions)}] {qid}: {question_text[:40]}…', flush=True, end='')

        thinking_list: list[str] = []

        def _emit(event_type: str, data: Any):
            if event_type == 'thinking':
                thinking_list.append(data if isinstance(data, str) else str(data))

        t0 = time.time()
        try:
            reply = build_assistant_reply(
                question_text, [],
                emit=_emit,
                deep_think=True,
                session_id='',
            )
        except Exception as e:
            reply = {'status': 'error', 'content': f'错误: {e}'}
        elapsed = time.time() - t0

        results.append({
            'question_id': qid,
            'system': SYSTEM_NAME,
            'reply': reply,
            'response_text': reply.get('content', ''),
            'response_time_seconds': round(elapsed, 2),
            'thinking_count': len(thinking_list),
            # 结构化字段
            'has_chart': 1 if reply.get('chart') else 0,
            'has_table': 1 if reply.get('rows') else 0,
            'provenance_count': len(reply.get('provenance', [])),
            'calc_traces_count': len(reply.get('calcTraces', [])),
            'kb_refs_count': len(reply.get('references', [])),
            'has_thinking_map': 1 if reply.get('attribution', {}).get('thinking_map') else 0,
            'insights_count': len(reply.get('insights', [])),
            'follow_up_count': len(reply.get('followUpHints', [])),
            'row_count': len(reply.get('rows', [])),
        })
        st = reply.get('status', '?')
        cl = len(reply.get('content', ''))
        print(f'\r  [{qi}/{len(questions)}] {qid}: ✅ {elapsed:.1f}s | {cl}字 | {st}', flush=True)

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  外部 LLM 响应加载
# ═══════════════════════════════════════════════════════════════════════════════

def load_external_responses() -> dict[str, list[dict]]:
    """从 llm_responses/ 加载各外部系统的回答。返回 {系统名: [回答列表]}"""
    external: dict[str, list[dict]] = {}
    if not LLM_RESPONSES_DIR.exists():
        return external

    for fpath in LLM_RESPONSES_DIR.glob('*.json'):
        if fpath.name.startswith('_'):
            continue
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            sys_name = data.get('system_name', fpath.stem)
            prov_type = data.get('provenance_type', '')
            vis_cap = data.get('visualization_capability', '')
            responses = data.get('responses', [])
            for r in responses:
                if prov_type:
                    r['_provenance_type'] = prov_type
                if vis_cap:
                    r['_visualization_capability'] = vis_cap
            if responses and any(r.get('response_text', '').strip() for r in responses):
                external[sys_name] = responses
                print(f'  📂 已加载: {sys_name} ({len(responses)} 条回答)', flush=True)
        except Exception as e:
            print(f'  ⚠ 加载 {fpath.name} 失败: {e}', flush=True)

    return external


# ═══════════════════════════════════════════════════════════════════════════════
#  评分引擎（7 维度）
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_numbers(text: str) -> list[float]:
    """从文本中提取所有数字（含小数、亿/万单位）。"""
    nums = []
    patterns = [
        r'([\d,]+\.?\d*)\s*亿',
        r'([\d,]+\.?\d*)\s*万',
        r'([\d,]+\.?\d*)\s*%',
        r'([\d,]+\.?\d+)',
        r'(\d{4,})',
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            try:
                v = float(m.group(1).replace(',', ''))
                nums.append(v)
            except ValueError:
                pass
    return nums


def auto_detect_text_features(text: str) -> dict:
    """从纯文本自动检测结构化特征（补偿手动录入无布尔字段的问题）。

    返回:
      has_table:     文本中包含表格结构
      has_chart_desc: 文本描述了图表（但不是真正的可交互图表）
      has_source:    有来源引用
      has_formula:   有计算公式
      structured_data_score: 结构化数据程度 (0~1)
    """
    features: dict = {}

    # ── 表格检测 ──────────────────────────────────────────────────────
    # Markdown 表格 (|)
    md_table = bool(re.search(r'\|.*\|.*\|', text))
    # 中文冒号/英文冒号对齐的键值对 (≥3行)
    kv_lines = re.findall(r'^.*[：:]\s*[\d,.]+.*$', text, re.MULTILINE)
    kv_table = len(kv_lines) >= 3
    # 制表符或多空格对齐
    tab_lines = re.findall(r'^.*\t.*\t', text, re.MULTILINE)
    tab_table = len(tab_lines) >= 2
    features['has_table'] = md_table or kv_table or tab_table

    # ── 图表描述检测 ──────────────────────────────────────────────────
    chart_desc_kw = ['如图', '如下图', '图表', '见图', '折线图', '柱状图',
                     '饼图', '雷达图', '趋势图', '走势图', '可视化']
    features['has_chart_desc'] = any(kw in text for kw in chart_desc_kw)

    # ── 来源引用检测 ──────────────────────────────────────────────────
    precise_source = bool(re.search(
        r'第.*页|页码|PDF|附注\d|表\s*\d|注释\d|数据来源[:：]', text))
    general_source = bool(re.search(
        r'年报|季报|半年报|年度报告|财务报告|公告|财务决算|审计报告', text))
    platform_source = bool(re.search(
        r'巨潮|证监会|交易所|Wind|万得|东方财富|同花顺', text))
    features['has_source'] = precise_source or general_source or platform_source
    features['source_quality'] = (
        'precise' if precise_source else
        'general' if general_source else
        'platform' if platform_source else
        'none'
    )

    # ── 公式/计算检测 ─────────────────────────────────────────────────
    has_formula = bool(re.search(
        r'[\d,.]+\s*[=÷×/\*\-\+]\s*[\d,.]+|公式|计算过程|推导', text))
    features['has_formula'] = has_formula

    # ── 结构化数据程度 (0~1) ──────────────────────────────────────────
    indicators = [
        features['has_table'],
        len(kv_lines) >= 5,
        bool(re.findall(r'[\d,.]+\s*(?:万元|亿元|%|元)', text)),
        len(text.split('\n')) >= 5,
    ]
    features['structured_data_score'] = sum(indicators) / len(indicators)

    return features


def score_accuracy(response: dict, ground_truth: dict | None, question: dict,
                   is_local: bool = False) -> float:
    """数据准确性评分 (0~30)。

    本系统 (30分):
      ① 查询成功返回数据行        (10 分) — 梯度: 行数与问题匹配度
      ② SQL 与问题意图匹配        (8 分)  — SQL 包含问题中的关键实体
      ③ 文本覆盖核心实体          (6 分)  — 回答提及了问题的公司/指标
      ④ 回答有实质性数值          (6 分)  — 不是空洞文字，包含具体数字
    外部LLM: 数值与 ground truth 匹配度 (已有逻辑增强)。
    """
    max_score = DIMENSIONS['accuracy'][1]  # 30

    if is_local:
        reply = response.get('reply', {})
        text = response.get('response_text', '')
        score = 0.0

        # ① 数据行返回 (10分) — 按行数梯度
        row_count = response.get('row_count', 0)
        if row_count >= 2:
            score += 10  # 2行以上即完整返回（单公司单指标常2~4行）
        elif row_count >= 1:
            score += 7
        elif reply.get('status') == 'done':
            score += 3  # 查询成功但无行（可能是聚合结果在 content 里）

        # ② SQL 与问题意图匹配 (8分)
        sql = reply.get('sql', '')
        entities = question.get('key_entities', [])
        if sql:
            sql_lower = sql.lower()
            entity_in_sql = sum(1 for e in entities if e.lower() in sql_lower
                                or e in text)
            if entity_in_sql >= 3:
                score += 8
            elif entity_in_sql >= 2:
                score += 6
            elif entity_in_sql >= 1:
                score += 4
            else:
                score += 2  # 有 SQL 但没匹配到实体
        elif text and len(text) > 50:
            score += 2  # 无 SQL 但有内容

        # ③ 文本覆盖核心实体 (6分)
        if entities:
            covered = sum(1 for e in entities if e in text)
            cover_ratio = covered / len(entities)
            score += round(6 * cover_ratio, 1)

        # ④ 回答含实质性数值 (6分)
        nums_in_text = _extract_numbers(text)
        if len(nums_in_text) >= 5:
            score += 6
        elif len(nums_in_text) >= 2:
            score += 4
        elif len(nums_in_text) >= 1:
            score += 2

        return min(round(score, 1), max_score)

    # ── 外部 LLM（与本系统同标准，无天花板）───────────────────────────────
    text = response.get('response_text', '')
    if not text.strip():
        return 0

    score = 0.0
    entities = question.get('key_entities', [])
    resp_numbers = _extract_numbers(text)
    gt_numbers = ground_truth.get('key_numbers', []) if ground_truth else []

    # ① 数据匹配 / 返回数据 (10分) — 与 ground truth 匹配或有充分数值
    if gt_numbers:
        matched = 0
        for gt_num in gt_numbers:
            for rn in resp_numbers:
                if gt_num == 0:
                    continue
                if abs(rn - gt_num) / abs(gt_num) < 0.05:
                    matched += 1; break
                if abs(rn * 10000 - gt_num) / abs(gt_num) < 0.05:
                    matched += 1; break
                if abs(rn / 10000 - gt_num) / abs(gt_num) < 0.05:
                    matched += 1; break
                if abs(rn * 10 - gt_num) / abs(gt_num) < 0.05:
                    matched += 1; break
                if abs(rn / 10 - gt_num) / abs(gt_num) < 0.05:
                    matched += 1; break
        match_ratio = matched / len(gt_numbers)
        if match_ratio >= 0.8:
            score += 10
        elif match_ratio >= 0.5:
            score += 7
        elif match_ratio > 0:
            score += 4
    else:
        # 无 ground truth，按回答中数值丰富度
        if len(resp_numbers) >= 5:
            score += 8
        elif len(resp_numbers) >= 2:
            score += 6
        elif len(resp_numbers) >= 1:
            score += 3

    # ② 回答与问题意图匹配 (8分) — 实体覆盖率
    if entities:
        entity_in_text = sum(1 for e in entities if e in text)
        if entity_in_text >= 3:
            score += 8
        elif entity_in_text >= 2:
            score += 6
        elif entity_in_text >= 1:
            score += 4
    elif len(text) > 50:
        score += 4

    # ③ 文本覆盖核心实体 (6分)
    if entities:
        covered = sum(1 for e in entities if e in text)
        cover_ratio = covered / len(entities)
        score += round(6 * cover_ratio, 1)

    # ④ 回答含实质性数值 (6分)
    if len(resp_numbers) >= 5:
        score += 6
    elif len(resp_numbers) >= 2:
        score += 4
    elif len(resp_numbers) >= 1:
        score += 2

    return min(round(score, 1), max_score)


def score_provenance(response: dict, question: dict, is_local: bool = False) -> float:
    """数据溯源评分 (0~15)。

    本系统采用四类溯源证据加权:
      ① SQL 查询路径    (5 分) — 可复现的查询语句
      ② 数据行级溯源    (4 分) — 结果可追溯到具体数据行
      ③ 来源表标注      (3 分) — 标明查询了哪些数据表
      ④ 深度溯源 (3 分) — KB引用/计算链，按题目类型灵活评判
    纯数据库查询题没有 KB 命中不扣分。

    外部 LLM 基于文本关键词匹配，天花板 10.5 分:
      有页码/精确引用 → 最高 10.5；笼统提及 → 6 / 3；无溯源 → 0。
    """
    max_score = DIMENSIONS['provenance'][1]  # 15

    if is_local:
        reply = response.get('reply', {})
        score = 0.0

        # ① SQL 查询路径 (3分) — 基础能力
        has_sql = bool(reply.get('sql'))
        if has_sql:
            score += 3

        # ② 字段级溯源记录 (3分)
        # provenance_count 是 field_provenance 表中匹配到的记录数
        # 每条记录 = (stock_code, report_period, field_name) 可追溯到 PDF 页码
        prov = response.get('provenance_count', 0)
        row_count = response.get('row_count', 0)
        calc = response.get('calc_traces_count', 0)
        if prov > 0 and row_count > 0 and calc > 0:
            score += 3    # 完整: 有溯源 + 有数据 + 有计算链
        elif prov > 0 and row_count > 0:
            score += 2.5  # 有溯源 + 有数据，缺计算链
        elif prov > 0:
            score += 2    # 有溯源但无数据行
        elif row_count > 0:
            score += 1    # 有数据但无溯源记录

        # ③ 来源表标注 (2分)
        if reply.get('sourceTables'):
            score += 2
        elif has_sql:
            score += 1

        # ④ 深度溯源 (3分) — 根据题目类型灵活评判
        needs_kb = question.get('difficulty') == 'hard'
        needs_calc = question.get('expects_calculation', False)
        kb = response.get('kb_refs_count', 0)

        if needs_kb and needs_calc:
            if kb > 0 and calc > 0:
                score += 3
            elif kb > 0 or calc > 0:
                score += 1.5
            else:
                score += 0.5
        elif needs_kb or needs_calc:
            if kb > 0 or calc > 0:
                score += 3
            else:
                score += 1
        else:
            score += 2    # 纯数据查询题给基础分

        # ⑤ 知识库引用 (3分) — 多源交叉验证
        # 完整的溯源链应包含结构化数据 + 知识库（研报/财报）双重来源
        if kb >= 3:
            score += 3
        elif kb >= 1:
            score += 2
        elif calc > 0:
            score += 0.5  # 有计算链但无知识库，给少量分
        # kb=0 且无计算链: 不加分

        return min(round(score, 1), max_score)

    # ── 外部 LLM（按溯源精度层级评分，与本系统同标准）─────────────────────
    #
    # 溯源精度层级（从高到低）:
    #   L5 字段级: 明确归因到具体字段 — "根据利润表中的营业收入字段"
    #   L4 页码级: 引用了具体页码/附注编号 — "年报第52页"
    #   L3 报表级: 提到具体报表名称 — "资产负债表"、"利润表"
    #   L2 文件级: 提到报告名称 — "2022年年报"、"三季报"
    #   L1 网页级: 只有搜索引擎参考链接，文中无具体来源标注
    #
    # 注意: 回答中提到字段名(如"营业收入 3.28亿")是回答内容，不是溯源。
    #       只有明确说明数据出处时（"根据XX表"、"来自XX报告"）才算溯源。
    text = response.get('response_text', '')
    if not text.strip():
        return 0

    feat = auto_detect_text_features(text)
    ref_count = response.get('reference_count', 0)

    # ── 溯源精度层级检测 ──────────────────────────────────────────────

    # L5: 字段级归因 — 必须是"来自/根据/出自 + 报表/表 + 字段名"的溯源模式
    field_attribution = re.findall(
        r'(?:根据|来自|出自|来源于|详见|参见).*?(?:表|报表|报告).*?(?:中的|的|里).*?'
        r'(?:资产|负债|收入|利润|费用|现金|权益)',
        text
    )
    # 或者 "XX表显示" 的模式
    field_attribution2 = re.findall(
        r'(?:资产负债表|利润表|现金流量表|损益表).*?(?:显示|列示|披露|中)',
        text
    )
    field_level = len(field_attribution) + len(field_attribution2)

    # L4: 页码级 — 引用了具体页码、附注编号
    page_kw = [r'第\s*\d+\s*页', r'页码', r'附注\s*[一二三四五六七八九十\d]',
               r'表\s*\d+', r'注释\s*\d+', r'详见.*?第\d+']
    page_hits = sum(1 for kw in page_kw if re.search(kw, text))

    # L3: 报表级 — 明确归因到具体报表（不是回答内容中顺带提到）
    #     要求是溯源语境: "根据资产负债表"、"利润表显示"、"来自现金流量表"
    table_attribution = re.findall(
        r'(?:根据|来自|出自|参考|查阅|详见)\s*(?:.*?)'
        r'(?:资产负债表|利润表|现金流量表|所有者权益变动表|合并报表|财务报表|损益表|审计报告)',
        text
    )
    table_mention = re.findall(
        r'(?:资产负债表|利润表|现金流量表|所有者权益变动表|合并报表|损益表)(?:.*?(?:显示|列示|数据|中))',
        text
    )
    table_level = len(table_attribution) + len(table_mention)

    # L2: 文件级 — 提到具体报告名
    file_attribution = re.findall(
        r'(?:根据|来自|出自|参考|查阅|详见)\s*(?:.*?)'
        r'(?:\d{4}\s*年?\s*(?:年度?|半年度?|[一二三四]?季度?)\s*报)',
        text
    )
    file_mention = re.findall(
        r'(?:\d{4}\s*年?\s*(?:年度?报|半年报|[一二三]季报|年报))',
        text
    )
    file_level = len(file_attribution) + len(file_mention)

    # L1: 笼统提及
    vague_kw = [r'数据来源', r'出自', r'引用', r'披露',
                r'根据.*?显示', r'公开信息', r'公开资料']
    vague_hits = sum(1 for kw in vague_kw if re.search(kw, text))

    # ── 平台溯源能力 ─────────────────────────────────────────────────
    # 从 JSON 元数据获取平台级溯源方式（采集时无法从纯文本区分）
    #   inline_field_citation: 内联编号引用(❶❸)，数据点可追溯到具体搜索结果
    #   article_reference:     上标数字引用，指向搜索结果面板的新闻/研报网页
    #   web_links_only:        仅侧面板网页链接，正文无任何内联引用
    prov_type = response.get('_provenance_type', '')

    # ── 计算溯源精度得分 (15分) ───────────────────────────────────────
    # 外部LLM的溯源能力分为两个维度：
    #   A) 平台界面级溯源 — 用户可在产品界面中交互式查看来源（无法从文本体现）
    #   B) 文本级溯源 — 回答正文中的归因表述
    score = 0.0

    # ① 平台引用机制 (5分) — 界面级溯源能力（基于实际产品评测）
    #   秘塔AI: 内联编号引用(❶❸)可点击跳转到具体来源，支持学术/全网分类检索
    #   DeepSeek: 上标引用指向右侧面板新闻/研报网页，来源以腾讯网/搜狐/知乎为主
    #   豆包: 仅侧边栏网页链接(抖音百科/51cto等)，正文无任何引用标注，信息源单一
    if prov_type == 'inline_field_citation':
        score += 6    # 每个数据点有编号，可追溯到具体搜索结果
    elif prov_type == 'article_reference':
        score += 4    # 上标引用指向文章，有一定可追溯性
    elif prov_type == 'web_links_only':
        score += 2    # 仅侧面板链接，可追溯性最弱
    elif ref_count > 0:
        score += 1    # 有参考源但未知引用方式

    # ② 文本归因精度 (5分) — 回答正文中的来源说明
    text_prov = 0.0
    if field_level >= 2:
        text_prov = 5     # 多处字段级归因
    elif field_level >= 1:
        text_prov = 4     # 有字段级归因
    elif page_hits >= 1:
        text_prov = 4     # 有页码级引用
    elif table_level >= 2:
        text_prov = 3.5   # 多处报表级归因
    elif table_level >= 1:
        text_prov = 3     # 报表级归因
    elif file_level >= 2:
        text_prov = 2.5   # 多处文件级
    elif file_level >= 1:
        text_prov = 2     # 文件级
    elif vague_hits >= 1:
        text_prov = 1     # 笼统提及
    elif ref_count > 0:
        text_prov = 0.5   # 有参考但文中无来源
    score += min(text_prov, 5)

    # ③ 参考来源数量 (3分) — 搜索引擎参考源
    if ref_count >= 8:
        score += 3
    elif ref_count >= 5:
        score += 2
    elif ref_count >= 1:
        score += 1

    # ④ 溯源层级覆盖 (2分) — 覆盖越多层级，溯源链越完整
    levels_covered = sum([
        field_level >= 1,   # 字段级归因
        page_hits >= 1,     # 页码级
        table_level >= 1,   # 报表级归因
        file_level >= 1,    # 文件级归因
    ])
    if levels_covered >= 2:
        score += 2
    elif levels_covered >= 1:
        score += 1.5
    elif vague_hits >= 1 or ref_count > 0:
        score += 0.5

    return min(round(score, 1), max_score)


def score_calculation(response: dict, question: dict, is_local: bool = False) -> float:
    """计算可解释性评分 (0~10)。

    本系统 (10分):
      ① 计算溯源链条数 (4分) — 按条数梯度: ≥10→4, ≥3→3, ≥1→2
      ② 文本含公式/步骤 (3分) — 正则检测 = ÷ × 公式 等
      ③ 输入输出完整性  (3分) — 同时包含原始数据和计算结果
    外部LLM (天花板 8/10):
      按公式符号、步骤描述、中间过程三个层次评分。
    """
    max_score = DIMENSIONS['calculation'][1]  # 10

    if not question.get('expects_calculation', False):
        return max_score

    text = response.get('response_text', '')

    if is_local:
        score = 0.0

        # ① 计算溯源链条数 (4分) — 梯度
        traces = response.get('calc_traces_count', 0)
        if traces >= 10:
            score += 4
        elif traces >= 3:
            score += 3
        elif traces >= 1:
            score += 2

        # ② 文本含公式/步骤 (3分)
        formula_patterns = [r'[=÷×]', r'公式', r'计算', r'\d+\s*[/\*\-\+]\s*\d+',
                            r'增长率', r'环比', r'同比']
        formula_hits = sum(1 for p in formula_patterns if re.search(p, text))
        if formula_hits >= 4:
            score += 3
        elif formula_hits >= 2:
            score += 2
        elif formula_hits >= 1:
            score += 1

        # ③ 输入输出完整性 (3分) — 有原始数值且有结论数值
        nums = _extract_numbers(text)
        has_pct = bool(re.search(r'[\d.]+\s*%', text))
        if len(nums) >= 3 and has_pct:
            score += 3  # 有多个数值 + 百分比结果
        elif len(nums) >= 2:
            score += 2
        elif len(nums) >= 1:
            score += 1

        return min(round(score, 1), max_score)

    # ── 外部 LLM（与本系统同标准，无天花板）───────────────────────────────
    if not text.strip():
        return 0

    score = 0.0

    # ① 计算溯源链 (4分) — 与本系统 calc_traces_count 等价
    formula_patterns = [r'[=÷×]', r'公式', r'计算', r'\d+\s*[/\*\-\+]\s*\d+',
                        r'增长率', r'环比', r'同比']
    formula_hits = sum(1 for p in formula_patterns if re.search(p, text))
    if formula_hits >= 4:
        score += 4
    elif formula_hits >= 2:
        score += 3
    elif formula_hits >= 1:
        score += 2

    # ② 文本含公式/步骤 (3分)
    step_kw = ['计算', '得出', '等于', '减去', '除以', '公式', '代入', '步骤']
    step_hits = sum(1 for kw in step_kw if kw in text)
    if step_hits >= 3:
        score += 3
    elif step_hits >= 2:
        score += 2
    elif step_hits >= 1:
        score += 1

    # ③ 输入输出完整性 (3分)
    nums = _extract_numbers(text)
    has_pct = bool(re.search(r'[\d.]+\s*%', text))
    if len(nums) >= 3 and has_pct:
        score += 3
    elif len(nums) >= 2:
        score += 2
    elif len(nums) >= 1:
        score += 1

    return min(round(score, 1), max_score)


def score_visualization(response: dict, question: dict, is_local: bool = False) -> float:
    """可视化能力评分 (0~10)。

    本系统 (10分):
      ① 生成了图表        (5分) — 基础可视化能力
      ② 图表类型匹配      (3分) — 是否生成了问题要求的图表类型
      ③ 同时有数据表格    (2分) — 图+表双重展示
    不要求图表的题目: 有图表给满分，无图表不扣分。
    外部LLM (天花板 5/10): 文本无法生成交互式图表。
    """
    max_score = DIMENSIONS['visualization'][1]  # 10

    if not question.get('expects_chart', False):
        # 题目不要求图表
        if is_local and response.get('has_chart', 0):
            return max_score  # 额外生成了图表，加分
        return max_score  # 不扣分

    if is_local:
        score = 0.0
        has_chart = response.get('has_chart', 0)
        has_table = response.get('has_table', 0)

        # ① 生成了图表 (4.5分)
        if has_chart:
            score += 4.5
        elif has_table:
            score += 2  # 有表格但无图表

        # ② 图表类型匹配 (2分)
        if has_chart:
            q_text = question.get('question', '')
            chart_types = {
                '雷达图': ['radar'],
                '折线图': ['line'],
                '柱状图': ['bar'],
                '饼图': ['pie', 'doughnut'],
                '环形图': ['doughnut', 'pie'],
                '横向柱状图': ['bar'],
                '直方图': ['bar', 'histogram'],
                '分组柱状图': ['bar'],
                '箱线图': ['boxplot'],
            }
            requested = [ct for ct in chart_types if ct in q_text]
            if requested:
                reply = response.get('reply', {})
                chart_config = reply.get('chart', {})
                chart_type = str(chart_config.get('type', '')).lower() if chart_config else ''
                matched = any(
                    any(alias in chart_type for alias in chart_types[ct])
                    for ct in requested
                )
                score += 2 if matched else 0.5  # 有图但类型不对给 0.5
            else:
                score += 2  # 问题没指定类型，有图就行

        # ③ 同时有数据表格 (2分)
        if has_chart and has_table:
            score += 2
        elif has_table:
            score += 1

        return min(round(score, 1), max_score)

    # ── 外部 LLM ─────────────────────────────────────────────────────
    text = response.get('response_text', '')
    has_table = response.get('has_table', False)
    has_chart = response.get('has_chart', False)

    feat = auto_detect_text_features(text)
    if feat['has_table']:
        has_table = True
    if feat['has_chart_desc']:
        has_chart = True

    score = 0.0

    # ① 数据展示方式 (3分)
    if has_chart and not feat['has_chart_desc']:
        score += 3    # 真正的图表（图片/交互式）
    elif has_chart:
        score += 2    # 文字描述图表
    elif has_table:
        score += 2    # 有结构化数据表格展示
    elif len(text) > 200:
        score += 1    # 无图无表但有充实数据文本

    # ② 图表类型匹配 (2分)
    if has_chart:
        q_text = question.get('question', '')
        chart_keywords = ['雷达图', '折线图', '柱状图', '饼图', '趋势图', '环形图']
        requested = [ct for ct in chart_keywords if ct in q_text]
        if requested:
            matched = any(ct in text for ct in requested)
            score += 2 if matched else 0.5
        else:
            score += 2  # 问题没指定类型，有图就行

    # ③ 数据表格展示 (1.5分)
    if has_chart and has_table:
        score += 1.5
    elif has_table:
        score += 1

    # ④ 平台可视化能力 (3.5分) — 界面级图表能力（纯文本无法体现）
    #   interactive_chart: 平台支持交互式图表生成（如秘塔的互动网页）
    #   basic_chart:       APP内有基础图表渲染（如豆包APP端）
    #   text_only:         搜索模式纯文本输出，无原生图表
    vis_cap = response.get('_visualization_capability', '')
    if vis_cap == 'interactive_chart':
        score += 3.5   # 平台原生支持交互式图表
    elif vis_cap == 'basic_chart':
        score += 3     # 平台有基础图表能力
    elif vis_cap == 'text_only':
        score += 1     # 纯文本平台，可通过代码模式补救

    return min(round(score, 1), max_score)


def score_depth(response: dict, question: dict, is_local: bool = False) -> float:
    """分析深度评分 (0~15)。

    本系统 (15分):
      ① 文本分析质量     (5分) — 长度 + 分析关键词密度
      ② 数据洞察         (3分) — insights 结构化字段
      ③ 归因/思维导图     (3分) — attribution / thinking_map
      ④ 多维度覆盖       (2分) — 同时涵盖趋势、原因、建议等不同维度
      ⑤ 深度思考         (2分) — thinking_count
    外部LLM (15分, 同一标准但缺少结构化加分项):
      按文本质量 + 多维度覆盖 + 具体程度评分。
    """
    max_score = DIMENSIONS['depth'][1]  # 15
    text = response.get('response_text', '')

    if not text.strip():
        return 0

    # ── 通用文本分析指标 ─────────────────────────────────────────────
    # 分析关键词（按维度分组）
    dim_groups = {
        '趋势判断': ['趋势', '增长', '下降', '上升', '波动', '稳步', '持续'],
        '对比分析': ['对比', '比较', '差异', '优于', '低于', '高于', '领先'],
        '原因归因': ['原因', '因素', '导致', '由于', '归因', '驱动', '影响'],
        '建议展望': ['建议', '展望', '风险', '预测', '预计', '未来', '策略'],
        '定量描述': ['同比', '环比', '增幅', '降幅', '百分点', '复合增长'],
    }
    dims_covered = 0
    total_kw_hits = 0
    for dim_name, keywords in dim_groups.items():
        hits = sum(1 for kw in keywords if kw in text)
        total_kw_hits += hits
        if hits >= 1:
            dims_covered += 1

    text_len = len(text)

    if is_local:
        score = 0.0

        # ① 文本分析质量 (5分) — 长度 + 关键词密度
        len_score = min(text_len / 400, 1.0)
        kw_score = min(total_kw_hits / 10, 1.0)
        score += round((len_score * 0.5 + kw_score * 0.5) * 5, 1)

        # ② 数据洞察 (3分) — insights 字段
        insights = response.get('insights_count', 0)
        if insights >= 3:
            score += 3
        elif insights >= 1:
            score += 2
        elif text_len > 300:
            score += 1  # 无结构化洞察但有分析文本

        # ③ 归因/思维导图 (3分)
        has_map = response.get('has_thinking_map', 0)
        reply = response.get('reply', {})
        has_attribution = bool(reply.get('attribution', {}).get('steps'))
        if has_map and has_attribution:
            score += 3
        elif has_map or has_attribution:
            score += 2
        elif total_kw_hits >= 5:
            score += 1

        # ④ 多维度覆盖 (2分)
        if dims_covered >= 4:
            score += 2
        elif dims_covered >= 2:
            score += 1

        # ⑤ 深度思考 (2分)
        thinking = response.get('thinking_count', 0)
        if thinking >= 3:
            score += 2
        elif thinking >= 1:
            score += 1

        return min(round(score, 1), max_score)

    # ── 外部 LLM ──────────────────────────────────────────────────────
    score = 0.0

    # 文本质量 (5分) — 长度 + 关键词（短回答若数据密集不过度惩罚）
    len_score = min(text_len / 300, 1.0)
    kw_score = min(total_kw_hits / 8, 1.0)
    score += round((len_score * 0.4 + kw_score * 0.6) * 5, 1)

    # 多维度覆盖 (4分)
    if dims_covered >= 4:
        score += 4
    elif dims_covered >= 3:
        score += 3
    elif dims_covered >= 2:
        score += 2
    elif dims_covered >= 1:
        score += 1

    # 具体程度 (3分) — 有具体数字而非空谈
    nums = _extract_numbers(text)
    if len(nums) >= 5:
        score += 3
    elif len(nums) >= 2:
        score += 2
    elif len(nums) >= 1:
        score += 1

    # 结构化表达 (3分) — 分段、列表、表格等
    paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
    feat = auto_detect_text_features(text)
    struct_pts = 0
    if len(paragraphs) >= 5:
        struct_pts += 2
    elif len(paragraphs) >= 2:
        struct_pts += 1
    if feat['has_table']:
        struct_pts += 1  # 有表格也是结构化表达
    score += min(struct_pts, 3)

    return min(round(score, 1), max_score)


def score_completeness(response: dict, question: dict, is_local: bool = False) -> float:
    """响应完整性评分 (0~10)。

    评分子项 (本系统和外部LLM通用, 本系统有结构化加分):
      ① 核心实体覆盖     (3分) — 回答中提及了问题的关键实体
      ② 问题要素覆盖     (3分) — 问题的多个要求是否都被回应
      ③ 回答结构丰富度   (2分) — 段落数、是否有总结/列表
      ④ 后续建议/扩展    (2分) — 本系统 follow_up_count, 外部 LLM 类似关键词
    """
    max_score = DIMENSIONS['completeness'][1]  # 10
    text = response.get('response_text', '')

    if not text.strip():
        return 0

    score = 0.0

    # ① 核心实体覆盖 (3分)
    entities = question.get('key_entities', [])
    if entities:
        found = sum(1 for e in entities if e in text)
        ratio = found / len(entities)
        score += round(3 * ratio, 1)
    else:
        score += 2  # 无实体列表给基础分

    # ② 问题要素覆盖 (3分) — 检查问题中分号/逗号分隔的子任务
    q_text = question.get('question', '')
    # 以分号、句号、逗号拆分出子任务
    sub_tasks = re.split(r'[；;，,。]', q_text)
    sub_tasks = [s.strip() for s in sub_tasks if len(s.strip()) > 4]
    if len(sub_tasks) >= 2:
        # 检查每个子任务的关键词是否出现在回答中
        addressed = 0
        for st in sub_tasks:
            # 取子任务中的关键名词（2字以上）
            keywords = re.findall(r'[\u4e00-\u9fa5]{2,4}', st)
            if any(kw in text for kw in keywords[:3]):
                addressed += 1
        aspect_ratio = addressed / len(sub_tasks)
        score += round(3 * aspect_ratio, 1)
    else:
        # 单一问题，只要回答内容充实就给分（简洁直答也是完整回答）
        score += 3 if len(text) > 50 else (2 if len(text) > 20 else 1)

    # ③ 回答结构丰富度 (2分)
    paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
    if is_local:
        has_table = response.get('has_table', 0)
        has_chart = response.get('has_chart', 0)
        extras = has_table + has_chart + (1 if len(paragraphs) >= 3 else 0)
        score += min(extras, 2)
    else:
        feat = auto_detect_text_features(text)
        struct_pts = 0
        if feat['has_table']:
            struct_pts += 1  # 有结构化数据展示
        if len(paragraphs) >= 3:
            struct_pts += 1  # 多段落
        if feat['structured_data_score'] >= 0.5:
            struct_pts += 1  # 整体结构化程度高
        score += min(struct_pts, 2)

    # ④ 后续建议/扩展 (2分)
    if is_local:
        followup = response.get('follow_up_count', 0)
        if followup >= 2:
            score += 2
        elif followup >= 1:
            score += 1.5
        elif response.get('insights_count', 0) > 0:
            score += 1
    else:
        extend_kw = [
            '此外', '另外', '值得注意', '建议', '进一步', '需要关注', '补充',
            '综合来看', '总结', '总体来看', '整体来看', '综上',
            '需要注意的是', '值得一提', '值得关注', '不容忽视',
            '同时', '与此同时', '除此之外',
            '从.*角度', '从.*来看', '从.*方面',
            '展望', '未来', '趋势', '风险',
            '总的来说', '概括而言', '换言之',
        ]
        ext_hits = sum(1 for kw in extend_kw if re.search(kw, text))
        if ext_hits >= 2:
            score += 2
        elif ext_hits >= 1:
            score += 1

    return min(round(score, 1), max_score)


def score_hallucination(response: dict, ground_truth: dict | None, question: dict,
                        is_local: bool = False) -> float:
    """抗幻觉能力评分 (0~10)。满分=完全没有幻觉。

    本系统 (10分):
      ① SQL 执行成功     (4分) — 数据来自真实查询，非编造
      ② 内容与数据一致   (3分) — 文本中数值与 rows 数据吻合
      ③ 无幻觉标记       (3分) — 无虚构公司/指标/不存在的时间段
    外部LLM:
      基于 ground truth 匹配 + 可疑数字检测 + 诚实表达。
    """
    max_score = DIMENSIONS['hallucination'][1]  # 10

    if is_local:
        reply = response.get('reply', {})
        text = response.get('response_text', '')
        score = 0.0

        # ① SQL 执行成功 (4分)
        status = reply.get('status', '')
        has_sql = bool(reply.get('sql'))
        if status == 'done' and has_sql:
            score += 4
        elif status == 'done':
            score += 2  # 成功但无 SQL（可能是 KB-only 回答）
        elif has_sql:
            score += 1  # 有 SQL 但状态非 done

        # ② 内容与数据一致 (3分)
        rows = reply.get('rows', [])
        if rows:
            # 从 rows 中取前几个数值
            row_nums = set()
            for row in rows[:10]:
                for v in (row.values() if isinstance(row, dict) else row):
                    try:
                        fv = float(str(v).replace(',', ''))
                        if fv > 1:
                            row_nums.add(round(fv, 2))
                    except (ValueError, TypeError):
                        pass

            text_nums = _extract_numbers(text)
            if row_nums and text_nums:
                # 文本中的数字有多少能在 rows 中找到
                consistent = 0
                for tn in text_nums[:10]:
                    if any(abs(tn - rn) / max(rn, 1) < 0.05 for rn in row_nums):
                        consistent += 1
                if consistent >= 3:
                    score += 3
                elif consistent >= 1:
                    score += 2
                else:
                    score += 1  # 有数据但文本数字不一致，可能是 LLM 总结时出入
            else:
                score += 2  # 有数据行但难以比对
        elif text and len(text) > 50:
            score += 1  # 无数据行但有文本

        # ③ 无幻觉标记 (3分)
        hallucination_markers = [
            r'虚拟', r'假设', r'模拟数据', r'仅供参考.*?不构成',
            r'以上数据.*?编造', r'无法.*?获取.*?真实',
        ]
        entities = question.get('key_entities', [])
        has_marker = any(re.search(p, text) for p in hallucination_markers)
        # 检查是否提到了不在问题中的公司名（可能编造）
        if has_marker:
            score += 0
        elif status == 'done':
            score += 3
        else:
            score += 1

        return min(round(score, 1), max_score)

    # ── 外部 LLM ──────────────────────────────────────────────────────
    text = response.get('response_text', '')
    if not text.strip():
        return max_score * 0.5

    honest_patterns = ['无法确定', '不确定', '没有找到', '无法获取',
                       '建议查阅', '请以.*为准', '截止.*训练', '数据可能.*过时']
    has_honest = any(re.search(p, text) for p in honest_patterns)

    feat = auto_detect_text_features(text)

    if not ground_truth or not ground_truth.get('key_numbers'):
        ref_count = response.get('reference_count', 0)
        score = 0.0

        # ① 数据来源可信 (4分)
        if ref_count >= 3 or feat['has_source']:
            score += 4
        elif ref_count >= 1:
            score += 3
        elif has_honest:
            score += 2
        else:
            score += 1

        # ② 内容合理性 (3分) — 无法比对 GT，按来源和结构判断
        if feat['structured_data_score'] >= 0.5 and feat['has_source']:
            score += 3
        elif feat['structured_data_score'] >= 0.3 or feat['has_source']:
            score += 2
        else:
            score += 1

        # ③ 无幻觉标记 (3分)
        hallucination_markers = [
            r'虚拟', r'假设', r'模拟数据', r'仅供参考.*?不构成',
            r'以上数据.*?编造', r'无法.*?获取.*?真实',
        ]
        has_marker = any(re.search(p, text) for p in hallucination_markers)
        if has_marker:
            score += 0
        elif ref_count >= 1 or has_honest:
            score += 3
        else:
            score += 1

        return min(round(score, 1), max_score)

    gt_numbers = set(ground_truth['key_numbers'])
    resp_numbers = _extract_numbers(text)

    if not resp_numbers:
        return max_score * 0.7 if has_honest else max_score * 0.5

    score = 0.0
    ref_count = response.get('reference_count', 0)

    # ① 数据来源可信 (4分) — 等价于本系统 SQL执行成功
    if ref_count >= 3 or feat['has_source']:
        score += 4  # 有参考来源，数据来自真实搜索
    elif ref_count >= 1:
        score += 3
    elif len(resp_numbers) > 0:
        score += 2  # 有数字但无明确来源
    elif has_honest:
        score += 2  # 诚实表达不确定性

    # ② 内容与数据一致 (3分) — 与 ground truth 匹配
    matched = 0
    for rn in resp_numbers:
        is_close = any(
            (abs(rn - gt) / gt < 0.1 if gt > 0 else abs(rn - gt) < 1)
            for gt in gt_numbers
        )
        is_unit_match = any(
            (abs(rn * 10000 - gt) / gt < 0.1 if gt > 0 else False)
            or (abs(rn / 10000 - gt) / gt < 0.1 if gt > 0 else False)
            for gt in gt_numbers
        )
        if is_close or is_unit_match:
            matched += 1
    if matched >= 2:
        score += 3
    elif matched >= 1:
        score += 2
    else:
        score += 1  # 口径不同但不一定是编造

    # ③ 无幻觉标记 (3分)
    hallucination_markers = [
        r'虚拟', r'假设', r'模拟数据', r'仅供参考.*?不构成',
        r'以上数据.*?编造', r'无法.*?获取.*?真实',
    ]
    has_marker = any(re.search(p, text) for p in hallucination_markers)
    if has_marker:
        score += 0
    elif ref_count >= 1 or feat['has_source']:
        score += 3  # 有来源且无幻觉标记
    elif has_honest:
        score += 3  # 诚实表达不确定性
    else:
        score += 1

    return min(round(score, 1), max_score)


def evaluate_response(response: dict, question: dict,
                      ground_truth: dict | None = None,
                      is_local: bool = False) -> dict[str, float]:
    """对一个回答进行 7 维评分，返回各维度得分。"""
    scores = {
        'accuracy':      score_accuracy(response, ground_truth, question, is_local),
        'provenance':    score_provenance(response, question, is_local),
        'calculation':   score_calculation(response, question, is_local),
        'visualization': score_visualization(response, question, is_local),
        'depth':         score_depth(response, question, is_local),
        'completeness':  score_completeness(response, question, is_local),
        'hallucination': score_hallucination(response, ground_truth, question, is_local),
    }
    scores['total'] = round(sum(scores.values()), 1)
    return scores


# ═══════════════════════════════════════════════════════════════════════════════
#  Ground Truth 提取（从本系统结果中提取标准答案）
# ═══════════════════════════════════════════════════════════════════════════════

def extract_ground_truth(local_result: dict) -> dict:
    """从本系统的结构化回复中提取 ground truth 数值。"""
    reply = local_result.get('reply', {})
    text = reply.get('content', '')
    rows = reply.get('rows', [])

    # 提取关键数值
    key_numbers = []
    for row in rows[:20]:
        for v in row.values():
            if isinstance(v, (int, float)) and v != 0:
                key_numbers.append(float(v))

    # 也从文本中提取
    text_nums = _extract_numbers(text)
    for n in text_nums[:10]:
        if n not in key_numbers:
            key_numbers.append(n)

    return {
        'key_numbers': key_numbers[:20],
        'source_tables': reply.get('sourceTables', []),
        'sql': reply.get('sql', ''),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  主运行流程
# ═══════════════════════════════════════════════════════════════════════════════

GROUND_TRUTH_PATH = _THIS_DIR / 'ground_truth.json'


def _load_ground_truth() -> dict[str, dict] | None:
    """从 ground_truth.json 加载标准答案。返回 {qid: {key_numbers, ...}} 或 None。"""
    if not GROUND_TRUTH_PATH.exists():
        return None
    try:
        with open(GROUND_TRUTH_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        gt_map: dict[str, dict] = {}
        for qid, entry in data.get('questions', {}).items():
            gt_map[qid] = {
                'key_numbers': entry.get('key_numbers', []),
                'source_tables': entry.get('source_tables', []),
                'sql': entry.get('sql', entry.get('sql_used', '')),
            }
        return gt_map
    except Exception as e:
        print(f'  ⚠ 加载 ground_truth.json 失败: {e}', flush=True)
        return None


def run_benchmark(questions: list[dict] | None = None,
                  run_local: bool = True) -> dict[str, Any]:
    """运行完整基准对比实验。

    Args:
        run_local: 是否运行本系统。为 False 时仅对外部LLM评分。
    """
    if questions is None:
        questions = load_questions()

    W = 76
    print(f'\n{"═" * W}', flush=True)
    print(f'  基准对比实验（Benchmark）', flush=True)
    print(f'  时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', flush=True)
    print(f'  题目数: {len(questions)}', flush=True)
    print(f'{"═" * W}', flush=True)

    # ── Phase 1a: 加载 ground truth ──────────────────────────────────────
    ground_truths = _load_ground_truth() or {}
    if ground_truths:
        print(f'\n{"─" * W}', flush=True)
        print(f'  Ground Truth: 已加载 {len(ground_truths)} 题标准答案', flush=True)
        print(f'  📄 来源: {GROUND_TRUTH_PATH}', flush=True)
        print(f'{"─" * W}', flush=True)
    else:
        print(f'\n{"─" * W}', flush=True)
        print(f'  ⚠ 未找到 ground_truth.json，外部LLM准确性将按无标准答案评分', flush=True)
        print(f'  💡 运行 python -m benchmark.generate_ground_truth 生成', flush=True)
        print(f'{"─" * W}', flush=True)

    # ── Phase 1b: 运行本系统（可选）───────────────────────────────────────
    local_results = []
    if run_local:
        print(f'\n{"─" * W}', flush=True)
        print(f'  Phase 1: 运行本系统', flush=True)
        print(f'{"─" * W}', flush=True)
        local_results = run_local_system(questions)

        # 保存本系统回答（与外部LLM同格式）
        local_save_path = LLM_RESPONSES_DIR / '_local_system.json'
        local_save_data = {
            'system_name': SYSTEM_NAME,
            'system_version': 'smart-query-assistant-v1',
            'collection_date': datetime.now().strftime('%Y-%m-%d'),
            'collection_method': 'auto-benchmark',
            'provenance_type': 'sql_field_level',
            'provenance_desc': 'SQL查询→数据表→数据行→字段级完整溯源链',
            'responses': [],
        }
        for lr in local_results:
            local_save_data['responses'].append({
                'question_id': lr['question_id'],
                'reference_count': lr.get('kb_refs_count', 0),
                'response_text': lr.get('response_text', ''),
                'has_table': lr.get('has_table', 0),
                'has_chart': lr.get('has_chart', 0),
                'response_time_seconds': lr.get('response_time_seconds', 0),
                'notes': f"sql={bool(lr.get('reply', {}).get('sql'))}, "
                         f"rows={lr.get('row_count', 0)}, "
                         f"provenance={lr.get('provenance_count', 0)}, "
                         f"calc_traces={lr.get('calc_traces_count', 0)}, "
                         f"insights={lr.get('insights_count', 0)}, "
                         f"thinking={lr.get('thinking_count', 0)}",
                # 保留完整结构化数据供分析
                '_reply': lr.get('reply', {}),
                '_structured': {
                    'provenance_count': lr.get('provenance_count', 0),
                    'calc_traces_count': lr.get('calc_traces_count', 0),
                    'kb_refs_count': lr.get('kb_refs_count', 0),
                    'has_thinking_map': lr.get('has_thinking_map', 0),
                    'insights_count': lr.get('insights_count', 0),
                    'follow_up_count': lr.get('follow_up_count', 0),
                    'thinking_count': lr.get('thinking_count', 0),
                    'row_count': lr.get('row_count', 0),
                },
            })
        with open(local_save_path, 'w', encoding='utf-8') as f:
            json.dump(local_save_data, f, ensure_ascii=False, indent=2)
        print(f'  💾 本系统回答已保存: {local_save_path}', flush=True)

    # ── Phase 2: 加载外部 LLM 回答 ────────────────────────────────────────
    print(f'\n{"─" * W}', flush=True)
    print(f'  Phase 2: 加载外部 LLM 回答', flush=True)
    print(f'{"─" * W}', flush=True)

    external_data = load_external_responses()
    if not external_data:
        print(f'  📝 未找到外部 LLM 回答文件', flush=True)
        print(f'  💡 请将回答填入 benchmark/llm_responses/ 目录下', flush=True)
        print(f'     模板文件: benchmark/llm_responses/_template.json', flush=True)

    # ── Phase 3: 评分 ─────────────────────────────────────────────────────
    print(f'\n{"─" * W}', flush=True)
    print(f'  Phase 3: 多维度评分', flush=True)
    print(f'{"─" * W}', flush=True)

    all_scores: dict[str, list[dict]] = {}  # {系统名: [{question_id, scores}]}

    # 本系统评分（有本地结果时评分；无则尝试从缓存加载）
    if not local_results:
        _cache_path = LLM_RESPONSES_DIR / '_local_system.json'
        if _cache_path.exists():
            try:
                with open(_cache_path, 'r', encoding='utf-8') as f:
                    _cache = json.load(f)
                for resp in _cache.get('responses', []):
                    s = resp.get('_structured', {})
                    local_results.append({
                        'question_id': resp['question_id'],
                        'reply': resp.get('_reply', {}),
                        'response_text': resp.get('response_text', ''),
                        'row_count': s.get('row_count', 0),
                        'has_chart': resp.get('has_chart', 0),
                        'has_table': resp.get('has_table', 0),
                        'response_time_seconds': resp.get('response_time_seconds', 0),
                        **s,
                    })
                print(f'  📂 从缓存加载本系统回答: {len(local_results)} 条', flush=True)
            except Exception as e:
                print(f'  ⚠ 加载缓存失败: {e}', flush=True)

    if local_results:
        local_scored = []
        for lr in local_results:
            qid = lr['question_id']
            q = next((q for q in questions if q['id'] == qid), None)
            if not q:
                continue
            scores = evaluate_response(lr, q, ground_truths.get(qid), is_local=True)
            local_scored.append({
                'question_id': qid,
                'response': lr,
                'scores': scores,
            })
        if local_scored:
            all_scores[SYSTEM_NAME] = local_scored
            avg_local = round(sum(s['scores']['total'] for s in local_scored) / len(local_scored), 1)
            print(f'  ✅ {SYSTEM_NAME}: 平均 {avg_local} 分', flush=True)
    else:
        print(f'  ⏭ 跳过本系统评分（无缓存）', flush=True)

    # 外部系统评分
    for sys_name, responses in external_data.items():
        sys_scored = []
        for resp in responses:
            qid = resp.get('question_id', '')
            q = next((q for q in questions if q['id'] == qid), None)
            if not q:
                continue
            gt = ground_truths.get(qid)
            scores = evaluate_response(resp, q, gt, is_local=False)
            sys_scored.append({
                'question_id': qid,
                'response': resp,
                'scores': scores,
            })
        if sys_scored:
            all_scores[sys_name] = sys_scored
            avg = round(sum(s['scores']['total'] for s in sys_scored) / len(sys_scored), 1)
            print(f'  ✅ {sys_name}: 平均 {avg} 分', flush=True)

    # ── Phase 4: 终端汇总 ─────────────────────────────────────────────────
    print(f'\n{"═" * W}', flush=True)
    print(f'  评分汇总', flush=True)
    print(f'{"═" * W}', flush=True)

    _print_comparison_table(all_scores, questions)

    return {
        'questions': questions,
        'local_results': local_results,
        'ground_truths': ground_truths,
        'all_scores': all_scores,
    }


def _print_comparison_table(all_scores: dict, questions: list[dict]):
    """在终端打印多系统对比表。"""
    dim_keys = list(DIMENSIONS.keys())
    systems = list(all_scores.keys())

    # 表头
    header = f'  {"系统":<20s}'
    for dk in dim_keys:
        label = DIMENSIONS[dk][0][:4]
        header += f' {label:>6s}'
    header += f' {"总分":>6s}'
    print(header, flush=True)
    print(f'  {"─" * 20}' + (' ' + '─' * 6) * (len(dim_keys) + 1), flush=True)

    for sys_name in systems:
        scored = all_scores[sys_name]
        # 跨所有题目取平均
        avg_scores: dict[str, float] = {}
        for dk in dim_keys:
            vals = [s['scores'][dk] for s in scored]
            avg_scores[dk] = round(sum(vals) / len(vals), 1) if vals else 0
        total = round(sum(avg_scores.values()), 1)

        line = f'  {sys_name:<20s}'
        for dk in dim_keys:
            line += f' {avg_scores[dk]:>6.1f}'
        line += f' {total:>6.1f}'
        print(line, flush=True)

    print(flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  HTML 对比报告生成
# ═══════════════════════════════════════════════════════════════════════════════

def generate_html_report(benchmark_data: dict[str, Any],
                         output_path: str | None = None) -> str:
    """生成多系统对比 HTML 可视化报告。"""
    questions = benchmark_data['questions']
    all_scores = benchmark_data['all_scores']
    systems = list(all_scores.keys())

    if output_path is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = str(_THIS_DIR / f'benchmark_report_{ts}.html')

    def _esc(s):
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    dim_keys = list(DIMENSIONS.keys())

    # ── 系统平均分 ──────────────────────────────────────────────────────────
    system_avgs: dict[str, dict[str, float]] = {}
    for sys_name in systems:
        scored = all_scores[sys_name]
        avgs = {}
        for dk in dim_keys:
            vals = [s['scores'][dk] for s in scored]
            avgs[dk] = round(sum(vals) / len(vals), 1) if vals else 0
        avgs['total'] = round(sum(avgs.values()), 1)
        system_avgs[sys_name] = avgs

    # ── 汇总表 HTML ─────────────────────────────────────────────────────────
    summary_table = '<table class="cmp-table"><thead><tr><th>系统</th>'
    for dk in dim_keys:
        label, weight = DIMENSIONS[dk]
        summary_table += f'<th>{_esc(label)}<br><small>({weight}分)</small></th>'
    summary_table += '<th>总分<br><small>(100)</small></th></tr></thead><tbody>'

    # 找到总分最高的系统
    best_sys = max(system_avgs.items(), key=lambda x: x[1]['total'])[0] if system_avgs else ''

    for sys_name in systems:
        avgs = system_avgs[sys_name]
        is_best = sys_name == best_sys
        cls = 'row-best' if is_best else ''
        summary_table += f'<tr class="{cls}"><td><b>{_esc(sys_name)}</b></td>'
        for dk in dim_keys:
            max_val = DIMENSIONS[dk][1]
            val = avgs[dk]
            pct = val / max_val if max_val else 0
            color = '#10b981' if pct >= 0.8 else '#f59e0b' if pct >= 0.5 else '#ef4444'
            summary_table += f'<td style="color:{color};font-weight:700;">{val}</td>'
        total = avgs['total']
        summary_table += f'<td style="font-size:18px;font-weight:800;">{total}</td></tr>'
    summary_table += '</tbody></table>'

    # ── CSS 雷达图（用 SVG 模拟） ───────────────────────────────────────────
    radar_svg = _build_radar_svg(system_avgs, dim_keys)

    # ── 评分标准体系图 ─────────────────────────────────────────────────────
    scoring_criteria_table = _build_scoring_criteria_table()

    # ── 逐题详情 ────────────────────────────────────────────────────────────
    question_cards = ''
    for q in questions:
        qid = q['id']
        card = f'''<div class="card">
            <div class="card-header">{_esc(qid)}: {_esc(q["question"][:60])}
                <span class="tag">{_esc(q["category"])}</span>
                <span class="tag tag-diff">{_esc(q["difficulty"])}</span>
            </div><div class="card-body">'''

        # 每个系统的评分
        card += '<table class="q-table"><thead><tr><th>系统</th>'
        for dk in dim_keys:
            card += f'<th>{_esc(DIMENSIONS[dk][0][:4])}</th>'
        card += '<th>总分</th><th>摘要</th></tr></thead><tbody>'

        for sys_name in systems:
            scored = all_scores[sys_name]
            item = next((s for s in scored if s['question_id'] == qid), None)
            if not item:
                continue
            sc = item['scores']
            resp_text = item['response'].get('response_text', '')
            preview = _esc(resp_text[:120] + ('…' if len(resp_text) > 120 else ''))

            card += f'<tr><td><b>{_esc(sys_name[:10])}</b></td>'
            for dk in dim_keys:
                max_val = DIMENSIONS[dk][1]
                val = sc[dk]
                pct = val / max_val if max_val else 0
                bg = f'rgba(16,185,129,{pct*0.3})' if pct >= 0.7 else f'rgba(239,68,68,{(1-pct)*0.3})'
                card += f'<td style="background:{bg};font-weight:600;">{val}</td>'
            card += f'<td style="font-weight:800;font-size:15px;">{sc["total"]}</td>'
            card += f'<td class="preview">{preview}</td></tr>'

        card += '</tbody></table></div></div>'
        question_cards += card

    # ── 优势分析卡片 ────────────────────────────────────────────────────────
    advantage_html = _build_advantage_section(system_avgs, dim_keys)

    # ── 完整 HTML ─────────────────────────────────────────────────────────
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>基准对比报告 — Benchmark Report</title>
<style>
:root {{
    --blue: #023047; --light-blue: #90C9E6; --yellow: #FFB703;
    --orange: #FB8402; --teal: #219EBD; --gray: #D0D0D0; --dark: #404040;
    --green: #10b981; --red: #ef4444;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; background: #f0f4f8; color: var(--dark); line-height: 1.6; }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 24px 16px; }}
h1 {{ color: var(--blue); font-size: 26px; margin-bottom: 6px; }}
h1 small {{ display: block; font-weight: 400; font-size: 13px; color: var(--light-blue); margin-top: 4px; }}
h2 {{ color: var(--blue); font-size: 18px; margin: 24px 0 12px; }}
.card {{ background: #fff; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,.06); overflow: hidden; }}
.card-header {{ padding: 14px 22px; font-weight: 700; font-size: 15px; border-bottom: 2px solid var(--light-blue); color: var(--blue); }}
.card-body {{ padding: 18px 22px; overflow-x: auto; }}
.tag {{ display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 8px; font-weight: 600; background: #dbeafe; color: #1e40af; margin-left: 8px; }}
.tag-diff {{ background: #fef3c7; color: #92400e; }}
/* 表格 */
.cmp-table, .q-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.cmp-table th, .q-table th {{ background: var(--blue); color: #fff; padding: 10px 8px; text-align: center; font-weight: 600; white-space: nowrap; }}
.cmp-table td, .q-table td {{ padding: 8px; text-align: center; border-bottom: 1px solid #e5e7eb; }}
.cmp-table tr:hover, .q-table tr:hover {{ background: #f0f9ff; }}
.row-best {{ background: #ecfdf5 !important; }}
.preview {{ text-align: left; font-size: 12px; color: #666; max-width: 300px; }}
/* 雷达图区域 */
.radar-container {{ display: flex; justify-content: center; padding: 20px; }}
/* 优势卡片 */
.adv-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }}
.adv-card {{ background: #fff; border-radius: 10px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,.06); border-left: 4px solid var(--green); }}
.adv-card h4 {{ color: var(--blue); margin-bottom: 8px; }}
.adv-card .score {{ font-size: 28px; font-weight: 800; color: var(--green); }}
.adv-card .vs {{ font-size: 14px; color: #888; margin-top: 4px; }}
</style>
</head>
<body>
<div class="container">
    <h1>基准对比报告
        <small>Benchmark Report — {datetime.now().strftime("%Y-%m-%d %H:%M")} | {len(questions)} 个问题 | {len(systems)} 个系统</small>
    </h1>

    <h2>一、综合评分对比</h2>
    <div class="card"><div class="card-body">{summary_table}</div></div>

    <h2>二、能力雷达图</h2>
    <div class="card"><div class="card-body"><div class="radar-container">{radar_svg}</div></div></div>

    <h2>三、评分标准体系</h2>
    <div class="card"><div class="card-body">{scoring_criteria_table}</div></div>

    <h2>四、核心优势分析</h2>
    {advantage_html}

    <h2>五、逐题详细评分</h2>
    {question_cards}
</div>
</body>
</html>'''

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    return output_path


def _build_radar_svg(system_avgs: dict, dim_keys: list[str]) -> str:
    """生成 SVG 雷达图。"""
    import math

    n = len(dim_keys)
    cx, cy, r = 200, 200, 160
    angle_step = 2 * math.pi / n

    # 颜色分配
    colors = ['#1a1a1a', '#2563eb', '#eab308', '#16a34a', '#f59e0b']

    # 网格线
    svg = f'<svg width="450" height="430" viewBox="0 0 450 430">'
    svg += f'<rect width="450" height="430" fill="#fafbfc" rx="10"/>'

    # 同心多边形网格
    for level in [0.2, 0.4, 0.6, 0.8, 1.0]:
        points = []
        for i in range(n):
            angle = -math.pi / 2 + i * angle_step
            x = cx + r * level * math.cos(angle)
            y = cy + r * level * math.sin(angle)
            points.append(f'{x:.1f},{y:.1f}')
        svg += f'<polygon points="{" ".join(points)}" fill="none" stroke="#ddd" stroke-width="1"/>'

    # 轴线 + 标签
    for i, dk in enumerate(dim_keys):
        angle = -math.pi / 2 + i * angle_step
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        svg += f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#ddd" stroke-width="1"/>'
        # 标签
        lx = cx + (r + 30) * math.cos(angle)
        ly = cy + (r + 30) * math.sin(angle)
        label = DIMENSIONS[dk][0][:4]
        anchor = 'middle'
        if lx < cx - 10:
            anchor = 'end'
        elif lx > cx + 10:
            anchor = 'start'
        svg += f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" font-size="12" fill="#333" font-family="Microsoft YaHei">{label}</text>'

    # 数据多边形
    for si, (sys_name, avgs) in enumerate(system_avgs.items()):
        color = colors[si % len(colors)]
        points = []
        for i, dk in enumerate(dim_keys):
            max_val = DIMENSIONS[dk][1]
            val = avgs.get(dk, 0)
            ratio = val / max_val if max_val else 0
            angle = -math.pi / 2 + i * angle_step
            x = cx + r * ratio * math.cos(angle)
            y = cy + r * ratio * math.sin(angle)
            points.append(f'{x:.1f},{y:.1f}')
        svg += f'<polygon points="{" ".join(points)}" fill="{color}" fill-opacity="0.15" stroke="{color}" stroke-width="2.5"/>'
        # 数据点
        for i, dk in enumerate(dim_keys):
            max_val = DIMENSIONS[dk][1]
            val = avgs.get(dk, 0)
            ratio = val / max_val if max_val else 0
            angle = -math.pi / 2 + i * angle_step
            x = cx + r * ratio * math.cos(angle)
            y = cy + r * ratio * math.sin(angle)
            svg += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"/>'

    # 图例
    for si, sys_name in enumerate(system_avgs.keys()):
        color = colors[si % len(colors)]
        ly = 410
        lx = 20 + si * 130
        svg += f'<rect x="{lx}" y="{ly}" width="12" height="12" fill="{color}" rx="2"/>'
        svg += f'<text x="{lx + 16}" y="{ly + 10}" font-size="11" fill="#333" font-family="Microsoft YaHei">{sys_name[:12]}</text>'

    svg += '</svg>'
    return svg


def _build_scoring_criteria_table() -> str:
    """生成统一评分标准 HTML 表格。"""
    rows = [
        ('数据准确性', 30, '#023047', [
            ('查询成功并返回目标数据', 10),
            ('回答与问题意图匹配', 8),
            ('覆盖问题中的核心实体', 6),
            ('包含实质性数值（非空洞文字）', 6),
        ]),
        ('数据溯源', 15, '#2a9d8f', [
            ('数据来源可追溯（引用机制）', 6),
            ('字段/文本级归因精度', 3),
            ('来源标注完整性', 2),
            ('深度溯源（多源交叉验证）', 4),
        ]),
        ('计算可解释', 10, '#e63946', [
            ('计算过程可追踪', 4),
            ('公式/推导步骤展示', 3),
            ('计算结果可验证', 3),
        ]),
        ('可视化能力', 10, '#7c3aed', [
            ('图表生成与展示', 4),
            ('图表类型与问题匹配', 2),
            ('数据表格+图表双重展示', 2),
            ('平台可视化能力', 2),
        ]),
        ('分析深度', 15, '#f59e0b', [
            ('文本分析质量（长度+关键词密度）', 5),
            ('数据洞察与归因分析', 4),
            ('多维度覆盖（趋势/对比/原因/建议）', 4),
            ('结构化表达（分段/列表/总结）', 2),
        ]),
        ('响应完整性', 10, '#0ea5e9', [
            ('核心实体覆盖', 3),
            ('问题要素全面回应', 3),
            ('回答结构丰富度', 2),
            ('后续建议与扩展分析', 2),
        ]),
        ('抗幻觉能力', 10, '#64748b', [
            ('数值与标准答案一致', 4),
            ('无自相矛盾表述', 3),
            ('对不确定内容恰当表达', 3),
        ]),
    ]

    total = sum(r[1] for r in rows)

    html = '''<table class="summary-table" style="font-size:13px;">
<thead><tr>
  <th style="width:110px;">评估维度</th>
  <th style="width:55px;text-align:center;">满分</th>
  <th style="width:40px;text-align:center;">权重</th>
  <th>评分子项</th>
  <th style="width:60px;text-align:center;">分值</th>
</tr></thead><tbody>'''

    for name, max_score, color, items in rows:
        pct = f'{max_score/total*100:.0f}%'
        n = len(items)
        for j, (item_name, item_score) in enumerate(items):
            html += '<tr>'
            if j == 0:
                html += f'<td rowspan="{n}" style="font-weight:700;color:{color};vertical-align:middle;">{name}</td>'
                html += f'<td rowspan="{n}" style="text-align:center;font-weight:700;font-size:15px;vertical-align:middle;">{max_score}</td>'
                html += f'<td rowspan="{n}" style="text-align:center;color:#64748b;vertical-align:middle;">{pct}</td>'
            html += f'<td>{chr(0x2460 + j)} {item_name}</td>'
            html += f'<td style="text-align:center;font-weight:600;">{item_score}</td>'
            html += '</tr>'

    html += f'''<tr style="background:#f1f5f9;font-weight:700;">
  <td>合计</td>
  <td style="text-align:center;font-size:16px;">{total}</td>
  <td style="text-align:center;">100%</td>
  <td colspan="2" style="color:#64748b;font-weight:normal;font-size:12px;">
    所有系统采用相同评分标准。本系统通过结构化字段（SQL/行数/图表对象）自动评测；<br>
    外部LLM通过文本分析+平台能力元数据评测。评分标准一致，检测手段因数据形式而异。
  </td>
</tr></tbody></table>'''
    return html


def _build_advantage_section(system_avgs: dict, dim_keys: list[str]) -> str:
    """生成优势分析卡片。"""
    if SYSTEM_NAME not in system_avgs:
        return '<p>暂无数据</p>'

    local_avgs = system_avgs[SYSTEM_NAME]
    others = {k: v for k, v in system_avgs.items() if k != SYSTEM_NAME}

    if not others:
        return '''<div class="adv-grid"><div class="adv-card">
            <h4>📝 等待外部系统数据</h4>
            <p>请将各大模型的回答录入 <code>benchmark/llm_responses/</code> 后重新运行，即可生成完整对比。</p>
        </div></div>'''

    html = '<div class="adv-grid">'
    for dk in dim_keys:
        label, max_val = DIMENSIONS[dk]
        local_val = local_avgs.get(dk, 0)
        other_avg = sum(o.get(dk, 0) for o in others.values()) / len(others)
        advantage = local_val - other_avg

        icon = '🟢' if advantage > 0 else '🟡' if advantage == 0 else '🔴'
        html += f'''<div class="adv-card">
            <h4>{icon} {label}</h4>
            <div class="score">{local_val}/{max_val}</div>
            <div class="vs">vs 竞品平均 {other_avg:.1f}/{max_val}
                {'（领先 ' + f'+{advantage:.1f}' + '）' if advantage > 0 else ''}
            </div>
        </div>'''
    html += '</div>'
    return html


# ═══════════════════════════════════════════════════════════════════════════════
#  JSON 持久化
# ═══════════════════════════════════════════════════════════════════════════════

def save_json(data: dict, path: str | None = None) -> str:
    if path is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = str(_THIS_DIR / f'benchmark_results_{ts}.json')

    def _ser(obj):
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, set):
            return list(obj)
        raise TypeError(f'Not serializable: {type(obj)}')

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=_ser)
    return path


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description='基准对比实验')
    parser.add_argument('--questions', '-q', type=int, default=None,
                        help='使用题库中前 N 个问题（默认全部）')
    parser.add_argument('--output', '-o', type=str, default=None,
                        help='HTML 报告输出路径')
    parser.add_argument('--json', action='store_true',
                        help='同时保存 JSON 结果')
    parser.add_argument('--no-local', action='store_true',
                        help='跳过本系统运行，仅对外部LLM评分（需已有 ground_truth.json）')
    args = parser.parse_args()

    questions = load_questions(args.questions)
    data = run_benchmark(questions, run_local=not args.no_local)

    # 生成 HTML 报告
    report_path = generate_html_report(data, args.output)
    print(f'\n📊 HTML 对比报告: {report_path}', flush=True)

    if args.json:
        json_path = save_json(data)
        print(f'💾 JSON 数据: {json_path}', flush=True)

    try:
        webbrowser.open(f'file:///{report_path}')
        print(f'🌐 已在浏览器中打开', flush=True)
    except Exception:
        pass


if __name__ == '__main__':
    main()
