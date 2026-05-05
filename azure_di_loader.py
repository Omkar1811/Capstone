"""
azure_di_loader.py - Azure Document Intelligence loader for invoice extraction.
===============================================================================

Drop-in replacement for pdf_loader.py that uses Azure Document Intelligence
(prebuilt-layout model) for text and structure extraction.  Returns the SAME
page-dict schema so docetl.py can switch loaders transparently via the
INVOICE_LOADER environment variable.

Azure DI enhancements over pdfplumber
--------------------------------------
1. Table detection   -- Cells with row/column indices extracted as structured
                        grid text and appended to aligned_text.  The LLM sees
                        clean pipe-delimited tables instead of raw coordinates.
2. Key-value pairs   -- Invoice header fields (PO number, date, Invoice #, etc.)
                        detected automatically; extract_po_number() checks these
                        before falling back to regex.
3. Word confidence   -- Per-word confidence scores stored in di_confidence so
                        low-quality pages (e.g. poor scans) can be flagged.
4. ML reading order  -- Azure DI's layout model handles multi-column layouts,
                        rotated text, and non-standard page flows better than
                        coordinate-only grouping.
5. PyMuPDF images    -- Page images for the vision LLM are still rendered by
                        PyMuPDF at the configured DPI, same as pdf_loader.py.

Extra fields in each page dict (not present in pdf_loader output)
-----------------------------------------------------------------
  tables          list[str]      Formatted table strings, one per detected table
  key_value_pairs dict[str,str]  Header-level KV pairs (lower-cased key)
  di_confidence   float          Mean word confidence for the page (0.0-1.0)
  di_words        list[dict]     Raw word dicts: {text, x0, y0, x1, y1, confidence}

Required environment variables
-------------------------------
  AZURE_DI_ENDPOINT  -- e.g. https://my-resource.cognitiveservices.azure.com/
  AZURE_DI_KEY       -- 32-char Azure DI API key

Optional environment variables
-------------------------------
  AZURE_DI_MODEL_ID  -- Azure DI model to use (default: prebuilt-layout)
  INVOICE_VISION_DPI -- DPI for PyMuPDF image rendering (default: 150)
  OPENAI_MODEL       -- Fallback LLM model for llm_subtotal() (default: gpt-4o-mini)

Installation
------------
  pip install azure-ai-documentintelligence pymupdf python-dotenv
"""

import base64
import json as _json
import os
import re
from typing import Optional

import fitz  # PyMuPDF

# ---------------------------------------------------------------------------
# Azure SDK -- optional; failure raises a clear ImportError at call time
# ---------------------------------------------------------------------------
try:
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    from azure.core.credentials import AzureKeyCredential

    _AZURE_SDK_AVAILABLE = True
except ImportError:
    _AZURE_SDK_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_ENDPOINT = os.getenv("AZURE_DI_ENDPOINT", "")
_KEY = os.getenv("AZURE_DI_KEY", "")
_MODEL_ID = os.getenv("AZURE_DI_MODEL_ID", "prebuilt-layout")
_VISION_DPI = int(os.getenv("INVOICE_VISION_DPI", "150"))
_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

_POINTS_PER_INCH = 72.0  # 1 PDF point = 1/72 inch


# ============================================================================
# Internal helpers
# ============================================================================


def _polygon_to_bbox_pts(polygon: list, unit: str = "inch") -> tuple:
    """
    Convert an Azure DI flat polygon list [x0,y0,x1,y1,x2,y2,x3,y3]
    (4 corner points, counter-clockwise from top-left) to an axis-aligned
    bounding box (x0, y0, x1, y1) in PDF points.

    Azure DI uses inches for PDF documents; multiply by 72 to get points.
    For pixel-unit pages the coordinates are already absolute and no
    conversion is needed (multiplier = 1.0).
    """
    if not polygon or len(polygon) < 4:
        return 0.0, 0.0, 0.0, 0.0
    mult = _POINTS_PER_INCH if unit == "inch" else 1.0
    xs = [polygon[i] * mult for i in range(0, len(polygon), 2)]
    ys = [polygon[i] * mult for i in range(1, len(polygon), 2)]
    return min(xs), min(ys), max(xs), max(ys)


def _group_words_by_row(words: list, y_tol: float = 3.0) -> list:
    """Group word dicts (with y0 key) into visual rows by Y-coordinate proximity."""
    buckets: dict[int, list] = {}
    for w in words:
        key = round(float(w.get("y0", 0)) / y_tol)
        buckets.setdefault(key, []).append(w)
    return [
        sorted(row, key=lambda x: float(x.get("x0", 0)))
        for row in sorted(buckets.values(), key=lambda r: float(r[0].get("y0", 0)))
    ]


