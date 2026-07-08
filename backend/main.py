"""AP Invoice Console — FastAPI backend.

Run with:  uvicorn backend.main:app --host 0.0.0.0 --port 8700
The frontend is mounted at / so the whole app is served from one process.
"""
import json
import os
import re
import tempfile
import time
import zipfile
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from starlette.background import BackgroundTask  # noqa: E402

from . import auth, db, extraction, rag  # noqa: E402

app = FastAPI(title="AP Invoice Console")

# ------------------------------------------------------------------- auth

PUBLIC_PATHS = {"/login.html", "/styles.css", "/api/login", "/favicon.ico"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or auth.verify_token(request.cookies.get(auth.SESSION_COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse("/login.html", status_code=302)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginRequest, response: Response):
    if not auth.check_credentials(body.username, body.password):
        raise HTTPException(401, "Invalid username or password")
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.create_token(body.username),
        max_age=auth.SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}

UPLOAD_DIR = db.DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    rag.INDEX.rebuild()


# ---------------------------------------------------------------- ingestion

def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)


def _ingest_file(upload_name: str, file_bytes: bytes) -> dict:
    """Extract, dedupe, store one invoice file. Returns a result dict."""
    extracted = extraction.extract_invoice(upload_name, file_bytes)

    if not extracted.get("is_invoice", True):
        db.log_event("extraction_failed", f"{upload_name}: not recognized as an invoice")
        return {"ok": False, "filename": upload_name,
                "error": "Document was not recognized as an invoice."}

    dup = db.find_duplicate(extracted.get("vendor_name"), extracted.get("invoice_number"))
    if dup:
        db.log_event(
            "duplicate_blocked",
            f"{upload_name}: duplicate of invoice #{dup['id']} "
            f"({extracted.get('vendor_name')} / {extracted.get('invoice_number')})",
            invoice_id=dup["id"],
        )
        return {"ok": False, "filename": upload_name, "duplicate_of": dup["id"],
                "error": f"Duplicate: invoice {extracted.get('invoice_number')} from "
                         f"{extracted.get('vendor_name')} already exists (#{dup['id']})."}

    stored_name = f"{int(time.time() * 1000)}_{_safe_name(upload_name)}"
    (UPLOAD_DIR / stored_name).write_bytes(file_bytes)

    invoice_id = db.insert_invoice({
        **{k: extracted.get(k) for k in (
            "vendor_name", "vendor_address", "vendor_tax_id", "invoice_number",
            "invoice_date", "due_date", "currency", "subtotal", "tax_amount",
            "discount_amount", "total_amount", "payment_terms", "po_number",
            "line_items", "raw_text", "confidence",
        )},
        "extraction_warnings": extracted.get("warnings") or [],
        "source": "upload",
        "original_filename": upload_name,
        "stored_filename": stored_name,
    })
    db.log_event(
        "ingested",
        f"{extracted.get('vendor_name') or 'Unknown'} — "
        f"{extracted.get('invoice_number') or 'no number'} — "
        f"{extracted.get('currency') or 'USD'} {extracted.get('total_amount')}",
        invoice_id=invoice_id,
    )
    rag.INDEX.rebuild()
    return {"ok": True, "filename": upload_name, "invoice": db.get_invoice(invoice_id)}


@app.post("/api/invoices/upload")
async def upload_invoices(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            results.append({"ok": False, "filename": f.filename, "error": "File exceeds 25 MB limit."})
            continue
        try:
            results.append(_ingest_file(f.filename or "upload", data))
        except ValueError as e:
            results.append({"ok": False, "filename": f.filename, "error": str(e)})
        except Exception as e:  # extraction/API failure — report per file, keep batch going
            db.log_event("extraction_failed", f"{f.filename}: {e}")
            results.append({"ok": False, "filename": f.filename, "error": f"Extraction failed: {e}"})
    return {"results": results}


# ------------------------------------------------------------- manual entry

class LineItem(BaseModel):
    description: str
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class ManualInvoice(BaseModel):
    vendor_name: str
    invoice_number: str
    invoice_date: str | None = None
    due_date: str | None = None
    currency: str = "USD"
    subtotal: float | None = None
    tax_amount: float | None = None
    discount_amount: float | None = None
    total_amount: float
    payment_terms: str | None = None
    po_number: str | None = None
    notes: str | None = None
    line_items: list[LineItem] = []


@app.post("/api/invoices/manual")
def manual_entry(inv: ManualInvoice):
    dup = db.find_duplicate(inv.vendor_name, inv.invoice_number)
    if dup:
        raise HTTPException(409, f"Invoice {inv.invoice_number} from {inv.vendor_name} already exists (#{dup['id']}).")
    items = [i.model_dump(exclude_none=True) for i in inv.line_items]
    raw_text = " ".join(filter(None, [
        inv.vendor_name, inv.invoice_number, inv.invoice_date, inv.payment_terms,
        inv.po_number, inv.notes, *(i.get("description", "") for i in items),
    ]))
    invoice_id = db.insert_invoice({
        **inv.model_dump(exclude={"line_items"}),
        "line_items": items,
        "raw_text": raw_text,
        "source": "manual",
        "confidence": "high",
    })
    db.log_event("manual_entry", f"{inv.vendor_name} — {inv.invoice_number} — {inv.currency} {inv.total_amount}",
                 invoice_id=invoice_id)
    rag.INDEX.rebuild()
    return db.get_invoice(invoice_id)


# ------------------------------------------------------------------ queries

@app.get("/api/invoices")
def get_invoices(status: str | None = None, search: str | None = None):
    return db.list_invoices(status=status, search=search)


@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: int):
    inv = db.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return inv


