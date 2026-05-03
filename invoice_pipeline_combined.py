#!/usr/bin/env python3
"""
invoice_pipeline_combined.py — Combined Invoice Extraction + CSV Mapping Pipeline
==================================================================================

Single-file combination of invoice_extractor.py and invoice_csv_mapper.py.

LLM priority (credentials checked in this order):
  1. OpenAI API key  (OPENAI_API_KEY)          → uses OPENAI_MODEL (default gpt-4o-mini)
  2. Azure OpenAI    (AZURE_OPENAI_ENDPOINT +
                      AZURE_OPENAI_API_KEY)    → uses AZURE_OPENAI_DEPLOYMENT_NAME
  Azure Document Intelligence credentials are loaded for optional future use:
    AZURE_DI_ENDPOINT / AZURE_DI_KEY

Architecture (two-tier, fully agnostic)
------------------------------------------
Tier 2 — Multimodal vision extraction  (gpt-4o-mini, image + text)
Tier 3 — Text-only LLM extraction  (fallback if vision is unavailable)

Usage
-----
  # Extraction only (no CSV mapping):
  python invoice_pipeline_combined.py N298589.pdf
  python invoice_pipeline_combined.py N*.pdf --outdir results/
  python invoice_pipeline_combined.py invoice.pdf --no-vision

  # Extraction + CSV mapping:
  python invoice_pipeline_combined.py N298589.pdf --csv retail.csv --save results_mapped
  python invoice_pipeline_combined.py N*.pdf      --csv retail.csv --save results_mapped
  python invoice_pipeline_combined.py invoice.pdf --csv retail.csv --no-vision

Environment (.env)
------------------
  OPENAI_API_KEY                 — standard OpenAI key (preferred)
  OPENAI_MODEL                   — model name (default: gpt-4o-mini)
  AZURE_OPENAI_ENDPOINT          — Azure OpenAI endpoint (fallback)
  AZURE_OPENAI_API_KEY           — Azure OpenAI key     (fallback)
  AZURE_OPENAI_API_VERSION       — Azure OpenAI API version
  AZURE_OPENAI_DEPLOYMENT_NAME   — Azure deployment name
  AZURE_DI_ENDPOINT              — Azure Document Intelligence endpoint
  AZURE_DI_KEY                   — Azure Document Intelligence key
  INVOICE_VISION_DPI             — page raster DPI (default 150, range 96-300)
"""

import argparse
import base64
import io
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

# ── Optional library imports ───────────────────────────────────────────────────
try:
    import pdfplumber
    PLUMBER_OK = True
except ImportError:
    pdfplumber = None
    PLUMBER_OK = False

try:
    import fitz  # PyMuPDF — high-quality rasterization for vision API
    FITZ_OK = True
except ImportError:
    fitz = None
    FITZ_OK = False

try:
    from openai import AzureOpenAI, OpenAI
    OPENAI_OK = True
except ImportError:
    AzureOpenAI = OpenAI = None
    OPENAI_OK = False

try:
    import pandas as pd
    PANDAS_OK = True
except ImportError:
    pd = None
    PANDAS_OK = False

try:
    import warnings as _w
    _w.filterwarnings("ignore", module="pypdf")
    _w.filterwarnings("ignore", message=".*ARC4.*")
    import camelot
    CAMELOT_OK = True
except ImportError:
    camelot = None
    CAMELOT_OK = False

# ── Credentials ────────────────────────────────────────────────────────────────
# OpenAI (primary)
_OPENAI_KEY   = os.getenv("OPENAI_API_KEY", "")
_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# Azure OpenAI (fallback when OPENAI_API_KEY is absent)
_AZ_ENDPOINT  = os.getenv("AZURE_OPENAI_ENDPOINT", "")
_AZ_KEY       = os.getenv("AZURE_OPENAI_API_KEY", "")
_AZ_VERSION   = os.getenv("AZURE_OPENAI_API_VERSION", "")
_AZ_DEPLOY    = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "")

# Azure Document Intelligence (available for optional use)
DI_ENDPOINT   = os.getenv("AZURE_DI_ENDPOINT", "")
DI_KEY        = os.getenv("AZURE_DI_KEY", "")

# Two-stage extraction (Stage A = structural numeric/raw extraction, Stage B
# = identifier classification). Default OFF — opt-in via env var so the
# change is fully reversible without code edits. Set to "1", "true", "yes",
# or "on" (case-insensitive) to enable.
SPLIT_PROMPTS = os.getenv("INVOICE_SPLIT_PROMPTS", "").strip().lower() in (
    "1", "true", "yes", "on",
)

_llm_client = None
_llm_model  = ""


def _get_llm():
    """
    Return (client, model_name).
    Priority: OpenAI API key → Azure OpenAI → error.
    """
    global _llm_client, _llm_model
    if _llm_client:
        return _llm_client, _llm_model
    if not OPENAI_OK:
        raise RuntimeError("openai not installed: pip install openai")
    if _OPENAI_KEY:
        _llm_client = OpenAI(api_key=_OPENAI_KEY)
        _llm_model  = _OPENAI_MODEL
        print(f"  [LLM] OpenAI / {_llm_model}")
    elif _AZ_ENDPOINT and _AZ_KEY:
        _llm_client = AzureOpenAI(
            api_key=_AZ_KEY,
            azure_endpoint=_AZ_ENDPOINT,
            api_version=_AZ_VERSION,
        )
        _llm_model = _AZ_DEPLOY
        print(f"  [LLM] Azure OpenAI / {_llm_model}")
    else:
        raise RuntimeError(
            "No LLM credentials found.\n"
            "Set OPENAI_API_KEY  — or —\n"
            "Set AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY + "
            "AZURE_OPENAI_API_VERSION + AZURE_OPENAI_DEPLOYMENT_NAME"
        )
    return _llm_client, _llm_model


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION ENGINE  (from invoice_extractor.py)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Output schema ─────────────────────────────────────────────────────────────
OUTPUT_FIELDS = [
    "line_number", "reference_number", "item", "vend_cat_no", "description",
    "order_qty", "trns_qty", "inv_uom", "unit_price", "extended_price",
    "pack_size", "tracking_number", "batch_number",
]
_NUMERIC = {"order_qty", "trns_qty", "unit_price", "extended_price"}

# ── Keywords for automatic header detection ───────────────────────────────────

# ── Number parsing ────────────────────────────────────────────────────────────
def parse_num(v) -> Optional[float]:
    """Parse an invoice number string (handles commas, parentheses, etc.)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s.lower() in {"none", "null", "n/a", "-", ""}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    s = re.sub(r"[^\d,.\-]", "", s).strip()
    if not s or s in {".", ",", "-"}:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(",") < s.rfind(".") else \
            s.replace(".", "").replace(",", ".")
    elif "," in s:
        tail = len(s) - s.rfind(",") - 1
        s = s.replace(",", ".") if tail in (1, 2) else s.replace(",", "")
    try:
        r = float(s)
        return -abs(r) if neg else r
    except ValueError:
        return None


# ── Null-string sentinel helper ───────────────────────────────────────────────
_NULL_SENTINELS = {"none", "null", "n/a", "na", "nil", "-", "--", ""}

def _is_null_str(v) -> bool:
    """Return True when a string field should be treated as absent.

    LLMs occasionally return the string 'None', 'null', 'N/A', etc. instead
    of a proper JSON null.  This helper catches those sentinels so that
    downstream logic (recovery guards, dedup, normalization) correctly treats
    such fields as missing.
    """
    if v is None:
        return True
    return str(v).strip().lower() in _NULL_SENTINELS


def _coerce_null_sentinels(item: dict) -> dict:
    """Replace sentinel strings with None for all non-numeric fields."""
    str_fields = [f for f in OUTPUT_FIELDS if f not in _NUMERIC]
    for field in str_fields:
        if _is_null_str(item.get(field)):
            item[field] = None
    return item


# ── PDF loading ───────────────────────────────────────────────────────────────
def _vision_dpi(override: Optional[int] = None) -> int:
    if override is not None:
        d = override
    else:
        try:
            d = int(os.getenv("INVOICE_VISION_DPI", "200"))
        except ValueError:
            d = 200
    return max(96, min(int(d), 300))


def load_pdf(path: str, vision_dpi: Optional[int] = None) -> list[dict]:
    """
    Load the PDF with pdfplumber (geometry + text), then attach page PNGs
    for multimodal extraction.
    """
    if not PLUMBER_OK:
        raise RuntimeError("pip install pdfplumber")
    dpi = _vision_dpi(vision_dpi)
    pages: list[dict] = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            words = p.extract_words(
                x_tolerance=3, y_tolerance=3,
                keep_blank_chars=False, use_text_flow=False,
            ) or []
            pages.append({
                "page_num":  p.page_number,
                "words":     words,
                "text":      p.extract_text() or "",
                "width":     p.width,
                "image_b64": None,
            })

    if FITZ_OK:
        try:
            z   = dpi / 72.0
            mat = fitz.Matrix(z, z)
            doc = fitz.open(path)
            try:
                for i in range(min(len(pages), doc.page_count)):
                    pix = doc.load_page(i).get_pixmap(matrix=mat, alpha=False)
                    pages[i]["image_b64"] = base64.b64encode(
                        pix.tobytes("png")
                    ).decode("ascii")
            finally:
                doc.close()
        except Exception:
            pass

    missing = [i for i, p in enumerate(pages) if not p.get("image_b64")]
    if missing:
        try:
            with pdfplumber.open(path) as pdf:
                for i in missing:
                    if i >= len(pdf.pages):
                        break
                    try:
                        pi  = pdf.pages[i].to_image(resolution=dpi)
                        buf = io.BytesIO()
                        pi.original.save(buf, format="PNG")
                        pages[i]["image_b64"] = base64.b64encode(
                            buf.getvalue()
                        ).decode("ascii")
                    except Exception:
                        pass
        except Exception:
            pass

    return pages


# ── Subtotal detection ────────────────────────────────────────────────────────
_AMT = r"\$?\s*([\d,]+\.?\d{0,2})"
_SEP = r"[\s:.\-\$]*"
_SUBTOTAL_PATTERNS = [
    rf"INVOICE\s+TOTAL{_SEP}{_AMT}", rf"Invoice\s+Total{_SEP}{_AMT}",
    rf"Sub\s+Total{_SEP}{_AMT}",     rf"SUBTOTAL{_SEP}{_AMT}",
    rf"Subtotal{_SEP}{_AMT}",        rf"AMOUNT\s+DUE{_SEP}{_AMT}",
    rf"Amount\s+Due{_SEP}{_AMT}",    rf"Net\s+Due{_SEP}{_AMT}",
    rf"MERCHANDISE\s+TOTAL{_SEP}{_AMT}",
]


def _llm_subtotal_from_image(image_b64: str) -> Optional[float]:
    """Single tiny vision call to read the final invoice total from the
    last page when regex patterns fail. Used only as a fallback because
    real-world invoices use too many label variants to enumerate."""
    try:
        client, model = _get_llm()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "You extract a single numeric value — the final "
                    "merchandise total — from the last page of an invoice. "
                    "Output a single JSON object: {\"total\": <number>} when "
                    "you can read it; {\"total\": null} when no labelled "
                    "total is visible. Begin with '{', end with '}'. No "
                    "prose, no markdown, no commentary.")},
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{image_b64}",
                                   "detail": "high"}},
                    {"type": "text", "text": (
                        "Find the printed value labelled with one of: "
                        "'Invoice Total', 'Total Due', 'Amount Due', "
                        "'Net Due', 'Grand Total', 'Merchandise Total', "
                        "'Sub Total', or an equivalent phrase. Prefer the "
                        "merchandise/subtotal figure (BEFORE tax/freight) "
                        "if both are shown side-by-side. Return the JSON "
                        "object only.")},
                ]},
            ],
            max_tokens=64,
            temperature=0,
            response_format={"type": "json_object"},
            timeout=60,
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw)
        val = data.get("total")
        if val is None:
            return None
        f = parse_num(val) if not isinstance(val, (int, float)) else float(val)
        return f if (f and f > 0) else None
    except Exception as e:
        print(f"  [Subtotal] LLM fallback failed: {e}")
        return None


def detect_subtotal(pages: list) -> Optional[float]:
    full_text = "\n".join(p["text"] for p in pages)
    for pat in _SUBTOTAL_PATTERNS:
        hits = re.findall(pat, full_text, re.IGNORECASE)
        if hits:
            val = parse_num(hits[-1])
            if val and val > 0:
                return val

    last_img = None
    for p in reversed(pages):
        if p.get("image_b64"):
            last_img = p["image_b64"]
            break
    if last_img:
        print("  [Subtotal] Regex did not match — falling back to vision LLM")
        val = _llm_subtotal_from_image(last_img)
        if val:
            print(f"  [Subtotal] Detected via vision: {val}")
            return val
    return None


# ── Spatial word-grid utilities ───────────────────────────────────────────────
def _group_by_y(words: list, y_tol: float = 3.0) -> list[list[dict]]:
    buckets: dict[int, list] = {}
    for w in words:
        key = round(w["top"] / y_tol)
        buckets.setdefault(key, []).append(w)
    return [
        sorted(v, key=lambda x: x["x0"])
        for v in sorted(buckets.values(), key=lambda v: v[0]["top"])
    ]




# ═══════════════════════════════════════════════════════════════════════════════
# TIER 2 / 3 — Multimodal vision + text-only LLM extraction
# ═══════════════════════════════════════════════════════════════════════════════

def _render_aligned(words: list, x_scale: float = 5.0) -> str:
    rows  = _group_by_y(words)
    lines = []
    for row in rows:
        parts  = []
        prev_x = 0.0
        for w in sorted(row, key=lambda x: x["x0"]):
            gap = int(max(1, (w["x0"] - prev_x) / x_scale))
            parts.append(" " * gap + w["text"])
            prev_x = w["x1"]
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


# ── Prompts ───────────────────────────────────────────────────────────────────
#
# Design principles (apply to every prompt below):
#   • Structural intelligence lives in the SYSTEM message. Page-specific data
#     (image bytes, words, hints) lives in the USER message. Models follow the
#     system message more reliably than mid-prompt instructions.
#   • Vendor-agnostic: NO real brand names, vendor codes, or PO patterns from
#     the test set may leak into a prompt. Use abstract archetypes only.
#   • Cross-LLM portable: all output guards are stated explicitly inside the
#     prompt ("begin with {", "no markdown") so they work even when the LLM
#     does not honor `response_format={"type":"json_object"}`.
#   • Deterministic-friendly: no creative cues, temperature is set by the call
#     site (0–0.2 for extraction).

_EXTRACT_SYSTEM = """\
You are an invoice line-item extractor. You read purchase invoices in any
vendor format and emit a strictly-typed JSON list of line items.

CORE PRINCIPLES (non-negotiable):
1. Read the document as a TABLE. A line item lives on a single logical row.
   Identify column boundaries first, then read each row across those columns.
2. A valid line item has BOTH: a positive shipped quantity AND a positive
   unit price, AND an extended price approximately equal to
   shipped_qty × unit_price (within 1%). If the math does not balance,
   re-read the row before emitting it. Always fill BOTH `order_qty` and
   `trns_qty` — see the QUANTITY RULES below.
3. Never invent values. If a field is not present on the row, emit null.
   Do not copy values from prompt examples — only from real invoice data.
4. Output JSON only. No prose, no markdown, no commentary. Begin your reply
   with '{' and end it with '}'."""

_EXTRACTION_RULES = """\
HOW TO READ THE TABLE (mental procedure — apply to every page):
  Step 1. Locate the column header row (Line, Item, Qty, Description,
          Price, UOM/U/M/Each, Total, …). Note the X-coordinate of each
          header word.
  Step 2. Walk down the rows. For each row, read each cell at its column
          X-coordinate. Column position is the source of truth — a value
          sitting under the "Qty" header is a quantity, even if it
          visually looks like something else.
  Step 3. If a cell appears empty, look one row above AND one row below
          for a continuation value tied to the same line_number / SKU.
          Combine them into ONE JSON object.
  Step 4. If a row has neither a quantity nor a price, it is NOT a line
          item. Skip it.
  Step 5. Verify each row: extended_price ≈ shipped_qty × unit_price
          (within 1¢). If not, you mis-read one of the three values.

EXTRACT: every product/service row whose shipped quantity > 0 AND
unit price > 0.

SKIP these row types:
  • Column header rows
  • Grand total / subtotal / tax / freight / shipping / discount rows
  • Section / group / project / room headers and group-totals
  • Back-ordered items where shipped/invoiced quantity is 0
  • Addresses, payment terms, notes, page numbers, separator lines

MULTI-LINE ITEMS: when one line item spans multiple visual rows because
its description wraps, its catalog code appears on a sub-row, or its
batch / pack-size is printed beneath — collapse those sub-rows into ONE
JSON object. Use the line_number or SKU as the anchor.

