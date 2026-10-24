from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from config import CONFIG, get_system_settings, update_system_settings

from .core import context as _ctx
from .core.context import BASE_DIR, CORS_ORIGINS, FIELD_LABELS, RESULT_DIR, RESULT_WORKBOOK_PATH, TABLE_LABELS, TABLE_ORDER, reload_paths
from .core.db import build_columns_from_row, column_label, now_text, open_connection, rewrite_numeric_sql, row_to_dict, validate_read_only_sql
from .core.models import AssistantQueryRequest, SqlQueryRequest
from .services.assistant import build_assistant_reply
from .services.session_memory import get_session_memory

_stream_executor = ThreadPoolExecutor(max_workers=4)
from .services.database import build_database_overview, build_quality_rules, build_table_schemas
from .services.knowledge import (
    add_uploaded_pdf, delete_document, ingest_attachment5,
    list_documents, search_kb,
)
from .services.llm import llm_available

import sys as _sys
_sys.path.insert(0, str(BASE_DIR))
from quality_report import build_report as _build_quality_report, save_visual_report as _save_visual_report
from .tasks.task2_runtime import export_task2_results, load_task2_questions
from .tasks.task3_runtime import export_task3_results, find_task3_question_file, load_task3_questions

app = FastAPI(title='Smart Query API', version='1.0.0')


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
    expose_headers=['X-Actual-Page']
)
app.mount('/result', StaticFiles(directory=RESULT_DIR), name='result')

_DIST_DIR = BASE_DIR / 'smart-query-ui' / 'dist'
if _DIST_DIR.exists():
    app.mount('/assets', StaticFiles(directory=_DIST_DIR / 'assets'), name='assets')


@app.get('/api/health')
def health() -> dict[str, Any]:
    return {'status': 'ok', 'database': str(_ctx.DB_PATH), 'exists': _ctx.DB_PATH.exists(), 'checkedAt': now_text()}


@app.get('/api/system/settings')
def system_settings() -> dict[str, Any]:
    payload = get_system_settings()
    llm = payload.get('llm', {}) if isinstance(payload.get('llm'), dict) else {}
    llm['available'] = llm_available()
    payload['llm'] = llm
    return payload