def _render_aligned(words: list, x_scale: float = 5.0) -> str:
    """
    Convert a word list (with x0, y0, x1 fields in PDF points) to
    spatially-aligned text where leading spaces represent column positions.

    Identical logic to pdf_loader._render_aligned(); kept here so this
    module is fully self-contained.
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


def _format_table(table) -> str:
    """
    Render an Azure DI DocumentTable as a pipe-delimited grid.

    Column header rows (kind='columnHeader') are followed by a separator
    line so the LLM can distinguish header from data rows.  Merged cells
    are placed only in the first covered row/column; other cells are blank.
    """
    if not table or not getattr(table, "cells", None):
        return ""

    grid: dict[int, dict[int, str]] = {}
    header_rows: set[int] = set()
    for cell in table.cells:
        ri = cell.row_index
        ci = cell.column_index
        content = (cell.content or "").replace("\n", " ").strip()
        grid.setdefault(ri, {})[ci] = content
        if getattr(cell, "kind", "") == "columnHeader":
            header_rows.add(ri)

    num_cols = max(max(r.keys()) for r in grid.values()) + 1
    lines: list[str] = []
    for ri in sorted(grid.keys()):
        row = grid[ri]
        cells = [row.get(ci, "") for ci in range(num_cols)]
        lines.append(" | ".join(cells))
        if ri in header_rows:
            # Separator after header rows
            sep_len = sum(len(c) for c in cells) + 3 * (num_cols - 1)
            lines.append("-" * max(sep_len, num_cols * 3))
    return "\n".join(lines)


def _kv_pairs_for_page(kv_pairs, page_num: int) -> dict:
    """
    Extract Azure DI key-value pairs that belong to a specific page.

    Keys are lower-cased and trailing colons stripped for easy lookup.
    Returns {normalised_key: value_string}.
    """
    result: dict[str, str] = {}
    for kv in kv_pairs or []:
        key_obj = getattr(kv, "key", None)
        val_obj = getattr(kv, "value", None)
        if not key_obj:
            continue
        # Filter to the requested page
        regions = getattr(key_obj, "bounding_regions", None) or []
        if regions and all(
            getattr(r, "page_number", None) != page_num for r in regions
        ):
            continue
        key_text = (getattr(key_obj, "content", None) or "").strip().rstrip(":")
        val_text = (getattr(val_obj, "content", None) or "").strip() if val_obj else ""
        if key_text and val_text:
            result[key_text.lower()] = val_text
    return result


# ============================================================================
# Public API
# ============================================================================


def load_pdf_pages(pdf_path: str, dpi: int = _VISION_DPI) -> list[dict]:
    """
    Analyse a PDF with Azure Document Intelligence and return one page dict
    per page in the same schema as pdf_loader.load_pdf_pages().

    Schema (same as pdf_loader, with extra Azure DI fields)
    -------------------------------------------------------
    page_num        int          1-based page number
    words           list[dict]   Normalised word dicts: {text, x0, y0, x1, y1}
    raw_text        str          Plain text (lines joined)
    aligned_text    str          Spatially-aligned text + formatted tables
    width           float        Page width in PDF points
    height          float        Page height in PDF points
    image_b64       str          Base64 PNG rendered by PyMuPDF at `dpi` DPI

    Azure DI extras
    ---------------
    tables          list[str]    Formatted table strings, one per detected table
    key_value_pairs dict         Detected KV pairs (lower-cased keys)
    di_confidence   float        Mean word confidence (0.0-1.0)
    di_words        list[dict]   Same as words (kept for inspection)

    Two-pass approach
    -----------------
    Pass 1 -- Azure DI (prebuilt-layout):
      Sends PDF bytes to the REST API.  Extracts words (with bounding
      polygons and confidence), reading-order lines, tables, and KV pairs.

    Pass 2 -- PyMuPDF:
      Renders each page to a PNG at `dpi` DPI and base64-encodes it.
      This is identical to pdf_loader.py so the same vision LLM prompt
      works regardless of which loader is active.
    """
    if not _AZURE_SDK_AVAILABLE:
        raise ImportError(
            "azure-ai-documentintelligence is not installed.\n"
            "Run: pip install azure-ai-documentintelligence"
        )
    if not _ENDPOINT or not _KEY:
        raise EnvironmentError(
            "AZURE_DI_ENDPOINT and AZURE_DI_KEY must be set in environment.\n"
            "Example:\n"
            "  AZURE_DI_ENDPOINT=https://my-resource.cognitiveservices.azure.com/\n"
            "  AZURE_DI_KEY=<your-32-char-key>"
        )

    # ---- Pass 1: Azure Document Intelligence --------------------------------
    client = DocumentIntelligenceClient(
        endpoint=_ENDPOINT,
        credential=AzureKeyCredential(_KEY),
    )

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    print(f"  [Azure DI] Submitting {len(pdf_bytes)//1024} KB to {_MODEL_ID} ...")
    poller = client.begin_analyze_document(
        model_id=_MODEL_ID,
        body=AnalyzeDocumentRequest(bytes_source=pdf_bytes),
    )
    result = poller.result()

    # Build page_num -> list[table] mapping from top-level table list
    page_tables: dict[int, list] = {}
    for tbl in result.tables or []:
        for region in getattr(tbl, "bounding_regions", None) or []:
            pg = getattr(region, "page_number", None)
            if pg is not None:
                page_tables.setdefault(pg, []).append(tbl)

    pages: list[dict] = []
    for di_page in result.pages or []:
        pg_num = di_page.page_number
        unit = getattr(di_page, "unit", "inch")
        mult = _POINTS_PER_INCH if unit == "inch" else 1.0

        page_w = float(di_page.width or 0) * mult
        page_h = float(di_page.height or 0) * mult

        # Build normalised word list
        di_words: list[dict] = []
        confidences: list[float] = []
        for word in di_page.words or []:
            poly = getattr(word, "polygon", None) or []
            x0, y0, x1, y1 = _polygon_to_bbox_pts(poly, unit)
            conf = float(getattr(word, "confidence", 1.0) or 1.0)
            di_words.append(
                {
                    "text": word.content or "",
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "confidence": conf,
                }
            )
            confidences.append(conf)

        # Spatially-aligned text from word coordinates
        aligned = _render_aligned(di_words)

        # Plain text from Azure DI reading-order lines
        raw_text = "\n".join(
            (getattr(line, "content", None) or "")
            for line in (di_page.lines or [])
            if getattr(line, "content", None)
        )

        # Format tables for this page and append to aligned_text
        page_tbl_objs = page_tables.get(pg_num, [])
        table_strings = [_format_table(t) for t in page_tbl_objs]
        table_strings = [s for s in table_strings if s.strip()]

        combined_aligned = aligned
        if table_strings:
            combined_aligned += "\n\nDETECTED TABLES (Azure Document Intelligence):\n"
            for idx, ts in enumerate(table_strings, 1):
                combined_aligned += f"\nTable {idx}:\n{ts}\n"

        # Key-value pairs for this page
        kv = _kv_pairs_for_page(result.key_value_pairs, pg_num)

        pages.append(
            {
                # Standard schema (same as pdf_loader)
                "page_num": pg_num,
                "words": di_words,
                "raw_text": raw_text,
                "aligned_text": combined_aligned,
                "width": page_w,
                "height": page_h,
                "image_b64": None,  # filled in by PyMuPDF pass below
                # Azure DI extras
                "tables": table_strings,
                "key_value_pairs": kv,
                "di_confidence": (
                    sum(confidences) / len(confidences) if confidences else 0.0
                ),
                "di_words": di_words,
            }
        )

    # ---- Pass 2: PyMuPDF -- render pages to base64 PNG ----------------------
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
    Detect the invoice grand total from page text using prioritised regex.

    Identical to pdf_loader.detect_subtotal(); kept here so this module
    is self-contained.  Uses raw_text from Azure DI reading-order lines.

    Strategy
    --------
    1. High-priority labels (Invoice Total, Amount Due, Net Due) -- return
       the LAST match (handles header repetition).
    2. Low-priority labels (Subtotal, Merchandise Total) -- take the MAXIMUM
       value found, which corresponds to the cumulative/overall total.
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
    LLM-based fallback: use gpt-4o-mini vision on the last 2 pages to find
    the invoice grand total.

    Identical to pdf_loader.llm_subtotal().
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
                    "image_url": {
                        "url": f"data:image/png;base64,{img}",
                        "detail": "high",
                    },
                }
            )
        aligned = p.get("aligned_text", "")
        if aligned.strip():
            user_parts.append(
                {"type": "text", "text": f"Page {p['page_num']} text:\n{aligned}\n"}
            )

    if not user_parts:
        return None

    user_parts.append(
        {
            "type": "text",
            "text": (
                "What is the FINAL INVOICE TOTAL (the grand total amount due for the "
                "entire invoice, not a line item total or page subtotal)?\n"
                'Return ONLY a JSON object: {"invoice_total": <number or null>}'
            ),
        }
    )

    try:
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
    Extract the PO number, checking Azure DI key-value pairs first, then
    falling back to regex on raw_text.

    Azure DI often detects "Purchase Order" as a form field, giving a more
    reliable result than regex alone -- especially on structured invoices.
    """
    # Priority 1: Azure DI key-value pairs from the first two pages
    _po_kv_patterns = re.compile(
        r"purchase\s*order|p\.?o\.?\s*(?:number|no\.?|#)?|po\s*(?:number|no\.?|#)?",
        re.IGNORECASE,
    )
    for p in pages[:2]:
        for key, val in (p.get("key_value_pairs") or {}).items():
            if _po_kv_patterns.search(key) and val and len(val.strip()) >= 3:
                return val.strip()

    # Priority 2: Regex on raw_text (same as pdf_loader)
    _po_regex_patterns = [
        r"Purchase\s+Order\s*(?:Number|No\.?|#)?\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})",
        r"P\.?O\.?\s*(?:Number|No\.?|#)\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})",
        r"\bPO\s*[:\-#]\s*([A-Z0-9][-A-Z0-9]{2,20})",
        r"Purchase\s+Order\s*[:\-]?\s+([A-Z0-9][-A-Z0-9]{2,20})",
        r"Order\s+(?:Number|No\.?|#)\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})",
        r"(?:^|\s)([N][0-9]{5,})(?:\s|$)",  # e.g. N335512
    ]
    text = "\n".join(p.get("raw_text", "") for p in pages[:2])
    candidates: list[str] = []
    for pat in _po_regex_patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            candidates.append(m.group(1).strip())
    if not candidates:
        return None
    alpha_first = [c for c in candidates if c and c[0].isalpha()]
    return alpha_first[0] if alpha_first else candidates[0]


