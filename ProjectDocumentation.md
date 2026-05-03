# Invoice Validation Project — Full Technical Documentation

**Project:** Agnostic Invoice Line-Item Extraction & Validation Engine  
**Primary pipeline:** `invoice_pipeline_combined.py`  
**Comparison script:** `invoice_compare.py`  
**Web UI:** FastAPI backend (`backend/api.py`) + React + Vite frontend (`frontend/`)  
**LLM:** OpenAI `gpt-4o` (fallback: Azure OpenAI)  
**Last updated:** 2026-05-04

---

## 1. Executive Summary

This project extracts line items from vendor invoice PDFs and validates them against purchase order rows stored in a CSV or Excel reference file. The entire output is a structured JSON per invoice that can then be compared field-by-field against the reference data, with results displayed in a web UI.

The system is **vendor-agnostic**: it handles diverse invoice layouts without hardcoding vendor-specific rules. Different vendors use different column orderings, identifier naming conventions, row structures, and page layouts — and the pipeline is designed to adapt to all of them rather than being tuned to any one format.

The high-level flow is:

```
Invoice PDF
    │
    ▼
PO Extraction (regex → LLM)
    │
    ▼
Three-Tier Extraction
    ├─ T1: Camelot (deterministic table)
    ├─ T2: Multimodal Vision LLM (gpt-4o, image + text)
    └─ T3: Text-only LLM fallback
    │
    ▼
Best-Tier Selection (subtotal diff → identifier coverage)
    │
    ▼
Post-Extraction Normalization (field swaps, barcode null, recovery)
    │
    ▼
Dynamic Column Mapping (discover_mapping → _validate_mapping)
    │
    ▼
Mapped JSON Output → Comparison → Web UI
```

---

## 2. Business Problem

Finance and procurement teams receive invoices from vendors and must validate them against internal purchase order records. Key questions they need to answer:

- Did the vendor use the correct PO?
- Which invoice line items match the PO rows?
- Which rows have mismatched quantities, prices, or identifiers?
- Which invoice rows are not found in the PO data?
- Which PO rows were not invoiced?

The manual workflow is slow and error-prone. For invoices with 40–120 line items, a user may spend 20–40 minutes on a single invoice. This system aims to reduce that effort to near-zero while keeping output explainable for finance review.

---

## 3. Why This Is Hard

### Invoice-side challenges

- Vendors use completely different column names, orderings, and layouts
- One logical line item can span multiple visual rows (description wrap, sub-row identifiers)
- The same SKU may appear multiple times for different shipments (different tracking numbers)
- Identifier fields (item code, vendor catalog number, barcode) are often ambiguous or stacked
- Some PDFs are scanned images with no machine-readable text layer
- Dense invoices may have 40+ items on a single page with minimal whitespace
- Page boundaries can split a single line item across two pages
- Tracking/batch/lot information appears as sub-rows under the main item row

### Reference data (CSV/Excel) challenges

- The CSV uses different column names than the invoice (e.g. "Bltr_Itm#" → "Item")
- The mapping between invoice columns and CSV columns varies per vendor
- The CSV may contain more PO rows than appear on a partial/interim invoice
- Identifier codes in the CSV may differ in format from the invoice (e.g. `100-0288` vs `1000288`)
- Some CSV columns contain buyer-internal codes that don't appear on the invoice at all

---

## 4. Project File Structure

```
GenAI Capstone/
├── invoice_pipeline_combined.py     # Main extraction + mapping pipeline (3,700+ lines)
├── invoice_compare.py               # Field-level comparison: extracted vs CSV (315 lines)
│
├── backend/
│   └── api.py                       # FastAPI REST backend for the web UI
│
├── frontend/                        # React + Vite + TypeScript web UI
│   ├── src/
│   │   ├── App.tsx                  # Root component, state management
│   │   ├── api.ts                   # Type-safe API client
│   │   ├── types.ts                 # TypeScript interfaces
│   │   └── components/
│   │       ├── Toolbar.tsx          # PO selector, upload buttons, status badge
│   │       ├── PDFViewer.tsx        # Page-by-page PDF renderer
│   │       ├── CSVPreview.tsx       # Reference data preview table
│   │       ├── ExtractedFields.tsx  # Extracted line items table
│   │       └── ComparisonTable.tsx  # Grouped, filterable comparison view
│   ├── vite.config.ts               # Dev proxy: /api → localhost:8000
│   └── package.json
│
├── uploads/                         # PDFs uploaded via UI (renamed to <po>.pdf)
├── results/                         # Cached extraction JSON files (one per PO)
├── compare/                         # Comparison JSON files (one per PO)
│
├── retail.csv                       # Reference PO data (CSV or Excel)
├── .env                             # API keys and model config
└── ProjectDocumentation.md          # This file
```