MULTIPLE SHIPMENTS (do NOT collapse): When the SAME product (same
SKU / barcode) appears 2 or more times on the page, each occurrence
with a DIFFERENT carrier tracking number (e.g. "FedEx 460973632980"
then "FedEx 460973632958"), each occurrence is a SEPARATE shipment and
must produce a SEPARATE JSON object — one per shipment. Do NOT merge
them. The buyer's item code (e.g. "045082") will be the same for all
shipments of the same product; that is expected and correct.
The distinguishing factor is the CARRIER TRACKING NUMBER.
Rule: same product + same tracking number → one item. Same product +
different tracking numbers → multiple items (one per tracking number).

IDENTIFIER FIELD RULES (principle-based — apply in order, stop at first match):

  A. KEEP COMPOUND CODES INTACT. Many vendor catalog codes contain
     internal hyphens, slashes, dashes or dots (for example
     "ABC-123-XYZ", "100-0288", "12345/02-XX", "AB.99.001"). When you
     see ONE token printed under a single header column — even if it
     contains hyphens or letter-digit mixes — emit it as ONE value.
     Do NOT split such a token across `item`, `vend_cat_no`, or
     `reference_number`. The whole token belongs in a single field.

  B. Long numeric codes printed as a barcode-style identifier
     (visually distinct from short item codes; commonly 10 digits or
     more, no letters)
       → reference_number

  C. Mixed alphanumeric codes (contain letters, OR contain hyphens /
     dashes / slashes / dots, OR otherwise look like a vendor's
     printed catalog code)
       → vend_cat_no

  D. Pure short numeric codes that are clearly a separate column from
     any barcode and from any catalog code (i.e. a SHORT integer ID,
     no separators, distinctly shorter than barcodes on the same row)
       → item
     This includes short numeric codes (typically 4–8 digits) that
     appear within the line-item block under a confusing label such as
     "Tracking number", "Tracking #", "Cust Item", "PO Line", or
     "Reference" — when the value has NO carrier name (FedEx, UPS,
     DHL, USPS, OnTrac, …) attached, it is the buyer's item code, not
     a real shipment tracking number. See ITEM-vs-TRACKING RULES.

  E. The leftmost "Line" / "No." / "#" / "Ln" column (a short row
     counter that increments down the page)
       → line_number  (never put a barcode, catalog code, or item code here)

  F. If only ONE identifier-style column is visible and you cannot
     decide between B / C / D, prefer `vend_cat_no` (the most common
     physical column on real invoices).

  G. NEVER place the SAME value into two identifier fields on one row.
     Pick the single best-fitting field; leave the others null.

FIELD GUIDE:
  line_number      — short row counter from the leftmost Line/No./# column
  reference_number — long numeric barcode (UPC / EAN / GTIN), when present
  item             — short buyer-side item/part code (the buyer's own
                     SKU). Often appears as a small numeric value
                     printed somewhere within the line-item block (NOT
                     the vendor catalog code, NOT the barcode).
                     See ITEM-vs-TRACKING RULES below.
  vend_cat_no      — vendor catalog code (alphanumeric or hyphenated)
  description      — full product name / description (merge multi-line text)
  order_qty        — quantity ORDERED on the PO  (always fill — see QTY rules)
  trns_qty         — quantity SHIPPED / invoiced (use for line math)
  inv_uom          — unit of measure (EA, CA, BX, PK, CS, …) — see UOM rules
  unit_price       — price per single unit
  extended_price   — line total ≈ trns_qty × unit_price
  pack_size        — pack-size label (e.g. "10/PK", "50/BX")
  tracking_number  — CARRIER shipment tracking number. A real tracking
                     number ALWAYS has either (a) an explicit carrier
                     name printed alongside it — FedEx, UPS, DHL, USPS,
                     OnTrac, LaserShip, etc. — OR (b) is clearly a long
                     carrier reference (typically 10+ characters,
                     often alphanumeric). See ITEM-vs-TRACKING RULES.
  batch_number     — lot / batch / expiry number