# ============================================================================
# CLI entry point (for standalone inspection)
# ============================================================================

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    if len(sys.argv) < 2:
        print("Usage: python azure_di_loader.py <invoice.pdf> [--text] [--tables] [--kv]")
        print("  --text    Print aligned text for each page")
        print("  --tables  Print detected tables per page")
        print("  --kv      Print key-value pairs per page")
        sys.exit(1)

    pdf_path = sys.argv[1]
    show_text = "--text" in sys.argv
    show_tables = "--tables" in sys.argv
    show_kv = "--kv" in sys.argv

    if not _AZURE_SDK_AVAILABLE:
        print("ERROR: azure-ai-documentintelligence not installed.")
        print("Run: pip install azure-ai-documentintelligence")
        sys.exit(1)

    if not _ENDPOINT or not _KEY:
        print("ERROR: Set AZURE_DI_ENDPOINT and AZURE_DI_KEY in your .env file.")
        sys.exit(1)

    print(f"\nLoading: {pdf_path}")
    print(f"Endpoint: {_ENDPOINT}")
    print(f"Model   : {_MODEL_ID}")
    pages = load_pdf_pages(pdf_path)

    po = extract_po_number(pages)
    subtotal = detect_subtotal(pages)

    print(f"\nPages     : {len(pages)}")
    print(f"PO Number : {po or '(not found)'}")
    print(f"Subtotal  : {subtotal or '(not found)'}")
    print(f"DPI used  : {_VISION_DPI}")

    print("\nPage summary:")
    for p in pages:
        words = len(p.get("words", []))
        img = "yes" if p.get("image_b64") else "no"
        conf = p.get("di_confidence", 0.0)
        tbls = len(p.get("tables", []))
        kvs = len(p.get("key_value_pairs", {}))
        print(
            f"  Page {p['page_num']:2d}: {words:4d} words  confidence={conf:.2f}"
            f"  tables={tbls}  kv_pairs={kvs}  image={img}"
        )

    if show_text:
        for p in pages:
            print(f"\n{'='*60}")
            print(f"PAGE {p['page_num']} ALIGNED TEXT")
            print("=" * 60)
            print(p.get("aligned_text", ""))

    if show_tables:
        for p in pages:
            tbls = p.get("tables", [])
            if tbls:
                print(f"\n{'='*60}")
                print(f"PAGE {p['page_num']} TABLES")
                print("=" * 60)
                for i, t in enumerate(tbls, 1):
                    print(f"\n--- Table {i} ---\n{t}")

    if show_kv:
        for p in pages:
            kv = p.get("key_value_pairs", {})
            if kv:
                print(f"\n{'='*60}")
                print(f"PAGE {p['page_num']} KEY-VALUE PAIRS")
                print("=" * 60)
                for k, v in kv.items():
                    print(f"  {k!r:40s} = {v!r}")
