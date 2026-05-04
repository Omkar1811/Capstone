# Invoice Processing — Step-by-Step Workflow

This document explains in detail what happens at every step, from uploading a PDF to viewing the comparison results.

---

## Overview

```
User uploads PDF
       ?
       ?
  Step 1: PO Extraction
       ?
       ?
  Step 2: PDF Renamed to <PO>.pdf
       ?
       ?
  Step 3: User clicks Extract
       ?
       ??? Step 3a: Load PDF pages + detect subtotal
       ??? Step 3b: Tier 1 — Camelot (deterministic)
       ??? Step 3c: Tier 2 — Vision LLM (gpt-4o, per page)
       ??? Step 3d: Tier 3 — Text LLM (fallback only)
       ??? Step 3e: Best tier selected
       ??? Step 3f: Post-extraction normalization
       ??? Step 3g: Subtotal verification
       ??? Step 3h: Cache result to results/<PO>.json
       ?
       ?
  Step 4: User clicks Compare
       ?
       ??? Step 4a: Load extraction result + CSV rows
       ??? Step 4b: Match invoice rows to CSV rows
       ??? Step 4c: Compare each field
       ??? Step 4d: Save compare/<PO>_compare.json
       ?
       ?
  Step 5: View results in UI
```

---

## Step 1 — Upload PDF

**Trigger:** User clicks "Upload PDF" and selects a file.

**What happens:**

1. The frontend sends the PDF file as a binary upload (`POST /api/upload-pdf`).
2. The backend saves the file into the `uploads/` folder under its **original filename** (e.g., `uploads/1637737.pdf`).
3. The PDF is opened with PyMuPDF to count the total number of pages.
4. `_extract_po_number()` is called to find the PO number inside the PDF.

### 1a — PO Extraction (Regex Pass)

The first two pages of the PDF are read as plain text using pdfplumber. The text is tested against 9 regex patterns, in priority order from most specific to least:

| Priority | Pattern matches | Example |
|---|---|---|
| 1 | `Purchase Order Number: ...` | `Purchase Order Number: N335512` |
| 2 | `P.O. Number: ...` | `P.O. No: N335512` |
| 3 | `PO: ...` or `PO# ...` | `PO: N335512` |
| 4 | `Purchase Order N335512` (no colon) | `Purchase Order N335512` |
| 5 | `FO No: ...` | `FO No: N335512` |
| 6 | `Customer P.O.: ...` | `Customer P.O.: N335512` |
| 7 | `Cust PO: ...` | `Cust PO: N335512` |
| 8 | `Your Order No: ...` | `Your Order No: N335512` |
| 9 | `Order Number: ...` (generic) | `Order Number: N335512` |

All matches are collected. If any match starts with a letter (e.g., `N335512`), that is preferred over purely numeric matches (e.g., `1637737`), because buyer POs in this domain typically begin with a letter.

### 1b — PO Extraction (LLM Fallback)

If regex finds nothing, a single LLM call is made:
- Model: `gpt-4o` (or configured model)
- Input: first 3,000 characters of page text
- Instruction: "Return ONLY the purchase order number. Return UNKNOWN if not found."
- Max tokens: 20, Temperature: 0

### 1c — PO Not Found

If both regex and LLM fail, the backend returns:
```json
{ "po": null, "error": "PO number not found in PDF" }
```
The UI shows a red error banner: **"No PO number found in this PDF"**. Processing stops here.

---

## Step 2 — File Rename

If the PO is found, the file is renamed from `uploads/1637737.pdf` ? `uploads/N335512.pdf`.

This ensures that all subsequent operations (PDF viewer, extraction, comparison) use the same consistent key — the PO extracted from the PDF content — regardless of what the file was originally named.

The UI updates the PO selector to show `N335512`.

---

## Step 3 — Extraction (User Clicks "Extract")

**Trigger:** User clicks "Extract" in the toolbar.

**Request:** `POST /api/extract/N335512`

### 3a — Cache Check

The backend checks if `results/N335512.json` already exists.
- If yes ? return the cached result immediately (marked `_cached: true`). No LLM calls made.
- If no ? proceed with full extraction.

To force re-extraction (bypass cache), the user clicks **"Re-extract"**, which calls `POST /api/extract/N335512?force=true`.

### 3b — Load PDF Pages

`load_pdf()` opens the PDF and for each page extracts:

| Data | How | Purpose |
|---|---|---|
| Text words + bounding boxes | pdfplumber | Used to build spatially-aligned text (preserves column layout) |
| Page image (PNG, base64) | PyMuPDF at DPI from env (default 150) | Sent to vision LLM |

