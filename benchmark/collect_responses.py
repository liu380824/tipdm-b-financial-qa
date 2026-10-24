#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
外部大模型回答收集工具
=======================
支持两种模式：
  1. API 自动化（DeepSeek）— 需要 API Key
  2. 交互式手动录入（豆包、秘塔AI）— 逐题提示，粘贴回答

使用方法:
  # 交互式手动录入（豆包）
  python -m benchmark.collect_responses --system 豆包

  # 交互式手动录入（秘塔AI）
  python -m benchmark.collect_responses --system 秘塔AI

  # DeepSeek API 自动化
  python -m benchmark.collect_responses --system DeepSeek --api-key sk-xxx

  # DeepSeek 用环境变量传 key
  set DEEPSEEK_API_KEY=sk-xxx
  python -m benchmark.collect_responses --system DeepSeek

  # 只收集前 3 题
  python -m benchmark.collect_responses --system 豆包 --questions 3
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
_RESPONSES_DIR = _THIS_DIR / 'llm_responses'


def load_questions(limit: int | None = None) -> list[dict]:
    with open(_THIS_DIR / 'questions.json', 'r', encoding='utf-8') as f:
        questions = json.load(f)
    return questions[:limit] if limit else questions


def save_responses(system_name: str, responses: list[dict], version: str = ''):
    """保存回答到 llm_responses/ 目录。"""
    _RESPONSES_DIR.mkdir(exist_ok=True)

    # 文件名映射
    name_map = {
        '豆包': 'doubao',
        'DeepSeek': 'deepseek',
        'deepseek': 'deepseek',
        '秘塔AI': 'mita',
        '秘塔': 'mita',
    }
    fname = name_map.get(system_name, system_name.lower().replace(' ', '_'))
    fpath = _RESPONSES_DIR / f'{fname}.json'

    data = {
        'system_name': system_name,
        'system_version': version,
        'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'responses': responses,
    }
    with open(fpath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f'\n  💾 已保存到 {fpath}', flush=True)
    return str(fpath)


# ═══════════════════════════════════════════════════════════════════════════════
#  模式 1: 交互式手动录入
# ═══════════════════════════════════════════════════════════════════════════════

def collect_manual(system_name: str, questions: list[dict]) -> list[dict]:
    """交互式逐题录入，用户从网页端复制粘贴回答。"""
    print(f'\n{"═" * 60}', flush=True)
    print(f'  手动收集模式: {system_name}', flush=True)
    print(f'  共 {len(questions)} 题', flush=True)
    print(f'{"═" * 60}', flush=True)
    print(f'\n  📋 操作步骤:', flush=True)

    urls = {
        '豆包': 'https://www.doubao.com',
        'DeepSeek': 'https://chat.deepseek.com',
        '秘塔AI': 'https://metaso.cn',
    }
    url = urls.get(system_name, '对应平台网址')
    print(f'  1. 打开 {url}', flush=True)
    print(f'  2. 对每个问题，复制问题 → 粘贴到对话框 → 等待回答', flush=True)
    print(f'  3. 复制完整回答 → 粘贴到此终端', flush=True)
    print(f'  4. 输入 END 结束当前题目的输入', flush=True)
    print(f'  5. 输入 SKIP 跳过当前题目', flush=True)
    print(f'  6. 输入 QUIT 终止收集（已收集的会保存）', flush=True)

    responses = []
    for qi, q in enumerate(questions, 1):
        qid = q['id']
        question = q['question']

        print(f'\n{"─" * 60}', flush=True)
        print(f'  题目 {qi}/{len(questions)} [{qid}] ({q["category"]})', flush=True)
        print(f'  ┌─────────────────────────────────────────────', flush=True)
        print(f'  │ {question}', flush=True)
        print(f'  └─────────────────────────────────────────────', flush=True)
        print(f'  👆 请将上面的问题复制到 {system_name}，然后粘贴回答:', flush=True)
        print(f'  （输入 END 结束 / SKIP 跳过 / QUIT 退出）', flush=True)

        lines = []
        t0 = time.time()
        quit_flag = False

        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip().upper() == 'END':
                break
            if line.strip().upper() == 'SKIP':
                lines = []
                break
            if line.strip().upper() == 'QUIT':
                quit_flag = True
                break
            lines.append(line)

        elapsed = time.time() - t0
        text = '\n'.join(lines).strip()

        if text:
            # 自动检测文本中的表格和图表特征
            has_table = bool(
                re.search(r'\|.*\|.*\|', text) or  # Markdown 表格
                len(re.findall(r'^.*[：:]\s*[\d,.]+.*$', text, re.MULTILINE)) >= 3  # 键值对
            )
            has_chart = bool(
                re.search(r'如图|如下图|见图|折线图|柱状图|饼图|雷达图|趋势图', text)
            )
            if has_table:
                print(f'  � 自动检测: 包含表格/结构化数据', flush=True)
            if has_chart:
                print(f'  📈 自动检测: 提及了图表', flush=True)

            responses.append({
                'question_id': qid,
                'response_text': text,
                'has_table': has_table,
                'has_chart': has_chart,
                'response_time_seconds': round(elapsed, 1),
                'notes': '',
            })
            print(f'  ✅ 已记录 ({len(text)} 字)', flush=True)
        else:
            responses.append({
                'question_id': qid,
                'response_text': '',
                'has_table': False,
                'has_chart': False,
                'response_time_seconds': 0,
                'notes': '已跳过',
            })
            print(f'  ⏭ 已跳过', flush=True)

        if quit_flag:
            print(f'\n  🛑 用户中止，保存已收集的 {len([r for r in responses if r["response_text"]])} 条', flush=True)
            break

    return responses


