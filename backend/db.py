"""SQLite storage layer for invoices and chat history."""
import json
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(
    __file__).resolve().parent.parent / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "invoices.db"

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_name TEXT,
    vendor_address TEXT,
    vendor_tax_id TEXT,
    invoice_number TEXT,
    invoice_date TEXT,
    due_date TEXT,
    currency TEXT DEFAULT 'INR',
    subtotal REAL,
    tax_amount REAL,
    discount_amount REAL,
    total_amount REAL,
    payment_terms TEXT,
    po_number TEXT,
    line_items TEXT,          -- JSON array
    raw_text TEXT,            -- full OCR text used for RAG retrieval
    notes TEXT,
    source TEXT DEFAULT 'upload',   -- upload | manual | chat
    original_filename TEXT,
    stored_filename TEXT,
    status TEXT DEFAULT 'pending',  -- pending | approved | paid | rejected
    confidence TEXT DEFAULT 'high', -- high | medium | low
    extraction_warnings TEXT,       -- JSON array of issues found during extraction
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER,
    event TEXT NOT NULL,        -- ingested | manual_entry | status_change | duplicate_blocked | extraction_failed
    detail TEXT,
    created_at TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, detail: str = "", invoice_id: int | None = None) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO activity_log (invoice_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (invoice_id, event, detail, _now()),
        )


def insert_invoice(data: dict) -> int:
    fields = [
        "vendor_name", "vendor_address", "vendor_tax_id", "invoice_number",
        "invoice_date", "due_date", "currency", "subtotal", "tax_amount",
        "discount_amount", "total_amount", "payment_terms", "po_number",
        "line_items", "raw_text", "notes", "source", "original_filename",
        "stored_filename", "status", "confidence", "extraction_warnings",
    ]
    row = {f: data.get(f) for f in fields}
    if isinstance(row["line_items"], (list, dict)):
        row["line_items"] = json.dumps(row["line_items"])
    if isinstance(row["extraction_warnings"], (list, dict)):
        row["extraction_warnings"] = json.dumps(row["extraction_warnings"])
    row["currency"] = "INR"
    row["status"] = row["status"] or "pending"
    row["confidence"] = row["confidence"] or "high"
    row["source"] = row["source"] or "upload"

    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    with _lock, _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO invoices ({cols}, created_at) VALUES ({placeholders}, ?)",
            (*row.values(), _now()),
        )
        return cur.lastrowid


def find_duplicate(vendor_name: str | None, invoice_number: str | None) -> dict | None:
    if not vendor_name or not invoice_number:
        return None
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM invoices WHERE LOWER(vendor_name) = LOWER(?) AND LOWER(invoice_number) = LOWER(?) LIMIT 1",
            (vendor_name.strip(), invoice_number.strip()),
        ).fetchone()
        return dict(row) if row else None


def get_invoice(invoice_id: int) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return _hydrate(dict(row)) if row else None


def list_invoices(status: str | None = None, search: str | None = None, limit: int = 200) -> list[dict]:
    query = "SELECT * FROM invoices"
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if search:
        clauses.append(
            "(vendor_name LIKE ? OR invoice_number LIKE ? OR raw_text LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _lock, _connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_hydrate(dict(r)) for r in rows]


def update_status(invoice_id: int, status: str) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE invoices SET status = ? WHERE id = ?", (status, invoice_id))
        return cur.rowcount > 0


def delete_invoice(invoice_id: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
        return cur.rowcount > 0


def list_activity(limit: int = 100) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?", (
                limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def dashboard_stats() -> dict:
    with _lock, _connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(total_amount),0) s FROM invoices").fetchone()
        by_status = conn.execute(
            "SELECT status, COUNT(*) c, COALESCE(SUM(total_amount),0) s FROM invoices GROUP BY status"
        ).fetchall()
        by_vendor = conn.execute(
            """SELECT vendor_name, COUNT(*) c, COALESCE(SUM(total_amount),0) s
               FROM invoices WHERE vendor_name IS NOT NULL
               GROUP BY vendor_name ORDER BY s DESC LIMIT 8"""
        ).fetchall()
        by_month = conn.execute(
            """SELECT substr(COALESCE(invoice_date, created_at), 1, 7) m,
                      COUNT(*) c, COALESCE(SUM(total_amount),0) s
               FROM invoices GROUP BY m ORDER BY m DESC LIMIT 12"""
        ).fetchall()
        due_soon = conn.execute(
            """SELECT * FROM invoices
               WHERE status IN ('pending','approved') AND due_date IS NOT NULL AND due_date != ''
               ORDER BY due_date ASC LIMIT 10"""
        ).fetchall()
        needs_review = conn.execute(
            "SELECT COUNT(*) c FROM invoices WHERE confidence = 'low' AND status = 'pending'"
        ).fetchone()
    return {
        "total_invoices": total["c"],
        "total_amount": round(total["s"], 2),
        "by_status": [dict(r) for r in by_status],
        "top_vendors": [dict(r) for r in by_vendor],
        "by_month": sorted([dict(r) for r in by_month], key=lambda r: r["m"] or ""),
        "due_soon": [_hydrate(dict(r)) for r in due_soon],
        "needs_review": needs_review["c"],
    }


def all_for_retrieval() -> list[dict]:
    """Lightweight rows used to build the RAG index."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT id, vendor_name, invoice_number, invoice_date, due_date, currency,
                      subtotal, tax_amount, total_amount, payment_terms, po_number,
                      line_items, raw_text, notes, status, source, created_at
               FROM invoices"""
        ).fetchall()
        return [_hydrate(dict(r)) for r in rows]


def backup_to(dest_path: str) -> None:
    """Write a consistent snapshot of the DB using SQLite's backup API."""
    with _lock, _connect() as src:
        dst = sqlite3.connect(dest_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()


def validate_db_file(path: str) -> bool:
    """Check a file is a SQLite DB containing our invoices table."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ok = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='invoices'"
            ).fetchone() is not None
            if ok:
                conn.execute("PRAGMA integrity_check")
            return ok
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def replace_db(new_db_path) -> None:
    """Swap in a restored DB file. Safe because connections are per-call."""
    with _lock:
        shutil.copyfile(new_db_path, DB_PATH)
    init_db()  # ensure schema (e.g. activity_log) exists on older backups


def _hydrate(row: dict) -> dict:
    for key in ("line_items", "extraction_warnings"):
        if row.get(key):
            try:
                row[key] = json.loads(row[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return row