**Spatially-aligned text** groups words by their Y-coordinate into visual rows, preserving the column layout that the LLM needs to correctly associate values with their header. For example:

```
Line  Item#     Description                Qty   Price   Total
001   023199    MONOSOF 2-0 BLK 75CM SC2   8.00  89.36   714.88
```

A page with no machine-readable text (scanned PDF) is still processed — the image is sent to the LLM without the text component.

### 3c — Subtotal Detection

`detect_subtotal()` scans all page texts for lines matching common invoice total labels:

- `Subtotal`, `Sub-Total`, `Sub Total`
- `Invoice Total`, `Total Amount`, `Amount Due`
- `Total Due`, `Balance Due`, `Grand Total`

The associated numeric value is extracted and stored as the `subtotal` anchor. If no label is found, a vision LLM call (`_llm_subtotal_from_image`) is made on the last page image.

The subtotal is used throughout extraction as a **math contract anchor** — the LLM is told "extracted totals should sum to $X" to guide it toward completeness.

---

### 3d — Tier 1: Camelot (Deterministic Table Extraction)

If `camelot-py` is installed, Camelot tries to parse the PDF as a structured table.

**Attempts two flavors in order:**

| Flavor | How it works | Best for |
|---|---|---|
| `lattice` | Detects visible grid lines (requires Ghostscript) | Bordered tables |
| `stream` | Uses whitespace gaps between columns | Borderless tables |

**Safety gates — all must pass or Camelot is rejected:**

1. At least one table found with ? 2 rows and ? 3 columns
2. Camelot accuracy score ? 70
3. A header row must be detectable (contains invoice keywords)
4. At least one of `unit_price` or `extended_price` must be mappable
5. At least 3 plausible line items extracted
6. If subtotal is known: extracted total must be within 2% of it

Even if Camelot passes all gates, T2-Vision still runs. Camelot's result is a **candidate**, not a final answer.

---

### 3e — Tier 2: Multimodal Vision LLM (Primary Extraction Path)

For each page of the PDF, the following pipeline runs:

#### 3e-i — One-Shot Column Layout Analysis (first page only)

A single LLM call analyses the first content page to understand the invoice's column structure:
- How many identifier columns are present?
- What are the column names?
- Is the layout "one-row-per-item" or "multi-row-per-item"?
- Are there sub-rows for tracking numbers, batch numbers, etc.?

The result is stored as `layout_context` and injected into every subsequent per-page prompt.

#### 3e-ii — First-Page Field Schema Anchoring

After extracting the first page, the pipeline votes on the **dominant value type** for each identifier field:
- `item_code` — short numeric buyer code (e.g., `023199`)
- `catalog_code` — mixed alphanumeric vendor code (e.g., `SM-923`, `100-0288`)
- `barcode` — long numeric code (e.g., `20884521079295`)

This schema is stored as `schema_context` and injected into subsequent pages so the LLM applies consistent field assignments across the whole invoice.

#### 3e-iii — Per-Page Prompt Assembly

Each page prompt is assembled from these blocks:

| Block | Content | Purpose |
|---|---|---|
| **System message** | Core extraction rules, field guide, identifier decision rules, quantity rules, UOM rules | Tells the LLM how to read any invoice |
| **Page image** (PNG) | The rendered page at 200 DPI | Lets the LLM see the visual layout |
| **Aligned text** | Spatially-grouped words preserving column positions | Provides text that the LLM can search precisely |
| **layout_context** | Column structure from first-page analysis | "This invoice has 3 identifier columns..." |
| **schema_context** | Field type votes from first page | "On this invoice, `item` is always a 6-digit code" |
| **subtotal_hint** | Detected invoice total | "Extracted items should sum to $945,951.26" |
| **page_ctx** | Page position info | "Page 2 of 4" — helps with continuation handling |
| **csv_sample** | Up to 10 rows from the reference CSV | Shows the LLM what "Item" and "Vend Cat No" values look like in practice |
| **retry_hint** | (On retry only) Description of quality issues from the previous attempt | Guides the LLM to correct specific problems |

**Anti-hallucination guard on CSV sample:**
The CSV rows are accompanied by an explicit instruction: *"These rows are reference patterns only. Do NOT copy values from them. Extract ONLY what is visible on the invoice page."*

#### 3e-iv — LLM Call and Quality Check

The LLM call is made with:
- Temperature: 0 (deterministic)
- Response format: JSON
- Max tokens: 4,096

