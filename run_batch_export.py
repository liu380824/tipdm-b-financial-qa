"""Direct batch export runner for 附件4 (Task2) and 附件6 (Task3)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent

# ── 问题文件路径：优先命令行参数，其次环境变量，最后相对路径默认值 ──────────────
parser = argparse.ArgumentParser(description='批量导出任务二（附件4）与任务三（附件6）结果')
parser.add_argument('--task2-file', default=os.environ.get('TASK2_FILE', '附件4：问题汇总.xlsx'),
                    help='附件4：问题汇总.xlsx 路径')
parser.add_argument('--task3-file', default=os.environ.get('TASK3_FILE', '附件6：问题汇总.xlsx'),
                    help='附件6：问题汇总.xlsx 路径')
_args = parser.parse_args()

TASK2_FILE = Path(_args.task2_file)
TASK3_FILE = Path(_args.task3_file)

# ── sanity checks ─────────────────────────────────────────────────────────────
for label, p in [('附件4 (Task2)', TASK2_FILE), ('附件6 (Task3)', TASK3_FILE)]:
    if not p.exists():
        print(f'[ERROR] {label} 文件不存在: {p}')
        sys.exit(1)
    print(f'[OK] {label}: {p}')

# ── imports ───────────────────────────────────────────────────────────────────
print('\n正在加载后端模块...')
from backend.services.assistant import build_assistant_reply
from backend.tasks.task2_runtime import export_task2_results
from backend.tasks.task3_runtime import export_task3_results

# ── Task 2 ────────────────────────────────────────────────────────────────────
# print('\n' + '='*60)
# print('开始处理任务二（附件4：问题汇总.xlsx）...')
# print('='*60)
# try:
#     result2 = export_task2_results(BASE_DIR, build_assistant_reply, question_file=TASK2_FILE)
#     print(f'\n[Task2 完成] 共处理 {result2["questionCount"]} 道题')
#     print(f'  结果文件: {result2["workbookPath"]}')
# except Exception as e:
#     print(f'[Task2 ERROR] {e}', file=sys.stderr)
#     import traceback; traceback.print_exc()

# ── Task 3 ────────────────────────────────────────────────────────────────────
print('\n' + '='*60)
print('开始处理任务三（附件6：问题汇总.xlsx）...')
print('='*60)
try:
    result3 = export_task3_results(BASE_DIR, build_assistant_reply, question_file=TASK3_FILE)
    print(f'\n[Task3 完成] 共处理 {result3["questionCount"]} 道题')
    print(f'  结果文件: {result3["workbookPath"]}')
except Exception as e:
    print(f'[Task3 ERROR] {e}', file=sys.stderr)
    import traceback; traceback.print_exc()

print('\n全部批量导出完成。')
