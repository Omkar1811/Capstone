#!/usr/bin/env python3
"""
backend/api.py — FastAPI backend for the Invoice Processing UI.

Wraps invoice_pipeline_combined.py and invoice_compare.py as REST endpoints.

Start with:
    uvicorn backend.api:app --reload --port 8000
  or from this folder:
    python backend/api.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the project root importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import fitz  # PyMuPDF
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from invoice_compare import compare_invoice
from invoice_pipeline_combined import extract_invoice, _extract_po_number

# ── App setup ──────────────────────────────────────────────────────────────────

app = FastAPI(title="Invoice Processing API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR  = Path("uploads")
RESULTS_DIR = Path("results")
COMPARE_DIR = Path("compare")
CSV_PATH    = Path("retail.csv")

for d in [UPLOAD_DIR, RESULTS_DIR, COMPARE_DIR]:
    d.mkdir(exist_ok=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _find_pdf(po: str) -> Path | None:
    for directory in [UPLOAD_DIR, Path(".")]:
        for ext in [".pdf", ".PDF"]:
            p = directory / f"{po}{ext}"
            if p.exists():
                return p
    return None


def _load_csv() -> pd.DataFrame | None:
    if not CSV_PATH.exists():
        return None
    suffix = CSV_PATH.suffix.lower()
    try:
        if suffix in (".xlsx", ".xls", ".xlsm", ".xlsb"):
            return pd.read_excel(CSV_PATH, dtype=str)
        return pd.read_csv(CSV_PATH, dtype=str)
    except Exception as exc:
        print(f"[CSV] Failed to load {CSV_PATH}: {exc}")
        return None


def _csv_rows_for_po(po: str) -> tuple[list[dict], list[str]]:
    df = _load_csv()
    if df is None:
        return [], []
    po_col = df.columns[0]
    rows = df[df[po_col].str.strip() == po]
    return rows.to_dict(orient="records"), list(df.columns)


# ── PDF endpoints ──────────────────────────────────────────────────────────────

@app.post("/api/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    """
    Upload a PDF.

    Saves the file, extracts the PO number from its content, then renames the
    file to <po>.pdf so that all subsequent endpoints (PDF viewer, extraction,
    compare) can locate it consistently by PO number.

    Returns po_number and page count.  po is null when the PO cannot be found.
    """
    content = await file.read()
    # Save under the original filename first so _extract_po_number can open it.
    dest = UPLOAD_DIR / file.filename
    dest.write_bytes(content)

    doc = fitz.open(stream=content, filetype="pdf")
    pages = len(doc)
    doc.close()

    # Extract the PO number from the PDF content itself.
    po = _extract_po_number(str(dest))

    if po is None:
        # Keep the file under its original name so the user can still see it.
        return {"po": None, "filename": file.filename, "pages": pages,
                "error": "PO number not found in PDF"}

    # Rename the saved PDF to <po>.pdf so _find_pdf(po) can locate it.
    canonical = UPLOAD_DIR / f"{po}.pdf"
    if dest != canonical:
        dest.rename(canonical)

    return {"po": po, "filename": file.filename, "pages": pages}


@app.get("/api/pdf-info/{po}")
def pdf_info(po: str):
    pdf = _find_pdf(po)
    if not pdf:
        raise HTTPException(404, f"PDF not found for PO {po}")
    doc = fitz.open(str(pdf))
    pages = len(doc)
    doc.close()
    return {"po": po, "pages": pages}


@app.get("/api/pdf-page/{po}")
def pdf_page(po: str, page: int = 1, dpi: int = 150):
    """Render one PDF page as PNG. `page` is 1-indexed."""
    pdf = _find_pdf(po)
    if not pdf:
        raise HTTPException(404, f"PDF not found for PO {po}")

    doc = fitz.open(str(pdf))
    if page < 1 or page > len(doc):
        doc.close()
        raise HTTPException(400, f"Page {page} out of range (1–{len(doc)})")

    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = doc[page - 1].get_pixmap(matrix=mat)
    png = pix.tobytes("png")
    doc.close()

    return Response(content=png, media_type="image/png")


# ── CSV endpoints ──────────────────────────────────────────────────────────────

@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    """Upload a CSV or Excel file (saved as retail.csv / retail.xlsx)."""
    global CSV_PATH
    content = await file.read()
    dest = Path(file.filename)
    dest.write_bytes(content)
    CSV_PATH = dest

    # Return a quick summary so the UI can confirm the file loaded correctly.
    df = _load_csv()
    if df is None:
        raise HTTPException(400, f"Could not parse uploaded file as CSV/Excel: {file.filename}")
    po_col = df.columns[0]
    distinct_pos = sorted(df[po_col].dropna().str.strip().unique().tolist())
    return {"filename": file.filename, "rows": len(df), "pos": distinct_pos}


@app.get("/api/csv-rows/{po}")
def csv_rows(po: str):
    rows, columns = _csv_rows_for_po(po)
    return {"po": po, "columns": columns, "rows": rows, "total": len(rows)}


# ── PO listing ─────────────────────────────────────────────────────────────────

@app.get("/api/list-pos")
def list_pos():
    """Return all POs with existing results or uploaded PDFs."""
    pos: set[str] = set()
    for f in RESULTS_DIR.glob("*.json"):
        pos.add(f.stem)
    for d in [UPLOAD_DIR, Path(".")]:
        for f in d.glob("*.pdf"):
            pos.add(f.stem)
    return {"pos": sorted(pos)}


# ── Extraction endpoint ────────────────────────────────────────────────────────

def _do_extraction(po: str, force: bool) -> dict:
    # Check cache using the caller-provided PO first.
    cache = RESULTS_DIR / f"{po}.json"
    if cache.exists() and not force:
        data = json.loads(cache.read_text(encoding="utf-8"))
        data["_cached"] = True
        return data

    pdf = _find_pdf(po)
    if not pdf:
        raise FileNotFoundError(f"PDF not found for PO {po}")

    sample_rows, csv_columns = _csv_rows_for_po(po)

    result = extract_invoice(
        str(pdf),
        use_vision=True,
        csv_samples=sample_rows[:10],
        csv_columns=csv_columns,
    )

    # Always write the cache under the caller-provided `po`.  Since upload
    # now renames the PDF to <po>.pdf, `po` is already the PDF-extracted PO.
    # The `po_number` field inside the JSON is used by compare for CSV lookup.
    cache = RESULTS_DIR / f"{po}.json"
    cache.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    result["_cached"] = False
    return result


@app.post("/api/extract/{po}")
async def run_extraction(po: str, force: bool = False):
    """Run invoice extraction (async; uses cache unless force=true)."""
    try:
        result = await run_in_threadpool(_do_extraction, po, force)
        return result
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"Extraction failed: {e}")


@app.get("/api/results/{po}")
def get_results(po: str):
    path = RESULTS_DIR / f"{po}.json"
    if not path.exists():
        raise HTTPException(404, f"No results for {po}. Run extraction first.")
    return json.loads(path.read_text(encoding="utf-8"))


# ── Comparison endpoint ────────────────────────────────────────────────────────

def _do_compare(po: str) -> dict:
    results_path = RESULTS_DIR / f"{po}.json"
    if not results_path.exists():
        raise ValueError(f"No extraction results for {po}. Run extraction first.")

    data  = json.loads(results_path.read_text(encoding="utf-8"))
    items = data.get("line_items", [])

    # Use the PO number stored inside the result file (which is the one
    # extracted from the PDF) rather than the URL parameter — they may differ
    # if the PDF used a non-standard PO label.
    csv_po = data.get("po_number") or po
    csv_rows, _ = _csv_rows_for_po(csv_po)

    result = compare_invoice(csv_po, items, csv_rows)

    out = COMPARE_DIR / f"{po}_compare.json"
    out.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return result


@app.post("/api/compare/{po}")
async def run_comparison(po: str):
    try:
        return await run_in_threadpool(_do_compare, po)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Comparison failed: {e}")


@app.get("/api/compare/{po}")
def get_comparison(po: str):
    path = COMPARE_DIR / f"{po}_compare.json"
    if not path.exists():
        raise HTTPException(404, f"No comparison for {po}. Run comparison first.")
    return json.loads(path.read_text(encoding="utf-8"))


# ── Dev runner ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.api:app", host="0.0.0.0", port=8000, reload=True)