---

## 5. Environment Setup

### `.env` file

```env
# Primary LLM — standard OpenAI (preferred)
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o                  # gpt-4o or gpt-4o-mini

# Fallback — Azure OpenAI (used only when OPENAI_API_KEY is absent)
AZURE_OPENAI_ENDPOINT=https://...
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_API_VERSION=2024-04-01-preview
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o

# Reserved for future Azure Document Intelligence integration
AZURE_DI_ENDPOINT=https://...
AZURE_DI_KEY=...

# Optional extraction tuning
INVOICE_VISION_DPI=150               # Page render DPI (96–300, default 150)
INVOICE_SPLIT_PROMPTS=false          # Enable experimental two-stage extraction
```

### Python dependencies

```powershell
pip install fastapi "uvicorn[standard]" pdfplumber pymupdf camelot-py opencv-python pandas openpyxl python-dotenv openai
```

### Frontend dependencies

```powershell
cd frontend
npm install
```

---

## 6. How to Run

### Start servers (two terminal windows)

**Terminal 1 — Backend:**
```powershell
uvicorn backend.api:app --reload --port 8000
```

**Terminal 2 — Frontend:**
```powershell
cd frontend
npm run dev
```

Open the UI at **http://localhost:5173**

### CLI usage (no UI)

```powershell
# Extraction only — saves raw JSON to results/
python invoice_pipeline_combined.py N298589.pdf

# Extraction + CSV mapping — saves mapped JSON to results_mapped/
python invoice_pipeline_combined.py N298589.pdf --csv retail.csv --save results_mapped

# Multiple invoices at once
python invoice_pipeline_combined.py N*.pdf --csv retail.csv --save results_mapped

# Force re-extraction (bypass cache)
python invoice_pipeline_combined.py N298589.pdf --csv retail.csv --save results_mapped --force

# Text-only mode (skip vision, useful for debugging)
python invoice_pipeline_combined.py N298589.pdf --no-vision

# Run comparison only (after extraction)
python invoice_compare.py
python invoice_compare.py --po N298589
```

### Clear extraction cache

```powershell
Remove-Item results\*.json
Remove-Item compare\*_compare.json
```

---

## 7. Architecture — Three-Tier Extraction

The extraction runs up to three tiers per invoice and selects the best result.

### Tier 1 — Camelot (Deterministic Table Extraction)

**What it does:** Uses the `camelot` library to detect and parse machine-readable tables from the PDF. Tries `lattice` flavor first (for bordered tables; requires Ghostscript), then `stream` flavor (for borderless/whitespace-delimited tables).