After receiving the response, a **quality check** inspects the output:
- Are there items where `extended_price` doesn't match `trns_qty × unit_price`?
- Are there suspiciously large or small values for any field?
- Are all items on a dense page accounted for (item-count sanity)?

If the quality check fails, one **retry** is made with a `retry_hint` describing the specific issues found.

---

### 3f — Tier 3: Text-Only LLM (Fallback)

T3 runs in these situations:
- Vision tier (T2) is unavailable (PyMuPDF not installed or `--no-vision` flag)
- T2 returned **zero items** for any page
- T2 is non-PASS and the subtotal is known (runs as a cross-check)

T3 uses the same prompt structure as T2 but **without the page image**. Only the spatially-aligned text is sent.

---

### 3g — Best Tier Selection

After all tiers complete, their results are compared:

**Primary sort key: Subtotal difference**
```
diff = |extracted_total - detected_subtotal|
```
Lower diff = better. A diff of $0.00 is a PASS.

**Tiebreaker: Identifier coverage**
```
coverage = count of items where both `item` AND `vend_cat_no` are non-null
```
Higher coverage = better. This prevents Camelot from winning a tie by producing mathematically correct but identifier-empty results.

**Example comparison output:**
```
[compare] T1-camelot : items=28 diff=0.0  id_coverage=0/28  status=PASS
[compare] T2-vision  : items=28 diff=0.0  id_coverage=26/28 status=PASS
[compare] Selected   : T2-vision
```

---

### 3h — Post-Extraction Normalization

After the winning tier is selected, a deterministic Python layer applies corrections:

#### 1. Null Sentinel Coercion
LLMs sometimes return the string `"None"`, `"null"`, `"N/A"`, `"-"` instead of a JSON null. These are converted to Python `None`.

#### 2. Field Swap Detection
If `item` contains a catalog-style value (e.g., `SM-923`) and `vend_cat_no` contains an item-code value (e.g., `023199`), they are swapped.

#### 3. Barcode Reclassification
If `line_number` contains a 10+ digit barcode, it is moved to `reference_number`. Line numbers should always be short sequential counters.

#### 4. Tracking vs. Item Reclassification
Short numeric codes (4–8 digits) placed by the LLM in `tracking_number` — without an associated carrier name (FedEx, UPS, DHL) — are moved to `item`. Real tracking numbers always appear with a carrier name.

#### 5. Cross-Field Deduplication
If the LLM placed the same value into two identifier fields (e.g., `item = "023199"` and `vend_cat_no = "023199"`), the duplicate is nulled. The field that best matches the page's schema type is kept.

#### 6. Missing Identifier Recovery
If any items still have null `item` or `vend_cat_no` after all the above, a **targeted re-prompt** is issued for those specific rows. The re-prompt sends the page image with a focused instruction: "Find the identifier for these specific line items." Recovered values are merged back.

#### 7. Hallucination Filter
Items matching known hallucination patterns (e.g., a line item with a description that exactly matches a prompt example) are removed.

#### 8. Outlier Price Filter
If there are 5+ items and a line item's `unit_price` is more than 3× the 90th-percentile unit price, it is removed. This catches cases where the LLM accidentally read a page subtotal as a line-item unit price.

---

### 3i — Subtotal Verification

```
extracted_total = sum of all extended_price values
diff = |extracted_total ? detected_subtotal|
status = PASS if diff < $0.01 else FAIL
```

`math_issues` lists any rows where `extended_price ? trns_qty × unit_price` beyond a 1¢ tolerance.

> **Important:** The system never adjusts values to make the total match. FAIL means "investigate", not "auto-correct".

---

### 3j — Cache Result

The final result is written to `results/N335512.json`:

```json
{
  "source":       "uploads/N335512.pdf",
  "po_number":    "N335512",
  "tier":         "T2-vision",
  "line_items":   [ ...28 items... ],
  "verification": {
    "status":            "PASS",
    "extracted_total":   945951.26,
    "detected_subtotal": 945951.26,
    "diff":              0.0,
    "line_count":        28,
    "math_issues":       []
  }
}
```

The UI shows the PASS/FAIL badge and displays the extracted line items in the "Extracted Fields" panel.

---

## Step 4 — Comparison (User Clicks "Compare")

**Trigger:** User clicks "Compare" in the toolbar.

**Request:** `POST /api/compare/N335512`

### 4a — Load Data