# ═══════════════════════════════════════════════════════════════════════════════
#  模式 2: DeepSeek API 自动化
# ═══════════════════════════════════════════════════════════════════════════════

def collect_deepseek_api(questions: list[dict], api_key: str) -> list[dict]:
    """通过 DeepSeek API 自动收集回答。"""
    try:
        from openai import OpenAI
    except ImportError:
        print('  ⚠ 需要安装 openai 库: pip install openai', flush=True)
        sys.exit(1)

    client = OpenAI(
        api_key=api_key,
        base_url='https://api.deepseek.com',
    )

    print(f'\n{"═" * 60}', flush=True)
    print(f'  DeepSeek API 自动收集', flush=True)
    print(f'  模型: deepseek-chat | 共 {len(questions)} 题', flush=True)
    print(f'{"═" * 60}', flush=True)

    responses = []
    for qi, q in enumerate(questions, 1):
        qid = q['id']
        question = q['question']
        print(f'  [{qi}/{len(questions)}] {qid}: {question[:40]}…', flush=True, end='')

        t0 = time.time()
        try:
            completion = client.chat.completions.create(
                model='deepseek-chat',
                messages=[
                    {'role': 'system', 'content': '你是一个专业的财务分析助手，请基于你的知识准确回答关于中国上市公司财报的问题。如果不确定数据，请明确说明。'},
                    {'role': 'user', 'content': question},
                ],
                temperature=0.1,
                max_tokens=2000,
            )
            text = completion.choices[0].message.content or ''
            elapsed = time.time() - t0

            responses.append({
                'question_id': qid,
                'response_text': text,
                'has_table': '|' in text,  # Markdown 表格检测
                'has_chart': False,
                'response_time_seconds': round(elapsed, 1),
                'notes': f'model=deepseek-chat, tokens={completion.usage.total_tokens if completion.usage else "?"}',
            })
            print(f'\r  [{qi}/{len(questions)}] {qid}: ✅ {elapsed:.1f}s | {len(text)}字', flush=True)
        except Exception as e:
            elapsed = time.time() - t0
            print(f'\r  [{qi}/{len(questions)}] {qid}: ❌ {e}', flush=True)
            responses.append({
                'question_id': qid,
                'response_text': f'API错误: {e}',
                'has_table': False,
                'has_chart': False,
                'response_time_seconds': round(elapsed, 1),
                'notes': f'error: {e}',
            })

        # 避免 API 限流
        time.sleep(1)

    return responses


# ═══════════════════════════════════════════════════════════════════════════════
#  模式 3: 豆包 API 自动化（火山引擎）
# ═══════════════════════════════════════════════════════════════════════════════

