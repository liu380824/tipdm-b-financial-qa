"""
单题批量测试脚本
用法:
  python test_single_batch.py B3001              # 按编号测试单题
  python test_single_batch.py B3001 B3003 B3010  # 测试多题
  python test_single_batch.py --all              # 测试全部（慎用）
  python test_single_batch.py --list             # 列出所有题号
  python test_single_batch.py B3001 --write       # 测试并写入结果文件

输出:
  - 控制台打印: 编号、问题、SQL、图表格式、回答内容
  - 图片保存到: result/ 目录
  - 写入结果:   result_2.xlsx (B1xxx) / result_3.xlsx (B2xxx)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 确保项目根目录在 sys.path
sys.path.insert(0, str(BASE_DIR))

from backend.services.assistant import build_assistant_reply
from openpyxl import load_workbook as _load_wb, Workbook as _Workbook

from backend.tasks.task2_runtime import (
    load_task2_questions,
    save_chart_image,
    _normalize_batch_text,
)
from backend.tasks.task3_runtime import load_task3_questions

# 问题文件路径：可用环境变量覆盖，默认为当前目录下的文件名
TASK2_FILE = Path(os.environ.get('TASK2_FILE', '附件4：问题汇总.xlsx'))
TASK3_FILE = Path(os.environ.get('TASK3_FILE', '附件6：问题汇总.xlsx'))


def run_single_question(question_rec: dict, result_dir: Path) -> dict:
    """模拟批量导出流程处理单题，返回结果字典。"""
    question_id = str(question_rec['id'])
    turns = list(question_rec['questions'])
    history: list[dict] = []
    answers = []
    sql_fragments = []
    chart_formats = []
    image_index = 1

    for i, turn in enumerate(turns, 1):
        prompt = str(turn.get('Q', '')).strip()
        print(f'  [Turn {i}] Q: {prompt}')

        user_message = {
            'id': f'{question_id}-user-{len(history) + 1}',
            'role': 'user',
            'content': prompt,
        }
        history_with_user = history + [user_message]

        t0 = time.time()
        _is_task2 = question_id.startswith('B3')
        reply = build_assistant_reply(
            prompt, history_with_user,
            render_mode='batch_export',
            deep_think=True,
            fast_mode=True,
            skip_kb=_is_task2,
        )
        elapsed = time.time() - t0

        # 图表
        images = []
        if reply.get('chart'):
            image_path = result_dir / f'{question_id}_{image_index}.jpg'
            images.append(save_chart_image(reply['chart'], image_path))
            image_index += 1
            chart_type = str(reply['chart'].get('typeLabel', '图表'))
            chart_formats.append(chart_type)
            print(f'         图表: {chart_type} -> {image_path.name}')
        # Task3 multi-intent: additional charts
        for extra_chart in (reply.get('charts') or [])[1:]:
            img_path = result_dir / f'{question_id}_{image_index}.jpg'
            images.append(save_chart_image(extra_chart, img_path))
            image_index += 1
        for task_result in (reply.get('taskResults') or []):
            if task_result.get('chart'):
                img_path = result_dir / f'{question_id}_{image_index}.jpg'
                images.append(save_chart_image(task_result['chart'], img_path))
                image_index += 1
        if not images:
            print(f'         图表: 无')

        # SQL
        if reply.get('sql'):
            sql_fragments.append(str(reply['sql']))

        # 回答 — Task3 (B2xxx) 需要包含 references
        raw_content = str(reply.get('content', ''))
        content = _normalize_batch_text(raw_content)
        answer_payload: dict = {'content': content, 'image': images}
        if question_id.startswith('B4'):
            from backend.tasks.task3_runtime import _format_references
            answer_payload['references'] = _format_references(
                reply.get('references') or [], prompt)
        answers.append({'Q': prompt, 'A': answer_payload})
        _refs = reply.get('references') or []
        print(f'         Refs: {len(_refs)} items')
        print(f'         A ({elapsed:.1f}s): {content[:200]}')
        if len(content) > 200:
            print(f'         ... (共 {len(content)} 字)')

        history = history_with_user + [reply]

    sql_text = '\n'.join(sql_fragments) if sql_fragments else '无'
    chart_format = '、'.join(chart_formats) if chart_formats else '无'

    result = {
        '编号': question_id,
        '问题类型': question_rec.get('questionType', ''),
        'SQL': sql_text,
        '图表格式': chart_format,
        '回答': answers,
        '_turns': turns,
    }
    return result


def _write_results_to_xlsx(results: list[dict], base_dir: Path) -> None:
    """将测试结果写入 result_2.xlsx / result_3.xlsx（按编号自动判断）。
    如果文件已存在则覆盖对应行，否则新建。"""
    t2_rows = [r for r in results if r['编号'].startswith('B3')]
    t3_rows = [r for r in results if r['编号'].startswith('B4')]

    if t2_rows:
        _patch_workbook(
            base_dir / 'result_2.xlsx',
            ['编号', '问题', 'SQL 查询语句', '图形格式', '回答'],
            '任务二结果',
            {r['编号']: [
                r['编号'],
                json.dumps(r['_turns'], ensure_ascii=False),
                r['SQL'],
                r['图表格式'],
                json.dumps(r['回答'], ensure_ascii=False),
            ] for r in t2_rows},
            {'A': 12, 'B': 48, 'C': 72, 'D': 14, 'E': 100},
        )
    if t3_rows:
        _patch_workbook(
            base_dir / 'result_3.xlsx',
            ['编号', '问题', 'SQL查询语法', '回答'],
            '任务三结果',
            {r['编号']: [
                r['编号'],
                json.dumps(r['_turns'], ensure_ascii=False),
                r['SQL'],
                json.dumps(r['回答'], ensure_ascii=False, indent=4),
            ] for r in t3_rows},
            {'A': 12, 'B': 48, 'C': 72, 'D': 120},
        )


def _patch_workbook(
    path: Path,
    header: list[str],
    sheet_title: str,
    patch_rows: dict[str, list],
    col_widths: dict[str, int],
) -> None:
    """打开已有 xlsx（若不存在则新建），按编号覆盖或追加行。"""
    if path.exists():
        wb = _load_wb(str(path))
        ws = wb.active
        # 找已有编号 → 行号映射
        existing: dict[str, int] = {}
        for row_idx in range(2, ws.max_row + 1):
            cell_val = str(ws.cell(row=row_idx, column=1).value or '').strip()
            if cell_val:
                existing[cell_val] = row_idx
        for qid, row_data in patch_rows.items():
            if qid in existing:
                target_row = existing[qid]
                for col_idx, val in enumerate(row_data, 1):
                    ws.cell(row=target_row, column=col_idx, value=val)
                print(f'  📝 已覆盖写入 {path.name} (编号 {qid}, 行 {target_row})')
            else:
                ws.append(row_data)
                print(f'  📝 已追加写入 {path.name} (编号 {qid})')
    else:
        wb = _Workbook()
        ws = wb.active
        ws.title = sheet_title
        ws.append(header)
        for qid, row_data in patch_rows.items():
            ws.append(row_data)
            print(f'  📝 已新建写入 {path.name} (编号 {qid})')
        for col_letter, width in col_widths.items():
            ws.column_dimensions[col_letter].width = width
    try:
        wb.save(str(path))
    except PermissionError:
        print(f'  ⚠ 无法写入 {path.name}（文件可能被 Excel 打开），请关闭后重试')
        return


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    write_mode = '--write' in args
    args = [a for a in args if a != '--write']

    questions = []
    if TASK2_FILE.exists():
        questions += load_task2_questions(BASE_DIR, question_file=TASK2_FILE)
    if TASK3_FILE.exists():
        questions += load_task3_questions(BASE_DIR, question_file=TASK3_FILE)
    if not questions:
        print('未找到题目文件，请检查路径')
        sys.exit(1)
    q_map = {str(q['id']): q for q in questions}

    if '--list' in args:
        print(f'共 {len(questions)} 题:')
        for q in questions:
            turns_preview = ' -> '.join(t.get('Q', '')[:30] for t in q['questions'])
            print(f"  {q['id']:8s} [{q['questionType']:6s}] {turns_preview}")
        return

    if '--all' in args:
        target_ids = [str(q['id']) for q in questions]
    else:
        target_ids = args

    result_dir = BASE_DIR / 'result'
    result_dir.mkdir(exist_ok=True)

    total = len(target_ids)
    all_results: list[dict] = []
    for idx, qid in enumerate(target_ids, 1):
        if qid not in q_map:
            print(f'[{idx}/{total}] ⚠ 未找到题号 {qid}，跳过')
            continue

        q = q_map[qid]
        turns_text = ' -> '.join(t.get('Q', '')[:40] for t in q['questions'])
        print(f'\n{"="*70}')
        print(f'[{idx}/{total}] {qid} ({q.get("questionType", "")}) {turns_text}')
        print(f'{"="*70}')

        t_start = time.time()
        result = run_single_question(q, result_dir)
        t_total = time.time() - t_start
        all_results.append(result)

        print(f'\n  --- 结果汇总 ({t_total:.1f}s) ---')
        print(f'  SQL: {result["SQL"][:150]}{"..." if len(result["SQL"]) > 150 else ""}')
        print(f'  图表: {result["图表格式"]}')
        for a in result['回答']:
            print(f'  Q: {a["Q"][:60]}')
            print(f'  A: {a["A"]["content"][:200]}')

    if all_results:
        print(f'\n--- 写入结果文件 ---')
        _write_results_to_xlsx(all_results, BASE_DIR)

    print(f'\n✅ 测试完成，共 {total} 题')


if __name__ == '__main__':
    main()
