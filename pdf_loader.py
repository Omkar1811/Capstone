"""
pdf_loader.py - PDF loading helpers for the invoice extraction pipeline.
========================================================================

Responsibilities
----------------
- Load every page of a PDF using pdfplumber (word-level bounding boxes)
  and PyMuPDF (high-quality PNG rendering for vision LLM).
- Build spatially-aligned text from word coordinates so column positions
  are preserved for LLM table parsing.
- Detect the invoice subtotal / grand total from page text (regex + LLM fallback).
- Extract the PO number from the first two pages of the invoice.

This module is intentionally free of DocETL / pipeline concerns.
It is imported by invoice_docetl.py which orchestrates the DocETL pipeline.
"""

import base64
import os
import re
from typing import Optional

import fitz  # PyMuPDF
import pdfplumber

# Read config from environment (populated by the entry-point before import)
_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
_VISION_DPI = int(os.getenv("INVOICE_VISION_DPI", "150"))


# ????????????????????????????????????????????????????????????????????????????????
# Internal helpers
# ????????????????????????????????????????????????????????????????????????????????


def _group_words_by_row(words: list, y_tol: float = 3.0) -> list:
    """Group pdfplumber word dicts into visual rows by top-coordinate proximity."""
    buckets: dict[int, list] = {}
    for w in words:
        key = round(float(w.get("top", 0)) / y_tol)
        buckets.setdefault(key, []).append(w)
    return [
        sorted(row, key=lambda x: float(x.get("x0", 0)))
        for row in sorted(buckets.values(), key=lambda r: float(r[0].get("top", 0)))
    ]


def _render_aligned(words: list, x_scale: float = 5.0) -> str:
    """
    Convert a pdfplumber word list (with bounding boxes) into spatially-aligned text.

    Each word's X coordinate is converted to leading spaces so that column
    positions are preserved in the output string.  This is critical for LLM
    table parsing because the model can infer column membership from horizontal
    position even when the PDF has no explicit grid lines.

    Example output (invoice row):
        20884521079295  SN-628  MONOSOF 2-0 BLK  8.00  CA  89.36  714.88
    """
    rows = _group_words_by_row(words)
    lines = []
    for row in rows:
        parts: list[str] = []
        prev_x = 0.0
        for w in sorted(row, key=lambda x: float(x.get("x0", 0))):
            x0 = float(w.get("x0", 0))
            gap = int(max(1, (x0 - prev_x) / x_scale))
            parts.append(" " * gap + str(w.get("text", "")))
            prev_x = float(w.get("x1", 0))
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


# ????????????????????????????????????????????????????????????????????????????????
# Public API
# ????????????????????????????????????????????????????????????????????????????????


def load_pdf_pages(pdf_path: str, dpi: int = _VISION_DPI) -> list[dict]:
    """
    Load all pages of a PDF and return a list of page dicts.

    Each page dict contains:
      page_num    (int)    1-based page number
      words       (list)   pdfplumber word objects with bounding boxes
      raw_text    (str)    plain text extracted by pdfplumber (for regex scans)
      aligned_text(str)    spatially-aligned text built from word X coordinates
      width       (float)  page width in points
      height      (float)  page height in points
      image_b64   (str)    base64-encoded PNG rendered by PyMuPDF at `dpi` DPI

    pdfplumber pass
    ---------------
    - extract_words(x_tolerance=3, y_tolerance=3): returns per-word bounding
      boxes that feed _render_aligned().  Tolerances merge closely-spaced chars
      into words without over-merging columns.
    - extract_text(): plain fallback for regex-based subtotal / PO detection.

    PyMuPDF (fitz) pass
    -------------------
    - fitz.Matrix(z, z) where z = dpi/72 scales the page to the target DPI.
    - Default 150 DPI balances rendering speed vs. text legibility for gpt-4o-mini.
    - Pages are rendered as PNG, base64-encoded, and stored in image_b64.
    - Used by the vision LLM with "detail": "high" for maximum token allocation.
    """
    pages: list[dict] = []

    # ---- pdfplumber: extract words + raw text ----
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for p in pdf.pages:
                words = p.extract_words(
                    x_tolerance=3,
                    y_tolerance=3,
                    keep_blank_chars=False,
                    use_text_flow=False,
                ) or []
                raw_text = p.extract_text() or ""
                pages.append(
                    {
                        "page_num": p.page_number,
                        "words": words,
                        "raw_text": raw_text,
                        "aligned_text": _render_aligned(words),
                        "width": float(p.width),
                        "height": float(p.height),
                        "image_b64": None,
                    }
                )
    except Exception as exc:
        print(f"  [pdfplumber] Error loading {pdf_path}: {exc}")
        return pages

    # ---- PyMuPDF: render pages to base64 PNG ----
    try:
        z = dpi / 72.0
        mat = fitz.Matrix(z, z)
        doc = fitz.open(pdf_path)
        for i in range(min(len(pages), doc.page_count)):
            pix = doc.load_page(i).get_pixmap(matrix=mat, alpha=False)
            pages[i]["image_b64"] = base64.b64encode(pix.tobytes("png")).decode("ascii")
        doc.close()
    except Exception as exc:
        print(f"  [PyMuPDF] Image rendering failed (vision will degrade): {exc}")

    return pages