def collect_doubao_api(questions: list[dict], api_key: str,
                       model: str = 'doubao-pro-32k') -> list[dict]:
    """通过火山引擎 API 调用豆包模型。"""
    try:
        from openai import OpenAI
    except ImportError:
        print('  ⚠ 需要安装 openai 库: pip install openai', flush=True)
        sys.exit(1)

    client = OpenAI(
        api_key=api_key,
        base_url='https://ark.cn-beijing.volces.com/api/v3',
    )

    print(f'\n{"═" * 60}', flush=True)
    print(f'  豆包 API 自动收集（火山引擎）', flush=True)
    print(f'  模型: {model} | 共 {len(questions)} 题', flush=True)
    print(f'{"═" * 60}', flush=True)

    responses = []
    for qi, q in enumerate(questions, 1):
        qid = q['id']
        question = q['question']
        print(f'  [{qi}/{len(questions)}] {qid}: {question[:40]}…', flush=True, end='')

        t0 = time.time()
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': '你是一个专业的财务分析助手，请基于你的知识准确回答关于中国上市公司财报的问题。如果不确定数据，请明确说明。'},
                    {'role': 'user', 'content': question},
                ],
                temperature=0.1,
                max_tokens=2000,
            )
            text = completion.choices[0].message.content or ''
            elapsed = time.time() - t0

            responses.append({
                'question_id': qid,
                'response_text': text,
                'has_table': '|' in text,
                'has_chart': False,
                'response_time_seconds': round(elapsed, 1),
                'notes': f'model={model}',
            })
            print(f'\r  [{qi}/{len(questions)}] {qid}: ✅ {elapsed:.1f}s | {len(text)}字', flush=True)
        except Exception as e:
            elapsed = time.time() - t0
            print(f'\r  [{qi}/{len(questions)}] {qid}: ❌ {e}', flush=True)
            responses.append({
                'question_id': qid,
                'response_text': f'API错误: {e}',
                'has_table': False,
                'has_chart': False,
                'response_time_seconds': round(elapsed, 1),
                'notes': f'error: {e}',
            })
        time.sleep(1)

    return responses


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description='外部大模型回答收集工具')
    parser.add_argument('--system', '-s', required=True,
                        help='系统名称: 豆包 / DeepSeek / 秘塔AI')
    parser.add_argument('--api-key', '-k', type=str, default=None,
                        help='API Key（DeepSeek/豆包）')
    parser.add_argument('--model', '-m', type=str, default=None,
                        help='模型名称（豆包 API 专用，如 doubao-pro-32k）')
    parser.add_argument('--questions', '-q', type=int, default=None,
                        help='只收集前 N 题')
    parser.add_argument('--version', '-v', type=str, default='',
                        help='模型版本备注')
    args = parser.parse_args()

    questions = load_questions(args.questions)
    system = args.system

    # API Key 查找顺序: 参数 > 环境变量
    api_key = args.api_key
    if not api_key and system.lower() == 'deepseek':
        api_key = os.environ.get('DEEPSEEK_API_KEY', '')
    if not api_key and system == '豆包':
        api_key = os.environ.get('ARK_API_KEY', os.environ.get('DOUBAO_API_KEY', ''))

    if system.lower() == 'deepseek' and api_key:
        responses = collect_deepseek_api(questions, api_key)
        version = args.version or 'deepseek-chat'
    elif system == '豆包' and api_key:
        model = args.model or 'doubao-pro-32k'
        responses = collect_doubao_api(questions, api_key, model)
        version = args.version or model
    else:
        if system.lower() == 'deepseek' and not api_key:
            print('  💡 未提供 API Key，切换到手动录入模式', flush=True)
            print('     如需 API 模式: --api-key sk-xxx 或设置 DEEPSEEK_API_KEY 环境变量', flush=True)
        responses = collect_manual(system, questions)
        version = args.version or '网页版'

    save_responses(system, responses, version)

    filled = sum(1 for r in responses if r.get('response_text', '').strip())
    print(f'\n  📊 收集完成: {filled}/{len(questions)} 题有回答', flush=True)
    print(f'  💡 接下来运行 python -m benchmark.run_benchmark 生成对比报告', flush=True)


if __name__ == '__main__':
    main()