ITEM-vs-TRACKING RULES (important — these labels are often misleading):
  • Some invoices print the buyer's own item code in the line-item
    block with a confusing label such as "Tracking number", "Tracking #",
    "Cust Item", "PO Line", "Reference", or even no label at all.
  • Distinguishing rule:
      - VALUE has a carrier name attached (FedEx / UPS / DHL / USPS /
        OnTrac / LaserShip / "tracking #" with carrier prefix)
        → tracking_number
      - VALUE is a short numeric or alphanumeric code (typically 4–8
        characters) and has NO carrier name attached, even if its
        label says "tracking" or similar
        → item   (this is the buyer's SKU / PO line code, NOT a
                  shipment tracking number)
  • If a single line has BOTH (a short numeric labeled "Tracking
    number" AND a longer carrier-prefixed code) — the short one is the
    buyer item code, the long carrier one is the real tracking number.
    Extract them into `item` and `tracking_number` respectively.
  • Never put a value with a carrier name into `item`. Never put a
    bare short numeric into `tracking_number` unless you are certain
    it is a carrier reference.

QUANTITY RULES (always fill BOTH order_qty AND trns_qty):
  • If the invoice shows TWO quantity columns (e.g. "Ordered" and
    "Shipped" / "Invoice Qty"), extract each into its own field.
  • If the invoice shows ONLY ONE quantity column (just "Qty" or
    "Quantity"), copy that same value into BOTH order_qty AND trns_qty.
    Both fields must be a number — never leave one null when the other
    is filled.
  • Use trns_qty (shipped) for line-math verification with unit_price.

UOM RULES (extract `inv_uom` whenever an obvious unit of measure appears):
  • Look for a column header named "UOM", "U/M", "U.O.M.", "Unit",
    "Each", "Pkg", "PT" (per type), or similar.
  • The cell value is typically a SHORT alphabetic code: "EA", "CA",
    "BX", "PK", "CS", "DZ", "GA", "LB", "OZ", "FT", "M", … (1–4
    letters). Emit it verbatim, preserving the case shown on the
    invoice.
  • Do NOT emit numbers, prices, or pack quantities into `inv_uom`.
    If the invoice does not show an obvious UOM column, leave `inv_uom`
    null — do not guess.
  • If the only UOM-looking cell is a single letter (e.g. just "S"),
    that is probably truncated or a different column — leave it null.

COMMON MISTAKES TO AVOID:
  ✗ Wrapping the response in extra keys ({{"data": {{...}}, "result": ...}})
  ✗ Emitting a header row, total row, or freight/tax row as a line item
  ✗ Splitting one logical item into two rows when its description wraps
  ✗ Splitting ONE compound catalog code (e.g. "13165-02-IZAACX") across
    two identifier fields — keep it as one value (see Identifier Rule A)
  ✗ Putting product-description text (uppercase brand/family words,
    multiple words, spaces) into an identifier field
  ✗ Putting the same value in both `item` and `vend_cat_no` on one row
  ✗ Putting a number, price or quantity in `inv_uom`
  ✗ Trusting a "Tracking number" label literally — a short numeric
    value (4–8 chars) without a carrier name is the buyer item code,
    not a tracking number (see ITEM-vs-TRACKING RULES)
  ✗ Putting a carrier-prefixed code (e.g. "FedEx 460974016150") into
    any identifier field — that belongs only in `tracking_number`

Return EXACTLY this JSON shape (no markdown, no extra keys):
{{
  "line_items": [
    {{
      "line_number":      "<string or null>",
      "reference_number": "<string or null>",
      "item":             "<string or null>",
      "vend_cat_no":      "<string or null>",
      "description":      "<string or null>",
      "order_qty":        <number or null>,
      "trns_qty":         <number or null>,
      "inv_uom":          "<string or null>",
      "unit_price":       <number or null>,
      "extended_price":   <number or null>,
      "pack_size":        "<string or null>",
      "tracking_number":  "<string or null>",
      "batch_number":     "<string or null>"
    }}
  ]
}}"""

_VISION_PROMPT = """\
You are looking at a rendered page from a purchase invoice. You receive
TWO views of the same page — use BOTH:

  • IMAGE — for visual table structure: grid lines, column boundaries,
    row alignment, indentation, which numbers belong on the same row,
    and which rows are headers vs data vs totals.
  • EXTRACTED TEXT — for character-accurate values: exact SKUs, digits,
    decimal points, hyphens. The text below preserves the original
    column spacing of the PDF; horizontal whitespace = column gaps.

When the two disagree on a numeric or code value, trust the EXTRACTED
TEXT. When you need to know which row a value belongs to, trust the IMAGE.

PAGE POSITION:
{page_context}

{schema_context}

{csv_reference}

{subtotal_hint}

--- BEGIN EXTRACTED TEXT (column-aligned) ---
{aligned_text}
--- END EXTRACTED TEXT ---

{rules}"""

_TEXT_PROMPT = """\
The text below is one page of a PDF invoice. It has been rendered with
horizontal spacing that mirrors the ACTUAL COLUMN POSITIONS of the PDF.
Read whitespace as column boundaries — values that vertically align under
the same header word belong to the same column.

PAGE POSITION:
{page_context}

{schema_context}

{csv_reference}

{subtotal_hint}

--- SPATIALLY ALIGNED PAGE TEXT ---
{aligned_text}
--- END ---

{rules}"""


# ── Two-stage prompts (opt-in via INVOICE_SPLIT_PROMPTS env var) ──────────────
#
# The single _EXTRACT_SYSTEM + _EXTRACTION_RULES prompt above asks the LLM to
# do five distinct cognitive tasks at once: row detection, numeric extraction,
# identifier classification, item-vs-tracking disambiguation, and UOM
# extraction. When prompts get this large, attention is diluted and the model
# tends to drop the harder task (identifier classification) in favor of the
# easier ones (numeric extraction).
#
# The two-stage path keeps the easy stuff in Stage A and isolates the hard
# stuff in Stage B:
#
#   Stage A (per page) — STRUCTURAL EXTRACTION
#     - Find every line-item row.
#     - Pull every numeric / textual cell (qty, price, description, UOM,
#       extended price, batch, pack-size).
#     - For identifiers, collect EVERY token that LOOKS like an identifier
#       into a single `id_tokens` array, in left-to-right reading order,
#       WITHOUT trying to label them. The classifier (Stage B) handles
#       labelling.
#
#   Stage B (per page) — IDENTIFIER CLASSIFICATION
#     - Receives the small list of {row_index, id_tokens, description} from
#       Stage A and (optionally) the page image for context.
#     - Returns {row_index → {item, vend_cat_no, reference_number,
#       tracking_number}} only.
#     - This is a much smaller, focused task — the model can apply the
#       identifier-decision rules without competing for attention with
#       numeric extraction.
#
# The orchestrator merges Stage A and Stage B by row_index and emits the same
# normalize-able dict shape the single-prompt path produces. This means the
# downstream pipeline (schema voting, recovery, normalization, mapping) is
# unchanged.

_STAGE_A_SYSTEM = """\
You are an invoice line-item READER. Your job is the STRUCTURAL part of
extraction only:

  1. Identify every line-item row on the page.
  2. Read its numeric and textual cells (quantities, prices, description,
     UOM, batch, pack-size, extended price).
  3. Collect every short token that LOOKS like an identifier (barcodes,
     SKUs, catalog codes, line numbers) into one ordered list per row —
     in left-to-right, top-to-bottom reading order — WITHOUT trying to
     label which is which.

You DO NOT decide which identifier is the "item" vs "vend_cat_no" vs
"reference_number" vs "tracking_number". A separate downstream step
handles that. Just collect the tokens faithfully.

Output JSON only. No prose, no markdown. Begin your reply with '{' and
end it with '}'."""

_STAGE_A_RULES = """\
HOW TO READ THE PAGE:
  • A line item lives on a single logical row. When a row visually wraps
    across 2-3 lines (description continuation, catalog # on its own
    line, batch / pack-size on the next line), collapse those visual
    lines into ONE logical row using the SKU or line_number as anchor.
  • EXCEPTION — MULTIPLE SHIPMENTS: When the same product (same SKU /
    barcode) appears 2+ times, each with a DIFFERENT carrier tracking
    number (e.g. "FedEx 460973632980" vs "FedEx 460973632958"), emit
    ONE row per shipment. Do NOT collapse them. Same barcode + different
    tracking = different shipment = different row.
  • A valid line item has BOTH a positive shipped quantity AND a
    positive unit price, and extended_price ≈ trns_qty × unit_price
    (within 1¢). If the math doesn't balance, re-read before emitting.

SKIP these row types:
  • Column header rows
  • Grand total / subtotal / tax / freight / shipping / discount rows
  • Section / group / project / room headers and group totals
  • Back-ordered items where shipped/invoiced quantity is 0
  • Address blocks, payment terms, notes, page numbers, separators

QUANTITY RULES:
  • If the invoice shows TWO quantity columns (e.g. "Ordered" and
    "Shipped"/"Invoice Qty"), extract each into its own field.
  • If the invoice shows ONLY ONE quantity column, copy that same value
    into BOTH order_qty AND trns_qty. Both must be a number — never
    leave one null when the other is filled.
  • Use trns_qty (shipped) for line-math verification.

UOM RULES:
  • Look for a column header named "UOM", "U/M", "U.O.M.", "Unit",
    "Each", "Pkg", or similar.
  • The cell value is a SHORT alphabetic code (1-4 letters) — "EA",
    "CA", "BX", "PK", "CS", "DZ", "GA", "LB", "OZ", "FT", "M".
    Emit it verbatim, preserving the case shown on the invoice.
  • Do NOT emit numbers, prices, or pack quantities into inv_uom.
    If no obvious UOM column exists, leave inv_uom null.

ID_TOKENS RULES (this is the only place identifiers go in Stage A):
  • For each line-item row, list EVERY token printed within that row's
    visual block that could plausibly be an identifier:
      - long numeric barcodes (UPC / EAN / GTIN, usually 10+ digits)
      - vendor catalog codes (alphanumeric, often hyphenated, e.g.
        "ABC-123-XYZ", "100-0288", "12345/02-XX")
      - short numeric codes (4-8 digits) that appear inside the line
        block — including any value labeled "Tracking number",
        "Tracking #", "Cust Item", "PO Line", "Reference"
      - the leftmost row counter / line number column value, if any
      - carrier-prefixed shipment references (e.g. "FedEx 460974016150",
        "UPS 1Z..." )
  • List these tokens in the ORDER they visually appear (left-to-right,
    top-to-bottom). The order matters — Stage B uses position as a
    classification signal.
  • Each token should be a single contiguous string with no spaces
    (collapse "ABC - 123" into "ABC-123" if printed that way).
  • KEEP COMPOUND CODES INTACT. If a single token contains hyphens,
    slashes, dots, or letter-digit mixes (e.g. "13165-02-IZAACX"), emit
    it as ONE token. Do NOT split it across multiple list entries.
  • Do NOT include description words, brand names, units of measure, or
    pure decimal numbers (those are quantities/prices, not identifiers).
  • If a row's id_tokens list would be empty, emit [].

COMMON MISTAKES TO AVOID:
  ✗ Labeling identifiers in Stage A (that's Stage B's job)
  ✗ Splitting a compound code (e.g. "13165-02-IZAACX") into two tokens
  ✗ Putting product-description text into id_tokens
  ✗ Putting prices, quantities, or extended totals into id_tokens
  ✗ Emitting a header row, total row, or freight row as a line item
  ✗ Wrapping the response in extra keys

Return EXACTLY this JSON shape (no markdown, no extra keys):
{{
  "line_items": [
    {{
      "row_index":      <integer, starting from 1, in page reading order>,
      "id_tokens":      ["<token1>", "<token2>", ...],
      "description":    "<string or null>",
      "order_qty":      <number or null>,
      "trns_qty":       <number or null>,
      "inv_uom":        "<string or null>",
      "unit_price":     <number or null>,
      "extended_price": <number or null>,
      "pack_size":      "<string or null>",
      "batch_number":   "<string or null>"
    }}
  ]
}}"""

_STAGE_A_VISION_PROMPT = """\
You are looking at a rendered page from a purchase invoice. You receive
TWO views of the same page — use BOTH:

  • IMAGE — for visual table structure: column boundaries, row alignment,
    which numbers belong on the same row, which rows are headers/data/totals.
  • EXTRACTED TEXT — for character-accurate values: exact digits, decimal
    points, hyphens. The text below preserves original column spacing;
    horizontal whitespace = column gaps.

When the two disagree on a numeric or code value, trust the EXTRACTED TEXT.
When you need to know which row a value belongs to, trust the IMAGE.

PAGE POSITION:
{page_context}

{csv_reference}

{subtotal_hint}

--- BEGIN EXTRACTED TEXT (column-aligned) ---
{aligned_text}
--- END EXTRACTED TEXT ---

{rules}"""

_STAGE_A_TEXT_PROMPT = """\
The text below is one page of a PDF invoice, rendered with horizontal
spacing that mirrors the ACTUAL COLUMN POSITIONS of the PDF. Read
whitespace as column boundaries — values that vertically align under the
same header word belong to the same column.

PAGE POSITION:
{page_context}

{csv_reference}

{subtotal_hint}

--- SPATIALLY ALIGNED PAGE TEXT ---
{aligned_text}
--- END ---

{rules}"""


_STAGE_B_SYSTEM = """\
You are an invoice IDENTIFIER CLASSIFIER. A previous step already
extracted every line item's quantities, prices, description, and a list
of raw identifier tokens (`id_tokens`) per row. Your only job is to
classify each row's tokens into the correct identifier slots:

  • reference_number — long numeric barcode (UPC / EAN / GTIN), usually
    10+ digits, no letters
  • vend_cat_no      — vendor catalog code (alphanumeric, often
    hyphenated; "ABC-123-XYZ", "100-0288", "12345/02-XX")
  • item             — short buyer-side item / part code (typically 4-8
    digits, no letters, no separators) printed inside the line-item
    block — including any short numeric value labeled "Tracking number",
    "Tracking #", "Cust Item", "PO Line", or "Reference" when no
    carrier name is attached. THIS IS THE BUYER'S OWN SKU.
  • line_number      — short row counter from the leftmost Line/No./#
    column (a small integer that increments down the page)
  • tracking_number  — REAL carrier shipment reference. Always has either
    a carrier name (FedEx / UPS / DHL / USPS / OnTrac / LaserShip)
    printed alongside it OR is clearly a long carrier reference
    (typically 10+ alphanumeric characters)

Output JSON only. No prose, no markdown. Begin '{' and end '}'."""

_STAGE_B_RULES = """\
DECISION ALGORITHM (apply per row, in this order — stop at first match
for each token):

  1. KEEP COMPOUND CODES INTACT. If a token contains hyphens, slashes,
     dots, or mixed letter/digit (e.g. "13165-02-IZAACX", "SN-628",
     "100-0288"), it is ONE value — do NOT split it across two slots.
  2. A token containing a carrier name (FedEx / UPS / DHL / USPS /
     OnTrac / LaserShip) → tracking_number.
  3. A pure-digit token of 10 or more characters → reference_number
     (barcode).
  4. A token with letters, hyphens, slashes, or dots (alphanumeric
     vendor code) → vend_cat_no.
  5. A pure-digit token of 4-8 characters → item (the buyer's short
     SKU). This is true EVEN IF its label on the invoice was
     "Tracking number" or similar — short numeric code without a
     carrier name is the buyer's item code, not a real tracking #.
  6. A pure-digit token of 1-3 characters that appears as a small row
     counter (typically the leftmost identifier in id_tokens, and
     monotonically increasing across rows on the page) → line_number.
  7. If only ONE identifier-style token exists on a row and rules 2-6
     don't classify it, prefer vend_cat_no.

NEVER place the SAME token into two slots on one row. Each token gets
ONE slot (or is dropped if it clearly isn't an identifier).

If no token fits a slot, emit null for that slot. Do NOT invent values
not present in the row's id_tokens list.

Return EXACTLY this JSON shape (no markdown, no extra keys):
{{
  "rows": [
    {{
      "row_index":        <same integer as Stage A>,
      "line_number":      "<string or null>",
      "reference_number": "<string or null>",
      "item":             "<string or null>",
      "vend_cat_no":      "<string or null>",
      "tracking_number":  "<string or null>"
    }}
  ]
}}"""

_STAGE_B_PROMPT = """\
For each row below, classify its `id_tokens` into the identifier slots
defined in the system message. You receive the page IMAGE only as
visual context (so you can see how a token was printed, what label it
sat under, etc.) — but the actual tokens to classify are listed below.
Do not pull in identifiers from the image that aren't in the lists.

PAGE POSITION:
{page_context}

ROWS FROM STAGE A:
{rows_block}

{rules}"""

def _classify_value(v: str) -> str:
    v = str(v or "").strip()
    if not v or v.lower() in ("none", "null", "n/a"):
        return "empty"
    if re.fullmatch(r'\d{10,}', v):    return "barcode"
    if re.fullmatch(r'\d{1,9}', v):    return "item_code"
    if re.fullmatch(r'[\d.,]+', v):    return "decimal"
    return "alphanum"


def _build_field_schema(items: list[dict],
                        min_confidence: float = 0.7,
                        sample_limit: int = 30) -> dict[str, str]:
    """Build a per-field type schema by VOTING across items.

    A field's type is only anchored if the dominant value type accounts for
    at least ``min_confidence`` of all non-empty samples. This avoids
    locking onto a misleading first-page pattern when real-world data
    legitimately mixes alpha and numeric codes within the same vendor.
    """
    TARGET = ["item", "reference_number", "vend_cat_no"]
    schema: dict[str, str] = {}
    for field in TARGET:
        counts: Counter = Counter()
        for item in items[:sample_limit]:
            v = item.get(field)
            if v:
                t = _classify_value(str(v))
                if t not in ("empty", "decimal"):
                    counts[t] += 1
        if not counts:
            continue
        total = sum(counts.values())
        top_type, top_count = counts.most_common(1)[0]
        if top_count / total >= min_confidence:
            schema[field] = top_type
    return schema


def _schema_context_block(schema: dict[str, str], sample_items: list[dict]) -> str:
    """Build the per-page schema-anchoring block. Tone is intentionally
    evidence-based ("based on what you saw on page 1, here is the observed
    mapping; prefer it unless contradicted") rather than imperative ("DO
    EXACTLY this") — the latter causes the model to faithfully repeat any
    page-1 mistake across remaining pages."""
    if not schema:
        return ""
    type_desc = {
        "barcode":   "long numeric barcode (10+ digits, e.g. UPC/EAN/GTIN)",
        "item_code": "short numeric code (1–9 digits, e.g. vendor item number)",
        "alphanum":  "alphanumeric catalog / reference code (letters + digits, "
                     "often hyphenated)",
    }
    lines = [
        "FIELD MAPPING OBSERVED ON EARLIER PAGES (preferred unless THIS "
        "page's evidence clearly contradicts it):"
    ]
    for field in ["item", "reference_number", "vend_cat_no"]:
        if field not in schema:
            continue
        samples = [str(it.get(field)) for it in sample_items[:6]
                   if it.get(field) and str(it.get(field)).strip()][:2]
        desc = type_desc.get(schema[field], schema[field])
        sample_str = f"  (examples seen: {', '.join(samples)})" if samples else ""
        lines.append(f"  • {field} → {desc}{sample_str}")
    lines.append(
        "Apply the same mapping on this page UNLESS values on this page "
        "clearly indicate a different column structure. In that case, prefer "
        "this page's evidence and follow the IDENTIFIER FIELD DECISION RULES."
    )
    return "\n".join(lines)


def _csv_reference_block(csv_samples: Optional[list[dict]],
                         csv_columns: Optional[list[str]] = None,
                         max_rows: int = 8,
                         max_cols: int = 10) -> str:
    """Build a CSV reference block to anchor the LLM's extraction.

    The reference is shown as PATTERN GUIDANCE — examples of what the
    downstream system expects each invoice field to LOOK LIKE — NOT as
    values to copy. The block intentionally:

      • Includes a strong anti-hallucination warning (do not invent values).
      • Strips obvious PO-number / order-number columns to avoid the LLM
        copying the PO number into every row.
      • Truncates long values so a wide CSV doesn't dominate the prompt.
      • Returns "" when there are no useful samples to share.
    """
    if not csv_samples:
        return ""

    keys: list[str] = []
    seen: set[str]  = set()
    if csv_columns:
        for c in csv_columns:
            if c not in seen:
                keys.append(c)
                seen.add(c)
    for row in csv_samples:
        if not isinstance(row, dict):
            continue
        for k in row.keys():
            if k not in seen:
                keys.append(k)
                seen.add(k)
        if len(keys) >= max_cols * 2:
            break

    skip_kw = ("order number", "ordernum", "po number", "ponum", "po_no", "po#")
    keys = [k for k in keys
            if not any(kw in k.lower().replace("_", " ").replace("-", " ")
                       for kw in skip_kw)]
    keys = keys[:max_cols]
    if not keys:
        return ""

    rows_to_show = csv_samples[:max_rows]
    sample_lines: list[str] = []
    for i, row in enumerate(rows_to_show, 1):
        if not isinstance(row, dict):
            continue
        cells = []
        for k in keys:
            v = row.get(k)
            s = "" if v is None else str(v).strip()
            if len(s) > 24:
                s = s[:21] + "..."
            cells.append(f"{k}={s!r}")
        sample_lines.append(f"  row{i}: " + ", ".join(cells))

    if not sample_lines:
        return ""

    header_line = "REFERENCE — How downstream rows look for THIS purchase order:"
    guard = (
        "These are PAST sample rows from the buyer's reference data — they "
        "show only the SHAPE / FORMAT each downstream field tends to take "
        "(e.g. how long an Item code is, whether Vend Cat No is alphanumeric "
        "or numeric, what UOM looks like).\n"
        "USE THESE SAMPLES ONLY AS PATTERN HINTS. NEVER copy a value from "
        "the samples into your output. Every value you emit MUST be visibly "
        "present on THIS invoice page. If a field is not visible on the "
        "page, emit null — do NOT borrow a value from the samples just "
        "because it would 'fit'."
    )
    return f"{header_line}\n{guard}\n" + "\n".join(sample_lines)


def _dedup_target_fields(items: list[dict], schema: dict[str, str]) -> tuple[list[dict], int]:
    """Detect when the LLM duplicated the same value into multiple identifier
    fields (e.g. ``item == vend_cat_no``). This happens when the invoice
    only has ONE identifier column on the page but the LLM, eager to fill
    every schema field, copies the same value into both. Keep the field
    that best matches the page-voted schema; null the rest.

    Priority when no schema match: ``vend_cat_no`` > ``item`` > ``reference_number``
    (vendor catalog number is the most common physical column on invoices).
    """
    TARGET   = ["vend_cat_no", "item", "reference_number"]
    PRIORITY = {"vend_cat_no": 3, "item": 2, "reference_number": 1}

    dedup_count = 0
    for item in items:
        vals = {f: str(item.get(f) or "").strip() for f in TARGET}
        groups: dict[str, list[str]] = {}
        for f, v in vals.items():
            if v:
                groups.setdefault(v, []).append(f)
        for v, fields in groups.items():
            if len(fields) <= 1:
                continue
            v_type = _classify_value(v)
            best   = None
            for f in fields:
                if schema.get(f) == v_type:
                    best = f
                    break
            if best is None:
                best = max(fields, key=lambda f: PRIORITY.get(f, 0))
            for f in fields:
                if f != best:
                    item[f] = None
                    dedup_count += 1
    return items, dedup_count


def _normalize_field_assignments(items: list[dict], schema: dict[str, str]) -> list[dict]:
    """Apply schema-based corrections without ever erasing a real value.

    Behaviour:
      • If two fields look swapped (each holds a value matching the OTHER
        field's expected type), swap them.
      • If a value doesn't match the schema and no swap candidate exists,
        KEEP the original value and attach a ``_warnings`` note. Downstream
        mapping / human review can decide what to do with it.

    This guards against legitimate real-world variance (e.g. one vendor
    using both numeric SKUs and alphanumeric catalog codes) silently
    erasing correct data.
    """
    if not schema:
        return items

    # ── Special single-field case: vend_cat_no holds item_code type values ──
    # When the LLM consistently places short buyer item codes into `vend_cat_no`
    # (because the invoice column header was ambiguous, e.g. "Item Code"), the
    # schema will show `vend_cat_no: item_code` as the ONLY field — there are
    # never enough fields for the bidirectional swap below to fire.
    # Fix: move the value to `item` (its natural home) and null out `vend_cat_no`.
    if schema.get("vend_cat_no") == "item_code" and "item" not in schema:
        moved = 0
        result = []
        for it in items:
            it = dict(it)
            vc_val = it.get("vend_cat_no")
            item_val = it.get("item")
            if (vc_val and not _is_null_str(vc_val)
                    and _classify_value(str(vc_val)) == "item_code"
                    and _is_null_str(item_val)):
                it["item"] = vc_val
                it["vend_cat_no"] = None
                moved += 1
            result.append(it)
        if moved:
            print(f"  [Normalize] Moved {moved} item_code value(s) from "
                  f"vend_cat_no -> item (schema correction)")
        return result

    TARGET = [f for f in ["item", "reference_number", "vend_cat_no"] if f in schema]
    if len(TARGET) < 2:
        return items

    swap_count    = 0
    warning_count = 0
    normalized: list[dict] = []
    for item in items:
        item  = dict(item)
        vals  = {f: str(item.get(f) or "").strip() for f in TARGET}
        types = {f: _classify_value(v) if v else "empty" for f, v in vals.items()}
        mismatched = {
            f for f in TARGET
            if types[f] not in ("empty", "decimal") and types[f] != schema.get(f)
        }
        if not mismatched:
            normalized.append(item)
            continue
        swapped: set[str] = set()
        for fa in sorted(mismatched):
            if fa in swapped:
                continue
            for fb in TARGET:
                if fb == fa or fb in swapped:
                    continue
                if types[fa] == schema.get(fb) and types[fb] == schema.get(fa):
                    item[fa], item[fb] = item.get(fb), item.get(fa)
                    swapped.add(fa)
                    swapped.add(fb)
                    swap_count += 1
                    break
        for f in mismatched - swapped:
            cur = item.get(f)
            if cur:
                item.setdefault("_warnings", []).append(
                    f"{f}={cur!r} does not match schema "
                    f"(expected {schema.get(f)}, got {_classify_value(str(cur))})"
                )
                warning_count += 1
        normalized.append(item)

    if swap_count or warning_count:
        print(f"  [Normalize] Swaps applied: {swap_count}, "
              f"non-matching values kept with warnings: {warning_count}")
    return normalized


_CARRIER_RE = re.compile(
    r"\b(fedex|fed ex|ups|dhl|usps|ontrac|lasership|royal\s*mail|"
    r"aramex|tnt|postnl|canada\s*post|china\s*post)\b",
    re.IGNORECASE,
)


def _looks_like_carrier_tracking(value: str) -> bool:
    """Return True when a value looks like a real shipment tracking number.

    Heuristic (vendor-agnostic):
      • Contains a known carrier name → tracking
      • Otherwise, a long alphanumeric token (>= 10 chars, mostly digits
        or carrier-style mixed) → tracking
      • Anything shorter without a carrier prefix is most likely the
        buyer's item code, not a real tracking number.
    """
    if not value:
        return False
    s = str(value).strip()
    if _CARRIER_RE.search(s):
        return True
    digits_only = re.sub(r"\D", "", s)
    if len(digits_only) >= 10:
        return True
    if len(s) >= 12 and re.fullmatch(r"[A-Za-z0-9\-\s]+", s):
        return True
    return False


def _looks_like_buyer_item_code(value: str) -> bool:
    """Return True when a value looks like a buyer-side short item code.

    A short numeric (4–8 digits) with no carrier name attached is the
    buyer's SKU / PO-line code on Covidien-style invoices that mislabel
    these values as "Tracking number".
    """
    if not value:
        return False
    s = str(value).strip()
    if _CARRIER_RE.search(s):
        return False
    return bool(re.fullmatch(r"\d{4,8}", s))


_RECOVERY_SYSTEM = (
    "You are reading a single invoice page. The main extraction already "
    "captured all numeric and financial fields but missed the BUYER ITEM "
    "CODE and/or VENDOR CATALOG CODE for some specific lines. "
    "Your only job is to look at those specific lines and report the "
    "missing codes. Output strict JSON only."
)

_RECOVERY_PROMPT = """Look at the attached page image AND the spatial text below.
For each line listed in TARGETS, find whichever of these is missing:
  (a) the BUYER ITEM CODE — a short identifier the buyer uses in their
      own purchasing system
  (b) the VENDOR CATALOG CODE — the vendor's own product catalog number

How to find these codes:
  BUYER ITEM CODE:
  • A short numeric code (typically 4-8 digits) printed inside the line
    block, sometimes labeled "Tracking number", "Tracking #", "Cust Item",
    "PO Line", "Reference", or with no label at all.
  • It has NO carrier name attached (FedEx / UPS / DHL / USPS / OnTrac /
    LaserShip / etc.). If a carrier name appears → that is a carrier
    tracking number, NOT the buyer code.
  • It is shorter and simpler than the barcode (UPC/GTIN, 10+ digits).

  VENDOR CATALOG CODE:
  • The vendor's own SKU / catalog number — typically alphanumeric or
    hyphenated (e.g. "SN-628", "8886803712", "ABC-123-XYZ").
  • Often printed on a sub-row directly below the barcode or description.
  • If the invoice has a printed column header like "Item #", "Cat #",
    "SKU", "Model", or "Product Code", the value in that column IS the
    vendor catalog code.

{csv_reference}

TARGETS (lines needing code recovery):
{targets_block}

PAGE TEXT (spatially aligned):
{aligned_text}

Return JSON in this exact shape (include ALL targets, even if unchanged):
{{
  "items": [
    {{
      "match_key": "<value used to identify this line>",
      "item_code": "<buyer item code or null>",
      "vend_cat_no": "<vendor catalog code or null>"
    }},
    ...
  ]
}}
"""


def _recover_missing_item_codes(items: list[dict],
                                pages: list[dict],
                                csv_reference: str = "",
                                layout_context: str = "") -> list[dict]:
    """Targeted re-prompt: for items missing the buyer Item code despite
    having barcode+price+description, send the page image with a focused
    prompt asking specifically for those item codes.

    This addresses the LLM-attention bias where the buyer code on the
    LAST visual block of a page is sometimes dropped in the initial
    extraction. We identify affected items deterministically (by field
    presence) and re-prompt only the affected pages with only the
    affected lines.
    """
    if not items:
        return items

    pages_by_num = {p["page_num"]: p for p in pages}
    affected_by_page: dict[int, list[dict]] = {}
    for it in items:
        if not _is_null_str(it.get("item")):
            continue
        if not it.get("reference_number"):
            continue
        if not (it.get("unit_price") or it.get("extended_price")):
            continue
        page_num = it.get("_page")
        if not page_num or page_num not in pages_by_num:
            continue
        affected_by_page.setdefault(page_num, []).append(it)

    # Also recover items where vend_cat_no is null/sentinel but item is present.
    for it in items:
        if not _is_null_str(it.get("vend_cat_no")):
            continue
        if not (it.get("unit_price") or it.get("extended_price")):
            continue
        page_num = it.get("_page")
        if not page_num or page_num not in pages_by_num:
            continue
        affected_by_page.setdefault(page_num, [])
        if it not in affected_by_page[page_num]:
            affected_by_page[page_num].append(it)

    if not affected_by_page:
        return items

    total_affected = sum(len(v) for v in affected_by_page.values())
    print(f"  [Recover] {total_affected} item(s) missing Item/Vend-Cat code across "
          f"{len(affected_by_page)} page(s) — targeted re-prompt")

    recovered = 0
    client, model = _get_llm()

    for page_num, target_items in sorted(affected_by_page.items()):
        page = pages_by_num[page_num]
        if not page.get("image_b64"):
            continue
        aligned = _render_aligned(page.get("words") or [])

        target_lines = []
        for t in target_items:
            missing = []
            if _is_null_str(t.get("item")):
                missing.append("buyer_item_code")
            if _is_null_str(t.get("vend_cat_no")):
                missing.append("vend_cat_no")
            # Build a stable match_key: prefer barcode, then item code, then description
            item_for_key = t.get("item") if not _is_null_str(t.get("item")) else None
            mk = (t.get("reference_number") or item_for_key or
                  (t.get("description") or "")[:50]).strip()
            target_lines.append(
                f"  - match_key: {mk!r}  "
                f"missing: {missing}  "
                f"description: {(t.get('description') or '')[:60]!r}  "
                f"unit_price: {t.get('unit_price')}"
            )
        targets_block = "\n".join(target_lines)

        # Prepend layout context so the LLM knows exactly where buyer codes
        # and vendor catalog codes appear on THIS invoice's pages.
        layout_hint = (
            f"\n\nINVOICE LAYOUT CONTEXT:\n{layout_context}\n"
            if layout_context else ""
        )
        prompt = _RECOVERY_PROMPT.format(
            csv_reference=csv_reference,
            targets_block=targets_block,
            aligned_text=aligned,
        ) + layout_hint
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _RECOVERY_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{page['image_b64']}",
                            "detail": "high",
                        }},
                        {"type": "text", "text": prompt},
                    ]},
                ],
                max_tokens=4096,
                temperature=0,
                response_format={"type": "json_object"},
                timeout=120,
            )
            raw = (resp.choices[0].message.content or "").strip()
            raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"^```\s*",     "", raw)
            raw = re.sub(r"```$",        "", raw).strip()
            s, e = raw.find("{"), raw.rfind("}")
            if s >= 0 and e > s:
                raw = raw[s: e + 1]
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
            parsed = payload.get("items") or payload.get("line_items") or []
        except Exception as e:
            print(f"  [Recover] Page {page_num} API error: {e}")
            continue

        # Build lookup by match_key (the anchor we sent to the LLM)
        recovery_by_key: dict[str, dict] = {}
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            mk = str(entry.get("match_key") or "").strip()
            # Also accept old-style response with "barcode" key
            if not mk:
                mk = str(entry.get("barcode") or "").strip()
            if mk:
                recovery_by_key[mk] = entry

        page_recovered = 0
        for it in target_items:
            mk = (it.get("reference_number") or it.get("item") or
                  (it.get("description") or "")[:50] or "").strip()
            entry = recovery_by_key.get(mk)
            if not entry:
                continue
            changed = False
            # Recover item code
            if not it.get("item"):
                ic = entry.get("item_code") or entry.get("item")
                if ic:
                    ic_s = str(ic).strip()
                    if ic_s and ic_s.lower() not in ("null", "none", ""):
                        it["item"] = ic_s
                        changed = True
            # Recover vendor catalog code
            if not it.get("vend_cat_no"):
                vc = entry.get("vend_cat_no")
                if vc:
                    vc_s = str(vc).strip()
                    if vc_s and vc_s.lower() not in ("null", "none", ""):
                        it["vend_cat_no"] = vc_s
                        changed = True
            if changed:
                page_recovered += 1
                recovered += 1
        if page_recovered:
            print(f"  [Recover] Page {page_num}: recovered {page_recovered}/"
                  f"{len(target_items)} identifier(s)")

    if recovered:
        print(f"  [Recover] Total recovered: {recovered} identifier(s) across "
              f"{total_affected} item(s)")
    else:
        print(f"  [Recover] No additional identifiers recovered — "
              f"those lines likely have no buyer/vendor code printed")
    return items


def _fix_misplaced_barcodes(items: list[dict]) -> list[dict]:
    """Move barcode-shaped values out of ``line_number`` into
    ``reference_number``.

    ``line_number`` is meant for short row counters (1-4 digits). When the
    LLM mistakenly places a 10+ digit numeric barcode there, this pass
    corrects it deterministically.
    """
    moved = 0
    for it in items:
        ln = it.get("line_number")
        if not isinstance(ln, str):
            continue
        s = ln.strip()
        digits_only = re.sub(r"\D", "", s)
        if len(digits_only) >= 10 and re.fullmatch(r"\d{10,}", s):
            if not (it.get("reference_number") or "").strip():
                it["reference_number"] = s
                it["line_number"] = None
                moved += 1
    if moved:
        print(f"  [Fix] Moved {moved} barcode value(s) from line_number -> reference_number")
    return items


def _reclassify_tracking_vs_item(items: list[dict]) -> list[dict]:
    """Safety-net post-processing for the common ITEM-vs-TRACKING confusion.

    When an invoice prints the buyer's item code in the line-item block
    with a misleading "Tracking number" label, even a well-prompted LLM
    may put it into ``tracking_number``. This pass fixes that case
    deterministically:

      • If ``tracking_number`` holds a short bare numeric and ``item``
        is empty → move it to ``item``.
      • If ``item`` holds a clearly carrier-prefixed value → move it to
        ``tracking_number``.

    Pure heuristic, no vendor-specific data — relies only on the
    presence/absence of carrier names and the value's shape.
    """
    moved_to_item    = 0
    moved_to_tracking = 0
    for it in items:
        tn = (it.get("tracking_number") or "").strip() if isinstance(it.get("tracking_number"), str) else ""
        item_val = (it.get("item") or "").strip() if isinstance(it.get("item"), str) else ""

        if tn and not item_val and _looks_like_buyer_item_code(tn) and not _looks_like_carrier_tracking(tn):
            it["item"] = tn
            it["tracking_number"] = None
            moved_to_item += 1
            continue

        if item_val and _looks_like_carrier_tracking(item_val) and not _looks_like_buyer_item_code(item_val):
            if not tn:
                it["tracking_number"] = item_val
                it["item"] = None
                moved_to_tracking += 1

    if moved_to_item or moved_to_tracking:
        msg = []
        if moved_to_item:
            msg.append(f"{moved_to_item} value(s) moved from tracking_number -> item")
        if moved_to_tracking:
            msg.append(f"{moved_to_tracking} value(s) moved from item -> tracking_number")
        print(f"  [Reclassify] " + "; ".join(msg) +
              " (carrier-prefix heuristic)")
    return items


def _parse_llm_json(raw: str) -> list[dict]:
    raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^```\s*",     "", raw)
    raw = re.sub(r"```$",        "", raw).strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s >= 0 and e > s:
        raw = raw[s: e + 1]
    try:
        return json.loads(raw).get("line_items", [])
    except Exception:
        try:
            last = raw.rfind("},")
            if last > 0:
                fixed = '{"line_items": [' + raw[:last + 1] + "]}"
                return json.loads(fixed).get("line_items", [])
        except Exception:
            pass
        return []


def _call_llm_vision(page: dict, subtotal_hint: str, page_context: str,
                     schema_context: str = "",
                     csv_reference: str = "") -> list[dict]:
    image_b64 = page.get("image_b64")
    if not image_b64:
        return []
    aligned = _render_aligned(page["words"])
    prompt  = _VISION_PROMPT.format(
        page_context   = page_context,
        schema_context = schema_context,
        csv_reference  = csv_reference,
        subtotal_hint  = subtotal_hint,
        aligned_text   = aligned,
        rules          = _EXTRACTION_RULES,
    )
    client, model = _get_llm()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{image_b64}", "detail": "high"}},
                    {"type": "text", "text": prompt},
                ]},
            ],
            max_tokens=16384,
            temperature=0,
            response_format={"type": "json_object"},
            timeout=180,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if resp.choices[0].finish_reason == "length":
            print("  [Vision] Warning: response truncated (very dense page)")
        return _parse_llm_json(raw)
    except Exception as e:
        err = str(e)
        if "vision" in err.lower() or "image" in err.lower():
            print("  [Vision] Not supported by this deployment — using text-only")
        else:
            print(f"  [Vision] API error: {e}")
        return []


def _call_llm_extract(page_words: list, subtotal_hint: str,
                      schema_context: str = "",
                      page_context: str = "",
                      csv_reference: str = "") -> list[dict]:
    aligned = _render_aligned(page_words)
    prompt  = _TEXT_PROMPT.format(
        page_context   = page_context,
        schema_context = schema_context,
        csv_reference  = csv_reference,
        subtotal_hint  = subtotal_hint,
        aligned_text   = aligned,
        rules          = _EXTRACTION_RULES,
    )
    client, model = _get_llm()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=16384,
            temperature=0,
            response_format={"type": "json_object"},
            timeout=120,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if resp.choices[0].finish_reason == "length":
            print("  [T3] Warning: page chunk truncated (very dense page)")
        return _parse_llm_json(raw)
    except Exception as e:
        print(f"  [T3] API error: {e}")
        return []


# ── Two-stage extraction (opt-in via INVOICE_SPLIT_PROMPTS) ──────────────────

def _call_llm_stage_a(page: dict, subtotal_hint: str, page_context: str,
                      csv_reference: str = "",
                      use_vision: bool = True) -> list[dict]:
    """Stage A: structural extraction. Pulls quantities, prices,
    description, UOM, batch, pack-size, and a raw `id_tokens` list per row
    — NO identifier classification."""
    aligned = _render_aligned(page["words"])
    image_b64 = page.get("image_b64") if use_vision else None

    if image_b64:
        prompt = _STAGE_A_VISION_PROMPT.format(
            page_context  = page_context,
            csv_reference = csv_reference,
            subtotal_hint = subtotal_hint,
            aligned_text  = aligned,
            rules         = _STAGE_A_RULES,
        )
    else:
        prompt = _STAGE_A_TEXT_PROMPT.format(
            page_context  = page_context,
            csv_reference = csv_reference,
            subtotal_hint = subtotal_hint,
            aligned_text  = aligned,
            rules         = _STAGE_A_RULES,
        )

    client, model = _get_llm()
    try:
        if image_b64:
            messages = [
                {"role": "system", "content": _STAGE_A_SYSTEM},
                {"role": "user",   "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{image_b64}",
                        "detail": "high"}},
                    {"type": "text", "text": prompt},
                ]},
            ]
        else:
            messages = [
                {"role": "system", "content": _STAGE_A_SYSTEM},
                {"role": "user",   "content": prompt},
            ]
        resp = client.chat.completions.create(
            model           = model,
            messages        = messages,
            max_tokens      = 8192,
            temperature     = 0,
            response_format = {"type": "json_object"},
            timeout         = 180 if image_b64 else 120,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if resp.choices[0].finish_reason == "length":
            print("  [Stage-A] Warning: response truncated")
        return _parse_llm_json(raw)
    except Exception as e:
        err = str(e)
        if image_b64 and ("vision" in err.lower() or "image" in err.lower()):
            print("  [Stage-A] Vision unsupported — falling back to text-only")
            return _call_llm_stage_a(page, subtotal_hint, page_context,
                                     csv_reference, use_vision=False)
        print(f"  [Stage-A] API error: {e}")
        return []


def _call_llm_stage_b(stage_a_rows: list[dict], page: dict,
                      page_context: str,
                      use_vision: bool = True) -> dict[int, dict]:
    """Stage B: identifier classification. Receives Stage A's compact
    {row_index, id_tokens, description} list and returns
    {row_index → {item, vend_cat_no, reference_number, line_number,
    tracking_number}}."""
    rows_for_b = []
    for r in stage_a_rows:
        if not isinstance(r, dict):
            continue
        ri = r.get("row_index")
        if ri is None:
            continue
        toks = r.get("id_tokens") or []
        if not isinstance(toks, list):
            toks = []
        toks = [str(t).strip() for t in toks if str(t).strip()]
        rows_for_b.append({
            "row_index":   ri,
            "id_tokens":   toks,
            "description": (r.get("description") or "")[:80],
        })

    if not rows_for_b:
        return {}

    rows_block = json.dumps(rows_for_b, ensure_ascii=False, indent=2)
    prompt = _STAGE_B_PROMPT.format(
        page_context = page_context,
        rows_block   = rows_block,
        rules        = _STAGE_B_RULES,
    )

    image_b64 = page.get("image_b64") if use_vision else None
    client, model = _get_llm()
    try:
        if image_b64:
            messages = [
                {"role": "system", "content": _STAGE_B_SYSTEM},
                {"role": "user",   "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{image_b64}",
                        "detail": "high"}},
                    {"type": "text", "text": prompt},
                ]},
            ]
        else:
            messages = [
                {"role": "system", "content": _STAGE_B_SYSTEM},
                {"role": "user",   "content": prompt},
            ]
        resp = client.chat.completions.create(
            model           = model,
            messages        = messages,
            max_tokens      = 4096,
            temperature     = 0,
            response_format = {"type": "json_object"},
            timeout         = 120,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"^```\s*",     "", raw)
        raw = re.sub(r"```$",        "", raw).strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s >= 0 and e > s:
            raw = raw[s: e + 1]
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        rows_out = payload.get("rows") or []
        by_idx: dict[int, dict] = {}
        for r in rows_out:
            if not isinstance(r, dict):
                continue
            ri = r.get("row_index")
            try:
                ri_int = int(ri)
            except (TypeError, ValueError):
                continue
            by_idx[ri_int] = r
        return by_idx
    except Exception as e:
        print(f"  [Stage-B] API error: {e}")
        return {}


def _validate_stage_b_against_tokens(stage_a_row: dict,
                                     stage_b_row: dict) -> dict:
    """Safety net: ensure Stage B only assigns tokens that actually
    appear in Stage A's id_tokens list. Drops any hallucinated value
    (one that wasn't in the source token list). Returns a CLEAN
    classified dict."""
    raw_tokens = stage_a_row.get("id_tokens") or []
    if not isinstance(raw_tokens, list):
        raw_tokens = []
    allowed = {str(t).strip() for t in raw_tokens if str(t).strip()}
    out: dict[str, Optional[str]] = {
        "line_number":      None,
        "reference_number": None,
        "item":             None,
        "vend_cat_no":      None,
        "tracking_number":  None,
    }
    if not isinstance(stage_b_row, dict):
        return out
    used: set[str] = set()
    for slot in ("reference_number", "vend_cat_no", "item",
                 "line_number", "tracking_number"):
        v = stage_b_row.get(slot)
        if v is None:
            continue
        s = str(v).strip()
        if not s or s.lower() in ("null", "none"):
            continue
        if s in allowed and s not in used:
            out[slot] = s
            used.add(s)
    return out


def _call_llm_two_stage(page: dict, subtotal_hint: str, page_context: str,
                        csv_reference: str = "",
                        use_vision: bool = True) -> list[dict]:
    """Two-stage orchestrator: runs Stage A (structural), then Stage B
    (classification), and merges into the same dict shape that the
    single-prompt path emits, so the rest of the pipeline is unchanged."""
    stage_a_items = _call_llm_stage_a(page, subtotal_hint, page_context,
                                      csv_reference, use_vision=use_vision)
    if not stage_a_items:
        return []

    by_idx = _call_llm_stage_b(stage_a_items, page, page_context,
                               use_vision=use_vision)

    merged: list[dict] = []
    for r in stage_a_items:
        if not isinstance(r, dict):
            continue
        ri = r.get("row_index")
        try:
            ri_int = int(ri)
        except (TypeError, ValueError):
            ri_int = None
        cls = _validate_stage_b_against_tokens(r, by_idx.get(ri_int, {}) if ri_int is not None else {})
        merged.append({
            "line_number":      cls["line_number"],
            "reference_number": cls["reference_number"],
            "item":             cls["item"],
            "vend_cat_no":      cls["vend_cat_no"],
            "description":      r.get("description"),
            "order_qty":        r.get("order_qty"),
            "trns_qty":         r.get("trns_qty"),
            "inv_uom":          r.get("inv_uom"),
            "unit_price":       r.get("unit_price"),
            "extended_price":   r.get("extended_price"),
            "pack_size":        r.get("pack_size"),
            "tracking_number":  cls["tracking_number"],
            "batch_number":     r.get("batch_number"),
        })
    return merged


def normalize(raw: dict) -> dict:
    out: dict[str, Any] = {f: None for f in OUTPUT_FIELDS}
    for field in OUTPUT_FIELDS:
        val = raw.get(field)
        if val is None:
            continue
        if field in _NUMERIC:
            out[field] = parse_num(val)
        elif isinstance(val, list):
            out[field] = ", ".join(str(v) for v in val if str(v).strip()) or None
        else:
            s = str(val).strip()
            # Treat LLM sentinel strings ("None", "null", "N/A", …) as absent
            if s.lower() in _NULL_SENTINELS:
                out[field] = None
            else:
                out[field] = s if s else None
    return out


def _item_dedup_key(item: dict):
    """Build a deduplication key for a line item.

    Design intent:
    • Prevents the LLM from emitting the *same* logical row twice.
    • Does NOT collapse rows that are separate shipments of the same
      product (same SKU, different carrier tracking numbers).
    • Does NOT use line_number as a primary identifier — line_number is
      LLM-inconsistent: the same item may appear on two adjacent pages
      (or two LLM calls) with and without a line_number, producing
      different keys that allow duplicates through.

    Key construction:
    • stable_id  = barcode (most reliable) → vend_cat_no → description
      (line_number deliberately excluded as primary ID)
    • extended_price differentiates same-SKU batches of different qty
    • tracking_number appended when present, so same SKU shipped on
      different FedEx/UPS references stays as separate invoice rows
    """
    ext = item.get("extended_price")
    if ext is None:
        return None
    cat  = (item.get("vend_cat_no") or "").strip()
    ref  = (item.get("reference_number") or "").strip()
    desc = (item.get("description") or "")[:40].strip()
    tn   = (item.get("tracking_number") or "").strip()
    # Prefer vend_cat_no (visible in both summary tables and detail pages) over
    # barcode (only on detail pages). This prevents the same item from getting
    # two different keys when it appears on a summary page (no barcode) AND on
    # a detail page (with barcode).
    stable_id = cat or ref or desc
    if not stable_id:
        return None
    if tn:
        return (stable_id, round(ext, 2), tn)
    return (stable_id, round(ext, 2))


_HALLUCINATION_PATTERNS = [
    re.compile(r"product\s+description\s+for\s+item", re.IGNORECASE),
    re.compile(r"^item\s+description$", re.IGNORECASE),
    re.compile(r"^placeholder", re.IGNORECASE),
    re.compile(r"^sample\s+item", re.IGNORECASE),
    re.compile(r"^n/a$", re.IGNORECASE),
    re.compile(r"^example\s+item", re.IGNORECASE),
]


def _is_hallucination(item: dict) -> bool:
    desc = (item.get("description") or "").strip()
    return any(p.search(desc) for p in _HALLUCINATION_PATTERNS)


_CURRENCY_CELL = re.compile(r"^\$?[\d,]+\.\d{2}$")


def _estimate_visible_item_rows(words: list) -> int:
    """Estimate the number of line-item rows visible on a page using
    spatial grouping. A row is treated as an item-row if it contains at
    least one currency-formatted decimal (matches typical unit / extended
    price cells). Used as a sanity check against LLM output to detect
    pages where the LLM dropped rows."""
    if not words:
        return 0
    rows = _group_by_y(words)
    count = 0
    for row in rows:
        for w in row:
            if _CURRENCY_CELL.match(w["text"].strip("$").lstrip("$")):
                count += 1
                break
    return count


# ── Quality detection: descriptions leaking into identifier fields ──────────
_IDENT_FIELDS = ("item", "vend_cat_no", "reference_number")


def _is_suspicious_code(v: Any) -> bool:
    """Detect when a value in an identifier field looks like a product
    description rather than an actual catalog/SKU code.

    Real codes are typically:
      • A single contiguous token (no whitespace)
      • Either alphanumeric with optional hyphens, or purely numeric
      • Generally short (catalog SKUs are rarely > 20 chars)

    Descriptions tend to have spaces, multiple words, or be long
    all-letter strings (e.g., "BIOSYN", "DERMALON", "EDGE DISP HANDSWITCH
    PENCIL SM"). This heuristic flags those.
    """
    if v is None:
        return False
    s = str(v).strip()
    if not s:
        return False
    if len(s.split()) > 1:
        return True
    if s.isalpha() and len(s) >= 7:
        return True
    return False


def _suspicious_count(items: list[dict]) -> int:
    """Count items that have at least one suspicious-looking identifier."""
    n = 0
    for item in items:
        for f in _IDENT_FIELDS:
            if _is_suspicious_code(item.get(f)):
                n += 1
                break
    return n


def _row_match_key(item: dict) -> Optional[tuple]:
    """A coarse key for aligning the same row across two extraction
    attempts. Uses extended_price + line_number when available; falls
    back to (qty, unit_price) which is usually unique enough per page."""
    ep   = item.get("extended_price")
    ln   = (item.get("line_number") or "").strip() if item.get("line_number") else ""
    qty  = item.get("trns_qty") or item.get("order_qty")
    up   = item.get("unit_price")
    if ep is not None and ln:
        return ("a", ln, round(ep, 2))
    if ep is not None and up is not None:
        return ("b", round(up, 2), round(ep, 2))
    if qty is not None and up is not None:
        return ("c", qty, round(up, 2))
    return None


def _merge_by_quality(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """Merge two extraction attempts row-by-row, preferring values that
    look like real codes over suspicious ones (descriptions). Items in
    ``primary`` form the base; ``secondary`` provides per-cell overrides
    when its value is non-suspicious AND the primary's is suspicious.

    Items in ``secondary`` with no match in ``primary`` are appended
    (they may be rows ``primary`` missed)."""
    if not secondary:
        return primary
    sec_index: dict[tuple, dict] = {}
    for it in secondary:
        k = _row_match_key(it)
        if k is not None and k not in sec_index:
            sec_index[k] = it

    matched_keys: set = set()
    merged: list[dict] = []
    swap_count = 0
    for prim in primary:
        k = _row_match_key(prim)
        sec = sec_index.get(k) if k else None
        if sec:
            matched_keys.add(k)
            new = dict(prim)
            for f in _IDENT_FIELDS:
                p_val = prim.get(f)
                s_val = sec.get(f)
                if (
                    s_val
                    and not _is_suspicious_code(s_val)
                    and (_is_suspicious_code(p_val) or not p_val)
                ):
                    new[f] = s_val
                    swap_count += 1
            merged.append(new)
        else:
            merged.append(prim)

    appended = 0
    for it in secondary:
        k = _row_match_key(it)
        if k is None or k in matched_keys:
            continue
        merged.append(it)
        matched_keys.add(k)
        appended += 1

    if swap_count or appended:
        print(f"  [Merge] Quality merge: {swap_count} cell(s) replaced with "
              f"non-suspicious values, {appended} additional row(s) recovered")
    return merged


_LAYOUT_SYSTEM = """\
You are an invoice layout analyst. Your job is to look at one page of an
invoice and describe, in plain text, exactly what information lives in each
visual column. You do NOT extract data — you only describe structure.

Output a compact JSON object (no markdown, begin with '{', end with '}'). """

_LAYOUT_PROMPT = """\
Look at this invoice page image and the column-aligned text below.

Identify EVERY distinct column that appears in the line-item table. For each
column, state:
  • its approximate horizontal position (far-left / left / center-left /
    center / center-right / right / far-right)
  • the printed column header text (e.g. "Item #", "Qty", "Description")
  • what type of value it contains (choose one):
      barcode / vend_cat_no / buyer_item_code / line_number / description /
      order_qty / shipped_qty / unit_price / extended_price / uom /
      carrier_tracking / batch / pack_size / other

Also describe how the line-item block is laid out (is it one row per item,
or a multi-row block where sub-values appear on subsequent lines?).

Finally, answer this specific question: "How do I identify the BUYER'S ITEM
CODE on this invoice?" (The buyer item code is the purchasing system's own
short ID — NOT the vendor catalog code, NOT the barcode, NOT the carrier
tracking number.)

--- BEGIN EXTRACTED TEXT (column-aligned) ---
{aligned_text}
--- END EXTRACTED TEXT ---

Return EXACTLY this shape:
{{
  "columns": [
    {{"position": "...", "header": "...", "type": "..."}}
  ],
  "layout_style": "one-row-per-item | multi-row-block",
  "buyer_code_guide": "<plain-text description of where and how the buyer item code appears>"
}}"""


def _analyze_layout(first_page: dict, client, model: str) -> str:
    """One LLM call against the first content page to identify column structure.

    Returns a short plain-text + JSON block that is injected into every
    per-page extraction prompt so the LLM does not have to re-infer layout
    from scratch for every page.
    """
    try:
        aligned_text = _render_aligned(first_page.get("words") or [])
        if not aligned_text.strip():
            return ""

        user_content: Any
        if first_page.get("image_b64"):
            user_content = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{first_page['image_b64']}",
                        "detail": "high",
                    },
                },
                {
                    "type": "text",
                    "text": _LAYOUT_PROMPT.format(aligned_text=aligned_text[:3000]),
                },
            ]
        else:
            user_content = _LAYOUT_PROMPT.format(aligned_text=aligned_text[:3000])

        messages = [
            {"role": "system", "content": _LAYOUT_SYSTEM},
            {"role": "user",   "content": user_content},
        ]
        resp = client.chat.completions.create(
            model           = model,
            messages        = messages,
            max_tokens      = 1024,
            temperature     = 0,
            response_format = {"type": "json_object"},
            timeout         = 60,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"^```\s*",     "", raw)
        raw = re.sub(r"```$",        "", raw).strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s >= 0 and e > s:
            parsed = json.loads(raw[s: e + 1])
        else:
            return ""

        # Build a concise human-readable layout summary for the extraction prompt
        cols = parsed.get("columns") or []
        col_lines = []
        for c in cols:
            header = c.get("header") or "(unlabeled)"
            ctype  = c.get("type") or "?"
            pos    = c.get("position") or ""
            col_lines.append(f"  [{pos}] '{header}' → {ctype}")

        style       = parsed.get("layout_style") or ""
        buyer_guide = parsed.get("buyer_code_guide") or ""

        parts = ["LAYOUT ANALYSIS (derived from page 1 of this invoice):"]
        if col_lines:
            parts.append("Columns:")
            parts.extend(col_lines)
        if style:
            parts.append(f"Layout style: {style}")
        if buyer_guide:
            parts.append(f"Buyer item code location: {buyer_guide}")
        parts.append("Use this layout for every page in this invoice.")

        layout_str = "\n".join(parts)
        print(f"  [Layout] Column structure identified: {len(cols)} column(s), style={style!r}")
        return layout_str

    except Exception as e:
        print(f"  [Layout] Analysis skipped: {e}")
        return ""


def _extract_llm_per_page(pages: list, subtotal: Optional[float],
                          use_vision: bool = True,
                          csv_reference: str = "") -> list[dict]:
    hint = ""
    if subtotal:
        hint = (
            f"SUBTOTAL CHECK: This invoice's printed subtotal is "
            f"{subtotal:,.2f}. The sum of every extended_price you emit "
            f"should equal that figure (within 1¢). If your running sum is "
            f"materially short, you have missed rows — re-scan the page."
        )

    all_items: list[dict] = []
    seen: set = set()
    vision_ok       = use_vision
    # Count processable pages: text-mode needs words; vision-mode can use image alone
    n_pages = len([
        p for p in pages
        if p.get("words") or (use_vision and p.get("image_b64"))
    ])
    page1_schema:   dict[str, str] = {}
    schema_ctx_str: str = ""

    # ── Layout analysis (one call per invoice, not per page) ─────────────────
    # Before processing any page, analyse the first content page to learn
    # the column structure of THIS specific invoice. The resulting layout
    # description is injected into every per-page extraction prompt so the
    # LLM does not have to re-infer column roles from scratch each time.
    # Result is stored on the first page dict so T3 (text-only) reuses it.
    layout_context = ""
    # For scanned PDFs there may be no text layer, so fall back to any page
    # with an image as the first content page for layout analysis.
    first_content = (
        next((p for p in pages if p.get("words")), None)
        or (next((p for p in pages if p.get("image_b64")), None) if use_vision else None)
    )
    if first_content:
        if "_layout_ctx" in first_content:
            layout_context = first_content["_layout_ctx"]   # reuse T2 analysis
        else:
            try:
                _client, _model = _get_llm()
                layout_context = _analyze_layout(first_content, _client, _model)
                first_content["_layout_ctx"] = layout_context  # cache for T3
            except Exception as _le:
                print(f"  [Layout] Skipped: {_le}")

    for idx, page in enumerate(pages):
        has_text  = bool(page.get("words"))
        has_image = bool(page.get("image_b64"))
        # Skip pages with neither extractable text nor an image
        if not has_text and not (use_vision and has_image):
            continue
        pnum = page["page_num"]
        if has_text:
            page_ctx = (
                f"PDF page {pnum} of {n_pages}. Line items may continue "
                f"from previous pages. Extract ONLY rows visibly present "
                f"on THIS page."
            )
        else:
            page_ctx = (
                f"PDF page {pnum} of {n_pages} (image-only / scanned page — "
                f"no machine-readable text layer). Extract ONLY rows visibly "
                f"present on THIS page from the image."
            )
        current_schema_ctx = schema_ctx_str if idx > 0 else ""
        if SPLIT_PROMPTS:
            print(f"  [{'Vision' if vision_ok else 'Text'}] Page {pnum} ...  (two-stage: A->B)")
        else:
            print(f"  [{'Vision' if vision_ok else 'Text'}] Page {pnum} ...")

        raw_items: list[dict] = []
        used_vision_for_page = False

        if SPLIT_PROMPTS:
            # Two-stage path: structural extraction (Stage A) followed by
            # identifier classification (Stage B). The two-stage path does
            # not consume `schema_context` because Stage B's classification
            # is driven by token shape (deterministic), not by prior pages.
            raw_items = _call_llm_two_stage(
                page, hint, page_ctx, csv_reference,
                use_vision=vision_ok and bool(page.get("image_b64")),
            )
            used_vision_for_page = bool(raw_items) and vision_ok and bool(page.get("image_b64"))
            if not raw_items and not page.get("image_b64"):
                vision_ok = False
        elif vision_ok:
            # Combine schema context with layout context — layout goes first so
            # the LLM sees the column map before the field-type constraints.
            combined_ctx = "\n\n".join(filter(None, [layout_context, current_schema_ctx]))
            raw_items = _call_llm_vision(page, hint, page_ctx, combined_ctx, csv_reference)
            used_vision_for_page = bool(raw_items)
            if not raw_items and page.get("image_b64"):
                raw_items = _call_llm_extract(page.get("words") or [], hint, combined_ctx, page_ctx, csv_reference)
            elif not page.get("image_b64"):
                vision_ok = False
                raw_items = _call_llm_extract(page.get("words") or [], hint, combined_ctx, page_ctx, csv_reference)
        else:
            combined_ctx = "\n\n".join(filter(None, [layout_context, current_schema_ctx]))
            raw_items = _call_llm_extract(page.get("words") or [], hint, combined_ctx, page_ctx, csv_reference)

        expected_rows = _estimate_visible_item_rows(page.get("words") or [])
        if expected_rows >= 4 and len(raw_items) < expected_rows * 0.7:
            print(f"  [Sanity] Page {pnum}: LLM returned {len(raw_items)} items "
                  f"but page appears to have ~{expected_rows} item rows — retrying")
            retry_hint = (
                hint + f" COMPLETENESS CHECK: this page should contain about "
                f"{expected_rows} line items. Walk through the table top-to-bottom "
                "and emit every row that has a positive quantity AND price, "
                "including any rows at the very bottom of the page and any "
                "continuation rows whose quantity/price are tied to a line "
                "above. Do NOT skip rows just because the description wraps."
            ).strip()
            retry_items: list[dict] = []
            if used_vision_for_page:
                retry_items = _call_llm_extract(page.get("words") or [], retry_hint, combined_ctx, page_ctx, csv_reference)
            elif page.get("image_b64"):
                retry_items = _call_llm_vision(page, retry_hint, page_ctx, combined_ctx, csv_reference)
            if len(retry_items) > len(raw_items):
                print(f"  [Sanity] Retry recovered {len(retry_items) - len(raw_items)} additional items")
                raw_items = raw_items + retry_items

        page_items: list[dict] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item = normalize(raw)
            key  = _item_dedup_key(item)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            page_items.append(item)

        # Quality retry: if many items have suspicious identifier values
        # (descriptions leaked into catalog code fields), do a second
        # extraction attempt with a focused prompt and merge by quality.
        # Vendor names are NEVER named in the prompt — only abstract
        # archetypes — to keep the pipeline format-agnostic.
        suspicious = _suspicious_count(page_items)
        if page_items and suspicious >= max(2, len(page_items) * 0.25):
            print(f"  [Quality] Page {pnum}: {suspicious}/{len(page_items)} items have "
                  f"description-like values in identifier fields — retrying with focused prompt")
            quality_hint = (
                hint + " IDENTIFIER QUALITY CHECK: the previous attempt placed "
                "product-description text into identifier fields (item / "
                "vend_cat_no / reference_number). Identifier values are SHORT "
                "(≤16 characters), almost always contain digits, rarely contain "
                "spaces, and never read like a product name. Multi-word uppercase "
                "phrases that read like a brand or product family are descriptions "
                "— place them in `description`, not in any identifier field. "
                "BUT do not prematurely null real identifier values: short numeric "
                "codes (4–8 digits) printed inside the line-item block — even if "
                "labeled 'Tracking number', 'Tracking #', or similar — are the "
                "buyer's item code (→ `item`), not descriptions and not real "
                "carrier tracking numbers. Real tracking numbers always have a "
                "carrier name (FedEx / UPS / DHL / USPS / OnTrac / LaserShip) or "
                "are clearly long carrier references. "
                "If TRULY only one identifier-style column is present and you "
                "see no buyer item code anywhere in the line block, prefer "
                "`vend_cat_no` and leave `item` and `reference_number` null."
            ).strip()
            quality_raw: list[dict] = []
            if page.get("image_b64"):
                quality_raw = _call_llm_vision(page, quality_hint, page_ctx, combined_ctx, csv_reference)
            else:
                quality_raw = _call_llm_extract(page.get("words") or [], quality_hint, combined_ctx, page_ctx, csv_reference)
            quality_items = []
            for raw in quality_raw:
                if not isinstance(raw, dict):
                    continue
                quality_items.append(normalize(raw))
            page_items = _merge_by_quality(page_items, quality_items)
            for it in page_items:
                k = _item_dedup_key(it)
                if k:
                    seen.add(k)

        for it in page_items:
            it["_page"] = pnum
        all_items.extend(page_items)

        if idx == 0 and page_items:
            page1_schema   = _build_field_schema(page_items)
            schema_ctx_str = _schema_context_block(page1_schema, page_items)
            if page1_schema:
                print(f"  [Schema] Page-1 field schema: {page1_schema}")

    # Recovery always uses vision LLM calls (images from `pages`) regardless
    # of whether the main extraction used vision or text-only mode.
    if all_items and any(p.get("image_b64") for p in pages):
        all_items = _recover_missing_item_codes(all_items, pages,
                                                csv_reference=csv_reference,
                                                layout_context=layout_context)

    if all_items:
        all_items = _fix_misplaced_barcodes(all_items)
        all_items = _reclassify_tracking_vs_item(all_items)

    effective_schema: dict[str, str] = {}
    if all_items and len(pages) > 1:
        final_schema = _build_field_schema(all_items, min_confidence=0.7)
        if final_schema:
            if final_schema != page1_schema:
                print(f"  [Schema] Final schema (voted across all pages): {final_schema}")
            all_items = _normalize_field_assignments(all_items, final_schema)
            print(f"  [Normalize] Post-extraction normalization applied (schema: {final_schema})")
            effective_schema = final_schema
        else:
            print("  [Normalize] Skipped — no field met confidence threshold (real-world variance)")
    if not effective_schema:
        effective_schema = page1_schema or {}

    if all_items:
        all_items, dedup_count = _dedup_target_fields(all_items, effective_schema)
        if dedup_count:
            print(f"  [Dedup] Removed {dedup_count} duplicate cross-field values "
                  f"(LLM filled multiple schema fields from a single invoice column)")

    if all_items:
        nullified = 0
        for it in all_items:
            for f in _IDENT_FIELDS:
                if _is_suspicious_code(it.get(f)):
                    it.setdefault("_warnings", []).append(
                        f"{f}={it[f]!r} looks like a description, not a code; nullified"
                    )
                    it[f] = None
                    nullified += 1
        if nullified:
            print(f"  [Cleanup] Nullified {nullified} description-like value(s) in "
                  f"identifier fields after retry+merge could not recover them")

    for it in all_items:
        it.pop("_page", None)

    return all_items


# ── Verification ──────────────────────────────────────────────────────────────

def verify(items: list, subtotal: Optional[float]) -> dict:
    math_issues: list[dict] = []
    total = 0.0
    for item in items:
        ep  = item.get("extended_price")
        up  = item.get("unit_price")
        qty = item.get("trns_qty") or item.get("order_qty")
        if ep is not None:
            total += ep
        if ep is not None and up is not None and qty is not None and qty > 0:
            expected = round(qty * up, 2)
            actual   = round(ep, 2)
            if abs(expected - actual) > 0.05:
                math_issues.append({
                    "id":       item.get("line_number")
                                or item.get("vend_cat_no")
                                or item.get("reference_number"),
                    "computed": expected,
                    "invoice":  actual,
                    "diff":     round(actual - expected, 4),
                })
    total = round(total, 2)
    diff  = round(total - subtotal, 2) if subtotal else None
    match = diff is not None and abs(diff) <= 1.00
    return {
        "extracted_total":   total,
        "detected_subtotal": subtotal,
        "subtotal_match":    match,
        "diff":              diff,
        "line_count":        len(items),
        "math_issues":       math_issues,
        "status":            "PASS" if (match and not math_issues) else
                             "WARN" if match else "FAIL",
    }


# ── Tier 1: deterministic table extraction with Camelot ──────────────────────
#
# Camelot is fast & deterministic but only succeeds on invoices that have a
# well-defined tabular structure (clear column boundaries, consistent rows).
# We gate every result behind safety checks so that a half-extracted table can
# never poison downstream mapping — failure simply falls through to T2-vision.

# Header keyword → canonical-field heuristic for Camelot DataFrame headers.
# Patterns are case-insensitive substrings / regex fragments.
_CAMELOT_HEADER_PATTERNS: list[tuple[str, list[str]]] = [
    ("line_number",      [r"^\s*line", r"line\s*#", r"line\s*no", r"^\s*no\.?\s*$",
                          r"^\s*#\s*$", r"item\s*line"]),
    ("vend_cat_no",      [r"vendor\s*item", r"vend\s*cat", r"cat(alog)?\s*#?",
                          r"part\s*#?", r"product\s*code"]),
    ("item",             [r"^\s*item\s*#?\s*$", r"item\s*number", r"item\s*code",
                          r"sku", r"bltr.*itm", r"buyer.*item"]),
    ("reference_number", [r"\bupc\b", r"barcode", r"\bref(erence)?\b",
                          r"customer.*ref"]),
    ("description",      [r"descr", r"product\s*name", r"^\s*name\s*$",
                          r"item\s*name"]),
    ("order_qty",        [r"order(ed)?\s*qty", r"qty\s*ord", r"ordered"]),
    ("trns_qty",         [r"ship(ped)?\s*qty", r"invoice(d)?\s*qty",
                          r"qty\s*ship", r"shipped", r"^\s*qty\s*$",
                          r"^\s*quantity\s*$"]),
    ("inv_uom",          [r"^\s*uom\s*$", r"u/m", r"^\s*unit\s*$",
                          r"^\s*pkg\s*$", r"pack\s*type"]),
    ("unit_price",       [r"unit\s*price", r"price/?\s*unit", r"price\s*each",
                          r"^\s*each\s*$", r"^\s*price\s*$"]),
    ("extended_price",   [r"ext(ended)?\s*price", r"line\s*total",
                          r"^\s*total\s*$", r"^\s*amount\s*$", r"net\s*amount"]),
    ("pack_size",        [r"pack\s*size", r"^\s*pack\s*$"]),
]


def _camelot_map_header(header_cells: list[str]) -> dict[int, str]:
    """Map Camelot column indices → canonical schema field names by header text."""
    mapping: dict[int, str] = {}
    used: set[str] = set()
    cells = [str(c or "").strip().lower() for c in header_cells]
    for field, patterns in _CAMELOT_HEADER_PATTERNS:
        if field in used:
            continue
        for idx, cell in enumerate(cells):
            if not cell or idx in mapping:
                continue
            if any(re.search(p, cell, re.IGNORECASE) for p in patterns):
                mapping[idx] = field
                used.add(field)
                break
    return mapping


def _camelot_find_header_row(df) -> int:
    """Scan the first few rows of a Camelot DataFrame for the most field-like
    header row. Returns row index, or -1 if no plausible header found."""
    best_idx, best_score = -1, 0
    for idx in range(min(4, len(df))):
        row = list(df.iloc[idx])
        col_map = _camelot_map_header(row)
        score = len(col_map)
        # Bonus if a price column is present — distinguishes line-item tables
        # from header / address blocks.
        if any(v in {"unit_price", "extended_price"} for v in col_map.values()):
            score += 2
        if score > best_score:
            best_score, best_idx = score, idx
    return best_idx if best_score >= 3 else -1


def _camelot_table_to_items(df, col_map: dict[int, str]) -> list[dict]:
    """Convert a Camelot DataFrame (post-header) into canonical-schema items."""
    items: list[dict] = []
    for _, row in df.iterrows():
        cells = [str(c or "").strip() for c in row]
        # Skip rows that look like totals / subtotals / spacers
        joined = " ".join(cells).lower()
        if not any(cells):
            continue
        if re.search(r"\b(sub\s*total|grand\s*total|total\s*due|balance\s*due"
                     r"|freight|shipping|tax|page\s+\d+\s+of)\b", joined):
            continue

        item = {f: None for f in OUTPUT_FIELDS}
        for idx, field in col_map.items():
            if idx >= len(cells):
                continue
            val = cells[idx]
            if not val:
                continue
            if field in _NUMERIC:
                item[field] = parse_num(val)
            else:
                item[field] = val

        # Sanity: must have a price OR (qty AND identifier) to count as a line
        has_price = (item.get("extended_price") is not None
                     or item.get("unit_price") is not None)
        has_qty   = (item.get("order_qty") is not None
                     or item.get("trns_qty") is not None)
        has_id    = any(item.get(f) for f in ("item", "vend_cat_no",
                                              "reference_number", "description"))
        if not (has_price and (has_qty or has_id)):
            continue
        items.append(item)
    return items


def _extract_camelot_t1(pdf_path: str,
                        subtotal: Optional[float]) -> Optional[list[dict]]:
    """Tier 1: deterministic table extraction with Camelot.

    Tries lattice flavor (needs Ghostscript) then stream flavor.
    Returns canonical-schema items only when:
      • A line-items table is found with a price column,
      • Row sanity passes (>=3 plausible items),
      • Computed total is within 2% of detected invoice subtotal (when known).

    Returns ``None`` on any failure — caller falls through to T2-vision.
    """
    if not CAMELOT_OK:
        return None

    last_reason = "no candidate tables"
    for flavor in ("lattice", "stream"):
        try:
            tables = camelot.read_pdf(pdf_path, pages="all", flavor=flavor,
                                      suppress_stdout=True)
        except Exception as e:
            msg = str(e).lower()
            if flavor == "lattice" and ("ghostscript" in msg or "gs" in msg
                                        or "image conversion" in msg):
                print(f"  [T1-camelot] lattice unavailable (no Ghostscript) "
                      f"— trying stream")
                continue
            print(f"  [T1-camelot] {flavor} flavor failed: {type(e).__name__}: {e}")
            continue

        if tables is None or len(tables) == 0:
            last_reason = f"{flavor}: 0 tables"
            continue

        all_items: list[dict] = []
        accepted_tables = 0
        for ti, table in enumerate(tables):
            try:
                acc = float(getattr(table, "accuracy", 0) or 0)
            except Exception:
                acc = 0.0
            df = table.df
            if df is None or df.shape[0] < 2 or df.shape[1] < 3:
                continue
            if acc < 70:
                continue

            hdr_idx = _camelot_find_header_row(df)
            if hdr_idx < 0:
                continue
            col_map = _camelot_map_header(list(df.iloc[hdr_idx]))
            if not any(v in {"unit_price", "extended_price"}
                       for v in col_map.values()):
                continue

            items = _camelot_table_to_items(df.iloc[hdr_idx + 1:], col_map)
            if items:
                all_items.extend(items)
                accepted_tables += 1
                print(f"  [T1-camelot] {flavor} table {ti+1}: "
                      f"acc={acc:.0f} cols={len(col_map)} rows={len(items)}")

        if len(all_items) < 3:
            last_reason = f"{flavor}: only {len(all_items)} items"
            continue

        if subtotal:
            total = sum((i.get("extended_price") or 0) for i in all_items)
            diff_pct = abs(total - subtotal) / subtotal if subtotal else 1.0
            if diff_pct > 0.02:
                print(f"  [T1-camelot] {flavor} extracted {len(all_items)} items "
                      f"but total ${total:,.2f} differs from subtotal "
                      f"${subtotal:,.2f} ({diff_pct:.1%}) — falling through to T2")
                last_reason = f"{flavor}: subtotal off by {diff_pct:.1%}"
                continue
            print(f"  [T1-camelot] {flavor} PASS: {len(all_items)} items, "
                  f"total ${total:,.2f} matches subtotal ${subtotal:,.2f}")
        else:
            print(f"  [T1-camelot] {flavor} extracted {len(all_items)} items "
                  f"(no subtotal to verify against)")

        return all_items

    print(f"  [T1-camelot] No flavor succeeded ({last_reason}) — using T2-vision")
    return None


# ── PO-number extraction from PDF ─────────────────────────────────────────────

# Ordered from most to least specific; each group captures the PO value.
# Broader aliases (FO No, Cust PO, Your PO, etc.) are included so that
# vendors who use non-standard labels are still handled.
_PO_PATTERNS = [
    # Standard labels
    r'Purchase\s+Order\s*(?:Number|No\.?|#)?\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})',
    r'P\.?O\.?\s*(?:Number|No\.?|#)\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})',
    r'\bPO\s*[:\-#]\s*([A-Z0-9][-A-Z0-9]{2,20})',
    r'Purchase\s+Order\s*[:\-]?\s+([A-Z0-9][-A-Z0-9]{2,20})',
    # Vendor-specific / alternative labels
    r'\bFO\s+No\.?\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})',           # FO No: N335512
    r'Customer\s+(?:P\.?O\.?|Order)\s*(?:No\.?|#)?\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})',
    r'(?:Cust(?:omer)?\.?\s+)?P\.?O\.?\s*[:\-#]\s*([A-Z0-9][-A-Z0-9]{2,20})',
    r'(?:Your|Buyer[\'s]*)\s+(?:P\.?O\.?|Order)\s*(?:No\.?|#)?\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})',
    # Generic "Order No" — lower priority because it often matches internal order IDs
    r'Order\s+(?:Number|No\.?|#)\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})',
]


def _extract_po_from_text(text: str) -> Optional[str]:
    """
    Return the best PO number found in *text* using regex, or None.

    Collects ALL pattern matches then prefers candidates that start with a
    letter (e.g. N335512) over purely-numeric strings (e.g. 1637737), since
    buyer PO numbers in this domain typically begin with a letter.
    """
    candidates: list[str] = []
    for pat in _PO_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            candidates.append(m.group(1).strip())

    if not candidates:
        return None

    # Prefer values that start with a letter (alphanumeric POs like N335512).
    alpha_first = [c for c in candidates if c and c[0].isalpha()]
    return alpha_first[0] if alpha_first else candidates[0]


def _extract_po_number(pdf_path: str) -> Optional[str]:
    """
    Extract the PO number from the PDF itself.

    Strategy (in priority order):
      1. Regex scan of the first two pages' raw text  (fast, no LLM).
      2. Single LLM call on the first-page text       (if regex fails).
      3. Returns None — caller decides how to handle a missing PO.
    """
    text = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for p in pdf.pages[:2]:
                text += (p.extract_text() or "") + "\n"
    except Exception:
        pass

    # 1. Regex
    po = _extract_po_from_text(text)
    if po:
        print(f"  [PO] Extracted from PDF text (regex): {po!r}")
        return po

    # 2. LLM fallback
    if text.strip():
        try:
            resp = _openai_client().chat.completions.create(
                model=_model(),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a document parser. Extract ONLY the purchase order number "
                            "(also labelled PO Number, PO#, Purchase Order, or Order Number) "
                            "from the invoice text below. "
                            "Return ONLY the alphanumeric PO number string with no extra text. "
                            "If you cannot find one, return UNKNOWN."
                        ),
                    },
                    {"role": "user", "content": text[:3000]},
                ],
                max_tokens=20,
                temperature=0,
            )
            candidate = resp.choices[0].message.content.strip()
            if candidate and candidate.upper() != "UNKNOWN":
                print(f"  [PO] Extracted from PDF text (LLM): {candidate!r}")
                return candidate
        except Exception as exc:
            print(f"  [PO] LLM extraction failed: {exc}")

    print("  [PO] Could not extract PO number from PDF.")
    return None


# ── Main extraction pipeline ──────────────────────────────────────────────────

def extract_invoice(pdf_path: str, use_vision: bool = True,
                    vision_dpi: Optional[int] = None,
                    csv_samples: Optional[list[dict]] = None,
                    csv_columns: Optional[list[str]] = None) -> dict:
    print(f"\n{'=' * 64}")
    print(f"  {Path(pdf_path).name}")
    print(f"{'=' * 64}")

    pages    = load_pdf(pdf_path, vision_dpi=vision_dpi)
    subtotal = detect_subtotal(pages)
    has_imgs = any(p.get("image_b64") for p in pages)
    dpi_used = _vision_dpi(vision_dpi)
    print(f"  Vision DPI: {dpi_used}  (page images ready: {has_imgs})")

    csv_reference = _csv_reference_block(csv_samples, csv_columns)
    if csv_reference:
        print(f"  [CSV-Ref] Sharing {len(csv_samples or [])} CSV sample row(s) "
              f"with the LLM as pattern reference")

    # Tier 1: deterministic Camelot extraction. Always runs (when available)
    # and is added as a CANDIDATE alongside T2/T3, never an early exit. This
    # guarantees the LLM tier always runs as a cross-check, so a Camelot
    # PASS that misses a non-tabular line (e.g. a freight row, or a row
    # parsed across two PDF tables) is not silently chosen over a more
    # complete LLM extraction. The best candidate by subtotal-diff wins.
    items_t1: list[dict] = []
    if CAMELOT_OK:
        print("  [T1-camelot] Attempting deterministic table extraction ...")
        items_t1 = _extract_camelot_t1(pdf_path, subtotal) or []
        if items_t1:
            v1_pre = verify(items_t1, subtotal)
            print(f"  [T1-camelot] verify={v1_pre['status']} "
                  f"(items={v1_pre['line_count']}, diff={v1_pre['diff']}) "
                  f"— continuing to T2 for cross-check")
    else:
        print("  [T1-camelot] camelot-py not installed — skipping")

    items_vision: list[dict] = []
    items_text:   list[dict] = []

    if use_vision and has_imgs:
        print("  [T2-vision] Per-page multimodal extraction ...")
        items_vision = _extract_llm_per_page(pages, subtotal, use_vision=True,
                                             csv_reference=csv_reference)

    if not (use_vision and has_imgs):
        print("  [T3-text] Per-page text extraction ...")
        items_text = _extract_llm_per_page(pages, subtotal, use_vision=False,
                                           csv_reference=csv_reference)
    elif not items_vision:
        # Vision ran but returned zero items — run text tier as fallback
        print("  [T3-text] Vision returned 0 items — falling back to text-only ...")
        items_text = _extract_llm_per_page(pages, subtotal, use_vision=False,
                                           csv_reference=csv_reference)
    else:
        v_vision = verify(items_vision, subtotal)
        if v_vision["status"] != "PASS" and subtotal:
            diff_pct = abs(v_vision["diff"] or 0) / subtotal
            print(f"  [T3-text] Vision non-PASS ({diff_pct:.2%} off) — comparing with text-only ...")
            items_text = _extract_llm_per_page(pages, subtotal, use_vision=False,
                                               csv_reference=csv_reference)

    candidates = []
    if items_t1:
        candidates.append(("T1-camelot", items_t1,     verify(items_t1,     subtotal)))
    if items_vision:
        candidates.append(("T2-vision",  items_vision, verify(items_vision, subtotal)))
    if items_text:
        candidates.append(("T3-text",    items_text,   verify(items_text,   subtotal)))

    if not candidates:
        items, tier, verification = [], "none", verify([], subtotal)
    elif len(candidates) == 1:
        tier, items, verification = candidates[0]
    else:
        def _identifier_coverage(item_list: list[dict]) -> int:
            """Count items with both 'item' and 'vend_cat_no' populated.

            Used as a tiebreaker when multiple tiers have the same subtotal
            diff: the tier whose items have the most identifier fields filled
            in (not null/sentinel) is preferred over a purely deterministic
            result that got the math right but put identifiers in the wrong
            schema fields (e.g. Camelot dropping buyer-item-codes on
            multi-row-block Covidien invoices).
            """
            return sum(
                1 for i in item_list
                if not _is_null_str(i.get("item"))
                and not _is_null_str(i.get("vend_cat_no"))
            )

        def _sort_key(candidate):
            tier_name, item_list, vfy = candidate
            diff_score = abs(vfy["diff"] or 0) if vfy["diff"] is not None else 1e9
            # Negate coverage so that higher coverage sorts lower (min wins)
            coverage_score = -_identifier_coverage(item_list)
            return (diff_score, coverage_score)

        candidates_sorted = sorted(candidates, key=_sort_key)
        best_tier, best_items, best_v = candidates_sorted[0]
        tier, items, verification = best_tier, best_items, best_v
        for t, itms, vv in candidates:
            cov = _identifier_coverage(itms)
            print(f"  [compare] {t}: items={vv['line_count']} diff={vv['diff']} "
                  f"id_coverage={cov}/{vv['line_count']} status={vv['status']}")
        print(f"  [compare] Selected: {tier}")

    before = len(items)
    items  = [i for i in items if not _is_hallucination(i)]
    removed = before - len(items)
    if removed:
        print(f"  [filter] Removed {removed} clearly-hallucinated items (matched hallucination patterns)")

    # Remove outlier unit-price rows — items whose unit_price is more than 3×
    # the 90th-percentile unit price across all other items on the invoice.
    # This catches cases where a page subtotal or running total is accidentally
    # read as a line item's unit price (e.g., a $1,229 "unit price" when all
    # other items cost $90–$440). The filter only fires when there are 5+ items
    # so it cannot accidentally fire on small invoices.
    unit_prices = sorted(
        [i["unit_price"] for i in items
         if isinstance(i.get("unit_price"), (int, float)) and i["unit_price"] > 0]
    )
    if len(unit_prices) >= 5:
        p90 = unit_prices[int(len(unit_prices) * 0.90)]
        threshold = max(p90 * 3.0, 1000.0)  # never trim below $1000/unit
        outliers = [i for i in items
                    if isinstance(i.get("unit_price"), (int, float))
                    and i["unit_price"] > threshold]
        if outliers:
            print(f"  [filter] Removing {len(outliers)} outlier-price item(s) "
                  f"(unit_price > {threshold:.0f}; likely a misread page total)")
            items = [i for i in items if i not in outliers]

    if len(items) != before:
        verification = verify(items, subtotal)

    v = verification
    print(
        f"\n  -- Result ({tier}) ----------------------------------------\n"
        f"  Status      : {v['status']}\n"
        f"  Items       : {v['line_count']}\n"
        f"  Extracted $ : {v['extracted_total']}\n"
        f"  Subtotal    : {v['detected_subtotal']}\n"
        f"  Diff        : {v['diff']}"
    )
    if v["math_issues"]:
        print(f"  Math issues : {len(v['math_issues'])}")
        for iss in v["math_issues"][:5]:
            print(f"    {iss}")

    po_number = _extract_po_number(pdf_path)
    return {
        "source": str(pdf_path), "po_number": po_number,
        "tier": tier, "line_items": items, "verification": verification,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAPPING ENGINE  (from invoice_csv_mapper.py)
# ═══════════════════════════════════════════════════════════════════════════════

def load_csv(csv_path: str):
    """Load a CSV or Excel file into a DataFrame. Detects format by extension."""
    if not PANDAS_OK:
        raise RuntimeError("pip install pandas openpyxl")
    ext = Path(csv_path).suffix.lower()
    if ext in (".xlsx", ".xls", ".xlsm", ".xlsb"):
        return pd.read_excel(csv_path, dtype=str).fillna("")
    return pd.read_csv(csv_path, dtype=str).fillna("")


def csv_sample(df, po_number: str, n: int = 10):
    col  = df.columns[0]
    rows = df[df[col].str.strip() == po_number.strip()]
    return rows.head(n)


# Note: an earlier version of this pipeline used an LLM to map invoice
# fields → CSV columns. That mapper is now `discover_mapping` (pure Python,
# value-overlap based) and the LLM mapping prompt was removed because it
# was unused dead code.

def _is_buyer_code_col(sample_rows: list[dict], col: str,
                       inv_field_values: dict[str, list[str]]) -> bool:
    """A buyer-internal code column has purely numeric, multi-digit values
    that do NOT appear on the vendor invoice. We require the invoice to
    contain numerics of *similar length* before assuming the buyer code
    is on the invoice — long barcodes (14 digits) are not the same thing
    as a 6-digit buyer SKU just because both are numeric."""
    name_lower = col.lower()
    skip_keywords = {"line", "qty", "quantity", "price", "amount", "uom", "order", "unit"}
    if any(kw in name_lower for kw in skip_keywords):
        return False
    vals = [str(r.get(col, "")).strip() for r in sample_rows if str(r.get(col, "")).strip()]
    if not vals:
        return False
    if not all(v.isdigit() for v in vals):
        return False
    avg_len = sum(len(v) for v in vals) / len(vals)
    if avg_len < 5:
        return False
    for field_vals in inv_field_values.values():
        for fv in field_vals:
            if fv.isdigit() and abs(len(fv) - avg_len) <= 2:
                return False
    return True


def _value_format(v: str) -> str:
    v = str(v).strip()
    if not v or v.lower() in ("none", "null", ""):
        return "empty"
    if v.replace(".", "").isdigit():
        return "decimal" if "." in v else "numeric"
    if re.match(r"^[A-Za-z0-9\-]+$", v):
        return "alphanum_dash"
    return "other"


def _formats_compatible(actual: str, expected: str) -> bool:
    """Check whether two ``_value_format`` results should be treated as
    interchangeable. ``numeric`` and ``decimal`` strings are valid
    instances of ``alphanum_dash`` (which accepts letters, digits, and
    hyphens), so a column whose majority is alphanumeric should still
    accept a purely numeric value without nullifying it."""
    if actual == expected:
        return True
    if expected == "alphanum_dash" and actual in ("numeric", "decimal"):
        return True
    if expected == "numeric" and actual == "decimal":
        return True
    return False


def _collect_inv_field_values(sample_items: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in sample_items:
        for field, val in item.items():
            if val is not None:
                out.setdefault(field, []).append(str(val).strip())
    return out


def _validate_mapping(
    mapping: dict[str, Optional[str]],
    sample_csv_rows: list[dict],
    sample_invoice_items: list[dict],
    csv_columns: list[str],
) -> dict[str, Optional[str]]:
    corrected = dict(mapping)
    inv_values = _collect_inv_field_values(sample_invoice_items)

    def used_fields() -> set:
        return {v for v in corrected.values() if v and v != "PO_NUMBER"}

    def _norm_cmp(v: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", str(v)).lower()

    def _matches_exact(csv_val: str, inv_vals: list[str]) -> bool:
        nv = _norm_cmp(csv_val)
        if not nv:
            return False
        for iv in inv_vals:
            if csv_val == iv:
                return True
            niv = _norm_cmp(iv)
            if not niv:
                continue
            if nv == niv:
                return True
            if len(nv) >= 6 and (nv in niv or niv.startswith(nv)):
                return True
        return False

    def _majority_format(vals: list[str]) -> str:
        if not vals:
            return "other"
        fmts = [_value_format(v) for v in vals if v]
        return Counter(fmts).most_common(1)[0][0] if fmts else "other"

    # Returns True when the CSV column name is a recognisable semantic label
    # for `field` — e.g. "Item" ↔ "item", "Vend Cat No" ↔ "vend_cat_no".
    # Used to protect clearly-named columns from being re-mapped by accidental
    # value overlap to an unrelated field.
    _SEMANTIC_RULES: dict[str, list[list[str]]] = {
        "item":        [["item"]],
        "line_number": [["line"]],
        "order_qty":   [["qty"], ["quantity"]],
        "unit_price":  [["price"]],
        "inv_uom":     [["uom"]],
        "vend_cat_no": [["vend", "cat"], ["vend"], ["catalog"]],
        "PO_NUMBER":   [["order", "number"]],
    }

    def _is_semantic_col(col_lower: str, field: str) -> bool:
        for kws in _SEMANTIC_RULES.get(field, []):
            if len(kws) == 1:
                if kws[0] in col_lower:
                    return True
            else:
                if all(kw in col_lower for kw in kws):
                    return True
        return False

    # Rule 0: semantic override for line-number columns
    for col in csv_columns:
        name_lower  = col.lower()
        mapped_field = corrected.get(col)
        if not mapped_field or mapped_field == "PO_NUMBER":
            continue
        if "line" in name_lower and "line_number" in inv_values:
            if mapped_field != "line_number" and "line_number" not in used_fields():
                corrected[col] = "line_number"

    # Tracks columns that were deliberately nulled because their semantically-
    # matched field has no extracted values.  Rule 3 must not resurrect these
    # through coincidental value overlap with a different field.
    intentionally_null: set[str] = set()

    # Rules 1 + 2
    for col in csv_columns:
        mapped_field = corrected.get(col)
        if not mapped_field or mapped_field == "PO_NUMBER":
            continue
        name_lower = col.lower()
        if any(kw in name_lower for kw in {"qty", "quantity", "price", "amount", "unit", "uom"}):
            continue
        csv_vals = [str(r.get(col, "")).strip() for r in sample_csv_rows
                    if str(r.get(col, "")).strip()]
        if not csv_vals:
            continue

        if _is_buyer_code_col(sample_csv_rows, col, inv_values):
            corrected[col] = None
            intentionally_null.add(col)
            continue

        current_inv_vals = inv_values.get(mapped_field, [])

        # If the mapped field has NO extracted values at all, the invoice
        # simply doesn't carry this field.  Do NOT remap the CSV column to
        # a different extracted field just because its values happen to
        # overlap — that produces the "Vend Cat No → item" false mapping.
        if not current_inv_vals:
            corrected[col] = None
            intentionally_null.add(col)
            continue

        if any(_matches_exact(v, current_inv_vals) for v in csv_vals):
            continue

        # If the column name is a recognised semantic label for the mapped
        # field (e.g. CSV "Item" → extracted `item`, CSV "Vend Cat No" →
        # extracted `vend_cat_no`), trust the semantic assignment even when
        # the CSV sample values don't exactly match the extracted values.
        # This prevents "Item" from being silently remapped to null just
        # because the buyer's internal item codes differ from the vendor's.
        if _is_semantic_col(name_lower, mapped_field):
            continue

        best_exact = None
        for field, fvals in inv_values.items():
            if field == mapped_field or field in used_fields():
                continue
            if any(_matches_exact(v, fvals) for v in csv_vals):
                best_exact = field
                break
        if best_exact:
            corrected[col] = best_exact
            continue

        # Note: format-only fallback was deliberately removed. Numeric vs
        # numeric is not enough evidence to remap a column — e.g. a 6-digit
        # buyer SKU column is "numeric" and so are 14-digit barcodes, but
        # they are not the same field.
        corrected[col] = None
        intentionally_null.add(col)

    # Rule 3: null resurrection by EXACT VALUE EVIDENCE only.
    # We do not resurrect on format match alone; a CSV column staying null
    # is far better than being silently mis-mapped to a same-format field
    # (e.g. CSV's `Item` 6-digit codes <-> invoice's 14-digit barcodes in
    # `reference_number`).
    for col in csv_columns:
        if corrected.get(col) is not None or col in intentionally_null:
            continue
        name_lower = col.lower()
        if any(kw in name_lower for kw in {"qty", "quantity", "price", "amount", "unit", "uom", "order"}):
            continue
        csv_vals = [str(r.get(col, "")).strip() for r in sample_csv_rows
                    if str(r.get(col, "")).strip()]
        if not csv_vals:
            continue

        best_exact = None
        for field, fvals in inv_values.items():
            if field in used_fields():
                continue
            if any(_matches_exact(v, fvals) for v in csv_vals):
                best_exact = field
                break
        if best_exact:
            corrected[col] = best_exact

    return corrected


def discover_mapping(
    csv_columns: list[str],
    sample_csv_rows: list[dict],
    sample_invoice_items: list[dict],
) -> dict[str, Optional[str]]:
    inv_values = _collect_inv_field_values(sample_invoice_items)
    corrected: dict[str, Optional[str]] = {}

    def used_set() -> set:
        return {v for v in corrected.values() if v and v != "PO_NUMBER"}

    _SEMANTIC = [
        (["order", "number"],  "PO_NUMBER"),
        (["line"],             "line_number"),
        (["qty"],              "order_qty"),
        (["quantity"],         "order_qty"),
        (["price"],            "unit_price"),
        (["uom"],              "inv_uom"),
        # Identifier-field name hints (lower priority — evaluated last so they
        # don't override PO/qty/price columns that happen to contain these words)
        (["item"],             "item"),           # "Item", "Item #", "Item No"
        (["vend", "cat"],      "vend_cat_no"),   # "Vend Cat No", "Vendor Catalog"
        (["vend"],             "vend_cat_no"),   # "Vendor #", "Vend No"
        (["catalog"],          "vend_cat_no"),   # "Catalog #", "Cat No"
    ]
    for col in csv_columns:
        name_lower = col.lower()
        for keywords, field in _SEMANTIC:
            if keywords == ["order", "number"]:
                matches = all(kw in name_lower for kw in keywords)
            elif len(keywords) > 1 and keywords[0] not in ("order",):
                # Require ALL words for multi-keyword identifier rules
                matches = all(kw in name_lower for kw in keywords)
            else:
                matches = any(kw in name_lower for kw in keywords)
            if matches and field not in used_set():
                corrected[col] = field
                break

    MIN_MATCHES  = 3
    MIN_FRACTION = 0.30

    def _norm(v: str) -> str:
        """Normalize a value for fuzzy comparison: strip non-alphanumerics
        and lowercase. Lets us recognize equivalent codes that differ only
        in formatting, e.g. '100-0288' vs '1000288'."""
        return re.sub(r"[^A-Za-z0-9]", "", str(v)).lower()

    remaining_cols = [c for c in csv_columns if c not in corrected]
    col_vals: dict[str, list[str]] = {}
    col_norm: dict[str, set[str]]   = {}
    col_fmts: dict[str, str] = {}
    for col in remaining_cols:
        vals = [
            str(r.get(col, "")).strip()
            for r in sample_csv_rows if str(r.get(col, "")).strip()
        ]
        col_vals[col] = vals
        col_norm[col] = {_norm(v) for v in vals if _norm(v)}
        col_fmts[col] = (Counter(_value_format(v) for v in vals).most_common(1)[0][0]
                         if vals else "other")

    inv_norm: dict[str, set[str]] = {}
    inv_fmts: dict[str, str] = {}
    for field, fvals in inv_values.items():
        if fvals:
            inv_norm[field] = {_norm(v) for v in fvals if _norm(v)}
            inv_fmts[field] = Counter(_value_format(v) for v in fvals).most_common(1)[0][0]

    scores: dict[tuple[str, str], int] = {}
    rejected: list[str] = []
    for col in remaining_cols:
        csv_norm_set = col_norm.get(col, set())
        if not csv_norm_set:
            continue
        csv_fmt = col_fmts.get(col, "other")
        for field, fvals in inv_values.items():
            if field in used_set():
                continue
            inv_norm_set = inv_norm.get(field, set())
            if not inv_norm_set:
                continue
            overlap = len(csv_norm_set & inv_norm_set)
            if overlap == 0:
                continue
            fraction = overlap / len(csv_norm_set)
            inv_fmt  = inv_fmts.get(field, "other")
            fmt_compat = (
                csv_fmt == "other" or inv_fmt == "other" or csv_fmt == inv_fmt
                or {csv_fmt, inv_fmt} == {"numeric", "decimal"}
                or {csv_fmt, inv_fmt} == {"numeric", "alphanum_dash"}
            )
            if not fmt_compat:
                rejected.append(f"'{col}'<->'{field}' overlap={overlap} "
                                f"but fmt {csv_fmt}\u2260{inv_fmt}")
                continue
            if overlap >= MIN_MATCHES or fraction >= MIN_FRACTION:
                scores[(col, field)] = overlap
            else:
                rejected.append(f"'{col}'<->'{field}' overlap={overlap}/{len(csv_norm_set)} "
                                f"below threshold ({MIN_MATCHES} or {MIN_FRACTION:.0%})")

    if scores:
        print(f"  [Mapper] Overlap scores: "
              + ", ".join(f"'{c}'->'{f}'={s}"
                          for (c, f), s in sorted(scores.items(), key=lambda x: -x[1])))
    if rejected:
        for r in rejected[:8]:
            print(f"  [Mapper] Rejected: {r}")

    for (col, field), _ in sorted(scores.items(), key=lambda x: -x[1]):
        if col in corrected or field in used_set():
            continue
        print(f"  [Mapper] Value-overlap: '{col}' -> '{field}' "
              f"({scores[(col, field)]} exact matches)")
        corrected[col] = field

    # ── Compound-code fallback ────────────────────────────────────────────
    # When the LLM splits a single compound catalog code across two
    # identifier fields (for example "13165-02-IZAACX" → item="13165" +
    # vend_cat_no="02-IZAACX"), no single invoice field matches the CSV
    # column. Try concatenating pairs of identifier fields (in both
    # orders, with and without a separator) and see if any concatenation
    # matches the CSV values. This is data-driven (uses CSV evidence)
    # so it stays vendor-agnostic.
    IDENT = ["item", "vend_cat_no", "reference_number"]
    item_pairs: list[tuple[str, str]] = []
    for inv_items in [sample_invoice_items]:
        for it in inv_items:
            row_pair = {}
            for f in IDENT:
                v = str(it.get(f) or "").strip()
                if v:
                    row_pair[f] = v
            if len(row_pair) >= 2:
                item_pairs.append(tuple(row_pair.items()))
        break

    if item_pairs:
        for col in remaining_cols:
            if col in corrected:
                continue
            csv_norm_set = col_norm.get(col, set())
            if not csv_norm_set:
                continue
            best_token: Optional[str] = None
            best_overlap = 0
            for f1 in IDENT:
                for f2 in IDENT:
                    if f1 == f2:
                        continue
                    if f1 in used_set() or f2 in used_set():
                        continue
                    for sep in ("-", "", "/", "."):
                        concat_norms: set[str] = set()
                        for it in sample_invoice_items:
                            v1 = str(it.get(f1) or "").strip()
                            v2 = str(it.get(f2) or "").strip()
                            if v1 and v2:
                                concat_norms.add(_norm(f"{v1}{sep}{v2}"))
                        if not concat_norms:
                            continue
                        overlap = len(csv_norm_set & concat_norms)
                        if overlap > best_overlap and (
                            overlap >= MIN_MATCHES
                            or overlap / len(csv_norm_set) >= MIN_FRACTION
                        ):
                            best_overlap = overlap
                            # Encode as "f1+f2" — apply_mapping will join with
                            # an inferred separator at output time.
                            best_token = f"{f1}+{f2}"
            if best_token:
                print(f"  [Mapper] Compound-code overlap: '{col}' -> "
                      f"'{best_token}' ({best_overlap} exact matches; "
                      f"LLM split a single column into two fields)")
                corrected[col] = best_token

    for col in csv_columns:
        if col not in corrected:
            print(f"  [Mapper] No match found for '{col}' -> null")
            corrected[col] = None

    return corrected


def _infer_col_format(csv_sample_rows: list[dict], col: str) -> str:
    vals = [str(r.get(col, "")).strip() for r in csv_sample_rows if str(r.get(col, "")).strip()]
    if not vals:
        return "other"
    return Counter(_value_format(v) for v in vals).most_common(1)[0][0]


def _normalize_row(row: dict, col_formats: dict[str, str], semantic_cols: set[str]) -> dict:
    non_semantic = {c: fmt for c, fmt in col_formats.items() if c not in semantic_cols}
    if len(non_semantic) < 1:
        return row

    cur_vals = {c: str(row.get(c) or "").strip() for c in non_semantic}
    cur_fmts = {c: (_value_format(v) if v else "other") for c, v in cur_vals.items()}
    corrected = dict(row)

    cols = list(non_semantic.keys())
    for i, ca in enumerate(cols):
        for cb in cols[i+1:]:
            exp_a, exp_b = non_semantic[ca], non_semantic[cb]
            val_a, val_b = cur_vals[ca], cur_vals[cb]
            act_a, act_b = cur_fmts[ca], cur_fmts[cb]
            if not val_a or not val_b:
                continue
            if exp_a != exp_b and act_a == exp_b and act_b == exp_a:
                corrected[ca], corrected[cb] = row.get(cb), row.get(ca)
                cur_vals[ca], cur_vals[cb] = val_b, val_a
                cur_fmts[ca], cur_fmts[cb] = act_b, act_a

    for col, exp_fmt in non_semantic.items():
        val = str(corrected.get(col) or "").strip()
        if not val:
            continue
        act_fmt = _value_format(val)
        if exp_fmt != "other" and not _formats_compatible(act_fmt, exp_fmt):
            normalized = re.sub(r"[^A-Za-z0-9]", "", val)
            norm_fmt   = _value_format(normalized) if normalized else "other"
            if _formats_compatible(norm_fmt, exp_fmt):
                continue
            corrected[col] = None

    return corrected


def apply_mapping(
    line_items: list[dict],
    mapping: dict[str, Optional[str]],
    po_number: str,
    sample_csv_rows: Optional[list[dict]] = None,
) -> list[dict]:
    _SEMANTIC_KW = {"line", "qty", "quantity", "price", "amount", "uom", "order", "unit"}

    col_formats: dict[str, str] = {}
    if sample_csv_rows:
        for csv_col in mapping:
            if mapping.get(csv_col) in (None, "PO_NUMBER"):
                continue
            if any(kw in csv_col.lower() for kw in _SEMANTIC_KW):
                continue
            fmt = _infer_col_format(sample_csv_rows, csv_col)
            if fmt != "other":
                col_formats[csv_col] = fmt

    semantic_cols = {c for c in mapping if any(
        kw in c.lower() for kw in _SEMANTIC_KW) or mapping.get(c) == "PO_NUMBER"}

    # When the mapper detected a split compound code (token like
    # "item+vend_cat_no"), pick the separator that matches the CSV column
    # values best so the joined value reads identically to what's in CSV.
    compound_seps: dict[str, str] = {}
    if sample_csv_rows:
        for csv_col, inv_field in mapping.items():
            if not inv_field or "+" not in inv_field:
                continue
            f1, f2 = inv_field.split("+", 1)
            csv_vals = [str(r.get(csv_col, "")).strip()
                        for r in sample_csv_rows
                        if str(r.get(csv_col, "")).strip()]
            csv_norm = {re.sub(r"[^A-Za-z0-9]", "", v).lower() for v in csv_vals}
            best_sep, best_score = "-", -1
            for sep in ("-", "/", ".", ""):
                hits = 0
                for it in line_items[: min(20, len(line_items))]:
                    v1 = str(it.get(f1) or "").strip()
                    v2 = str(it.get(f2) or "").strip()
                    if v1 and v2:
                        joined = re.sub(r"[^A-Za-z0-9]", "",
                                        f"{v1}{sep}{v2}").lower()
                        if joined in csv_norm:
                            hits += 1
                if hits > best_score:
                    best_score, best_sep = hits, sep
            compound_seps[csv_col] = best_sep

    output = []
    for item in line_items:
        row: dict[str, Any] = {}
        for csv_col, inv_field in mapping.items():
            if inv_field == "PO_NUMBER":
                row[csv_col] = po_number
            elif not inv_field:
                row[csv_col] = None
            elif "+" in inv_field:
                f1, f2 = inv_field.split("+", 1)
                v1 = item.get(f1)
                v2 = item.get(f2)
                if v1 and v2:
                    sep = compound_seps.get(csv_col, "-")
                    row[csv_col] = f"{v1}{sep}{v2}"
                elif v1:
                    row[csv_col] = v1
                elif v2:
                    row[csv_col] = v2
                else:
                    row[csv_col] = None
            else:
                row[csv_col] = item.get(inv_field)
        if col_formats:
            row = _normalize_row(row, col_formats, semantic_cols)
        output.append(row)
    return output


def validate_mapped(
    mapped_items: list[dict],
    mapping: dict[str, Optional[str]],
    subtotal: Optional[float],
) -> dict:
    qty_col   = next((c for c, f in mapping.items() if f in ("order_qty", "trns_qty")), None)
    price_col = next((c for c, f in mapping.items() if f == "unit_price"), None)
    total     = 0.0

    for item in mapped_items:
        qty   = parse_num(item.get(qty_col))   if qty_col   else None
        price = parse_num(item.get(price_col)) if price_col else None
        if qty is not None and price is not None:
            total += round(qty * price, 2)
        item["_extended"] = round(qty * price, 2) if (qty and price) else None

    total = round(total, 2)
    diff  = round(total - subtotal, 2) if subtotal else None
    match = diff is not None and abs(diff) <= 1.00
    return {
        "extracted_total":   total,
        "detected_subtotal": subtotal,
        "subtotal_match":    match,
        "diff":              diff,
        "line_count":        len(mapped_items),
        "status":            "PASS" if match else "FAIL",
    }


def process_invoice(pdf_path: str, df_csv, use_vision: bool = True,
                    vision_dpi: Optional[int] = None) -> dict:
    print(f"\n{'=' * 64}")
    print(f"  {Path(pdf_path).name}")
    print(f"{'=' * 64}")

    # Extract PO from the PDF itself.
    po = _extract_po_number(pdf_path)
    if po is None:
        print("  [PO] PO number not found in PDF. Cannot continue.")
        return {
            "po_number": None,
            "skipped": True,
            "reason": "PO number not found in PDF",
        }
    print(f"  PO: {po}")

    sample_df = csv_sample(df_csv, po, n=10)
    if sample_df.empty:
        print(f"  [Mapper] No CSV rows found for PO {po}. Skipping.")
        return {"po_number": po, "skipped": True, "reason": "No CSV rows"}

    csv_columns = list(df_csv.columns)
    sample_rows = sample_df.to_dict(orient="records")
    print(f"  CSV columns : {csv_columns}")
    print(f"  CSV rows    : {len(sample_df)} (sample)")

    cache_path = Path("results") / f"{po}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        result = cached
        print(f"  [Mapper] Using cached extraction from {cache_path}")
    else:
        result = extract_invoice(
            pdf_path,
            use_vision   = use_vision,
            vision_dpi   = vision_dpi,
            csv_samples  = sample_rows,
            csv_columns  = csv_columns,
        )
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            print(f"  [Mapper] Cached raw extraction -> {cache_path}")
        except Exception as e:
            print(f"  [Mapper] Could not write cache {cache_path}: {e}")

    line_items = result.get("line_items", [])
    subtotal   = result.get("verification", {}).get("detected_subtotal")

    if not line_items:
        print("  [Mapper] No items extracted from invoice.")
        return {"po_number": po, "line_items": [], "mapping": {}, "verification": {}}

    print(f"  Invoice items extracted: {len(line_items)}")
    print("  [Mapper] Discovering column mapping ...")

    mapping_sample = line_items[:min(8, len(line_items))]
    mapping = discover_mapping(csv_columns, sample_rows, mapping_sample)

    if not mapping:
        mapping = {c: None for c in csv_columns}

    mapping = _validate_mapping(mapping, sample_rows, mapping_sample, csv_columns)

    print("  [Mapper] Mapping discovered:")
    for csv_col, inv_field in mapping.items():
        print(f"    {csv_col:20s} <- {inv_field}")

    mapped_items = apply_mapping(line_items, mapping, po, sample_rows)
    verification = validate_mapped(mapped_items, mapping, subtotal)
    v = verification
    print(
        f"\n  -- Validation -------------------------------------------\n"
        f"  Status      : {v['status']}\n"
        f"  Items       : {v['line_count']}\n"
        f"  Computed $  : {v['extracted_total']}\n"
        f"  Subtotal    : {v['detected_subtotal']}\n"
        f"  Diff        : {v['diff']}"
    )

    clean_items = [{k: val for k, val in i.items() if k != "_extended"} for i in mapped_items]
    return {
        "po_number":    po,
        "csv_mapping":  mapping,
        "line_items":   clean_items,
        "verification": verification,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description=(
            "Invoice extraction + CSV mapping pipeline.\n"
            "  With --csv  : extracts invoice and maps to CSV column names.\n"
            "  Without --csv: extracts invoice to raw JSON only."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("pdfs",          nargs="+", help="PDF file(s) to process")
    ap.add_argument("--csv",         default=None,
                    help="Path to reference data file for column mapping (.csv, .xlsx, .xls)")
    ap.add_argument("--save",        default=None,
                    help="Save output here (file or directory)")
    ap.add_argument("--outdir",      default=None,
                    help="Write one JSON per PDF to this directory")
    ap.add_argument("--no-vision",   action="store_true",
                    help="Disable multimodal vision (text-only extraction)")
    ap.add_argument("--vision-dpi",  type=int, default=None,
                    help="Page raster DPI for vision (96-300, default 150)")
    args = ap.parse_args()

    use_vision = not args.no_vision
    df_csv     = None

    if args.csv:
        if not Path(args.csv).exists():
            print(f"CSV file not found: {args.csv}")
            return
        df_csv = load_csv(args.csv)

    results: list[dict] = []
    for pdf in args.pdfs:
        if not Path(pdf).exists():
            print(f"  File not found: {pdf}")
            continue

        if df_csv is not None:
            res = process_invoice(pdf, df_csv, use_vision=use_vision, vision_dpi=args.vision_dpi)
        else:
            res = extract_invoice(pdf, use_vision=use_vision, vision_dpi=args.vision_dpi)

        results.append(res)

        if args.outdir:
            Path(args.outdir).mkdir(parents=True, exist_ok=True)
            suffix = "_mapped" if df_csv is not None else ""
            out    = Path(args.outdir) / f"{Path(pdf).stem}{suffix}.json"
            out.write_text(
                json.dumps(res, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            print(f"  -> {out}")

    output = results[0] if len(results) == 1 else results

    if args.save:
        save_path = Path(args.save)
        if save_path.is_dir() or (len(results) > 1 and not save_path.suffix):
            save_path.mkdir(parents=True, exist_ok=True)
            for res in results:
                po     = res.get("po_number", "unknown")
                suffix = "_mapped" if df_csv is not None else ""
                out    = save_path / f"{po}{suffix}.json"
                out.write_text(
                    json.dumps(res, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                print(f"  Saved -> {out}")
        else:
            save_path.write_text(
                json.dumps(output, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            print(f"\nSaved -> {args.save}")
    elif not args.outdir:
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))

    return output


if __name__ == "__main__":
    main()
