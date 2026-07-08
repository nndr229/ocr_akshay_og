"""RAG chat over stored invoices.

Retrieval uses a lightweight in-process BM25 index rebuilt from SQLite on
each ingest — no external vector DB or embedding service needed at this
scale (thousands of invoices). Retrieved invoices are injected as context
into a streaming Claude Sonnet 4.5 chat.
"""
import json
import math
import os
import re
import threading
from collections import Counter

import anthropic

from . import db

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self._lock = threading.Lock()
        self.docs: list[dict] = []
        self.doc_tokens: list[Counter] = []
        self.doc_lens: list[int] = []
        self.df: Counter = Counter()
        self.avgdl: float = 0.0

    def rebuild(self) -> None:
        rows = db.all_for_retrieval()
        docs, doc_tokens, doc_lens, df = [], [], [], Counter()
        for row in rows:
            text = _document_text(row)
            tokens = _tokenize(text)
            counts = Counter(tokens)
            docs.append(row)
            doc_tokens.append(counts)
            doc_lens.append(len(tokens))
            for term in counts:
                df[term] += 1
        with self._lock:
            self.docs, self.doc_tokens, self.doc_lens, self.df = docs, doc_tokens, doc_lens, df
            self.avgdl = (sum(doc_lens) / len(doc_lens)) if doc_lens else 0.0

    def search(self, query: str, top_k: int = 6) -> list[dict]:
        q_terms = _tokenize(query)
        with self._lock:
            n = len(self.docs)
            if n == 0 or not q_terms:
                return []
            scores = []
            for i, counts in enumerate(self.doc_tokens):
                score = 0.0
                dl = self.doc_lens[i] or 1
                for term in q_terms:
                    tf = counts.get(term, 0)
                    if tf == 0:
                        continue
                    idf = math.log(1 + (n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                    score += idf * (tf * (self.k1 + 1)) / (
                        tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                    )
                if score > 0:
                    scores.append((score, i))
            scores.sort(reverse=True)
            return [self.docs[i] for _, i in scores[:top_k]]


INDEX = BM25Index()


def _document_text(row: dict) -> str:
    parts = [
        row.get("vendor_name") or "",
        row.get("invoice_number") or "",
        row.get("invoice_date") or "",
        row.get("payment_terms") or "",
        row.get("po_number") or "",
        row.get("status") or "",
        row.get("notes") or "",
        row.get("raw_text") or "",
    ]
    items = row.get("line_items")
    if isinstance(items, list):
        parts.extend(item.get("description", "") for item in items if isinstance(item, dict))
    return " ".join(parts)


def _invoice_summary(row: dict) -> str:
    items = row.get("line_items")
    items_str = ""
    if isinstance(items, list) and items:
        lines = [
            f"  - {i.get('description', '?')} x{i.get('quantity', 1)} @ {i.get('unit_price', '?')} = {i.get('amount', '?')}"
            for i in items[:15]
            if isinstance(i, dict)
        ]
        items_str = "\nLine items:\n" + "\n".join(lines)
    return (
        f"[Invoice #{row['id']}] {row.get('vendor_name') or 'Unknown vendor'} | "
        f"No: {row.get('invoice_number') or '-'} | Date: {row.get('invoice_date') or '-'} | "
        f"Due: {row.get('due_date') or '-'} | Status: {row.get('status')} | "
        f"Total: {row.get('currency') or 'INR'} {row.get('total_amount') if row.get('total_amount') is not None else '-'} "
        f"(subtotal {row.get('subtotal') if row.get('subtotal') is not None else '-'}, tax {row.get('tax_amount') if row.get('tax_amount') is not None else '-'}) | "
        f"Terms: {row.get('payment_terms') or '-'} | PO: {row.get('po_number') or '-'}"
        f"{items_str}"
    )


CHAT_SYSTEM = (
    "You are the accounts-payable assistant for this invoice management system. "
    "You answer questions about the company's invoices using the invoice records "
    "provided in the context below. Be precise with amounts, dates and vendor names — "
    "always cite the invoice number or [Invoice #id] you got a figure from. "
    "If the context doesn't contain enough information to answer, say so plainly "
    "rather than guessing. When asked for calculations (totals, aging, dues), show "
    "the invoices you included. Amounts may be in different currencies — never sum "
    "across currencies without flagging it.\n\n"
    "Dashboard summary:\n{stats}\n\n"
    "Relevant invoice records for this question:\n{context}"
)


def stream_chat(message: str, history: list[dict]):
    """Yield text chunks for a RAG-grounded chat answer."""
    hits = INDEX.search(message, top_k=6)
    context = "\n\n".join(_invoice_summary(h) for h in hits) if hits else "(no matching invoices found)"
    stats = db.dashboard_stats()
    stats_brief = json.dumps(
        {
            "total_invoices": stats["total_invoices"],
            "total_amount": stats["total_amount"],
            "by_status": stats["by_status"],
            "needs_review": stats["needs_review"],
        },
        default=str,
    )
    system = CHAT_SYSTEM.format(stats=stats_brief, context=context)

    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history[-12:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    messages.append({"role": "user", "content": message})

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=MODEL,
        max_tokens=2048,
        system=system,
        messages=messages,
    ) as stream:
        yield from stream.text_stream
