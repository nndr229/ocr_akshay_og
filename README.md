# AP Invoice Console

Accounts-payable invoice management app for Akshay, powered by Claude Sonnet 4.5.

- **OCR ingestion** — upload any sales invoice (PDF, PNG, JPG, GIF, WEBP). Claude's vision reads the document directly and extracts structured fields: vendor, invoice number, dates, line items, taxes, totals, payment terms. No local OCR engine required.
- **RAG chat** — ask questions about your invoices ("how much do we owe Acme?", "what's due this week?"). BM25 retrieval over all stored invoices grounds every answer. The **＋** button next to the chat input uploads new invoices straight from the conversation.
- **Dashboard** — totals, pending/paid breakdown, spend by month, top vendors, due-soon list, and a live activity log of everything coming in.
- **Invoice log** — searchable, filterable table with status workflow (pending → approved → paid / rejected) and full detail view.
- **Manual entry** — form for invoices that arrive without a document.
- **Safeguards** — duplicate detection (vendor + invoice number), low-confidence flagging for blurry scans, extraction warnings surfaced for human review.
- **Login** — the whole app sits behind a sign-in page. Credentials come from `APP_USERNAME` / `APP_PASSWORD` in `.env`; sessions are HMAC-signed HttpOnly cookies valid for 30 days. Sign out from the sidebar.

## Setup

```bash
cd accounting-app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then put your ANTHROPIC_API_KEY in .env
./run.sh
```

Open http://localhost:8700

## Deploying (Pella or any Python host)

The app is a single process with no external services — SQLite and the RAG
index live on disk under `DATA_DIR`.

**On Pella:**
1. Upload the project folder (skip `.venv/`, `data/`, and `.env` — they're local).
2. Pella installs `requirements.txt` automatically (Python 3.10+ required).
3. Start command: `python main.py` — it binds to Pella's injected `PORT` automatically.
4. Set environment variables in the Pella dashboard:
   - `ANTHROPIC_API_KEY` — your Claude API key (required)
   - `APP_USERNAME` / `APP_PASSWORD` — login credentials (default Akshay/Puli123)
   - `SESSION_SECRET` — any long random string, so logins survive redeploys
   - `CLAUDE_MODEL` — optional, defaults to `claude-sonnet-4-5`
   - `DATA_DIR` — optional; point it at a persistent volume if the platform
     wipes the app folder on redeploy, otherwise invoices are lost

**Anywhere else:** the same `python main.py` works on Railway/Render/Fly, a
`Procfile` is included for buildpack platforms, and a `Dockerfile` for
container hosts (`docker build -t ap-console . && docker run -p 8700:8700 --env-file .env ap-console`).

## Architecture

```
backend/
  main.py        FastAPI app — API routes + serves frontend at /
  extraction.py  Claude Sonnet 4.5 vision OCR (forced tool call → structured JSON)
  rag.py         BM25 retrieval index + streaming RAG chat
  db.py          SQLite storage (invoices + activity log)
frontend/        Vanilla JS single-page app (mounted by FastAPI)
data/            SQLite DB + original uploaded files (created at runtime)
```

The frontend is fully static and mounted by FastAPI (`StaticFiles`), so the
whole app runs as a single process — deployable anywhere uvicorn runs.
