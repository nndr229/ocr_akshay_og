"""Invoice OCR extraction using Claude Sonnet 4.5 vision.

PDFs and images are sent directly to the model as document/image blocks —
Claude performs the OCR and returns structured fields via a forced tool call,
which guarantees parseable JSON on Sonnet 4.5.
"""
import base64
import os

import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

SUPPORTED_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

EXTRACTION_TOOL = {
    "name": "record_invoice",
    "description": "Record the structured fields extracted from a sales invoice document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_invoice": {
                "type": "boolean",
                "description": "True if the document is actually an invoice/bill; false for receipts-only fragments, blank pages, or unrelated documents.",
            },
            "vendor_name": {"type": "string", "description": "Supplier/seller legal or trading name"},
            "vendor_address": {"type": "string"},
            "vendor_tax_id": {"type": "string", "description": "GSTIN / VAT / EIN / tax registration number if present"},
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string", "description": "ISO format YYYY-MM-DD"},
            "due_date": {"type": "string", "description": "ISO format YYYY-MM-DD; derive from payment terms if stated"},
            "currency": {"type": "string", "description": "ISO 4217 code, e.g. USD, INR, EUR"},
            "subtotal": {"type": "number"},
            "tax_amount": {"type": "number", "description": "Total tax (sum GST/VAT/sales tax components)"},
            "discount_amount": {"type": "number"},
            "total_amount": {"type": "number", "description": "Grand total payable"},
            "payment_terms": {"type": "string", "description": "e.g. Net 30, Due on receipt"},
            "po_number": {"type": "string", "description": "Purchase order reference if present"},
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": "number"},
                        "unit_price": {"type": "number"},
                        "amount": {"type": "number"},
                    },
                    "required": ["description"],
                },
            },
            "raw_text": {
                "type": "string",
                "description": "Complete plain-text transcription of ALL text on the document, preserving reading order. This powers search, so be thorough.",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Overall extraction confidence. Use low for blurry scans, missing totals, or ambiguous fields.",
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Issues a human should review: unreadable fields, math that doesn't add up, missing invoice number, suspected handwriting, etc.",
            },
        },
        "required": ["is_invoice", "raw_text", "confidence"],
    },
}

SYSTEM_PROMPT = (
    "You are an accounts-payable document processor. Extract invoice data with "
    "extreme accuracy. Transcribe numbers exactly as printed. If subtotal + tax "
    "does not equal the total, still record the printed values and add a warning. "
    "Dates must be normalized to YYYY-MM-DD. Never invent values for fields that "
    "are not on the document — omit them instead."
)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def build_content_block(filename: str, file_bytes: bytes) -> dict:
    """Build the Claude content block for a PDF or image file."""
    ext = os.path.splitext(filename.lower())[1]
    data = base64.standard_b64encode(file_bytes).decode("utf-8")
    if ext == ".pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        }
    if ext in SUPPORTED_IMAGE_TYPES:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": SUPPORTED_IMAGE_TYPES[ext], "data": data},
        }
    raise ValueError(f"Unsupported file type: {ext}. Use PDF, PNG, JPG, GIF or WEBP.")


def extract_invoice(filename: str, file_bytes: bytes) -> dict:
    """Run OCR extraction on an invoice file. Returns the tool-call fields."""
    content_block = build_content_block(filename, file_bytes)
    response = _client().messages.create(
        model=MODEL,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "record_invoice"},
        messages=[
            {
                "role": "user",
                "content": [
                    content_block,
                    {
                        "type": "text",
                        "text": "Extract all invoice data from this document using the record_invoice tool.",
                    },
                ],
            }
        ],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_invoice":
            return dict(block.input)
    raise RuntimeError("Model did not return structured extraction output.")
