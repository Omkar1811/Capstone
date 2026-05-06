#!/usr/bin/env python3
"""
invoice_docetl.py  Invoice extraction using DocETL's Python Pipeline API.

Uses docetl.api.Pipeline (not YAML + subprocess).  This script is named
invoice_docetl.py so it does NOT shadow the installed ``docetl`` package
(required for ``from docetl.api import Pipeline``).

Map prompts use only a single Jinja substitution: {{ input.llm_validate_block }}
Per-item formatting is built in Python inside vision_page_extractor.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any, Optional

from docetl.api import (
    Dataset,
    MapOp,
    ParsingTool,
    Pipeline,
    PipelineOutput,
    PipelineStep,
)
from dotenv import load_dotenv

load_dotenv()

# ?? Loader selection ???????????????????????????????????????????????????????????
_LOADER_CHOICE = os.getenv("INVOICE_LOADER", "pdf").lower()

if _LOADER_CHOICE == "azure":
    try:
        from azure_di_loader import (
            detect_subtotal,
            extract_po_number,
            llm_subtotal,
            load_pdf_pages,
        )
        _LOADER_NAME = "Azure Document Intelligence (prebuilt-layout)"
    except ImportError as _e:
        print(
            f"[WARN] Azure DI loader unavailable ({_e}).\n"
            "       Falling back to pdf_loader.\n"
            "       pip install azure-ai-documentintelligence"
        )
        _LOADER_CHOICE = "pdf"
        from pdf_loader import (
            detect_subtotal,
            extract_po_number,
            llm_subtotal,
            load_pdf_pages,
        )
        _LOADER_NAME = "pdfplumber + PyMuPDF (fallback)"
else:
    from pdf_loader import (
        detect_subtotal,
        extract_po_number,
        llm_subtotal,
        load_pdf_pages,
    )
    _LOADER_NAME = "pdfplumber + PyMuPDF"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL = "gpt-4o-mini"

# ?? Vision extractor (embedded; executed by DocETL dataset loader) ?????????????
_VISION_EXTRACT_FUNC = textwrap.dedent(
    '''
    def vision_page_extractor(document: dict) -> list:
        """OpenAI Vision line-item extraction; adds llm_validate_block for Map step."""
        import os
        import json

        def _safe_float(v):
            try:
                return float(str(v).replace(",", ""))
            except Exception:
                return 0.0

        def _recover_partial_json(text):
            try:
                return json.loads(text)
            except Exception:
                pass
            items = []
            depth = 0
            start = -1
            for i, ch in enumerate(text):
                if ch == "{":
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == "}":
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

        def _build_validate_block(pg, tot_pg, extracted, err_note):
            lines = [f"PAGE {pg} OF {tot_pg}"]
            if err_note:
                lines.append(f"EXTRACTION_NOTE: {err_note}")
            lines.append(f"CANDIDATE_ITEM_COUNT: {len(extracted)}")
            lines.append("--- CANDIDATE_ITEMS ---")
            for idx, it in enumerate(extracted, 1):
                if not isinstance(it, dict):
                    continue
                lines.append(
                    f"  [{idx}] reference_number={it.get('reference_number')!r} | "
                    f"vend_cat_no={it.get('vend_cat_no')!r} | item={it.get('item')!r}"
                )
                lines.append(f"      description={it.get('description')!r}")
                lines.append(
                    f"      order_qty={it.get('order_qty')!r} "
                    f"trns_qty={it.get('trns_qty')!r} "
                    f"inv_uom={it.get('inv_uom')!r}"
                )
                lines.append(
                    f"      unit_price={it.get('unit_price')!r} "
                    f"extended_price={it.get('extended_price')!r}"
                )
                lines.append(
                    f"      tracking_number={it.get('tracking_number')!r} "
                    f"batch_number={it.get('batch_number')!r}"
                )
                lines.append(
                    f"      line_number={it.get('line_number')!r}"
                )
            lines.append(
                "--- END_CANDIDATE_ITEMS --- "
                "Apply VALIDATION RULES from the outer prompt."
            )
            return "\\n".join(lines)

        api_key = os.environ.get("OPENAI_API_KEY", "")
        aligned_text = document.get("aligned_text", "")
        image_b64 = document.get("image_b64")
        page_num = document.get("page_num", 1)
        total_pages = document.get("total_pages", 1)

        if not api_key:
            block = _build_validate_block(page_num, total_pages, [], "No OPENAI_API_KEY set")
            return [{"extracted_items": [], "extraction_error": "No OPENAI_API_KEY", "llm_validate_block": block}]

        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
        except Exception as e:
            block = _build_validate_block(page_num, total_pages, [], str(e))
            return [{"extracted_items": [], "extraction_error": str(e), "llm_validate_block": block}]

        system_msg = (
            "You are an expert invoice line-item extractor. Extract ALL product line items.\\n"
            "REQUIREMENTS:\\n"
            "  - A valid item MUST have: unit_price != 0 (nonzero  positive OR negative)\\n"
            "  - CREDIT/RETURN rows have NEGATIVE unit_price  include them, do NOT skip them\\n"
            "  - extended_price = trns_qty x unit_price (may be negative for credits)\\n"
            "  - Never invent values not visible on the page\\n"
            "SKIP: column headers, subtotal/total/tax/freight rows, section labels, blank rows\\n\\n"
            "QUANTITY RULES  critical for invoices with multiple qty columns:\\n"
            "  trns_qty = the SHIPPED/INVOICED quantity in THIS invoice document.\\n"
            "    If the table has separate columns like 'QTY ORD' and 'SHIP QTY' or 'INV QTY',\\n"
            "    use the SHIP/INV column, NOT the ORD column.\\n"
            "  If an item shows 0 shipped (backordered), set trns_qty=0 and extended_price=0.\\n"
            "  order_qty = the originally ordered quantity (may differ from trns_qty).\\n\\n"
            "IDENTIFIER FIELD RULES  read carefully, do not swap these fields:\\n"
            "  reference_number: product barcode/UPC/GTIN  the FIRST long numeric string at the\\n"
            "    start of the product row. Typically 12-14 digits. NOT a carrier tracking number.\\n"
            "  vend_cat_no: vendor catalog code  ALPHANUMERIC, usually contains letters AND digits,\\n"
            "    may include hyphens. Examples: SM-923, E2350H, 88861744-41\\n"
            "  item: buyer PO item code  SHORT (4-8 digit) NUMERIC code ONLY (no letters).\\n"
            "  line_number: sequential row counter from leftmost No./Line column\\n"
            "  tracking_number: CARRIER tracking  starts with FedEx/UPS/DHL etc.\\n"
            "  batch_number: batch/lot code\\n\\n"
            "LAYOUT RULES: MULTI-ROW ITEMS collapse into ONE dict; "
            "MULTIPLE SHIPMENTS = separate rows per tracking_number. "
            "If no product rows, return items: []"
        )

        prompt_text = (
            f"Extract ALL line items from invoice page {page_num} of {total_pages}.\\n\\n"
            "SPATIALLY ALIGNED PAGE TEXT (horizontal spaces = column positions):\\n"
            f"{aligned_text}\\n\\n"
            'Return JSON with \\"items\\" array with fields:\\n'
            "line_number, reference_number, item, vend_cat_no, description, order_qty,\\n"
            "trns_qty, inv_uom, unit_price, extended_price, tracking_number, batch_number"
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

        err = None
        valid_items = []
        try:
            resp = _call_api(system_msg, use_image=True)
            raw = resp.choices[0].message.content or "{}"
            finish = resp.choices[0].finish_reason
            if finish == "length":
                resp2 = _call_api(system_msg, use_image=False)
                raw2 = resp2.choices[0].message.content or "{}"
                items1 = (_recover_partial_json(raw).get("items") or [])
                items2 = (_recover_partial_json(raw2).get("items") or [])
                raw = raw2 if len(items2) >= len(items1) else raw
            data = _recover_partial_json(raw)
            items = data.get("items", data.get("line_items", []))
            valid_items = [
                it for it in items
                if isinstance(it, dict) and abs(_safe_float(it.get("unit_price"))) > 0
            ]
        except Exception as exc:
            err = str(exc)

        block = _build_validate_block(page_num, total_pages, valid_items, err)
        return [{
            "extracted_items": valid_items,
            "extraction_error": err,
            "llm_validate_block": block,
        }]
    '''
).strip()

# Map prompt: single Jinja variable only (no {% for %} / {% if %})
_VALIDATE_MAP_PROMPT = """Validate and correct invoice line items for this page.