@app.put('/api/system/settings')
def save_system_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        result = update_system_settings(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reload_paths()
    llm = result.get('llm', {}) if isinstance(result.get('llm'), dict) else {}
    llm['available'] = llm_available()
    result['llm'] = llm
    return result


@app.get('/api/task2/questions')
def task2_questions() -> dict[str, Any]:
    records = load_task2_questions(BASE_DIR)
    return {
        'count': len(records),
        'records': records
    }


@app.post('/api/task2/export')
def task2_export() -> dict[str, Any]:
    return export_task2_results(BASE_DIR, build_assistant_reply)


@app.post('/api/task2/export-with-file')
async def task2_export_with_file(file: UploadFile = File(...)) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        return export_task2_results(BASE_DIR, build_assistant_reply, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get('/api/task2/result-file')
def task2_result_file() -> FileResponse:
    if not RESULT_WORKBOOK_PATH.exists():
        raise HTTPException(status_code=404, detail='result_2.xlsx 尚未生成，请先执行导出')
    return FileResponse(
        RESULT_WORKBOOK_PATH,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        filename='result_2.xlsx'
    )


_ALLOWED_TABLES: set[str] = set(TABLE_ORDER)


@app.get('/api/database/table/{table_name}')
def get_table_data(
    table_name: str,
    page: int = 1,
    limit: int = 50,
    search: str | None = None,
    stock_code: str | None = None,
    report_period: str | None = None,
) -> dict[str, Any]:
    if table_name not in _ALLOWED_TABLES:
        raise HTTPException(status_code=400, detail=f'未知表名：{table_name}')
    where_clauses: list[str] = []
    params: list[Any] = []
    if search:
        where_clauses.append("(COALESCE(stock_code,'') LIKE ? OR COALESCE(stock_abbr,'') LIKE ?)")
        params += [f'%{search}%', f'%{search}%']
    if stock_code:
        where_clauses.append("stock_code = ?")
        params.append(stock_code)
    if report_period:
        where_clauses.append("report_period = ?")
        params.append(report_period)
    where_sql = (' WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''
    offset = (page - 1) * limit
    with open_connection() as conn:
        total = conn.execute(f'SELECT COUNT(*) FROM {table_name}{where_sql}', params).fetchone()[0]  # type: ignore[index]
        cursor = conn.execute(
            f'SELECT * FROM {table_name}{where_sql} ORDER BY rowid ASC LIMIT ? OFFSET ?',
            params + [limit, offset]
        )
        col_names = [d[0] for d in (cursor.description or [])]
        rows = [row_to_dict(row) for row in cursor.fetchall()]
    columns = [{'key': c, 'label': FIELD_LABELS.get(c, c)} for c in col_names]
    return {'total': total, 'page': page, 'limit': limit, 'columns': columns, 'rows': rows}


@app.post('/api/database/table/{table_name}')
async def insert_table_row(table_name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if table_name not in _ALLOWED_TABLES:
        raise HTTPException(status_code=400, detail=f'未知表名：{table_name}')
    stock_code = str(payload.get('stock_code') or '').strip()
    report_period = str(payload.get('report_period') or '').strip()
    if not stock_code or not report_period:
        raise HTTPException(status_code=422, detail='stock_code 和 report_period 为必填字段')
    with open_connection() as conn:
        cur = conn.execute(f'PRAGMA table_info({table_name})')
        valid_cols = {row['name'] for row in cur.fetchall()}
    data: dict[str, Any] = {k: (v if v != '' else None) for k, v in payload.items() if k in valid_cols}
    data.setdefault('stock_code', stock_code)
    data.setdefault('report_period', report_period)
    if 'report_year' not in data and len(report_period) >= 4 and report_period[:4].isdigit():
        data['report_year'] = int(report_period[:4])
    fields = list(data.keys())
    values = [data[f] for f in fields]
    sql = f'INSERT OR REPLACE INTO {table_name} ({",".join(fields)}) VALUES ({",".join(["?"]*len(fields))})'
    with open_connection() as conn:
        conn.execute(sql, values)
    return {'ok': True, 'message': f'已保存 {stock_code} {report_period} 的记录'}


@app.delete('/api/database/table/{table_name}')
def delete_table_row(table_name: str, stock_code: str, report_period: str) -> dict[str, Any]:
    if table_name not in _ALLOWED_TABLES:
        raise HTTPException(status_code=400, detail=f'未知表名：{table_name}')
    with open_connection() as conn:
        cursor = conn.execute(
            f'DELETE FROM {table_name} WHERE TRIM(stock_code) = ? AND report_period = ?',
            (stock_code.strip(), report_period)
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail='未找到该记录')
    return {'ok': True, 'message': f'已删除 {cursor.rowcount} 条记录'}


@app.get('/api/database/overview')
def database_overview() -> dict[str, Any]:
    return build_database_overview()


@app.get('/api/database/schemas')
def database_schemas() -> list[dict[str, Any]]:
    return build_table_schemas()


@app.get('/api/database/quality-rules')
def database_quality_rules() -> list[dict[str, str]]:
    return build_quality_rules()


@app.post('/api/database/query')
def database_query(payload: SqlQueryRequest) -> dict[str, Any]:
    sql = validate_read_only_sql(payload.sql)
    sql = rewrite_numeric_sql(sql)
    with open_connection() as connection:
        cursor = connection.execute(sql)
        fetched = cursor.fetchmany(payload.limit + 1)
        rows = [row_to_dict(row) for row in fetched[:payload.limit]]
        columns = [{'key': item[0], 'label': column_label(item[0])} for item in (cursor.description or [])]
    return {
        'notice': f'SQL 执行成功，返回 {len(rows)} 条记录。' + (' 结果已截断。' if len(fetched) > payload.limit else ''),
        'columns': columns or build_columns_from_row(rows),
        'rows': rows,
        'truncated': len(fetched) > payload.limit
    }


@app.post('/api/assistant/query')
def assistant_query(payload: AssistantQueryRequest) -> dict[str, Any]:
    history = payload.history if payload.enable_context else []
    session_id = payload.session_id or ''
    reply = build_assistant_reply(payload.question, history, deep_think=payload.deep_think, session_id=session_id)
    if not payload.auto_chart:
        reply['chart'] = None
    if session_id:
        get_session_memory(session_id).update_from_reply(reply)
    return reply


@app.post('/api/assistant/stream')
async def assistant_stream(payload: AssistantQueryRequest) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _emit(event_type: str, data: Any) -> None:
        asyncio.run_coroutine_threadsafe(queue.put({'type': event_type, 'data': data}), loop)

    def _run() -> None:
        try:
            history = payload.history if payload.enable_context else []
            session_id = payload.session_id or ''
            reply = build_assistant_reply(payload.question, history, emit=_emit, deep_think=payload.deep_think, session_id=session_id)
            if not payload.auto_chart:
                reply['chart'] = None
            if session_id:
                get_session_memory(session_id).update_from_reply(reply)
            asyncio.run_coroutine_threadsafe(queue.put({'type': 'done', 'data': reply}), loop)
        except Exception as exc:
            asyncio.run_coroutine_threadsafe(queue.put({'type': 'error', 'data': str(exc)}), loop)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    loop.run_in_executor(_stream_executor, _run)

    async def _generate():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _generate(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


# ── Task 3 API ─────────────────────────────────────────────────────────────

@app.post('/api/task3/export')
def task3_export(file: UploadFile | None = File(default=None)) -> dict[str, Any]:
    tmp_path: Path | None = None
    try:
        if file and file.filename:
            suffix = Path(str(file.filename)).suffix or '.xlsx'
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                shutil.copyfileobj(file.file, tmp)
                tmp_path = Path(tmp.name)
        result = export_task3_results(
            BASE_DIR,
            build_assistant_reply,
            question_file=tmp_path,
        )
        return {**result, 'records': []}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@app.get('/api/task3/result-file')
def task3_result_file() -> FileResponse:
    path = BASE_DIR / 'result_3.xlsx'
    if not path.exists():
        raise HTTPException(status_code=404, detail='result_3.xlsx 尚未生成')
    return FileResponse(str(path), filename='result_3.xlsx',
                        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.get('/api/task3/questions')
def task3_questions() -> dict[str, Any]:
    try:
        qf = find_task3_question_file(BASE_DIR)
        qs = load_task3_questions(BASE_DIR, qf)
        return {'count': len(qs), 'file': str(qf)}
    except FileNotFoundError:
        return {'count': 0, 'file': None}




def _remap_pdf_path(file: str) -> Path:
    """Try to locate a PDF from persisted old paths or current workspace paths."""
    p = Path(file)
    if p.exists():
        return p
    parts = p.parts
    _reports_dir = Path(CONFIG.get('reports_dir', ''))
    if _reports_dir.exists():
        for i, part in enumerate(parts):
            if '附件2' in part:
                remainder = Path(*parts[i + 1:]) if i + 1 < len(parts) else Path()
                remapped = _reports_dir / remainder
                if remapped.exists():
                    return remapped
                break
    _research_dir = Path(CONFIG.get('research_reports_dir', ''))
    _research_candidates = [_research_dir]
    # Also try default B题 path as fallback
    _default_research = BASE_DIR / 'B题-示例数据' / '示例数据' / '附件5：研报数据'
    if _default_research != _research_dir:
        _research_candidates.append(_default_research)
    for candidate in _research_candidates:
        if candidate.exists():
            for i, part in enumerate(parts):
                if '附件5' in part:
                    remainder = Path(*parts[i + 1:]) if i + 1 < len(parts) else Path()
                    remapped = candidate / remainder
                    if remapped.exists():
                        return remapped
                    break
    # Fallback: relative to BASE_DIR
    if not p.is_absolute():
        p = BASE_DIR / file
    return p


@app.get('/api/pdf/page-image')
def pdf_page_image(
    file: str,
    page: int = 1,
    highlight: str | None = None,
    dpi: int = 200,
):
    """渲染 PDF 指定页为 PNG 图片，支持关键词高亮"""
    import fitz
    from io import BytesIO
    from fastapi.responses import Response

    pdf_path = _remap_pdf_path(file)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f'PDF 文件不存在: {file}')

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'无法打开 PDF: {exc}')

    page_idx = page - 1
    if page_idx < 0 or page_idx >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail=f'页码超出范围 (1-{len(doc)})')

    pg = doc[page_idx]

    # 高亮关键词（支持数字千分位 + 万元/元单位换算自动匹配）
    def _num_variants(n: float) -> set[str]:
        """为一个数值生成各种格式变体：带/不带千分位、不同小数位数。"""
        vs: set[str] = set()
        # 整数
        if n == int(n):
            vs.add(str(int(n)))
            vs.add(f'{int(n):,}')
        else:
            # 保留原始精度
            plain = f'{n:f}'.rstrip('0').rstrip('.')
            vs.add(plain)
            # 带千分位
            int_part, dec_part = plain.split('.', 1) if '.' in plain else (plain, '')
            if dec_part:
                vs.add(f'{int(int_part):,}.{dec_part}')
            else:
                vs.add(f'{int(int_part):,}')
            # 常见精度：2位小数
            vs.add(f'{n:,.2f}')
            vs.add(f'{n:.2f}')
        return vs

    def _build_variants(keyword: str) -> list[str]:
        """为关键词生成所有数值格式变体，按长度降序排列。"""
        variants = {keyword}
        clean = keyword.replace(',', '')
        try:
            num = float(clean)
            variants |= _num_variants(num)
            yuan = num * 10000
            variants |= _num_variants(yuan)
            if abs(num) >= 10000:
                wan = num / 10000
                variants |= _num_variants(wan)
        except (ValueError, TypeError):
            pass
        return sorted(variants, key=len, reverse=True)

    def _highlight_on_page(target_pg, kw_variants_list: list[list[str]]) -> bool:
        """在指定页面搜索并高亮所有关键词，返回是否至少找到一个。"""
        found_any = False
        for variants in kw_variants_list:
            for var in variants:
                areas = target_pg.search_for(var)
                if areas:
                    for rect in areas:
                        try:
                            annot = target_pg.add_highlight_annot(rect)
                            annot.set_colors(stroke=(0.2, 0.5, 1.0))
                            annot.update()
                        except (ValueError, TypeError):
                            # fitz 偶尔返回无效 rect（bad quads），跳过该区域
                            continue
                    found_any = True
                    break
        return found_any

    actual_page = page  # 实际返回的页码（可能因自动搜索而改变）

    if highlight:
        kw_variants_list = []
        for keyword in highlight.split('|'):
            keyword = keyword.strip()
            if not keyword:
                continue
            kw_variants_list.append(_build_variants(keyword))

        found = _highlight_on_page(pg, kw_variants_list)

        # 若当前页找不到高亮值，自动搜索其他页面
        if not found and kw_variants_list:
            for alt_idx in range(len(doc)):
                if alt_idx == page_idx:
                    continue
                alt_pg = doc[alt_idx]
                # 仅检测是否存在，不做高亮
                hit = False
                for variants in kw_variants_list:
                    for var in variants:
                        if alt_pg.search_for(var):
                            hit = True
                            break
                    if hit:
                        break
                if hit:
                    # 切换到找到值的页面，重新高亮
                    pg = doc[alt_idx]
                    actual_page = alt_idx + 1
                    _highlight_on_page(pg, kw_variants_list)
                    break

    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = pg.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes('png')
    doc.close()

    return Response(
        content=img_bytes,
        media_type='image/png',
        headers={'X-Actual-Page': str(actual_page)},
    )


@app.get('/api/pdf/info')
def pdf_info(file: str) -> dict[str, Any]:
    """获取 PDF 基本信息（页数等）"""
    import fitz
    pdf_path = _remap_pdf_path(file)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f'PDF 不存在: {file}')
    try:
        doc = fitz.open(str(pdf_path))
        page_count = len(doc)
        doc.close()
        return {'file': str(pdf_path), 'page_count': page_count}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────── Knowledge Base API ──────────────────────────────

@app.get('/api/knowledge/documents')
def kb_list_documents() -> list[dict[str, Any]]:
    return list_documents()


@app.post('/api/knowledge/ingest')
def kb_ingest() -> dict[str, Any]:
    """Index all PDFs found in 附件5 (background threads per file)."""
    results = ingest_attachment5(background=True)
    return {'triggered': len(results), 'documents': results}


@app.post('/api/knowledge/reindex')
def kb_reindex() -> dict[str, Any]:
    """Force re-index ALL documents (upgrade to hybrid vector+BM25 system)."""
    import threading
    from pathlib import Path as _Path
    from .services.knowledge import index_document
    from .core.kb_store import kb_conn as _kb_conn
    with _kb_conn() as conn:
        rows = conn.execute('SELECT id, doc_path FROM kb_documents').fetchall()
        conn.execute("UPDATE kb_documents SET status='pending'")
    triggered = []
    for r in rows:
        doc_id = r['id']
        doc_path = r['doc_path']
        triggered.append({'id': doc_id, 'doc_path': doc_path})
        threading.Thread(
            target=index_document,
            args=(doc_id, _Path(doc_path)),
            daemon=True,
        ).start()
    return {'triggered': len(triggered), 'documents': triggered}


@app.post('/api/knowledge/upload')
async def kb_upload(
    file: UploadFile = File(...),
    doc_type: str = '用户上传',
    stock_code: str | None = None,
    stock_name: str | None = None,
) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail='只支持 PDF 文件')
    data = await file.read()
    return add_uploaded_pdf(file.filename, data, doc_type, stock_code, stock_name)


@app.delete('/api/knowledge/documents/{doc_id}')
def kb_delete_document(doc_id: int) -> dict[str, Any]:
    try:
        delete_document(doc_id)
        return {'ok': True}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get('/api/knowledge/documents/{doc_id}/chunks')
def kb_document_chunks(doc_id: int) -> list[dict[str, Any]]:
    from .core.kb_store import kb_conn
    with kb_conn() as conn:
        rows = conn.execute(
            'SELECT id, chunk_idx, text, parent_text, page_no, chunk_type, section_title '
            'FROM kb_chunks WHERE doc_id=? ORDER BY chunk_idx',
            (doc_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get('/api/knowledge/documents/{doc_id}/file')
def kb_document_file(doc_id: int):
    from .core.kb_store import kb_conn
    from .core.context import KB_DOCS_DIR
    with kb_conn() as conn:
        row = conn.execute('SELECT doc_path, doc_name FROM kb_documents WHERE id=?', (doc_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='文档不存在')
    p = _remap_pdf_path(row['doc_path'])
    if not p.exists():
        # Also try kb_uploads folder by filename
        fallback = KB_DOCS_DIR / Path(row['doc_path']).name
        if fallback.exists():
            p = fallback
    if not p.exists():
        raise HTTPException(status_code=404, detail=f'文件不存在: {p}')
    suffix = p.suffix or '.pdf'
    return FileResponse(str(p), filename=row['doc_name'] + suffix,
                        media_type='application/pdf')


@app.get('/api/knowledge/search')
def kb_search(q: str, top_k: int = 5) -> list[dict[str, Any]]:
    if not q.strip():
        raise HTTPException(status_code=400, detail='查询不能为空')
    _INTERNAL = {'doc_id', 'effective_score'}
    return [{k: v for k, v in r.items() if k not in _INTERNAL}
            for r in search_kb(q, top_k=min(top_k, 20))]


# ─────────────────────────── Import Log Endpoints ────────────────────────────────

@app.get('/api/database/import-logs')
def import_logs(page: int = 1, limit: int = 20) -> dict[str, Any]:
    """Return paginated import job history."""
    import sqlite3
    conn = sqlite3.connect(str(_ctx.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute('SELECT COUNT(*) FROM import_jobs').fetchone()[0] or 0
        offset = (page - 1) * limit
        rows = conn.execute(
            'SELECT job_id, started_at, finished_at, total_reports, success_reports, '
            'warning_count, error_count FROM import_jobs '
            'ORDER BY COALESCE(started_at, finished_at) DESC LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
        return {'total': total, 'page': page, 'limit': limit, 'rows': [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get('/api/database/import-issues')
def import_issues(job_id: str | None = None, page: int = 1, limit: int = 50) -> dict[str, Any]:
    """Return paginated import issues, optionally filtered by job_id."""
    import sqlite3
    conn = sqlite3.connect(str(_ctx.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        where = ''
        params: tuple = ()
        if job_id:
            where = 'WHERE job_id = ?'
            params = (job_id,)
        total = conn.execute(f'SELECT COUNT(*) FROM import_issues {where}', params).fetchone()[0] or 0
        offset = (page - 1) * limit
        rows = conn.execute(
            f'SELECT id, job_id, table_name, field_name, stock_code, report_period, '
            f'issue_type, issue_level, message, created_at '
            f'FROM import_issues {where} '
            f'ORDER BY id DESC LIMIT ? OFFSET ?',
            params + (limit, offset),
        ).fetchall()
        return {'total': total, 'page': page, 'limit': limit, 'rows': [dict(r) for r in rows]}
    finally:
        conn.close()


# ──────────────────────── Database / KB Build Endpoints ───────────────────────────

import threading

_BUILD_TASKS: dict[str, dict[str, Any]] = {}


@app.post('/api/database/init')
def database_init() -> dict[str, Any]:
    """Create (or recreate) the database tables via create_database.py."""
    import importlib
    try:
        mod = importlib.import_module('create_database')
        db_path = CONFIG['database']['path']
        mod.create_financial_database(db_path)
        return {'ok': True, 'message': f'数据库已初始化: {db_path}'}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'初始化失败: {exc}') from exc


@app.post('/api/database/batch-import')
def database_batch_import() -> dict[str, Any]:
    """Trigger batch PDF extraction + import in background thread."""
    task_id = 'batch_import'
    if task_id in _BUILD_TASKS and _BUILD_TASKS[task_id].get('status') == 'running':
        return {'ok': False, 'message': '批量导入任务正在运行中，请勿重复触发', 'task_id': task_id}

    _BUILD_TASKS[task_id] = {'status': 'running', 'message': '正在导入…', 'started': _now_iso()}

    def _run():
        try:
            from data_importer import batch_import_reports
            reports_dir = CONFIG.get('reports_dir', '')
            db_path = CONFIG['database']['path']
            batch_import_reports(reports_dir, db_path)
            _BUILD_TASKS[task_id] = {'status': 'done', 'message': '批量导入完成', 'finished': _now_iso()}
        except Exception as exc:
            _BUILD_TASKS[task_id] = {'status': 'error', 'message': str(exc), 'finished': _now_iso()}

    threading.Thread(target=_run, daemon=True).start()
    return {'ok': True, 'message': '批量导入已在后台启动', 'task_id': task_id}


@app.get('/api/build-status/{task_id}')
def build_status(task_id: str) -> dict[str, Any]:
    """Poll the status of a background build task."""
    info = _BUILD_TASKS.get(task_id)
    if not info:
        return {'status': 'unknown', 'message': '未找到该任务'}
    return info


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


# ─────────────────────────────── Quality Report API ──────────────────────────────

_QUALITY_VIS_DIR = BASE_DIR / 'quality_visual_reports'


@app.get('/api/database/quality-report')
def quality_report_data() -> dict[str, Any]:
    """Run quality analysis on the current database and return structured JSON."""
    try:
        report = _build_quality_report(str(_ctx.DB_PATH), top_n=10)
        return report
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'质量报告生成失败: {exc}') from exc


@app.post('/api/database/quality-report/generate')
def quality_report_generate() -> dict[str, Any]:
    """Generate a new visual quality report and save to disk."""
    try:
        report = _build_quality_report(str(_ctx.DB_PATH), top_n=10)
        vis = _save_visual_report(_QUALITY_VIS_DIR, report)
        return {'ok': True, 'report_dir': vis['report_dir'], 'html': vis['html']}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'质量报告生成失败: {exc}') from exc


@app.get('/api/database/quality-reports')
def quality_report_list() -> list[dict[str, Any]]:
    """List all previously generated visual quality reports."""
    if not _QUALITY_VIS_DIR.exists():
        return []
    reports = []
    for d in sorted(_QUALITY_VIS_DIR.iterdir(), reverse=True):
        if d.is_dir() and d.name.startswith('quality_visual_report_'):
            ts_str = d.name.replace('quality_visual_report_', '')
            charts = [f.name for f in (d / 'charts').iterdir()] if (d / 'charts').exists() else []
            reports.append({
                'name': d.name,
                'timestamp': ts_str,
                'has_html': (d / 'index.html').exists(),
                'charts': charts,
            })
    return reports


@app.get('/api/database/quality-report/{report_name}/chart/{chart_name}')
def quality_report_chart(report_name: str, chart_name: str):
    """Serve an SVG chart from a specific quality report."""
    chart_path = _QUALITY_VIS_DIR / report_name / 'charts' / chart_name
    if not chart_path.exists() or not chart_path.suffix == '.svg':
        raise HTTPException(status_code=404, detail='图表不存在')
    from fastapi.responses import Response
    return Response(
        content=chart_path.read_text(encoding='utf-8'),
        media_type='image/svg+xml',
    )


@app.get('/', response_class=HTMLResponse)
@app.get('/{full_path:path}', response_class=HTMLResponse)
def spa_fallback(full_path: str = '') -> HTMLResponse:
    index = _DIST_DIR / 'index.html'
    if index.exists():
        return HTMLResponse(index.read_text(encoding='utf-8'))
    raise HTTPException(status_code=404, detail='前端未构建')