def detect_subtotal(pages: list[dict]) -> Optional[float]:
    """
    Detect the overall invoice grand total from page text using prioritised regex.

    Strategy
    --------
    1. High-priority patterns (Invoice Total, Amount Due, Net Due) - these
       labels are almost always printed only once for the final invoice total.
       Return the LAST match found (in case the label appears in a header too).
    2. Low-priority patterns (Subtotal, Merchandise Total) may appear multiple
       times for per-page or per-section running totals.  Take the MAXIMUM value
       found, which corresponds to the cumulative/overall total.
    """
    _AMT = r"\$?\s*([\d,]+\.?\d{0,2})"
    _SEP = r"[\s:.\-\$]*"

    hi_patterns = [
        rf"INVOICE\s+TOTAL{_SEP}{_AMT}",
        rf"Invoice\s+Total{_SEP}{_AMT}",
        rf"AMOUNT\s+DUE{_SEP}{_AMT}",
        rf"Amount\s+Due{_SEP}{_AMT}",
        rf"NET\s+DUE{_SEP}{_AMT}",
        rf"Net\s+Due{_SEP}{_AMT}",
        rf"TOTAL\s+AMOUNT\s+DUE{_SEP}{_AMT}",
        rf"ORDER\s+TOTAL{_SEP}{_AMT}",
        rf"Total\s+Order\s+(?:Amount|Value){_SEP}{_AMT}",
    ]
    lo_patterns = [
        rf"SUBTOTAL{_SEP}{_AMT}",
        rf"Subtotal{_SEP}{_AMT}",
        rf"Sub\s+Total{_SEP}{_AMT}",
        rf"MERCHANDISE\s+TOTAL{_SEP}{_AMT}",
        rf"TOTAL\s+AMOUNT{_SEP}{_AMT}",
        rf"TOTAL\s+INVOICE{_SEP}{_AMT}",
    ]

    full_text = "\n".join(p.get("raw_text", "") for p in pages)

    def _parse(val_str: str) -> Optional[float]:
        try:
            v = float(val_str.replace(",", ""))
            return v if v > 0 else None
        except ValueError:
            return None

    for pat in hi_patterns:
        hits = re.findall(pat, full_text, re.IGNORECASE)
        if hits:
            v = _parse(hits[-1])
            if v:
                return v

    all_low: list[float] = []
    for pat in lo_patterns:
        for raw in re.findall(pat, full_text, re.IGNORECASE):
            v = _parse(raw)
            if v:
                all_low.append(v)

    return max(all_low) if all_low else None


