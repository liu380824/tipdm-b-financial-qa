"""SQLite-backed knowledge base storage (documents + text chunks)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Generator

from .context import KB_DB_PATH, KB_DOCS_DIR

KB_DOCS_DIR.mkdir(parents=True, exist_ok=True)

_DDL = """
CREATE TABLE IF NOT EXISTS kb_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type    TEXT NOT NULL,
    doc_name    TEXT NOT NULL,
    doc_path    TEXT NOT NULL,
    stock_code  TEXT,
    stock_name  TEXT,
    report_date TEXT,
    doc_kind    TEXT,
    quality_score REAL,
    quality_notes TEXT,
    is_abstract INTEGER DEFAULT 0,
    has_ocr    INTEGER DEFAULT 0,
    page_count INTEGER,
    text_density REAL,
    index_started_at TEXT,
    total_chunks INTEGER DEFAULT 0,
    build_seconds REAL,
    indexed_at  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_doc_path ON kb_documents(doc_path);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    chunk_idx   INTEGER NOT NULL,
    text        TEXT NOT NULL,
    parent_text TEXT DEFAULT NULL,
    tokens      TEXT NOT NULL,
    embedding   TEXT DEFAULT NULL,
    page_no     INTEGER DEFAULT NULL,
    page_start  INTEGER DEFAULT NULL,
    page_end    INTEGER DEFAULT NULL,
    figure_titles TEXT DEFAULT NULL,
    chunk_type  TEXT DEFAULT NULL,
    section_title TEXT DEFAULT NULL,
    section_path TEXT DEFAULT NULL,
    retrieval_text TEXT DEFAULT NULL,
    entities_json TEXT DEFAULT NULL,
    metric_tags TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id);
"""

_MIGRATIONS = [
    "ALTER TABLE kb_chunks ADD COLUMN embedding TEXT DEFAULT NULL",
    "ALTER TABLE kb_chunks DROP COLUMN section",  # cleanup: section was never populated
    "ALTER TABLE kb_documents ADD COLUMN report_date TEXT",
    "ALTER TABLE kb_documents ADD COLUMN index_started_at TEXT",
    "ALTER TABLE kb_documents ADD COLUMN current_price TEXT",
    "ALTER TABLE kb_documents ADD COLUMN target_price TEXT",
    "ALTER TABLE kb_documents ADD COLUMN analyst TEXT",
    "ALTER TABLE kb_documents ADD COLUMN research_firm TEXT",
    "ALTER TABLE kb_documents ADD COLUMN forecast_json TEXT",
    "ALTER TABLE kb_documents ADD COLUMN sector TEXT",
    "ALTER TABLE kb_documents ADD COLUMN rating TEXT",
    "ALTER TABLE kb_documents ADD COLUMN excel_metadata TEXT",
    "ALTER TABLE kb_documents ADD COLUMN build_seconds REAL",
    "ALTER TABLE kb_documents ADD COLUMN doc_kind TEXT",
    "ALTER TABLE kb_documents ADD COLUMN quality_score REAL",
    "ALTER TABLE kb_documents ADD COLUMN quality_notes TEXT",
    "ALTER TABLE kb_documents ADD COLUMN is_abstract INTEGER DEFAULT 0",
    "ALTER TABLE kb_documents ADD COLUMN has_ocr INTEGER DEFAULT 0",
    "ALTER TABLE kb_documents ADD COLUMN page_count INTEGER DEFAULT NULL",
    "ALTER TABLE kb_documents ADD COLUMN text_density REAL DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN parent_text TEXT DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN page_no INTEGER DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN figure_titles TEXT DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN page_start INTEGER DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN page_end INTEGER DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN chunk_type TEXT DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN section_title TEXT DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN section_path TEXT DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN retrieval_text TEXT DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN entities_json TEXT DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN metric_tags TEXT DEFAULT NULL",
    "ALTER TABLE kb_chunks ADD COLUMN parent_text_id INTEGER DEFAULT NULL",
    """CREATE TABLE IF NOT EXISTS kb_parent_texts (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        hash TEXT NOT NULL UNIQUE,
        text TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS kb_extract_cache (
        doc_path       TEXT NOT NULL,
        file_size      INTEGER NOT NULL,
        file_mtime     REAL NOT NULL,
        ocr_dpi        INTEGER NOT NULL,
        full_text      TEXT,
        meta_json      TEXT,
        page_texts_json TEXT,
        page_blocks_json TEXT,
        extract_info_json TEXT,
        created_at     TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (doc_path)
    )""",
]


@contextmanager
def kb_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(KB_DB_PATH), check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_kb() -> None:
    with kb_conn() as conn:
        conn.executescript(_DDL)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass


init_kb()
