#!/usr/bin/env python3
"""
invoice_compare.py

Compare extracted invoice items (results/*.json) against retail.csv
and produce a per-field comparison JSON for each PO.

Statuses per field:
  MATCH              – invoice value matches CSV value
  NO MATCH           – both present but values differ
  NOT FOUND IN CSV   – invoice has a value; no corresponding CSV column

Null fields in a row are skipped entirely (not compared).
Always-null fields across the whole invoice are listed in the summary
under "fields_not_in_invoice".

Usage:
  python invoice_compare.py                          # all POs
  python invoice_compare.py --po N298589             # single PO
  python invoice_compare.py --results results --csv retail.csv --out compare
"""

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

# ── Field → CSV column mapping ────────────────────────────────────────────────
# Only invoice fields that have a direct counterpart in retail.csv.
FIELD_TO_CSV: dict[str, str] = {
    "line_number": "Line Number",
    "item":        "Item",
    "vend_cat_no": "Vend Cat No",
    "order_qty":   "Order Qty",
    "unit_price":  "Unit Price",
    "inv_uom":     "Inv Uom",
}

# Internal fields to exclude from comparison entirely
_META_FIELDS = {"_warnings", "_extended"}

# Fields where numeric (float) comparison applies
_NUMERIC = {"order_qty", "trns_qty", "unit_price", "extended_price"}

# Tolerance for numeric equality (e.g. rounding differences)
_NUM_TOL = 0.02


# ── Value helpers ──────────────────────────────────────────────────────────────

def _norm_str(v) -> str:
    """Lowercase + collapse whitespace for string comparison."""
    return re.sub(r"\s+", " ", str(v).strip().lower()) if v is not None else ""


def _norm_code(v) -> str:
    """Normalize an identifier code: strip all non-alphanumeric chars and lowercase.
    Treats '100-0288' and '1000288' as equal."""
    return re.sub(r"[^a-z0-9]", "", str(v).lower()) if v is not None else ""


# Fields where code-style normalization applies (strip dashes/spaces)
_CODE_FIELDS = {"item", "vend_cat_no", "line_number", "reference_number"}