{{ input.llm_validate_block }}

VALIDATION RULES:
1. MATH: extended_price equals trns_qty times unit_price within plus or minus one percent. Recalculate if wrong.
2. FIELD TYPES:
   reference_number = long numeric barcode (12-14 digits, FIRST number on the row)
   vend_cat_no = alphanumeric catalog code (contains LETTERS, e.g. SM-923, E2350H)
   item = SHORT numeric code (4-8 digits, NO letters).
   tracking_number = STARTS with carrier name (FedEx, UPS, DHL...)
3. SWAP FIX: If reference_number contains FedEx/UPS/DHL, move to tracking_number.
   If vend_cat_no is purely numeric (no letters), check if it should be item or reference_number.
4. NULL SENTINELS: Replace 'None', 'null', 'N/A', 'n/a', '-' with JSON null.
5. Remove items where unit_price is null or 0. KEEP items with NEGATIVE unit_price (credits).
6. SHIPPED QTY: If trns_qty=0 (backordered), extended_price must be 0.

Return JSON with key validated_items (array of objects with the same field names as candidates).
"""

# Gleaning prompts must satisfy DocETL Jinja detector; use harmless empty expression first.
_VALIDATE_GLEANING_PROMPT = """{{ "" }}

Review validated_items from your previous JSON reply in this conversation.
Check: math (extended_price vs trns_qty  unit_price), identifier roles
(barcode vs catalog vs tracking), no string sentinels like "null", credits kept.
Fix any remaining issues and return corrected validated_items JSON.
"""

_ITEM_SCHEMA_OUT = (
    "list[{"
    "line_number: string, reference_number: string, item: string, vend_cat_no: string, "
    "description: string, order_qty: number, trns_qty: number, inv_uom: string, "
    "unit_price: number, extended_price: number, tracking_number: string, batch_number: string"
    "}]"
)


def run_docetl_pipeline(
    pages_data: list[dict],
    run_id: str,
    workspace: Path,
) -> Optional[list[dict]]:
    input_path = workspace / f"{run_id}_input.json"
    output_path = workspace / f"{run_id}_output.json"
    intermediate_dir = str(workspace / f"{run_id}_intermediate")
    workspace.mkdir(parents=True, exist_ok=True)
    os.makedirs(intermediate_dir, exist_ok=True)

    input_path.write_text(json.dumps(pages_data, ensure_ascii=False), encoding="utf-8")

    print(f"  [DocETL] Input:    {input_path.name}  ({len(pages_data)} pages)")
    print(f"  [DocETL] Output:   {output_path.name}")

    validate_op = MapOp(
        name="validate_and_correct",
        type="map",
        model=MODEL,
        prompt=_VALIDATE_MAP_PROMPT,
        output={"schema": {"validated_items": _ITEM_SCHEMA_OUT}},
        gleaning={
            "num_rounds": 1,
            "validation_prompt": _VALIDATE_GLEANING_PROMPT,
        },
        litellm_completion_kwargs={
            "max_tokens": 16000,
            "temperature": 0,
        },
    )

    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", run_id)
    pt = ParsingTool(name="vision_page_extractor", function_code=_VISION_EXTRACT_FUNC)

    pipeline = Pipeline(
        name=f"invoice_etl_{safe_name}",
        datasets={
            "invoice_pages": Dataset(
                type="file",
                path=str(input_path.resolve()),
                parsing=[{"function": "vision_page_extractor"}],
            ),
        },
        parsing_tools=[pt],
        operations=[validate_op],
        steps=[
            PipelineStep(
                name="extract_and_validate",
                input="invoice_pages",
                operations=["validate_and_correct"],
            ),
        ],
        output=PipelineOutput(
            type="file",
            path=str(output_path.resolve()),
            intermediate_dir=intermediate_dir,
        ),
        default_model=MODEL,
        bypass_cache=False,
        system_prompt={
            "dataset_description": (
                "pages from vendor invoices (Covidien, Winchester, wholesalers, scanned or digital)."
            ),
            "persona": (
                "an expert accounts-payable specialist who extracts line items precisely "
                "and verifies qty times unit price equals extended price"
            ),
        },
    )

    prev_cwd = os.getcwd()
    try:
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
        # DocETL prints Unicode (e.g. checkmarks); Windows cp1252 consoles crash without this
        os.environ["PYTHONIOENCODING"] = "utf-8"
        os.environ["PYTHONUTF8"] = "1"
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        os.chdir(str(workspace.resolve()))
        cost = pipeline.run()
        print(f"  [DocETL] Pipeline finished (runner-reported cost: ${float(cost):.4f})")
    except Exception as exc:
        print(f"  [DocETL] FAILED: {exc}")
        return None
    finally:
        os.chdir(prev_cwd)

    if not output_path.exists():
        print(f"  [DocETL] Output file missing: {output_path}")
        return None
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else [data]
    except Exception as exc:
        print(f"  [DocETL] Failed to parse output JSON: {exc}")
        return None


# ?? Post-processing (unchanged) ????????????????????????????????????????????????


def _is_null_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip().lower() in (
        "none", "null", "n/a", "na", "", "-", "",
    )


def _coerce_nulls(item: dict) -> dict:
    return {k: (None if _is_null_str(v) else v) for k, v in item.items()}


def _safe_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _dedup_key(it: dict) -> tuple:
    return (
        str(it.get("reference_number") or ""),
        str(it.get("vend_cat_no") or ""),
        str(it.get("tracking_number") or ""),
        str(round(_safe_float(it.get("unit_price")), 2)),
    )


def _normalize_item_fields(it: dict) -> dict:
    item_val = it.get("item")
    ref_val = it.get("reference_number")
    tracking_val = it.get("tracking_number")

    if item_val and isinstance(item_val, str):
        stripped = item_val.strip().replace(" ", "")
        if len(stripped) >= 10 and stripped.isdigit():
            if not tracking_val:
                it["tracking_number"] = item_val
            it["item"] = None

    carrier_kw = ("fedex", "ups", "dhl", "usps", "freight")
    if ref_val and isinstance(ref_val, str):
        ref_low = ref_val.lower()
        if any(kw in ref_low for kw in carrier_kw):
            if not tracking_val:
                it["tracking_number"] = ref_val
            it["reference_number"] = None

    trns_qty_raw = it.get("trns_qty")
    order_qty_raw = it.get("order_qty")
    qty_src = trns_qty_raw if trns_qty_raw is not None else order_qty_raw
    qty = _safe_float(qty_src)
    upr = _safe_float(it.get("unit_price"))
    ext = _safe_float(it.get("extended_price"))

    if trns_qty_raw is not None and qty == 0 and ext != 0:
        it["extended_price"] = 0.0
    elif qty > 0 and upr != 0:
        expected_ext = round(qty * upr, 2)
        if ext == 0 or abs(ext - expected_ext) / max(abs(expected_ext), 0.01) > 0.05:
            it["extended_price"] = expected_ext

    return it


def aggregate_pages(page_records: list[dict]) -> list[dict]:
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
    extracted = sum(_safe_float(it.get("extended_price")) for it in items)
    if expected is None:
        status = "NO_SUBTOTAL"
    elif abs(extracted - expected) <= max(0.02 * expected, 1.0):
        status = "PASS"
    else:
        diff = extracted - expected
        status = f"FAIL (diff={diff:+.2f})"
    return extracted, status


def process_pdf(
    pdf_path: str,
    outdir: Path,
    workspace: Path,
) -> dict:
    pdf_path_obj = Path(pdf_path).resolve()
    pdf_name = pdf_path_obj.name
    stem = pdf_path_obj.stem

    print(f"\n{'=' * 64}")
    print(f"  PDF: {pdf_name}")
    print(f"{'=' * 64}")

    print(f"  [1/3] Loading PDF ({_LOADER_NAME}) ...")
    pages = load_pdf_pages(str(pdf_path_obj))
    if not pages:
        return {"error": "No pages loaded", "pdf": pdf_name}

    print(f"        Pages: {len(pages)}")

    subtotal = detect_subtotal(pages)
    po_number = extract_po_number(pages)
    print(f"        PO Number : {po_number or '(not found)'}")
    print(f"        Subtotal  : {subtotal} (regex)")

    print(f"  [2/3] Running DocETL pipeline ({len(pages)} pages) ...")

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
            "key_value_pairs": p.get("key_value_pairs", {}),
            "di_confidence": p.get("di_confidence", 1.0),
        }
        for p in pages
    ]

    run_id = f"{stem}_{uuid.uuid4().hex[:8]}"
    page_results = run_docetl_pipeline(docetl_input, run_id, workspace)

    if page_results is None:
        return {"error": "DocETL pipeline failed", "pdf": pdf_name, "po_number": po_number}

    print(f"  [3/3] Aggregating {len(page_results)} page results ...")
    items = aggregate_pages(page_results)
    item_sum = sum(_safe_float(it.get("extended_price")) for it in items)

    if (
        len(pages) > 3
        and subtotal is not None
        and item_sum > 0
        and abs(item_sum - subtotal) / max(item_sum, 0.01) > 0.20
    ):
        print("        Subtotal detection unreliable  trying LLM fallback...")
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

    out_stem = po_number or stem
    out_path = outdir / f"{out_stem}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"        Saved           : {out_path}")

    return result


def main() -> None:
    # Windows: allow Rich/DocETL to emit UTF-8 glyphs during pipeline.run()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Invoice extraction via DocETL Python Pipeline API."
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
        help="Workspace for DocETL temp files (default: docetl_workspace/)",
    )
    parser.add_argument("--loader", choices=["pdf", "azure"], default=None)

    args = parser.parse_args()

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

    print("\nInvoice DocETL (Python Pipeline API)")
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