**Safety gates** (all must pass, or T1 is rejected):
1. At least one table found with ≥ 2 rows and ≥ 3 columns
2. Table accuracy score ≥ 70 (Camelot's internal metric)
3. A header row must be found containing invoice column keywords
4. At least one of `unit_price` or `extended_price` must be mappable from the header
5. At least 3 plausible line items must be extracted
6. If the subtotal is known: the extracted total must be within 2% of it

**Important:** T1 always runs and its result is a **candidate** in the final comparison — it never causes an early exit. Even if T1 PASS, T2-vision also runs. The comparator picks the best.

**When T1 works well:** Simple, structured invoices with visible grid lines or consistent column alignment where the full table fits Camelot's parsing model.

**When T1 fails:** Complex layouts, scanned PDFs, invoices with merged cells, or invoices where the table spans pages inconsistently.

### Tier 2 — Multimodal Vision LLM (Primary Path)

**What it does:** Renders each PDF page as a PNG image (default 200 DPI for extraction quality), sends the image plus a spatially-aligned text representation to `gpt-4o`, and parses the JSON response. This is the primary tier for most real-world invoices.

**Per-page pipeline:**

```
Page N
  │
  ├─ Render page as PNG (PyMuPDF at 200 DPI)
  ├─ Extract words with bounding boxes (pdfplumber)
  ├─ Render aligned text (groups words by Y-coordinate into rows)
  │
  ├─ [First page] One-shot column layout analysis
  │     → identify column count, column names, layout style
  │     → inject as layout_context into all subsequent prompts
  │
  ├─ [First content page] Build field schema
  │     → derive dominant value type per field (item_code, catalog_code, barcode)
  │     → inject as schema_context into subsequent prompts
  │
  ├─ Build per-page prompt with:
  │     • System: _EXTRACT_SYSTEM + _EXTRACTION_RULES
  │     • User: PNG image + aligned text + layout_context + schema_context
  │             + subtotal_hint + page_ctx + csv_sample + retry_hint (on retry)
  │
  ├─ LLM call → parse JSON response
  ├─ Quality check (suspicious values, item count sanity)
  └─ Retry with quality_hint if quality check fails
```

**Scanned PDF support:** If a page has image data but no machine-readable text (scanned PDF), the page is still processed — the image is sent to the LLM without the aligned-text component.

**T3 fallback:** If T2 returns 0 items for a page, that page is re-processed with text-only mode (T3).

### Tier 3 — Text-Only LLM (Fallback)

**What it does:** The same extraction logic as T2 but sends only the spatially-aligned text (no image). Used when:
- Image rendering failed (PyMuPDF not available)
- T2 returned 0 items for a page
- `--no-vision` flag passed on CLI

### Tier Comparator

After all tiers run, the best result is selected using:

1. **Primary criterion:** Subtotal difference (`|extracted_total - detected_subtotal|`)
   - Lower diff wins. A tier with diff = 0 is PASS.
2. **Tiebreaker:** Identifier coverage — count of non-null `item` + `vend_cat_no` fields across all items. Higher coverage wins.

This prevents T1-Camelot from winning a tie with empty identifier fields.

---

## 8. PO Number Extraction

The PO number is extracted **from the PDF content**, not the filename. This matters because:
- Files are often renamed or given generic names (`invoice.pdf`, `1637737.pdf`)
- The true PO (e.g. `N335512`) may differ from the filename
- All downstream logic (CSV lookup, caching, comparison) must use the same key

**Three-stage strategy:**

**Stage 1 — Regex (fast, no LLM cost):**
Reads the first 2 pages via pdfplumber and tests against 9 patterns in priority order:

```python
# Standard labels
r'Purchase\s+Order\s*(?:Number|No\.?|#)?\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})'
r'P\.?O\.?\s*(?:Number|No\.?|#)\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})'
r'\bPO\s*[:\-#]\s*([A-Z0-9][-A-Z0-9]{2,20})'
r'Purchase\s+Order\s*[:\-]?\s+([A-Z0-9][-A-Z0-9]{2,20})'
# Non-standard / vendor-specific labels
r'\bFO\s+No\.?\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})'      # "FO No: N335512"
r'Customer\s+(?:P\.?O\.?|Order)\s*(?:No\.?|#)?\s*[:\-]\s*...'
r'(?:Cust(?:omer)?\.?\s+)?P\.?O\.?\s*[:\-#]\s*...'
r'(?:Your|Buyer[\'s]*)\s+(?:P\.?O\.?|Order)\s*...'
# Generic "Order No" — lower priority (often matches internal IDs)
r'Order\s+(?:Number|No\.?|#)\s*[:\-]\s*([A-Z0-9][-A-Z0-9]{2,20})'
```

All matches are collected. **Candidates starting with a letter** (e.g. `N335512`) are preferred over purely numeric matches (e.g. `1637737`), because buyer POs in this domain typically begin with a letter.

**Stage 2 — LLM (only if regex found nothing):**
Single call with `max_tokens=20, temperature=0`. Prompt explicitly asks for the "customer's purchase order number" and returns `UNKNOWN` if not found.

**Stage 3 — Returns `None`:**
No fallback to filename. The upload endpoint returns `{"po": null, "error": "PO number not found in PDF"}` and the UI shows an error banner.

**File rename on upload:**
After PO extraction, the uploaded PDF is renamed from its original name (`1637737.pdf`) to `<po>.pdf` (`N335512.pdf`). This ensures the PDF viewer, extraction cache, and comparison all use the same key consistently.

---

## 9. Prompt Engineering

### Design principles

- **Separation of concerns:** System message contains identity, schema, and rules. User message contains page data. Models follow system messages more reliably than buried mid-prompt instructions.
- **Vendor-agnostic:** No real brand names, vendor codes, or PO patterns from the test data appear in any prompt. Abstract archetypes only.
- **Cross-LLM portable:** All output guards are stated explicitly ("begin with `{`", "no markdown") so they work even without `response_format={"type":"json_object"}`.
- **Deterministic-friendly:** No creative cues, temperature set to 0–0.2 at the call site.

### System prompt structure (`_EXTRACT_SYSTEM` + `_EXTRACTION_RULES`)

**`_EXTRACT_SYSTEM`** — 4 core principles:
1. Read the document as a table; identify column boundaries first
2. A valid line item requires both a positive shipped qty AND a positive unit price; math must balance within 1%
3. Never invent values; null if not present; do not copy from prompt examples
4. Output JSON only; begin with `{`

**`_EXTRACTION_RULES`** — detailed extraction procedure:

| Section | Content |
|---|---|
| HOW TO READ THE TABLE | 5-step mental procedure: locate header, walk rows, check continuation, skip non-items, verify math |
| SKIP these row types | Headers, totals, tax, freight, section headers, back-ordered (qty=0) rows |
| MULTI-LINE ITEMS | When a row spans visual lines — collapse into ONE JSON object |
| MULTIPLE SHIPMENTS | Same SKU, different tracking numbers = separate JSON objects; do NOT merge |
| IDENTIFIER FIELD RULES | Priority A–G for assigning values to `reference_number`, `vend_cat_no`, `item`, `line_number` |
| FIELD GUIDE | Per-field definition with examples |
| ITEM-vs-TRACKING RULES | Short numeric code with no carrier name = buyer item code, not tracking number |
| QUANTITY RULES | Always fill both `order_qty` and `trns_qty`; they often differ |
| UOM RULES | Extract the invoice-visible UOM; do not infer or normalize |
| COMMON MISTAKES TO AVOID | Explicit list of known LLM failure modes |

### Per-page context injection

Each page prompt is assembled from:

| Block | Source | Purpose |
|---|---|---|
| `layout_context` | One-shot LLM call on first page | Column count, column names, layout style (one-row-per-item vs. multi-row) |
| `schema_context` | First-page field schema analysis | Dominant value type per field (item_code, catalog_code, barcode) |
| `subtotal_hint` | Python subtotal detection | Anchors the math contract — "extracted total should sum to $X" |
| `page_ctx` | Page counter | "Page 2 of 4" — helps LLM understand continuation context |
| `csv_sample` | Up to 10 CSV rows | Shows value patterns (e.g. that "Item" is a 6-digit code) |
| `retry_hint` | Quality check failure | "Previous attempt had suspicious values in these fields; review carefully" |

### CSV sample anti-hallucination guards

The 10 CSV rows passed to the LLM are accompanied by an explicit instruction:

> "These rows are reference patterns only. Do NOT copy values from them. Extract ONLY what is visible on the invoice page. If a value on the invoice matches a CSV value, that is a coincidence — still extract from the invoice."

This prevents the LLM from substituting known-good values from the CSV when the invoice is ambiguous.

### Experimental two-stage extraction (`INVOICE_SPLIT_PROMPTS=true`)

Splits each page into two sequential LLM calls:
- **Stage A:** Structural extraction — table layout, quantities, prices, descriptions
- **Stage B:** Identifier classification — categorize identifiers found in Stage A into `item`, `vend_cat_no`, `reference_number`

Off by default. Testing showed it improved identifier fill rates but introduced regressions on some invoice formats.

---

## 10. Post-Extraction Normalization

After the LLM returns items, a deterministic Python layer applies corrections:

### `_coerce_null_sentinels`
LLMs occasionally return the string `"None"`, `"null"`, `"N/A"`, `"-"`, or `"--"` instead of a proper JSON `null`. This function converts all such sentinel strings to Python `None` so downstream logic correctly treats them as missing.

### `_normalize_field_assignments(items, schema)`
Applies schema-based field corrections:

- **Field swap detection:** If `item` contains a catalog-style value and `vend_cat_no` contains an item-code-style value, swap them.
- **Barcode nullification:** If `line_number` contains a 10+ digit barcode, move it to `reference_number`.
- **vend_cat_no → item move:** When the schema shows `vend_cat_no: item_code` (the LLM consistently placed buyer item codes into vend_cat_no), move all values to `item` and null `vend_cat_no`.
- **Keep-original policy:** If a value doesn't match the schema but no swap candidate exists, keep the original and attach a `_warnings` note. Does not silently erase potentially correct data.

### `_dedup_target_fields(items, schema)`
Detects when the LLM duplicated the same value into multiple identifier fields (e.g. `item == vend_cat_no`). This happens when an invoice only has one identifier column. Keeps the field that best matches the page-voted schema; nulls the duplicates. Priority when no schema match: `vend_cat_no > item > reference_number`.

### `_reclassify_tracking_vs_item(items)`
Short numeric codes (typically 4–8 digits) placed in `tracking_number` by the LLM — but without an associated carrier name — are reclassified as buyer item codes and moved to `item`. Real tracking numbers always appear alongside a carrier name (FedEx, UPS, DHL, etc.) in the invoice.

### `_fix_misplaced_barcodes(items)`
Values of 10+ digits placed in `line_number` are moved to `reference_number`. Line numbers are always short sequential counters (typically 3 digits or fewer).

### `_recover_missing_item_codes(items, pages)`
After the full extraction, if any items still have null `item` or `vend_cat_no`, a **targeted re-prompt** is issued for each affected page. The re-prompt sends the page image with a focused instruction to find the missing identifier for specific rows. Results are merged back into the main item set.

This addresses the "LLM attention bias at page boundaries" problem — items at the very bottom of a page are sometimes missed in the initial pass.

---

## 11. Subtotal Detection and Verification

### Detection methods (tried in order)
1. **Regex patterns** on the spatially-aligned text — matches common labels like `Subtotal`, `Invoice Total`, `Amount Due`, `Total Due`, etc. Extracts the associated numeric value.
2. **Vision LLM fallback** (`_llm_subtotal_from_image`) — if regex finds nothing, sends the last page image to the LLM with a focused subtotal-extraction prompt.

### Verification

The verification block computes:
```
extracted_total = sum(item.extended_price for all items)
diff = |extracted_total - detected_subtotal|
status = PASS if diff < 0.01 else FAIL
```

**Important:** The subtotal is **diagnostic only**. The system never adjusts quantities, unit prices, or extended prices to make the total match. A FAIL status means "investigate", not "auto-correct".

---

## 12. Dynamic Column Mapping

The mapping engine bridges the gap between the extraction output (canonical field names like `item`, `vend_cat_no`) and the CSV column names (which vary per vendor: `Bltr_Itm#`, `Item`, `VND CAT`, etc.).

### `discover_mapping(csv_columns, csv_rows, invoice_items)`

Two-pass approach:

**Pass 1 — Semantic rules:** Match CSV column names to known field keywords:

| Keywords | Maps to field |
|---|---|
| `["item"]` | `item` |
| `["vend", "cat"]` or `["catalog"]` | `vend_cat_no` |
| `["line"]` | `line_number` |
| `["qty"]` or `["quantity"]` | `order_qty` |
| `["price"]` | `unit_price` |
| `["uom"]` | `inv_uom` |
| `["order", "number"]` | `PO_NUMBER` |

**Pass 2 — Value overlap scoring:** For each unmapped CSV column, score it against each extraction field by counting how many CSV cell values appear in the extracted invoice values (normalized: strip non-alphanumeric, lowercase). The field with the highest overlap score wins.

**Compound-code detection:** If a CSV column contains values that look like `field1 + separator + field2` (e.g. `N335512-002`), the mapper creates a compound mapping like `item+line_number` and the apply step joins the values with the detected separator.

**Buyer-code column detection (`_is_buyer_code_col`):** Identifies and skips CSV columns that contain purely numeric buyer-internal codes not present on the vendor invoice.

### `_validate_mapping(mapping, csv_rows, invoice_items, csv_columns)`

Validation and correction of the initial mapping:

**Semantic trust guard:** If a CSV column name is a clear semantic match for its mapped field (e.g. "Item" → `item`), the mapping is **trusted and preserved** even if extracted values are sparse. This prevents `_validate_mapping` from overriding correct mappings just because a recovery prompt filled in values late.

**Intentionally-null tracking:** If the mapped field has zero extracted values (e.g. `vend_cat_no` is always null in this invoice), the column is set to `None` and added to `intentionally_null`. This prevents it from being re-mapped to a different field by coincidental value overlap.

**Line Number special case:** The `Line Number` CSV column is left unmapped if no `line_number` values were extracted — avoids mapping it to an unrelated field.

**Format consistency check:** For each non-semantic column, validates that the assigned field's value format (numeric/alphanumeric/etc.) is consistent with what was extracted. Mismatches are nulled.

---

## 13. Comparison Engine (`invoice_compare.py`)

### Field mapping

Only invoice fields with direct CSV counterparts are compared:

| Invoice field | CSV column |
|---|---|
| `line_number` | Line Number |
| `item` | Item |
| `vend_cat_no` | Vend Cat No |
| `order_qty` | Order Qty |
| `unit_price` | Unit Price |
| `inv_uom` | Inv Uom |

Fields that are always null across an entire invoice (e.g. `tracking_number`) are listed in `summary.fields_not_in_invoice` rather than reported as `NOT FOUND IN CSV` per row.

### Row matching

For each extracted invoice item, the comparison engine looks for the best matching CSV row using three fallback strategies:

1. **Line Number match** — exact normalized match on `line_number`
2. **Vend Cat No match** — if line number fails, match on `vend_cat_no`
3. **Item match** — if vend_cat_no fails, match on `item`

### Value comparison

| Field type | Comparison method |
|---|---|
| Numeric fields (`order_qty`, `unit_price`) | Float comparison with tolerance `±0.02` |
| Identifier fields (`item`, `vend_cat_no`, `line_number`) | Strip all non-alphanumeric characters, then compare (handles `100-0288` vs `1000288`) |
| String fields | Lowercase + collapse whitespace |

### Comparison statuses

| Status | Meaning |
|---|---|
| `MATCH` | Invoice value and CSV value match (within tolerance) |
| `NO MATCH` | Both present but values differ |
| `NOT FOUND IN CSV` | Invoice has a value; this field has no CSV counterpart |
| `NOT FOUND IN INVOICE` | Field always null across the whole invoice (summary only) |

---

## 14. Web UI

### Layout

```
┌──────────────────────────────────────────────────────────────────┐
│ Toolbar: [PO dropdown] [Upload PDF] [Upload CSV] [Extract]       │
│          [Re-extract] [Compare]                [PASS · diff $0]  │
├────────────────────────────┬─────────────────────────────────────┤
│                            │ CSV File Preview                    │
│   Invoice PDF Preview      │ (rows for current PO, scrollable)  │
│   (page by page, nav       ├─────────────────────────────────────┤
│    arrows, page counter)   │ Extracted Fields from Invoice       │
│                            │ (line items table, non-null only)   │
├────────────────────────────┴─────────────────────────────────────┤
│ Comparison — CSV vs Extracted                                    │
│ [All·120] [No Match·0] [Not in CSV·360] [Match·480]  [Search]   │
│ ▶ Line 002  — ? missing                                          │
│ ▶ Line 003  — ✓ all match                                        │
│ ▼ Line 004  — ✗ mismatch                                         │
│     Field          Invoice Value    CSV Value      Status        │
│     line number    004              004            MATCH         │
│     item           001130           001130         MATCH         │
│     unit price     828.71           829.00         NO MATCH      │
└──────────────────────────────────────────────────────────────────┘
```

### Toolbar features

| Control | Behaviour |
|---|---|
| **PO dropdown** | Lists all POs with uploaded PDFs or cached results; switches active context |
| **Upload PDF** | Saves PDF, extracts PO from content, renames file; shows error if PO not found |
| **Upload CSV** | Saves CSV/Excel; shows green "X rows loaded" confirmation banner |
| **Extract** | Runs pipeline; uses cache if available; shows loading spinner |
| **Re-extract** | Forces full re-run, bypasses cache |
| **Compare** | Runs field-level comparison against CSV; shows loading spinner |
| **PASS/FAIL badge** | Shows extraction verification status and subtotal diff amount |

### CSV preview

- Appears after PDF upload or PO selection
- Shows rows filtered to the current PO
- Uses the PDF-extracted PO (`N335512`) for CSV filtering even when the file key is different (`1637737`)
- Shows "No CSV loaded" if no CSV uploaded yet, "No CSV rows for this PO" if CSV loaded but no matching rows

### Comparison table

- **Grouped by line item** — 120 items instead of 840 flat rows
- **Collapsed by default** — shows one row per item with a summary badge
- **Click to expand** — reveals all field rows for that item
- **Collapse all / Expand all** — bulk controls
- **Filter pills** — `All`, `No Match`, `Not in CSV`, `Match` — click to filter
- **Search** — filters across identifier, field name, invoice value, CSV value
- **Inline highlighting** — mismatched invoice values shown in red bold; CSV values in red
- **Item count** not row count in the header

---

## 15. Backend API Reference

Base URL: `http://localhost:8000`  
Interactive docs: `http://localhost:8000/docs`

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/upload-pdf` | Upload PDF; extracts PO from content; renames file to `<po>.pdf`; returns `{po, filename, pages}` or `{po: null, error}` |
| `GET` | `/api/pdf-info/{po}` | Returns page count for a PO |
| `GET` | `/api/pdf-page/{po}?page=N&dpi=150` | Renders one page as PNG; `page` is 1-indexed |
| `POST` | `/api/upload-csv` | Saves CSV/Excel; returns `{filename, rows, pos[]}` |
| `GET` | `/api/csv-rows/{po}` | Returns CSV rows filtered by PO and all column names |
| `GET` | `/api/list-pos` | Lists all POs with results or uploaded PDFs |
| `POST` | `/api/extract/{po}?force=false` | Runs extraction (async in threadpool); uses cache unless `force=true` |
| `GET` | `/api/results/{po}` | Returns cached extraction JSON |
| `POST` | `/api/compare/{po}` | Runs comparison; uses `po_number` inside the result JSON for CSV lookup |
| `GET` | `/api/compare/{po}` | Returns cached comparison JSON |

**Key design detail — compare uses stored `po_number`:** When the extraction result for PO `1637737` contains `"po_number": "N335512"`, the compare endpoint uses `N335512` to filter CSV rows — not `1637737`. This handles the case where the filename key differs from the PDF-embedded PO.

---

## 16. Line Item Output Schema

All extraction results use this canonical schema:

| Field | Type | Meaning |
|---|---|---|
| `line_number` | string \| null | Short row counter from the invoice's Line/No./# column |
| `reference_number` | string \| null | Long numeric barcode (UPC/EAN/GTIN); invoice-only, not in CSV |
| `item` | string \| null | Buyer/internal item code; maps to CSV "Item" column |
| `vend_cat_no` | string \| null | Vendor catalog number; maps to CSV "Vend Cat No" column |
| `description` | string \| null | Product description text |
| `order_qty` | number \| null | Quantity ordered |
| `trns_qty` | number \| null | Quantity shipped/invoiced (may differ from ordered) |
| `inv_uom` | string \| null | Unit of measure (EA, CA, BX, etc.) |
| `unit_price` | number \| null | Per-unit price |
| `extended_price` | number \| null | Line total (trns_qty × unit_price) |
| `pack_size` | string \| null | Pack size if visible (e.g. "1x500tab") |
| `tracking_number` | string \| null | Carrier tracking number; invoice-only |
| `batch_number` | string \| null | Batch/lot number; invoice-only |

---

## 17. Verification Output Schema

```json
{
  "extracted_total":   945951.26,
  "detected_subtotal": 945951.26,
  "subtotal_match":    true,
  "diff":              0.0,
  "line_count":        120,
  "status":            "PASS",
  "math_issues":       []
}
```

`math_issues` lists any line items where `extended_price ≠ trns_qty × unit_price` beyond a 1¢ tolerance.

---

## 18. Key Design Decisions

### PO from PDF content, not filename

Filename-based PO identification fails when users rename files or when multiple invoices for different POs are processed in bulk. Extracting from the PDF text makes the identification authoritative and consistent with what the invoice actually states.

### Camelot is a candidate, never an early exit

Earlier versions exited after T1-Camelot if it succeeded. This was changed because Camelot can produce a clean-looking but incomplete table (e.g. missing identifier columns). By always running T2-vision and selecting the best result via comparator, the pipeline catches these cases.

### Semantic trust guard in `_validate_mapping`

Without this guard, `_validate_mapping` could override a correct semantic assignment (e.g. "Item" → `item`) just because extracted item values were sparse (filled in late by the recovery prompt). The guard locks in semantically-obvious mappings before running value-overlap scoring.

### Subtotal is diagnostic, not a correction target

The system must report discrepancies accurately. If the system adjusted values to make the total match, it would hide real errors (wrong quantities, wrong prices) from the finance team.

### No filename fallback for PO extraction

When PO extraction fails, returning `None` and showing a clear error is better than silently using the filename. A wrong PO key would cause wrong CSV filtering, wrong caching, and wrong comparison — silent failures that are harder to diagnose than an explicit error.

---

## 19. Known Limitations

| Area | Limitation |
|---|---|
| **Scanned PDFs** | T2-vision works but accuracy depends on scan quality; heavily degraded scans may produce garbled text |
| **Non-standard PO labels** | Patterns cover ~15 common variants; unusual labels fall back to LLM extraction |
| **Very dense pages (80+ items)** | May hit LLM context limits; consider page-split strategies |
| **Multi-page line items** | Page-context injection handles most cases; occasional misses at page boundaries |
| **Cost tracking** | LLM token usage is printed to console but not persisted to JSON |
| **Large invoices (100+ pages)** | Not tested at scale; processing time increases linearly |
| **UOM normalization** | UOM is extracted as-is from the invoice; no unit conversion (EA vs. EACH vs. Each) |
| **Pack-size math** | No automatic conversion between pack-size pricing and per-unit CSV prices |

---

## 20. Glossary

| Term | Meaning |
|---|---|
| **T1 / T2 / T3** | Extraction tiers: Camelot, Vision LLM, Text LLM |
| **Canonical schema** | The fixed set of 13 output field names used across all tiers |
| **Field swap** | When the LLM places a value in the wrong identifier field (e.g. item code → vend_cat_no) |
| **Barcode** | Long numeric identifier (10+ digits, UPC/EAN/GTIN); goes to `reference_number` |
| **Item code** | Short buyer-side SKU (4–8 digits); goes to `item` |
| **Catalog code** | Mixed alphanumeric vendor identifier; goes to `vend_cat_no` |
| **Compound code** | CSV column where two invoice fields are joined (e.g. `item + line_number`) |
| **Schema anchoring** | Analyzing the first content page to determine dominant value type per field |
| **Layout analysis** | One-shot LLM call to identify column structure before per-page extraction |
| **Recovery prompt** | Targeted re-prompt for rows still missing identifiers after main extraction |
| **Sentinel string** | String like "None", "null", "N/A" returned by LLM instead of JSON null |
| **Subtotal hint** | Detected invoice total injected into extraction prompt as a math-contract anchor |
| **Semantic trust** | Preserving a column mapping because the column name clearly identifies the field |
| **Intentionally null** | A CSV column explicitly unmapped because its matched field has no extracted values |
| **CSV sample** | Up to 10 reference rows shown to the LLM as value-pattern guidance only |