def _to_float(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _compare(field: str, inv_val, csv_val) -> str:
    """Return MATCH or NO MATCH for one field."""
    if field in _NUMERIC:
        a, b = _to_float(inv_val), _to_float(csv_val)
        if a is None or b is None:
            return "NO MATCH"
        return "MATCH" if abs(a - b) <= _NUM_TOL else "NO MATCH"
    if field in _CODE_FIELDS:
        # Strip punctuation so '100-0288' == '1000288'
        return "MATCH" if _norm_code(inv_val) == _norm_code(csv_val) else "NO MATCH"
    return "MATCH" if _norm_str(inv_val) == _norm_str(csv_val) else "NO MATCH"


# ── Row matching ───────────────────────────────────────────────────────────────

def _build_csv_lookup(csv_rows: list[dict]) -> tuple[dict, dict, dict]:
    """Build three dicts keyed by line_number / vend_cat_no / item."""
    by_line: dict[str, dict] = {}
    by_vend: dict[str, dict] = {}
    by_item: dict[str, dict] = {}

    line_col = next((c for c in (csv_rows[0] if csv_rows else {}) if "line" in c.lower()), None)
    vend_col = next((c for c in (csv_rows[0] if csv_rows else {})
                     if "vend" in c.lower() or "cat" in c.lower()), None)
    item_col = next((c for c in (csv_rows[0] if csv_rows else {})
                     if c.lower().strip() == "item"), None)

    for row in csv_rows:
        if line_col:
            k = str(row.get(line_col, "")).strip().lstrip("0") or "0"
            by_line[k] = row
        if vend_col:
            k = str(row.get(vend_col, "")).strip()
            if k:
                by_vend[k] = row
        if item_col:
            k = str(row.get(item_col, "")).strip()
            if k:
                by_item[k] = row

    return by_line, by_vend, by_item


def _find_csv_row(item: dict,
                  by_line: dict, by_vend: dict, by_item: dict
                  ) -> tuple[dict | None, dict]:
    """Return (csv_row, match_key_dict). Tries line → vend_cat → item."""
    line_val = str(item.get("line_number") or "").strip().lstrip("0") or None
    if line_val and line_val in by_line:
        return by_line[line_val], {"line_number": line_val}

    vc_val = str(item.get("vend_cat_no") or "").strip() or None
    if vc_val and vc_val in by_vend:
        return by_vend[vc_val], {"vend_cat_no": vc_val}

    it_val = str(item.get("item") or "").strip() or None
    if it_val and it_val in by_item:
        return by_item[it_val], {"item": it_val}

    # No match — build a descriptive key from whatever is non-null
    key = {k: item[k] for k in ("line_number", "vend_cat_no", "item") if item.get(k)}
    return None, key


# ── Core comparison ────────────────────────────────────────────────────────────

def compare_invoice(po: str, items: list[dict], csv_rows: list[dict]) -> dict:
    """
    Compare extracted items against CSV rows for one PO.
    Returns the full comparison dict.
    """
    # Global field analysis ─ which fields are ever non-null?
    ever_non_null: set[str] = set()
    all_known: set[str] = set()
    for item in items:
        for k, v in item.items():
            if k in _META_FIELDS:
                continue
            all_known.add(k)
            if v is not None:
                ever_non_null.add(k)

    always_null = sorted(all_known - ever_non_null)

    # CSV lookup tables
    by_line, by_vend, by_item = _build_csv_lookup(csv_rows)

    # Per-row comparison
    comparison: list[dict] = []
    stats: dict[str, int] = {"MATCH": 0, "NO MATCH": 0, "NOT FOUND IN CSV": 0}

    for item in items:
        csv_row, match_key = _find_csv_row(item, by_line, by_vend, by_item)

        fields_out: dict[str, dict] = {}

        for field, inv_val in item.items():
            if field in _META_FIELDS:
                continue
            if inv_val is None:
                continue  # skip null fields on this row

            csv_col = FIELD_TO_CSV.get(field)

            if csv_col is None:
                # Invoice has a value; no matching CSV column exists
                status = "NOT FOUND IN CSV"
                csv_val = None
            elif csv_row is None:
                # No CSV row was matched for this invoice item
                status = "NOT FOUND IN CSV"
                csv_val = None
            else:
                raw = str(csv_row.get(csv_col, "")).strip()
                csv_val = raw if raw else None
                if csv_val is None:
                    status = "NOT FOUND IN CSV"
                else:
                    status = _compare(field, inv_val, csv_val)

            fields_out[field] = {
                "invoice": inv_val,
                "csv":     csv_val,
                "status":  status,
            }
            stats[status] = stats.get(status, 0) + 1

        if fields_out:
            comparison.append({
                "match_key":    match_key,
                "csv_row_found": csv_row is not None,
                "fields":       fields_out,
            })

    return {
        "po_number": po,
        "comparison": comparison,
        "summary": {
            "total_invoice_items":    len(items),
            "csv_rows_available":     len(csv_rows),
            "items_matched_to_csv":   sum(1 for r in comparison if r["csv_row_found"]),
            "items_unmatched_to_csv": sum(1 for r in comparison if not r["csv_row_found"]),
            "fields_not_in_invoice":  always_null,
            "field_stats":            stats,
        },
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compare invoice extractions against retail.csv")
    ap.add_argument("--results", default="results",    help="Folder with extracted JSONs")
    ap.add_argument("--csv",     default="retail.csv", help="Path to retail CSV/Excel")
    ap.add_argument("--out",     default="compare",    help="Output folder for comparison JSONs")
    ap.add_argument("--po",      default=None,         help="Process a single PO only (e.g. N298589)")
    args = ap.parse_args()

    results_dir = Path(args.results)
    out_dir     = Path(args.out)
    out_dir.mkdir(exist_ok=True)

    # Load reference CSV / Excel
    csv_path = Path(args.csv)
    try:
        if csv_path.suffix.lower() in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
            df = pd.read_excel(csv_path, dtype=str)
        else:
            df = pd.read_csv(csv_path, dtype=str)
    except Exception as e:
        print(f"ERROR loading CSV: {e}", file=sys.stderr)
        sys.exit(1)

    po_col = df.columns[0]

    # Collect result JSONs
    json_files = sorted(results_dir.glob("*.json"))
    if args.po:
        json_files = [f for f in json_files if f.stem == args.po]
    if not json_files:
        print("No result JSON files found.")
        sys.exit(0)

    all_summaries: list[dict] = []

    for jf in json_files:
        po = jf.stem
        print(f"\n{'='*60}")
        print(f"  {po}")
        print(f"{'='*60}")

        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ERROR reading {jf}: {e}")
            continue

        items = data.get("line_items", [])
        if not items:
            print("  No line items — skipping.")
            continue

        # Filter CSV rows for this PO
        csv_df   = df[df[po_col].str.strip() == po]
        csv_rows = csv_df.to_dict(orient="records")
        if not csv_rows:
            print(f"  WARNING: no CSV rows found for {po} — all fields will be NOT FOUND IN CSV")

        result = compare_invoice(po, items, csv_rows)

        out_path = out_dir / f"{po}_compare.json"
        out_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        s = result["summary"]
        print(f"  Invoice items  : {s['total_invoice_items']}")
        print(f"  CSV rows       : {s['csv_rows_available']}")
        print(f"  Matched to CSV : {s['items_matched_to_csv']}")
        print(f"  Unmatched      : {s['items_unmatched_to_csv']}")
        print(f"  Field stats    : {s['field_stats']}")
        print(f"  Always null    : {s['fields_not_in_invoice']}")
        print(f"  -> {out_path}")

        all_summaries.append({"po_number": po, **s})

    # Write a combined summary
    summary_path = out_dir / "_summary.json"
    summary_path.write_text(
        json.dumps(all_summaries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nAll done. Summary -> {summary_path}")


if __name__ == "__main__":
    main()