def llm_subtotal(pages: list[dict], api_key: str, model: str = _MODEL) -> Optional[float]:
    """
    LLM-based fallback: use gpt-4o-mini vision on the last 2 pages to find the
    invoice grand total.

    Called by the pipeline when the regex-detected subtotal appears unreliable
    (i.e. item sum diverges from regex subtotal by more than 20% on multi-page
    invoices).  Sends the last two page images + aligned text and asks the model
    for the final invoice total as a JSON number.
    """
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
    except Exception:
        return None

    last_pages = pages[-2:] if len(pages) >= 2 else pages
    user_parts: list = []

    for p in last_pages:
        img = p.get("image_b64")
        if img:
            user_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img}", "detail": "high"},
                }
            )
        aligned = p.get("aligned_text", "")
        if aligned.strip():
            user_parts.append({"type": "text", "text": f"Page {p['page_num']} text:\n{aligned}\n"})

    if not user_parts:
        return None

    user_parts.append(
        {
            "type": "text",
            "text": (
                "What is the FINAL INVOICE TOTAL (the grand total amount due for the "
                "entire invoice, not a line item total or page subtotal)?\n"
                "Return ONLY a JSON object: {\"invoice_total\": <number or null>}"
            ),
        }
    )

    try:
        import json as _json

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an invoice total extractor. Find the final grand total "
                        "amount due for the entire invoice. Return only JSON."
                    ),
                },
                {"role": "user", "content": user_parts},
            ],
            max_tokens=100,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = _json.loads(raw)
        val = data.get("invoice_total")
        if val and float(str(val)) > 0:
            return float(str(val))
    except Exception:
        pass
    return None


def extract_po_number(pages: list[dict]) -> Optional[str]:
    """
    Extract the PO number from the first two pages using regex patterns.

    Scans for common labels like "Purchase Order:", "PO#", "P.O. No." and
    alphanumeric codes that follow.  Prefers codes that start with a letter
    (e.g. N335512) over purely numeric codes.  Returns None if nothing is
    found - there is no filename fallback.
    """
    _PO_PATTERNS = [
        r"Purchase\s+Order\s*(?:Number|No\.?|#)?\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})",
        r"P\.?O\.?\s*(?:Number|No\.?|#)\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})",
        r"\bPO\s*[:\-#]\s*([A-Z0-9][-A-Z0-9]{2,20})",
        r"Purchase\s+Order\s*[:\-]?\s+([A-Z0-9][-A-Z0-9]{2,20})",
        r"Order\s+(?:Number|No\.?|#)\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})",
        r"(?:^|\s)([N][0-9]{5,})(?:\s|$)",  # e.g. N335512
    ]
    text = "\n".join(p.get("raw_text", "") for p in pages[:2])
    candidates: list[str] = []
    for pat in _PO_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            candidates.append(m.group(1).strip())
    if not candidates:
        return None
    alpha_first = [c for c in candidates if c and c[0].isalpha()]
    return alpha_first[0] if alpha_first else candidates[0]


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    if len(sys.argv) < 2:
        print("Usage: python pdf_loader.py <invoice.pdf> [--text]")
        print("  --text  also print aligned text for each page")
        sys.exit(1)

    pdf_path = sys.argv[1]
    show_text = "--text" in sys.argv

    print(f"\nLoading: {pdf_path}")
    pages = load_pdf_pages(pdf_path)

    po = extract_po_number(pages)
    subtotal = detect_subtotal(pages)

    print(f"Pages     : {len(pages)}")
    print(f"PO Number : {po or '(not found)'}")
    print(f"Subtotal  : {subtotal or '(not found)'}")
    print(f"DPI used  : {_VISION_DPI}")

    print("\nPage summary:")
    for p in pages:
        words = len(p.get("words", []))
        img = "yes" if p.get("image_b64") else "no"
        print(f"  Page {p['page_num']:2d}: {words:4d} words  image={img}  aligned_chars={len(p.get('aligned_text',''))}")

    if show_text:
        for p in pages:
            print(f"\n{'='*60}")
            print(f"PAGE {p['page_num']} ALIGNED TEXT")
            print("="*60)
            print(p.get("aligned_text", ""))
