#!/usr/bin/env python3
"""
docetl.py — Invoice line-item extraction pipeline using DocETL framework.
=========================================================================

Architecture
------------
[Loader]      Two interchangeable PDF loaders (selected via INVOICE_LOADER env var):
                pdf_loader      -- pdfplumber (word bounding boxes) + PyMuPDF (images)
                                   Default; works offline with no cloud credentials.
                azure_di_loader -- Azure Document Intelligence prebuilt-layout model
                                   + PyMuPDF (images).  Adds table detection, KV-pair
                                   extraction, and word-confidence scores.
[DocETL]      LLM pipeline with gpt-4o-mini:
                * Custom parsing tool: OpenAI vision API (image + aligned text) per page
                * Map operation: validate & correct extracted items (with gleaning)
[Python]      Aggregate pages per invoice, verify subtotals, write JSON

DocETL features used
--------------------
- Custom parsing_tools: runs full Python (including vision API calls) during data loading
- Map operation with gleaning: automatic LLM refinement if validation fails
- LLM call caching: ~/.cache/docetl avoids re-calling for unchanged pages
- System prompt persona: domain-specific context for all LLM operations

Loader selection
----------------
  Set INVOICE_LOADER=pdf    (default) to use pdfplumber + PyMuPDF
  Set INVOICE_LOADER=azure  to use Azure Document Intelligence + PyMuPDF

  Azure DI requires two additional env vars:
    AZURE_DI_ENDPOINT  -- https://<resource>.cognitiveservices.azure.com/
    AZURE_DI_KEY       -- your Azure DI API key
  Optionally:
    AZURE_DI_MODEL_ID  -- model to use (default: prebuilt-layout)

Usage
-----
  python docetl.py invoice.pdf
  python docetl.py 1636462.pdf 1637908.pdf
  python docetl.py *.pdf
  python docetl.py N298589.pdf --outdir my_results
  python docetl.py invoice.pdf --loader azure

Requirements
------------
  pip install docetl pdfplumber pymupdf python-dotenv pyyaml
  For Azure loader: pip install azure-ai-documentintelligence
  OPENAI_API_KEY in .env (uses gpt-4o-mini)
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
# pdfplumber / PyMuPDF / base64 / re live in pdf_loader.py — not needed here

# Loaders must be imported AFTER load_dotenv so env vars are available
load_dotenv()

# ── Loader selection (env var or --loader CLI flag set before import) ─────────
# INVOICE_LOADER=pdf   → pdfplumber + PyMuPDF  (default, no cloud creds needed)
# INVOICE_LOADER=azure → Azure Document Intelligence + PyMuPDF
_LOADER_CHOICE = os.getenv("INVOICE_LOADER", "pdf").lower()

if _LOADER_CHOICE == "azure":
    try:
        from azure_di_loader import (  # noqa: E402
            detect_subtotal,
            extract_po_number,
            llm_subtotal,
            load_pdf_pages,
        )
        _LOADER_NAME = "Azure Document Intelligence (prebuilt-layout)"
    except ImportError as _e:
        print(
            f"[WARN] Azure DI loader unavailable ({_e}).\n"
            "       Falling back to pdf_loader (pdfplumber + PyMuPDF).\n"
            "       Install with: pip install azure-ai-documentintelligence"
        )
        _LOADER_CHOICE = "pdf"
        from pdf_loader import (  # noqa: E402
            detect_subtotal,
            extract_po_number,
            llm_subtotal,
            load_pdf_pages,
        )
        _LOADER_NAME = "pdfplumber + PyMuPDF (fallback)"
else:
    from pdf_loader import (  # noqa: E402
        detect_subtotal,
        extract_po_number,
        llm_subtotal,
        load_pdf_pages,
    )
    _LOADER_NAME = "pdfplumber + PyMuPDF"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL = "gpt-4o-mini"  # Fixed as per requirements


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 1 — PDF loading is delegated to pdf_loader.py
#   load_pdf_pages, detect_subtotal, llm_subtotal, extract_po_number
#   are imported at the top of this file from pdf_loader.
# ════════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 2 — DOCETL PIPELINE DEFINITION
# ════════════════════════════════════════════════════════════════════════════════

# Vision extraction code embedded inside DocETL's custom parsing tool.
# DocETL executes this Python code during dataset loading for each page document.
# The function calls the OpenAI vision API directly — this is DocETL's mechanism
# for running arbitrary Python logic in a pipeline step.
_VISION_EXTRACT_FUNC = textwrap.dedent(
    '''
    def vision_page_extractor(document: dict) -> list:
        """
        Per-page invoice line-item extraction via OpenAI gpt-4o-mini vision API.
        Runs inside DocETL's custom parsing tool framework during data loading.
        Combines image (PyMuPDF-rendered PNG) + aligned text (pdfplumber) for best results.
        """
        import os, json, re

        def _safe_float(v):
            try: return float(str(v).replace(",", ""))
            except: return 0.0

        def _recover_partial_json(text):
            """
            Handle truncated JSON responses by extracting all complete item objects.
            Wraps in {"items": [...]} if the top-level array/object is incomplete.
            """
            # Try direct parse
            try:
                return json.loads(text)
            except Exception:
                pass
            # Find all {...} blocks that have a non-zero unit_price (positive or negative)
            items = []
            depth = 0
            start = -1
            for i, ch in enumerate(text):
                if ch == '{':
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0 and start != -1:
                        try:
                            obj = json.loads(text[start:i+1])
                            if isinstance(obj, dict) and abs(_safe_float(obj.get("unit_price"))) > 0:
                                items.append(obj)
                        except Exception:
                            pass
                        start = -1
            return {"items": items}

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return [{"extracted_items": [], "extraction_error": "No OPENAI_API_KEY set"}]

        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
        except Exception as e:
            return [{"extracted_items": [], "extraction_error": f"OpenAI init: {e}"}]

        aligned_text = document.get("aligned_text", "")
        image_b64    = document.get("image_b64")
        page_num     = document.get("page_num", 1)
        total_pages  = document.get("total_pages", 1)

        system_msg = (
            "You are an expert invoice line-item extractor. Extract ALL product line items.\\n"
            "REQUIREMENTS:\\n"
            "  - A valid item MUST have: unit_price != 0 (nonzero — positive OR negative)\\n"
            "  - CREDIT/RETURN rows have NEGATIVE unit_price — include them, do NOT skip them\\n"
            "  - extended_price = trns_qty x unit_price (may be negative for credits)\\n"
            "  - Never invent values not visible on the page\\n"
            "SKIP: column headers, subtotal/total/tax/freight rows, section labels, blank rows\\n\\n"
            "QUANTITY RULES — critical for invoices with multiple qty columns:\\n"
            "  trns_qty = the SHIPPED/INVOICED quantity in THIS invoice document.\\n"
            "    If the table has separate columns like 'QTY ORD' and 'SHIP QTY' or 'INV QTY',\\n"
            "    use the SHIP/INV column, NOT the ORD column.\\n"
            "  If an item shows 0 shipped (backordered), set trns_qty=0 and extended_price=0.\\n"
            "  order_qty = the originally ordered quantity (may differ from trns_qty).\\n\\n"
            "IDENTIFIER FIELD RULES — read carefully, do not swap these fields:\\n"
            "  reference_number: product barcode/UPC/GTIN — the FIRST long numeric string at the\\n"
            "    start of the product row. Typically 12-14 digits. NOT a carrier tracking number.\\n"
            "    Example: 20884521079295 (Covidien), 069945112018 (Winchester), 12345678901234\\n"
            "  vend_cat_no: vendor catalog code — ALPHANUMERIC, usually contains letters AND digits,\\n"
            "    may include hyphens. Examples: SM-923, E2350H, 88861744-41, 100-0288, GG-123\\n"
            "    It may also appear as a sub-row below the main product row.\\n"
            "  item: buyer PO item code — SHORT (4-8 digit) NUMERIC code ONLY (no letters).\\n"
            "    Often appears on a sub-row labeled 'Tracking number' (this is the buyer's internal\\n"
            "    reference, NOT the carrier tracking). Examples: 019779, 023199, 029044\\n"
            "    RULE: if a line says '[6-digit-number] Tracking number', the 6-digit number is item.\\n"
            "  line_number: sequential row counter (1, 2, 3...) from leftmost No./Line column\\n"
            "  tracking_number: CARRIER tracking — the number AFTER the carrier name.\\n"
            "    Examples: 'FedEx (US) 460974016150', 'UPS 1Z...', 'DHL ...'\\n"
            "    RULE: tracking_number ALWAYS starts with carrier name (FedEx/UPS/DHL etc.)\\n"
            "  batch_number: batch/lot code (alphanumeric, e.g. D5B2604FY, A2D0463Y)\\n\\n"
            "LAYOUT RULES:\\n"
            "  MULTI-ROW ITEMS: One logical item may span multiple visual lines.\\n"
            "    Main row: [barcode] [catalog_code] [description] [qty] [UOM] [unit_price] [ext_price]\\n"
            "    Sub-row 1 (optional): repeated catalog_code alone\\n"
            "    Sub-row 2 (optional): [item_code] Tracking number  ← item field, NOT carrier tracking\\n"
            "    Sub-row 3 (optional): [CarrierName] [carrier_tracking_number]  ← tracking_number\\n"
            "    Sub-row 4 (optional): Batch: [batch_code]  ← batch_number\\n"
            "    Collapse ALL sub-rows into ONE item dict.\\n"
            "  MULTIPLE SHIPMENTS: Same product (same barcode) with DIFFERENT carrier tracking\\n"
            "    = separate line items. Keep each shipment as its own row with same price data.\\n"
            "  If a page has NO product rows (header/footer/address/signature page), return items: []"
        )

        prompt_text = (
            f"Extract ALL line items from invoice page {page_num} of {total_pages}.\\n\\n"
            "SPATIALLY ALIGNED PAGE TEXT (horizontal spaces = column positions):\\n"
            f"{aligned_text}\\n\\n"
            "Return JSON with \\"items\\" array. Each item must have these fields:\\n"
            "line_number (string|null), reference_number (string|null), item (string|null),\\n"
            "vend_cat_no (string|null), description (string|null), order_qty (number|null),\\n"
            "trns_qty (number|null), inv_uom (string|null), unit_price (number|null),\\n"
            "extended_price (number|null), tracking_number (string|null), batch_number (string|null)"
        )

        def _call_api(msgs, use_image=True):
            parts = []
            if use_image and image_b64:
                parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_b64}",
                        "detail": "high",
                    },
                })
            parts.append({"type": "text", "text": prompt_text})
            return client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": msgs}, {"role": "user", "content": parts}],
                max_tokens=16000,
                temperature=0,
                response_format={"type": "json_object"},
            )

        try:
            resp = _call_api(system_msg, use_image=True)
            raw = resp.choices[0].message.content or "{}"
            finish = resp.choices[0].finish_reason
            # If response was truncated, retry with text-only (no image) to save tokens
            if finish == "length":
                resp2 = _call_api(system_msg, use_image=False)
                raw2 = resp2.choices[0].message.content or "{}"
                # Use whichever response has more items
                items1 = (_recover_partial_json(raw).get("items") or [])
                items2 = (_recover_partial_json(raw2).get("items") or [])
                raw = raw2 if len(items2) >= len(items1) else raw
        except Exception as exc:
            return [{"extracted_items": [], "extraction_error": str(exc)}]

        try:
            data = _recover_partial_json(raw)
        except Exception as exc:
            return [{"extracted_items": [], "extraction_error": f"JSON parse: {exc}"}]

        items = data.get("items", data.get("line_items", []))
        valid_items = [
            it for it in items
            if isinstance(it, dict) and abs(_safe_float(it.get("unit_price"))) > 0
        ]
        return [{"extracted_items": valid_items, "extraction_error": None}]
    '''
).strip()


def _build_validate_prompt() -> str:
    """Build the Jinja2 prompt template for the validate_and_correct map operation."""
    return (
        "Validate and correct extracted line items from invoice page "
        "{{ input.page_num }} of {{ input.total_pages }}.\n\n"
        "{% if input.extraction_error %}\n"
        "NOTE: Extraction had an error: {{ input.extraction_error }}\n"
        "{% endif %}\n"
        "EXTRACTED ITEMS ({{ input.extracted_items | length }} items):\n"
        "{% for it in input.extracted_items %}\n"
        "  Item {{ loop.index }}:\n"
        "    reference_number={{ it.reference_number }} | vend_cat_no={{ it.vend_cat_no }} "
        "| item={{ it.item }}\n"
        "    description={{ it.description }}\n"
        "    trns_qty={{ it.trns_qty }}, unit_price={{ it.unit_price }}, "
        "extended_price={{ it.extended_price }}\n"
        "    tracking={{ it.tracking_number }}\n"
        "{% endfor %}\n\n"
        "VALIDATION RULES:\n"
        "1. MATH: extended_price = trns_qty x unit_price (±1%). Recalculate if wrong.\n"
        "2. FIELD TYPES:\n"
        "   reference_number = long numeric barcode (12-14 digits, FIRST number on the row)\n"
        "   vend_cat_no = alphanumeric catalog code (contains LETTERS, e.g. SM-923, E2350H)\n"
        "   item = SHORT numeric code (4-8 digits, NO letters). "
        "If a row says '[6-digit-number] Tracking number', that number is item.\n"
        "   tracking_number = STARTS with carrier name (FedEx, UPS, DHL...)\n"
        "3. SWAP FIX: If reference_number contains 'FedEx'/'UPS'/'DHL', it belongs in "
        "tracking_number. If vend_cat_no is purely numeric (no letters), check if it should "
        "be item or reference_number.\n"
        "4. NULL SENTINELS: Replace 'None', 'null', 'N/A', 'n/a', '-' with actual null.\n"
        "5. Remove items where unit_price is null or 0. KEEP items with NEGATIVE unit_price "
        "(they are credit/return rows — their extended_price will also be negative).\n"
        "6. SHIPPED QTY: trns_qty = quantity invoiced/shipped in this document. If an item has "
        "trns_qty=0 (backordered — ordered but not yet shipped), keep extended_price=0.\n\n"
        "Return JSON: {\"validated_items\": [...]}"
    )


def _build_validate_gleaning_prompt() -> str:
    return (
        "Review the validated_items:\n"
        "1. Math: extended_price = trns_qty x unit_price (±1%)?\n"
        "2. Field types correct?\n"
        "   - reference_number: 12-14 digit barcode (should NOT start with carrier names)\n"
        "   - vend_cat_no: alphanumeric WITH letters (e.g. SM-923, not just digits)\n"
        "   - item: 4-8 digit numeric ONLY (no letters)\n"
        "   - tracking_number: starts with carrier name (FedEx/UPS/DHL)\n"
        "3. No null sentinel strings ('None', 'null') remaining?\n"
        "Fix any remaining issues and return corrected validated_items."
    )


def build_pipeline_config(
    input_path: str, output_path: str, intermediate_dir: str
) -> dict:
    """
    Build the DocETL pipeline configuration as a Python dict (serialised to YAML).

    Pipeline structure:
      datasets.invoice_pages
        → parsing tool (vision_page_extractor): calls OpenAI vision API per page
            adds: extracted_items, extraction_error
      operations.validate_and_correct (map, with gleaning)
        → LLM validates math + identifiers → adds: validated_items
      pipeline output: JSON with one record per page
    """
    item_schema = (
        "list[{"
        "line_number: string, "
        "reference_number: string, "
        "item: string, "
        "vend_cat_no: string, "
        "description: string, "
        "order_qty: number, "
        "trns_qty: number, "
        "inv_uom: string, "
        "unit_price: number, "
        "extended_price: number, "
        "tracking_number: string, "
        "batch_number: string"
        "}]"
    )

    return {
        "default_model": MODEL,
        "bypass_cache": False,
        "system_prompt": {
            "dataset_description": (
                "pages extracted from vendor purchase order invoices in various formats "
                "(Covidien/Medtronic surgical supplies, Winchester ammunition, CritterCuff "
                "pet products, generic wholesale invoices)"
            ),
            "persona": (
                "an expert accounts payable specialist who reads invoices precisely, "
                "extracts every line item without missing or inventing data, and verifies "
                "that all extended prices equal quantity times unit price"
            ),
        },
        "parsing_tools": [
            {
                "name": "vision_page_extractor",
                "function_code": _VISION_EXTRACT_FUNC,
            }
        ],
        "datasets": {
            "invoice_pages": {
                "type": "file",
                "source": "local",
                "path": input_path,
                "parsing": [{"function": "vision_page_extractor"}],
            }
        },
        "operations": [
            {
                "name": "validate_and_correct",
                "type": "map",
                "model": MODEL,
                "prompt": _build_validate_prompt(),
                "output": {"schema": {"validated_items": item_schema}},
                "gleaning": {
                    "num_rounds": 1,
                    "validation_prompt": _build_validate_gleaning_prompt(),
                },
                "litellm_completion_kwargs": {
                    "max_tokens": 16000,
                    "temperature": 0,
                },
            }
        ],
        "pipeline": {
            "steps": [
                {
                    "name": "extract_and_validate",
                    "input": "invoice_pages",
                    "operations": ["validate_and_correct"],
                }
            ],
            "output": {
                "type": "file",
                "path": output_path,
                "intermediate_dir": intermediate_dir,
            },
        },
    }


def run_docetl_pipeline(
    pages_data: list[dict],
    run_id: str,
    workspace: Path,
) -> Optional[list[dict]]:
    """
    Serialise pages_data to JSON, write YAML pipeline, run `docetl run` via subprocess.
    Returns the output records (one per input page) or None on failure.
    """
    input_path = workspace / f"{run_id}_input.json"
    output_path = workspace / f"{run_id}_output.json"
    pipeline_path = workspace / f"{run_id}_pipeline.yaml"
    intermediate_dir = str(workspace / f"{run_id}_intermediate")

    # Write input JSON
    input_path.write_text(json.dumps(pages_data, ensure_ascii=False), encoding="utf-8")

    # Build and write pipeline YAML
    config = build_pipeline_config(
        str(input_path.resolve()),
        str(output_path.resolve()),
        intermediate_dir,
    )
    pipeline_path.write_text(
        yaml.dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

    print(f"  [DocETL] Input:    {input_path.name}  ({len(pages_data)} pages)")
    print(f"  [DocETL] Pipeline: {pipeline_path.name}")

    env = os.environ.copy()
    env["OPENAI_API_KEY"] = OPENAI_API_KEY
    # Force UTF-8 output so DocETL's unicode checkmarks don't crash on Windows
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    proc = subprocess.run(
        ["docetl", "run", str(pipeline_path.resolve())],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(workspace),
    )

    # Print stdout/stderr — encode to ASCII for Windows console safety
    def _safe_print(prefix: str, text: str, tail: int = 20) -> None:
        for line in text.strip().splitlines()[-tail:]:
            try:
                print(f"{prefix}{line}")
            except UnicodeEncodeError:
                print(f"{prefix}{line.encode('ascii', 'replace').decode('ascii')}")

    if proc.stdout.strip():
        _safe_print("    [docetl] ", proc.stdout, tail=20)
    if proc.stderr.strip():
        _safe_print("    [docetl ERR] ", proc.stderr, tail=10)

    if proc.returncode != 0:
        print(f"  [DocETL] FAILED (exit code {proc.returncode})")
        return None

    print("  [DocETL] Pipeline completed successfully")

    if not output_path.exists():
        print(f"  [DocETL] Output file missing: {output_path}")
        return None

    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else [data]
    except Exception as exc:
        print(f"  [DocETL] Failed to parse output JSON: {exc}")
        return None


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 3 — POST-PROCESSING
# ════════════════════════════════════════════════════════════════════════════════


def _is_null_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip().lower() in (
        "none", "null", "n/a", "na", "", "-", "–",
    )


def _coerce_nulls(item: dict) -> dict:
    """Convert null-sentinel strings to Python None in all fields."""
    return {k: (None if _is_null_str(v) else v) for k, v in item.items()}


def _safe_float(v: Any) -> float:
    """Safely convert a value to float, returning 0.0 on failure."""
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _dedup_key(it: dict) -> tuple:
    """
    Deduplication key for line items.
    tracking_number differentiates multiple shipments of the same SKU.
    """
    return (
        str(it.get("reference_number") or ""),
        str(it.get("vend_cat_no") or ""),
        str(it.get("tracking_number") or ""),
        str(round(_safe_float(it.get("unit_price")), 2)),
    )


def _normalize_item_fields(it: dict) -> dict:
    """
    Fix common LLM field assignment errors post-extraction:
    - item must be 4-8 numeric digits only. If 10+ digits, it's a tracking number.
    - reference_number must not contain carrier keywords.
    - Recalculate extended_price if it's significantly off from qty × unit_price.
    """
    item_val = it.get("item")
    ref_val = it.get("reference_number")
    tracking_val = it.get("tracking_number")

    # Fix: item containing a long tracking number
    if item_val and isinstance(item_val, str):
        stripped = item_val.strip().replace(" ", "")
        if len(stripped) >= 10 and stripped.isdigit():
            if not tracking_val:
                it["tracking_number"] = item_val
            it["item"] = None

    # Fix: reference_number containing a carrier tracking string
    carrier_kw = ("fedex", "ups", "dhl", "usps", "freight")
    if ref_val and isinstance(ref_val, str):
        ref_low = ref_val.lower()
        if any(kw in ref_low for kw in carrier_kw):
            if not tracking_val:
                it["tracking_number"] = ref_val
            it["reference_number"] = None

    # Fix: recalculate extended_price when off by more than 5%.
    # Use trns_qty preferentially. Only fall back to order_qty when trns_qty is None (not just 0).
    trns_qty_raw = it.get("trns_qty")
    order_qty_raw = it.get("order_qty")
    qty_src = trns_qty_raw if trns_qty_raw is not None else order_qty_raw
    qty = _safe_float(qty_src)
    upr = _safe_float(it.get("unit_price"))
    ext = _safe_float(it.get("extended_price"))

    # If trns_qty is explicitly 0, the item is backordered — extended_price should be 0.
    if trns_qty_raw is not None and qty == 0 and ext != 0:
        it["extended_price"] = 0.0
    elif qty > 0 and upr != 0:
        # Handle both positive items and negative credits.
        expected_ext = round(qty * upr, 2)
        if ext == 0 or abs(ext - expected_ext) / max(abs(expected_ext), 0.01) > 0.05:
            it["extended_price"] = expected_ext

    return it


def aggregate_pages(page_records: list[dict]) -> list[dict]:
    """
    Aggregate validated_items from all page records (one record per page).
    - Falls back to extracted_items if validated_items is absent/empty.
    - Removes true duplicates (same item extracted twice from same page).
    - Preserves multiple shipments (same product + different tracking = separate rows).
    - Applies null-sentinel coercion and field normalization on all items.
    """
    all_items: list[dict] = []
    seen: set[tuple] = set()

    for rec in sorted(page_records, key=lambda r: r.get("page_num", 0)):
        items: list[dict] = rec.get("validated_items") or rec.get("extracted_items") or []
        for raw_it in items:
            if not isinstance(raw_it, dict):
                continue
            it = _normalize_item_fields(_coerce_nulls(raw_it))
            if abs(_safe_float(it.get("unit_price"))) == 0:
                continue
            key = _dedup_key(it)
            if key in seen:
                continue
            seen.add(key)
            all_items.append(it)

    return all_items


def verify_total(
    items: list[dict], expected: Optional[float]
) -> tuple[float, str]:
    """
    Compute sum of extended_price for all items and compare to detected subtotal.
    Returns (extracted_total, status_string).
    """
    extracted = sum(_safe_float(it.get("extended_price")) for it in items)
    if expected is None:
        status = "NO_SUBTOTAL"
    elif abs(extracted - expected) <= max(0.02 * expected, 1.0):
        status = "PASS"
    else:
        diff = extracted - expected
        status = f"FAIL (diff={diff:+.2f})"
    return extracted, status


# ════════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════════════════


def process_pdf(
    pdf_path: str,
    outdir: Path,
    workspace: Path,
) -> dict:
    """
    Full pipeline for a single PDF:
      1. Load with pdfplumber + PyMuPDF
      2. Detect subtotal / extract PO number (Python)
      3. Run DocETL pipeline (vision extraction + LLM validation)
      4. Aggregate pages, verify total, save JSON
    """
    pdf_path_obj = Path(pdf_path).resolve()
    pdf_name = pdf_path_obj.name
    stem = pdf_path_obj.stem

    print(f"\n{'=' * 64}")
    print(f"  PDF: {pdf_name}")
    print(f"{'=' * 64}")

    # ── Phase 1: PDF loading ─────────────────────────────────────────────────
    print(f"  [1/3] Loading PDF ({_LOADER_NAME}) ...")
    pages = load_pdf_pages(str(pdf_path_obj))
    if not pages:
        return {"error": "No pages loaded", "pdf": pdf_name}

    print(f"        Pages: {len(pages)}")

    subtotal = detect_subtotal(pages)
    po_number = extract_po_number(pages)
    print(f"        PO Number : {po_number or '(not found)'}")
    print(f"        Subtotal  : {subtotal} (regex)")

    # ── Phase 2: DocETL pipeline ─────────────────────────────────────────────
    print(f"  [2/3] Running DocETL pipeline ({len(pages)} pages) ...")

    # Build input records (one per page)
    # aligned_text already includes Azure DI table data when azure_di_loader is used.
    # key_value_pairs and di_confidence are bonus fields; pdf_loader pages return {}.
    total_pages = len(pages)
    docetl_input = [
        {
            "pdf_path": str(pdf_path_obj),
            "pdf_name": pdf_name,
            "po_number": po_number,
            "subtotal": subtotal,
            "page_num": p["page_num"],
            "total_pages": total_pages,
            "aligned_text": p.get("aligned_text", ""),
            "image_b64": p.get("image_b64"),
            # Azure DI extras (empty dicts/lists when pdf_loader is used)
            "key_value_pairs": p.get("key_value_pairs", {}),
            "di_confidence": p.get("di_confidence", 1.0),
        }
        for p in pages
    ]

    run_id = f"{stem}_{uuid.uuid4().hex[:8]}"
    page_results = run_docetl_pipeline(docetl_input, run_id, workspace)

    if page_results is None:
        return {"error": "DocETL pipeline failed", "pdf": pdf_name, "po_number": po_number}

    # ── Phase 3: Aggregate + verify ──────────────────────────────────────────
    print(f"  [3/3] Aggregating {len(page_results)} page results ...")
    items = aggregate_pages(page_results)
    item_sum = sum(_safe_float(it.get("extended_price")) for it in items)

    # If regex subtotal seems inconsistent (>20% off from item sum on multi-page invoices),
    # use LLM to extract the true invoice total from the last pages.
    if (
        len(pages) > 3
        and subtotal is not None
        and item_sum > 0
        and abs(item_sum - subtotal) / max(item_sum, 0.01) > 0.20
    ):
        print("        Subtotal detection unreliable — trying LLM fallback...")
        llm_total = llm_subtotal(pages, OPENAI_API_KEY)
        if llm_total and abs(item_sum - llm_total) < abs(item_sum - subtotal):
            print(f"        Subtotal (LLM)  : {llm_total}")
            subtotal = llm_total

    extracted_total, status = verify_total(items, subtotal)

    print(f"        Items extracted : {len(items)}")
    print(f"        Extracted total : {extracted_total:.2f}")
    print(f"        Subtotal        : {subtotal}")
    print(f"        Status          : {status}")

    result = {
        "pdf": pdf_name,
        "po_number": po_number,
        "subtotal": subtotal,
        "extracted_total": round(extracted_total, 2),
        "verification_status": status,
        "item_count": len(items),
        "items": items,
    }

    # Save
    out_stem = po_number or stem
    out_path = outdir / f"{out_stem}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"        Saved           : {out_path}")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "DocETL-powered invoice line-item extraction.\n"
            "Loader: pdf (pdfplumber+PyMuPDF, default) or azure (Azure DI+PyMuPDF)."
        )
    )
    parser.add_argument("pdfs", nargs="+", help="PDF file path(s) or glob patterns")
    parser.add_argument(
        "--outdir",
        default="results_docetl",
        help="Output directory for JSON results (default: results_docetl/)",
    )
    parser.add_argument(
        "--workspace",
        default="docetl_workspace",
        help="Workspace directory for DocETL temp files (default: docetl_workspace/)",
    )
    parser.add_argument(
        "--loader",
        choices=["pdf", "azure"],
        default=None,
        help=(
            "PDF loader to use: 'pdf' (pdfplumber+PyMuPDF, default) or "
            "'azure' (Azure Document Intelligence+PyMuPDF). "
            "Overrides INVOICE_LOADER env var."
        ),
    )
    args = parser.parse_args()

    # If --loader was provided on CLI, re-import the chosen loader module.
    # This allows overriding the env-var-based import at the top of the file.
    global load_pdf_pages, detect_subtotal, extract_po_number, llm_subtotal
    global _LOADER_NAME, _LOADER_CHOICE
    if args.loader and args.loader != _LOADER_CHOICE:
        _LOADER_CHOICE = args.loader
        if args.loader == "azure":
            try:
                import azure_di_loader as _ldr
                load_pdf_pages = _ldr.load_pdf_pages
                detect_subtotal = _ldr.detect_subtotal
                extract_po_number = _ldr.extract_po_number
                llm_subtotal = _ldr.llm_subtotal
                _LOADER_NAME = "Azure Document Intelligence (prebuilt-layout)"
            except ImportError as e:
                print(f"ERROR: Cannot load azure_di_loader: {e}")
                print("Install with: pip install azure-ai-documentintelligence")
                sys.exit(1)
        else:
            import pdf_loader as _ldr
            load_pdf_pages = _ldr.load_pdf_pages
            detect_subtotal = _ldr.detect_subtotal
            extract_po_number = _ldr.extract_po_number
            llm_subtotal = _ldr.llm_subtotal
            _LOADER_NAME = "pdfplumber + PyMuPDF"

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    # Expand globs
    pdf_files: list[str] = []
    for pat in args.pdfs:
        expanded = glob.glob(pat)
        if expanded:
            pdf_files.extend(expanded)
        elif Path(pat).exists():
            pdf_files.append(pat)
        else:
            print(f"WARNING: No file matched: {pat}")

    if not pdf_files:
        print("ERROR: No PDF files found.")
        sys.exit(1)

    print(f"\nDocETL Invoice Extractor")
    print(f"Model   : {MODEL}")
    print(f"Loader  : {_LOADER_NAME}")
    print(f"PDFs    : {len(pdf_files)}")
    print(f"Output  : {outdir}/")

    results: list[dict] = []
    for pdf in pdf_files:
        try:
            r = process_pdf(pdf, outdir, workspace)
            results.append(r)
        except Exception as exc:
            print(f"\nERROR processing {pdf}: {exc}")
            import traceback
            traceback.print_exc()
            results.append({"error": str(exc), "pdf": Path(pdf).name})

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 64}")
    print("SUMMARY")
    print(f"{'=' * 64}")
    print(f"  {'PDF':<32}  {'PO':<14}  {'Items':>5}  {'Total':>10}  Status")
    print(f"  {'-'*32}  {'-'*14}  {'-'*5}  {'-'*10}  ------")
    for r in results:
        pdf_n = r.get("pdf", "?")[:32]
        po = r.get("po_number") or "N/A"
        items = r.get("item_count", 0)
        total = r.get("extracted_total", 0.0)
        st = r.get("verification_status", r.get("error", "ERROR"))
        print(f"  {pdf_n:<32}  {po:<14}  {items:>5}  {total:>10.2f}  {st}")


if __name__ == "__main__":
    main()