class StatusUpdate(BaseModel):
    status: str


VALID_STATUSES = {"pending", "approved", "paid", "rejected"}


@app.patch("/api/invoices/{invoice_id}/status")
def set_status(invoice_id: int, body: StatusUpdate):
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Status must be one of {sorted(VALID_STATUSES)}")
    if not db.update_status(invoice_id, body.status):
        raise HTTPException(404, "Invoice not found")
    db.log_event("status_change", f"Invoice #{invoice_id} → {body.status}", invoice_id=invoice_id)
    rag.INDEX.rebuild()
    return db.get_invoice(invoice_id)


@app.delete("/api/invoices/{invoice_id}")
def remove_invoice(invoice_id: int):
    if not db.delete_invoice(invoice_id):
        raise HTTPException(404, "Invoice not found")
    db.log_event("status_change", f"Invoice #{invoice_id} deleted")
    rag.INDEX.rebuild()
    return {"ok": True}


@app.get("/api/stats")
def stats():
    return db.dashboard_stats()


@app.get("/api/activity")
def activity():
    return db.list_activity()


# ---------------------------------------------------------- backup / restore

@app.get("/api/backup")
def download_backup():
    """Full backup: SQLite snapshot + original uploaded files, as a zip."""
    tmp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_zip.close()
    tmp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp_db.close()
    try:
        db.backup_to(tmp_db.name)
        with zipfile.ZipFile(tmp_zip.name, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db.name, "invoices.db")
            if UPLOAD_DIR.exists():
                for f in UPLOAD_DIR.iterdir():
                    if f.is_file():
                        zf.write(f, f"uploads/{f.name}")
    finally:
        os.unlink(tmp_db.name)
    return FileResponse(
        tmp_zip.name,
        filename=f"ap-backup-{date.today().isoformat()}.zip",
        media_type="application/zip",
        background=BackgroundTask(os.unlink, tmp_zip.name),
    )


@app.post("/api/restore")
async def restore_backup(file: UploadFile = File(...)):
    """Restore a backup zip: replaces the DB and re-adds uploaded files."""
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "Backup exceeds 200 MB limit.")
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "backup.zip"
        zip_path.write_bytes(data)
        try:
            zf = zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile:
            raise HTTPException(400, "Not a valid backup zip.")
        with zf:
            names = zf.namelist()
            if "invoices.db" not in names:
                raise HTTPException(400, "Backup zip does not contain invoices.db.")
            db_tmp = Path(tmpdir) / "invoices.db"
            db_tmp.write_bytes(zf.read("invoices.db"))
            if not db.validate_db_file(str(db_tmp)):
                raise HTTPException(400, "invoices.db in the backup is not a valid invoice database.")
            restored_files = 0
            for name in names:
                if name.startswith("uploads/") and not name.endswith("/"):
                    safe = _safe_name(Path(name).name)
                    if safe:
                        (UPLOAD_DIR / safe).write_bytes(zf.read(name))
                        restored_files += 1
            db.replace_db(db_tmp)
    rag.INDEX.rebuild()
    count = len(db.list_invoices(limit=100000))
    db.log_event("restore", f"Backup restored: {count} invoices, {restored_files} files")
    return {"ok": True, "invoices": count, "files": restored_files}


# --------------------------------------------------------------------- chat

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@app.post("/api/chat")
def chat(req: ChatRequest):
    def event_stream():
        try:
            for chunk in rag.stream_chat(req.message, req.history):
                yield f"data: {json.dumps({'text': chunk})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ----------------------------------------------------------------- frontend

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
