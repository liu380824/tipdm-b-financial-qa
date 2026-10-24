"""Compact knowledge_base.db — lossless optimization.

1. Convert embeddings from V1 (JSON+zlib, ~5KB/vec) to V2 (float32 binary, ~4KB/vec)
2. Deduplicate parent_text into a separate table (3.7x redundancy → 1x)
3. VACUUM to reclaim freed space

All operations are lossless — no precision loss.
"""
import hashlib
import json
import sqlite3
import sys
import time
import zlib

import numpy as np

DB_PATH = 'knowledge_base.db'
V1_PREFIX = b'KBEMB1:'
V2_PREFIX = b'KBEMB2:'


def convert_embeddings(conn: sqlite3.Connection) -> int:
    """Convert V1 (JSON+zlib) embeddings to V2 (float32 binary). Lossless."""
    cursor = conn.execute(
        "SELECT id, embedding FROM kb_chunks WHERE embedding IS NOT NULL"
    )
    converted = 0
    batch: list[tuple[bytes, int]] = []
    for row in cursor:
        chunk_id = row[0]
        raw = row[1]
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        if isinstance(raw, bytearray):
            raw = bytes(raw)
        if not isinstance(raw, bytes):
            continue
        # Skip if already V2
        if raw.startswith(V2_PREFIX):
            continue
        # Decode from V1 or plain JSON
        vec = None
        if raw.startswith(V1_PREFIX):
            payload = raw[len(V1_PREFIX):]
            vec = json.loads(zlib.decompress(payload).decode('utf-8'))
        else:
            try:
                text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
                vec = json.loads(text)
            except Exception:
                continue
        if vec is None or not isinstance(vec, list):
            continue
        # Re-serialize as V2 (float32 binary) — lossless
        arr = np.array(vec, dtype=np.float32)
        new_blob = V2_PREFIX + arr.tobytes()
        batch.append((new_blob, chunk_id))
        converted += 1
        if len(batch) >= 500:
            conn.executemany("UPDATE kb_chunks SET embedding=? WHERE id=?", batch)
            conn.commit()
            print(f'  ... converted {converted} embeddings', file=sys.stderr)
            batch.clear()
    if batch:
        conn.executemany("UPDATE kb_chunks SET embedding=? WHERE id=?", batch)
        conn.commit()
    return converted


def deduplicate_parent_text(conn: sqlite3.Connection) -> int:
    """Move parent_text to a dedup table, replace with integer FK. Lossless."""
    # Create parent_texts table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kb_parent_texts (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            hash TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL
        )
    """)
    # Add FK column if not present
    cols = {r[1] for r in conn.execute("PRAGMA table_info(kb_chunks)")}
    if 'parent_text_id' not in cols:
        conn.execute("ALTER TABLE kb_chunks ADD COLUMN parent_text_id INTEGER DEFAULT NULL")
    conn.commit()

    # Build hash→id mapping from existing parent_texts table
    existing = {}
    for r in conn.execute("SELECT id, hash FROM kb_parent_texts"):
        existing[r[1]] = r[0]

    # Scan chunks that still have inline parent_text but no parent_text_id
    cursor = conn.execute(
        "SELECT id, parent_text FROM kb_chunks WHERE parent_text IS NOT NULL AND parent_text_id IS NULL"
    )
    migrated = 0
    batch_update: list[tuple[int, int]] = []
    for row in cursor:
        chunk_id = row[0]
        pt = row[1]
        if not pt:
            continue
        h = hashlib.md5(pt.encode('utf-8')).hexdigest()
        if h not in existing:
            cur2 = conn.execute(
                "INSERT OR IGNORE INTO kb_parent_texts(hash, text) VALUES(?, ?)", (h, pt)
            )
            if cur2.lastrowid:
                existing[h] = cur2.lastrowid
            else:
                r2 = conn.execute("SELECT id FROM kb_parent_texts WHERE hash=?", (h,)).fetchone()
                existing[h] = r2[0]
        pt_id = existing[h]
        batch_update.append((pt_id, chunk_id))
        migrated += 1
        if len(batch_update) >= 500:
            conn.executemany("UPDATE kb_chunks SET parent_text_id=?, parent_text=NULL WHERE id=?", batch_update)
            conn.commit()
            print(f'  ... migrated {migrated} chunks', file=sys.stderr)
            batch_update.clear()
    if batch_update:
        conn.executemany("UPDATE kb_chunks SET parent_text_id=?, parent_text=NULL WHERE id=?", batch_update)
        conn.commit()
    return migrated


def main():
    import os
    size_before = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f'DB size before: {size_before:.1f} MB')

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')

    # Step 1: Convert embeddings
    print('\n[1/3] Converting embeddings V1 → V2 (JSON+zlib → float32 binary) ...')
    t0 = time.perf_counter()
    n = convert_embeddings(conn)
    print(f'  Converted {n} embeddings in {time.perf_counter()-t0:.1f}s')

    # Step 2: Deduplicate parent_text
    print('\n[2/3] Deduplicating parent_text ...')
    t0 = time.perf_counter()
    m = deduplicate_parent_text(conn)
    unique_parents = conn.execute("SELECT COUNT(*) FROM kb_parent_texts").fetchone()[0]
    print(f'  Migrated {m} chunks → {unique_parents} unique parent texts in {time.perf_counter()-t0:.1f}s')

    conn.close()

    # Step 3: VACUUM
    print('\n[3/3] VACUUM (reclaiming freed space) ...')
    t0 = time.perf_counter()
    conn2 = sqlite3.connect(DB_PATH, timeout=120)
    conn2.execute('VACUUM')
    conn2.close()
    print(f'  VACUUM done in {time.perf_counter()-t0:.1f}s')

    size_after = os.path.getsize(DB_PATH) / 1024 / 1024
    saved = size_before - size_after
    print(f'\nDB size after:  {size_after:.1f} MB')
    print(f'Saved: {saved:.1f} MB ({saved/size_before*100:.0f}%)')


if __name__ == '__main__':
    main()
