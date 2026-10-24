"""Standalone script to fully rebuild the knowledge base with latest code."""
import sys
import time
from pathlib import Path

# 保证可从任意工作目录运行：把项目根目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backend.services.knowledge as kb_module
from backend.services.knowledge import ingest_attachment5
from backend.core.kb_store import kb_conn

# Reset Excel metadata cache so fresh data is loaded
kb_module._excel_meta_cache = None

# Mark all existing documents as pending to force full re-indexing
with kb_conn() as conn:
    conn.execute("UPDATE kb_documents SET status='pending'")
print('[rebuild] All documents marked as pending')

# Run full ingestion + indexing (synchronous)
print('[rebuild] Starting full knowledge base rebuild...')
print('[rebuild] This will: read Excel metadata, extract PDFs, embed chunks, store vectors\n')

started_at = time.perf_counter()
results = ingest_attachment5(background=False)
total_seconds = time.perf_counter() - started_at

doc_ids = [r['id'] for r in results]
build_info: dict[int, dict[str, object]] = {}
if doc_ids:
    placeholders = ','.join('?' * len(doc_ids))
    with kb_conn() as conn:
        rows = conn.execute(
            f'SELECT id, status, total_chunks, build_seconds FROM kb_documents WHERE id IN ({placeholders})',
            doc_ids,
        ).fetchall()
    build_info = {int(r['id']): dict(r) for r in rows}

print()
print('=' * 60)
print('[rebuild] DONE')
print(f'[rebuild] Total build time: {total_seconds:.2f}s')
for r in results:
    info = build_info.get(r['id'], {})
    status = info.get('status', r['status'])
    build_seconds = info.get('build_seconds')
    total_chunks = info.get('total_chunks')
    build_text = f'{float(build_seconds):.2f}s' if build_seconds is not None else 'N/A'
    print(f"  doc {r['id']}: {r['doc_name'][:40]}  status={status}  chunks={total_chunks}  build={build_text}")