- Extraction result loaded from `results/N335512.json`
- The `po_number` stored inside the JSON (e.g., `N335512`) is used to filter the CSV — **not the URL parameter**. This handles cases where the file key differs from the PDF-embedded PO.
- CSV rows for PO `N335512` are loaded from `retail.csv`.

### 4b — Row Matching

For each extracted invoice item, the comparison engine finds the best matching CSV row using three fallback strategies:

```
1st try ? Match on line_number (exact, normalized: strip leading zeros)
2nd try ? Match on vend_cat_no (normalized: strip all non-alphanumeric chars)
3rd try ? Match on item (normalized: strip all non-alphanumeric chars)
No match ? Row is reported as "unmatched"
```

### 4c — Field-by-Field Comparison

Once a CSV row is matched, each invoice field is compared to its corresponding CSV column:

| Invoice field | CSV column | Comparison method |
|---|---|---|
| `line_number` | Line Number | Alphanumeric normalization (strip non-alphanumeric, compare) |
| `item` | Item | Alphanumeric normalization |
| `vend_cat_no` | Vend Cat No | Alphanumeric normalization (handles `100-0288` == `1000288`) |
| `order_qty` | Order Qty | Float comparison, tolerance ±0.02 |
| `unit_price` | Unit Price | Float comparison, tolerance ±0.02 |
| `inv_uom` | Inv Uom | Case-insensitive string match |

**Status per field:**

| Status | Meaning |
|---|---|
| `MATCH` | Values agree (within tolerance) |
| `NO MATCH` | Both present but values differ |
| `NOT FOUND IN CSV` | Invoice has a value; no CSV counterpart exists |

**Fields always null** across the entire invoice (e.g., `tracking_number`, `batch_number`) are listed in the summary under `fields_not_in_invoice` instead of generating hundreds of "NOT FOUND IN CSV" rows.

**Null rows are skipped** — if a field is null on a specific invoice row, it is not compared for that row.

### 4d — Save Result

Comparison output is written to `compare/N335512_compare.json`.

---

## Step 5 — View Results in the UI

### Toolbar
Shows a **PASS** (green) or **FAIL** (red) badge with the subtotal difference amount.

### PDF Preview (left panel)
- Displays the invoice page-by-page
- Navigation arrows to move between pages
- Page counter (e.g., "Page 2 of 4")

### CSV Preview (top-right panel)
- Shows the reference CSV rows filtered to the current PO
- Uses the PDF-extracted PO for filtering (not the filename), so the correct rows always appear even if the file was uploaded with a different name

### Extracted Fields (middle-right panel)
- Table of all extracted line items
- Only shows non-null fields per row

### Comparison Table (bottom panel)
- **Grouped by line item** — one collapsible row per invoice line (e.g., Line 001, Line 002)
- **Collapsed by default** — click any row to expand and see all fields
- **Group badge** — each group shows one of:
  - `? all match` — every compared field matches
  - `? mismatch` — at least one NO MATCH
  - `? missing` — some fields not in CSV
- **Filter pills** — click `No Match`, `Not in CSV`, or `Match` to filter the view
- **Search bar** — searches across field names, invoice values, and CSV values
- **Inline highlighting** — NO MATCH rows show the invoice value in **bold red** and the CSV value in red

---

## What Gets Cached and Where

| File | Location | Created when |
|---|---|---|
| Uploaded PDF | `uploads/<PO>.pdf` | PDF uploaded via UI |
| Extraction result | `results/<PO>.json` | "Extract" or "Re-extract" clicked |
| Comparison result | `compare/<PO>_compare.json` | "Compare" clicked |
| Reference CSV/Excel | `retail.csv` (root) | CSV uploaded via UI |

To start fresh on a specific invoice, delete `results/<PO>.json` and `compare/<PO>_compare.json` and click "Re-extract".

---

## LLM Calls Made Per Invoice

| Step | # Calls | Model | Purpose |
|---|---|---|---|
| PO Extraction (regex fallback) | 0–1 | gpt-4o | Find PO number (only if regex fails) |
| Column layout analysis | 1 | gpt-4o | Understand invoice column structure |
| Per-page extraction | N pages | gpt-4o | Extract line items (vision + text) |
| Per-page quality retry | 0–N | gpt-4o | Retry pages that failed quality check |
| Subtotal detection fallback | 0–1 | gpt-4o | Find subtotal if regex missed it |
| Missing identifier recovery | 0–K | gpt-4o | Re-prompt for rows with null item/vend_cat_no |

For a typical 3-page invoice: **4–6 LLM calls** in the normal path, up to **10** with retries and recovery.
