import sqlite3, os

# Find KB database
for root, dirs, files in os.walk('backend'):
    for f in files:
        if f.endswith('.db') and 'kb' in f.lower():
            print(f"Found: {os.path.join(root, f)}")

# Check common paths
for p in ['backend/kb.db', 'backend/knowledge.db', 'backend/data/kb.db', 'kb.db', 'knowledge_base.db']:
    if os.path.exists(p):
        print(f"\nDB: {p}")
        conn = sqlite3.connect(p)
        conn.row_factory = sqlite3.Row
        # Check docs
        docs = conn.execute("SELECT doc_name, stock_name, stock_code FROM kb_documents").fetchall()
        print(f"Total docs: {len(docs)}")
        for d in docs:
            print(f"  {d['stock_name']} ({d['stock_code']}): {d['doc_name'][:60]}")
        # Check if 百克生物 exists
        bk = conn.execute("SELECT * FROM kb_documents WHERE stock_name LIKE '%百克%'").fetchall()
        print(f"\n百克生物 docs: {len(bk)}")
        conn.close()
        break
