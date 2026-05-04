# Invoice Processing — Step-by-Step Workflow

This document explains in precise detail what happens at every step, from uploading a PDF to viewing the comparison results. Every library call, data transformation, and decision point is documented.

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
       ??? 3a: Cache check
       ??? 3b: load_pdf()  ? pdfplumber + PyMuPDF run here
       ??? 3c: detect_subtotal()
       ??? 3d: Tier 1 — Camelot (deterministic)
       ??? 3e: Tier 2 — Vision LLM  (gpt-4o, image + aligned text per page)
       ??? 3f: Tier 3 — Text LLM    (fallback, aligned text only)
       ??? 3g: Best tier selected
       ??? 3h: Post-extraction normalization (Python only)
       ??? 3i: Subtotal verification
       ??? 3j: Cache result ? results/<PO>.json
       ?
       ?
  Step 4: User clicks Compare
       ?
       ??? 4a: Load extraction result + CSV rows
       ??? 4b: Match invoice rows to CSV rows
       ??? 4c: Compare each field value
       ??? 4d: Save ? compare/<PO>_compare.json
       ?
       ?
  Step 5: View results in UI
```

---

## Step 1 — Upload PDF

**Trigger:** User clicks "Upload PDF" and selects a file.

**What happens:**

1. The frontend sends the PDF binary to `POST /api/upload-pdf`.
2. The backend saves the raw bytes to `uploads/<original_filename>.pdf` (e.g., `uploads/1637737.pdf`).
3. **PyMuPDF (`fitz`)** opens the file from the in-memory bytes stream (`fitz.open(stream=content, filetype="pdf")`) to count the total number of pages. The file is immediately closed. This is a metadata-only read — no content is extracted here.
4. `_extract_po_number()` is called to find the PO number inside the PDF content.

---

### Step 1a — PO Extraction: pdfplumber reads the first two pages

```python
with pdfplumber.open(pdf_path) as pdf:
    for p in pdf.pages[:2]:
        text += (p.extract_text() or "") + "\n"
```

**What pdfplumber does here:**
- Opens the PDF and reads the first two pages only (performance optimisation — PO numbers always appear early)
- `p.extract_text()` returns all text content from the page as a single flat string, with words separated by spaces and rows separated by newlines
- The result is plain text with no coordinate information — just the raw characters in reading order

This text is then tested against 9 regex patterns in priority order:

| Priority | Pattern label | Example it matches |
|---|---|---|
| 1 | `Purchase Order Number: ...` | `Purchase Order Number: N335512` |
| 2 | `P.O. Number: ...` | `P.O. No: N335512` |
| 3 | `PO: ...` or `PO# ...` | `PO: N335512` |
| 4 | `Purchase Order N335512` (no colon) | `Purchase Order N335512` |
| 5 | `FO No: ...` | `FO No: N335512` |
| 6 | `Customer P.O.: ...` | `Customer P.O.: N335512` |
| 7 | `Cust PO: ...` | `Cust PO: N335512` |
| 8 | `Your Order No: ...` | `Your Order No: N335512` |
| 9 | `Order Number: ...` (generic, lowest priority) | `Order Number: N335512` |

All matches across all 9 patterns are collected into a list. Candidates that start with a letter (e.g., `N335512`) are preferred over purely numeric ones (e.g., `1637737`), because buyer POs in this domain typically begin with a letter prefix.

### Step 1b — PO Extraction: LLM fallback (only if regex found nothing)

If the regex scan produced zero candidates, a single LLM call is made:
- Input: first 3,000 characters of the raw pdfplumber text
- System: "Extract ONLY the purchase order number. Return UNKNOWN if not found."
- Max tokens: 20, Temperature: 0 (no creativity, deterministic output)

### Step 1c — PO Not Found

If both regex and LLM fail, the backend returns:
```json
{ "po": null, "error": "PO number not found in PDF" }
```
The UI shows a red error banner. Processing stops. The file is kept under its original name.

---

## Step 2 — File Rename

If the PO is found (e.g., `N335512`), the saved file is renamed:

```
uploads/1637737.pdf  ?  uploads/N335512.pdf
```

All subsequent operations (PDF viewer, extraction, comparison, cache lookup) use `N335512` as the key. This decouples the system from whatever the user chose to name the file.

The UI updates the PO selector dropdown to show `N335512`.

---

## Step 3 — Extraction (User Clicks "Extract")

**Request:** `POST /api/extract/N335512`

---

### Step 3a — Cache Check

The backend looks for `results/N335512.json`.
- **Found, and `force=false`:** Return cached result immediately (`_cached: true`). Zero LLM calls.
- **Not found, or `force=true`:** Proceed with full extraction below.

---

### Step 3b — `load_pdf()`: pdfplumber + PyMuPDF both run here

This is the most important setup step. `load_pdf()` produces a **list of page dictionaries** — one per PDF page — that every subsequent tier reads from. Here is what each library does and why:

---

#### Pass 1 — pdfplumber extracts words with bounding boxes

```python
with pdfplumber.open(path) as pdf:
    for p in pdf.pages:
        words = p.extract_words(
            x_tolerance=3, y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=False,
        )
```

**What pdfplumber returns for each word:**

| Key | Type | Example value |
|---|---|---|
| `text` | string | `"89.36"` |
| `x0` | float | `432.5`  (left edge, in points from left) |
| `x1` | float | `456.2`  (right edge) |
| `top` | float | `214.8`  (distance from top of page) |
| `bottom` | float | `224.1` |
| `fontname` | string | `"Helvetica"` |

**Why `extract_words` and not `extract_text`:**
- `extract_text()` loses all positional information
- `extract_words()` preserves the X/Y coordinates of every word
- These coordinates are critical for the spatially-aligned text rendering (Step 3b-ii below) because they let us reconstruct the column layout that the LLM needs to correctly map values to headers

**Parameters explained:**
- `x_tolerance=3`: words within 3 points horizontally are joined into one word (handles slightly spaced characters)
- `y_tolerance=3`: words within 3 points vertically are considered the same row
- `keep_blank_chars=False`: discard blank characters
- `use_text_flow=False`: use geometric position, not reading-order flow — important because invoice tables don't always follow natural text flow

Each page dictionary after pdfplumber looks like:
```python
{
    "page_num":  1,
    "words":     [ {"text": "Item", "x0": 45.0, "top": 112.0, ...}, ... ],
    "text":      "Item  Description  Qty  Price\n001  Surgical Suture  8  89.36\n...",
    "width":     612.0,   # page width in points
    "image_b64": None,    # filled in by PyMuPDF next
}
```

---

#### Pass 2 — PyMuPDF renders each page as a PNG image

```python
z   = dpi / 72.0          # e.g., 200/72 = 2.78x scale factor
mat = fitz.Matrix(z, z)   # transformation matrix
doc = fitz.open(path)
for i in range(len(pages)):
    pix = doc.load_page(i).get_pixmap(matrix=mat, alpha=False)
    pages[i]["image_b64"] = base64.b64encode(pix.tobytes("png")).decode("ascii")
doc.close()
```

**What PyMuPDF does here:**
- `fitz.Matrix(z, z)` scales the page coordinate system. At 200 DPI: `200/72 = 2.78`, so a 612-point wide page becomes `612 × 2.78 = 1701 pixels` wide
- `get_pixmap()` renders the page at full quality — fonts, images, shading, borders — into a raw pixel buffer
- `pix.tobytes("png")` encodes the buffer as a PNG
- `base64.b64encode()` converts the PNG bytes to a base64 ASCII string that can be embedded directly in an OpenAI API JSON request as `data:image/png;base64,...`

**Why 200 DPI (default)?**
- 72 DPI = 1:1 with PDF points — too low, text becomes blurry at small font sizes
- 150 DPI — readable but some fine detail is lost
- 200 DPI — sharp enough for small fonts (8pt), fine lines, and tightly-packed table cells
- 300 DPI — very sharp but produces large images that increase LLM token cost
- The DPI is configurable via `INVOICE_VISION_DPI` in `.env`

**Fallback: pdfplumber image renderer**
If PyMuPDF is not installed, pdfplumber's own renderer is used:
```python
pi  = pdf.pages[i].to_image(resolution=dpi)
buf = io.BytesIO()
pi.original.save(buf, format="PNG")
pages[i]["image_b64"] = base64.b64encode(buf.getvalue()).decode("ascii")
```
This is slightly lower quality but works without the `pymupdf` package.

---

#### Spatially-aligned text rendering (`_render_aligned`)

After both passes, the word list from pdfplumber is converted into a human-readable string that preserves column layout. This is sent alongside the image to the LLM.

```python
def _render_aligned(words, x_scale=5.0):
    # Group words into rows by Y-coordinate (tolerance = 3 points)
    rows = _group_by_y(words)
    lines = []
    for row in rows:
        parts = []
        prev_x = 0.0
        for w in sorted(row, key=lambda x: x["x0"]):
            # Convert X distance into spaces: 1 space per 5 points
            gap = int(max(1, (w["x0"] - prev_x) / x_scale))
            parts.append(" " * gap + w["text"])
            prev_x = w["x1"]
        lines.append("".join(parts).strip())
    return "\n".join(lines)
```

**Example transformation:**

pdfplumber word list (simplified):
```
{"text": "Line",  "x0": 45,  "top": 112}
{"text": "Item#", "x0": 95,  "top": 112}
{"text": "Qty",   "x0": 320, "top": 112}
{"text": "Price", "x0": 420, "top": 112}
{"text": "001",   "x0": 45,  "top": 130}
{"text": "023199","x0": 95,  "top": 130}
{"text": "8.00",  "x0": 320, "top": 130}
{"text": "89.36", "x0": 420, "top": 130}
```

After `_render_aligned` (at `x_scale=5.0`, so 1 space per 5 points):
```
Line  Item#                         Qty       Price
001   023199                        8.00      89.36
```

This is critical: by preserving column positions as spaces, the LLM can see that `8.00` is under the `Qty` header, not the `Price` header — even for invoices with unusual spacing.

**For scanned PDFs (no machine-readable text):**
pdfplumber `extract_words()` returns an empty list. The page still has an image from PyMuPDF. The aligned text is an empty string, and the LLM processes the image alone. The pipeline is designed to handle this gracefully — it checks `page.get("words") or []` before calling `_render_aligned`.

---

### Step 3c — Subtotal Detection

`detect_subtotal()` scans all page `text` fields (the flat `p.extract_text()` strings from pdfplumber) using regex against 9 common invoice total labels:

```
INVOICE TOTAL    Invoice Total    Sub Total    SUBTOTAL
Subtotal         AMOUNT DUE       Amount Due   Net Due
MERCHANDISE TOTAL
```

The regex captures the number following the label. The **last match** is used (since the grand total appears on the last page). The number is parsed by `parse_num()` which handles commas, dollar signs, and European decimal formats.

**If regex finds nothing:**
A single vision LLM call is made on the last page image (`_llm_subtotal_from_image`):
- Sends the last page PNG at `detail: "high"` quality
- Asks the LLM to find the final invoice total value
- Max tokens: 64, Temperature: 0
- Returns a JSON object `{"total": <number>}` or `{"total": null}`

The subtotal is stored and used as a math-contract anchor in every extraction prompt: *"Extracted items should sum to $945,951.26"*

---

### Step 3d — Tier 1: Camelot (Deterministic Table Extraction)

Camelot is a Python library that uses PDF geometry (either visible line rules or whitespace gaps) to parse tables from PDFs without any LLM.

**Two flavors tried in sequence:**

| Flavor | Method | Requires |
|---|---|---|
| `lattice` | Detects printed grid lines using image analysis | Ghostscript installed |
| `stream` | Detects columns using whitespace gaps between text clusters | Nothing extra |

**Safety gates (all must pass before T1 result is used as a candidate):**

1. Table must have ? 2 rows and ? 3 columns
2. Camelot's internal accuracy score ? 70
3. At least one header keyword found in the first row (Line, Item, Qty, Price, Description, UOM, etc.)
4. At least one of `unit_price` or `extended_price` maps from the header
5. At least 3 valid line items extracted (price AND qty both present)
6. If subtotal is known: `|extracted_total ? subtotal| / subtotal ? 2%`

**Key design point:** Even when Camelot passes all gates, **T2-Vision still runs**. Camelot's result is added as a candidate alongside T2 and T3, and the comparator picks the best. This prevents a case where Camelot correctly computed the math but silently dropped identifier fields.

---

### Step 3e — Tier 2: Multimodal Vision LLM (Primary Extraction Path)

T2 processes **each page independently** using both the PNG image from PyMuPDF and the spatially-aligned text from pdfplumber. Here is every sub-step:

---

#### Step 3e-i — One-Shot Column Layout Analysis (first content page only)

Before extracting any line items, a single LLM call is made on the first content page to understand the invoice's structural layout:

- **Sends:** The page PNG + aligned text
- **Asks:** How many identifier columns? What are their names? Is the layout one-row-per-item or multi-row? Are there sub-rows for batch/tracking?
- **Returns:** A short text block like: `"This invoice uses a multi-row layout. Each item block spans 4-5 rows: barcode on row 1, catalog code + description on row 2, tracking number on row 3, buyer item code labeled 'Tracking number' on row 4, quantities and price on row 5."`

This is stored as `layout_context` and prepended to every subsequent per-page prompt.

#### Step 3e-ii — First-Page Schema Anchoring

After extracting line items from the first page, the pipeline votes on the **dominant value type** for each identifier field across all extracted items:

- `item_code` — short numeric buyer code, e.g. `023199` (4–8 digits)
- `catalog_code` — mixed alphanumeric vendor code, e.g. `SM-923`, `100-0288`
- `barcode` — long numeric identifier, e.g. `20884521079295` (10+ digits)

The vote is based on character pattern matching. The result, e.g.:
```
item: "item_code"         (most items had 6-digit numeric values)
vend_cat_no: "catalog_code" (most items had alphanumeric hyphenated values)
reference_number: "barcode"  (most items had 14-digit numeric values)
```

This `schema_context` is injected into all subsequent per-page prompts so the LLM applies **consistent** field assignments. Without this, the LLM might place `023199` into `vend_cat_no` on page 2 if it had independently decided that field looked "catalog-like."

#### Step 3e-iii — Per-Page Prompt Assembly

For each page, the full prompt is assembled from 9 blocks:

| Block | What it contains | Why it is needed |
|---|---|---|
| **System message** | Core extraction rules, field guide (13 fields), identifier decision rules A–G, quantity rules, UOM rules, list of common mistakes to avoid | The universal instruction set — applies to any invoice format |
| **Page image** (PNG) | The rendered page at 200 DPI, encoded as `data:image/png;base64,...` | Lets the LLM see the visual layout — fonts, alignment, borders, indentation |
| **Aligned text** | Spatially-reconstructed column layout from pdfplumber words | Gives the LLM searchable, precise text to copy verbatim — avoids OCR errors in the image |
| **layout_context** | Output of the one-shot layout analysis | "This invoice uses multi-row blocks; track item codes labeled as 'Tracking number'" |
| **schema_context** | Field type votes from the first page | "item is always a 6-digit code on this invoice" |
| **subtotal_hint** | Detected invoice total | "All extracted items should sum to $945,951.26" |
| **page_ctx** | Page position | "Page 2 of 4" — helps the LLM understand this is a continuation page |
| **csv_sample** | Up to 10 reference CSV rows for this PO | Shows the LLM what "Item" and "Vend Cat No" values look like — pattern guidance only |
| **retry_hint** | (On retry only) Description of quality issues from first attempt | "Previous attempt had math errors on rows 3 and 7; re-read those rows carefully" |

**Anti-hallucination guard on CSV sample:**
The sample rows are preceded by an explicit instruction:
> *"These rows are REFERENCE PATTERNS ONLY. Do NOT copy values from them into your output. Extract ONLY what is physically printed on the invoice page. Do not substitute a CSV value even if the invoice appears similar — only exact invoice text counts."*

#### Step 3e-iv — LLM API Call Parameters

```python
client.chat.completions.create(
    model    = "gpt-4o",
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user",   "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,...", "detail": "high"}},
            {"type": "text", "text": <assembled prompt>},
        ]},
    ],
    max_tokens      = 16384,
    temperature     = 0,
    response_format = {"type": "json_object"},
    timeout         = 180,
)
```

- `detail: "high"` tells the OpenAI vision model to use its high-resolution tile processing — the page is divided into 512×512 tiles and each tile is analysed separately. This is necessary for dense invoice tables where small text must be read accurately.
- `temperature: 0` ensures deterministic output — the model always produces the same result for the same input.
- `response_format: json_object` forces valid JSON output, preventing markdown fences or prose text from corrupting the parse.
- `max_tokens: 16384` allows for large invoices with many line items per page.

#### Step 3e-v — JSON Response Parsing (`_parse_llm_json`)

The raw response text is cleaned before parsing:
1. Strip any accidental markdown fences (` ```json `, ` ``` `)
2. Find the first `{` and last `}` — extract just that slice
3. `json.loads()` the slice
4. Return `response["line_items"]` — the list of item dicts

**Partial-JSON recovery:** If the response was truncated mid-item (finish_reason = "length"), the parser finds the last complete `},` in the response and wraps everything up to that point in a valid JSON envelope, salvaging all complete items.

#### Step 3e-vi — Per-Page Quality Check and Retry

After parsing, each page's items are inspected:

1. **Math check:** For every item, is `extended_price ? trns_qty × unit_price` within 1¢?
2. **Suspicious value check:** Are any numeric fields implausibly large (e.g., `unit_price = 945951`, which is the invoice total, not a unit price)?
3. **Item count sanity:** For a dense page (many words), did the LLM return suspiciously few items?

If any check fails, **one retry** is made with a `retry_hint` appended to the prompt:
```
"IMPORTANT: Your previous response had issues:
 - Row 3: extended_price (714.88) ? trns_qty (8.0) × unit_price (89.36) = 714.88 ?
 - Row 7: unit_price (945951.26) looks like the invoice total, not a unit price
Please re-read those specific rows carefully and correct them."
```

---

### Step 3f — Tier 3: Text-Only LLM (Fallback)

T3 runs when:
- Vision is unavailable (PyMuPDF not installed, or `--no-vision` flag passed)
- T2 returned zero items for any page
- T2 produced a non-PASS result and the subtotal is known (T3 runs as a cross-check)

T3 uses **exactly the same extraction rules and prompt structure as T2** but sends only the spatially-aligned text — no PNG image. The `image_url` block is omitted from the message.

```python
client.chat.completions.create(
    model    = "gpt-4o",
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user",   "content": <text-only prompt>},  # no image
    ],
    max_tokens      = 16384,
    temperature     = 0,
    response_format = {"type": "json_object"},
    timeout         = 120,   # shorter — no image processing
)
```

For machine-readable PDFs, T3 accuracy is close to T2. For PDFs with unusual fonts or complex visual structure, T2 is significantly better.

---

### Step 3g — Best Tier Selection

After all tiers complete, their results go into a candidates list:

```python
candidates = [
    ("T1-camelot", items_t1,     verify(items_t1,     subtotal)),
    ("T2-vision",  items_vision, verify(items_vision, subtotal)),
    ("T3-text",    items_text,   verify(items_text,   subtotal)),
]
```

Each candidate is scored by a two-key sort:

**Key 1 — Subtotal difference (lower is better):**
```
diff = abs(sum(item.extended_price) - detected_subtotal)
```

**Key 2 — Identifier coverage (higher is better, used only on tie):**
```
coverage = count of items where item != null AND vend_cat_no != null
```

Negated so that higher coverage sorts lower (sort is ascending for min-first):
```python
sort_key = (diff_score, -coverage_score)
```

**Example:**
```
T1-camelot: diff=0.0  id_coverage=0/28   ? key=(0.0, 0)
T2-vision:  diff=0.0  id_coverage=26/28  ? key=(0.0, -26)
Winner: T2-vision  (same diff, but ?26 < 0, so T2 wins)
```

---

### Step 3h — Post-Extraction Normalization (deterministic Python, no LLM)

The winning tier's item list is passed through 8 deterministic correction steps:

#### 1. Null Sentinel Coercion
LLMs sometimes return `"None"`, `"null"`, `"N/A"`, `"-"`, `"--"`, or `""` as string values instead of JSON `null`. Every string field is checked against this sentinel list and converted to Python `None`.

#### 2. Field Swap Detection (`_normalize_field_assignments`)
Uses the page schema votes to detect when the LLM consistently placed values in the wrong identifier field:
- If `item` holds catalog-style values (`SM-923`) and `vend_cat_no` holds item-code-style values (`023199`) ? swap them
- Special case: if the page schema shows `vend_cat_no: item_code` as the **only** populated identifier field (the LLM put buyer codes in the wrong bucket), **move all values from `vend_cat_no` to `item`**

#### 3. Barcode Reclassification (`_fix_misplaced_barcodes`)
If `line_number` contains a 10+ digit numeric string ? move to `reference_number` and null `line_number`. Line numbers are always short sequential counters (1–999 typically).

#### 4. Tracking vs. Item Reclassification (`_reclassify_tracking_vs_item`)
For each item, two checks:
- If `tracking_number` holds a short numeric code (4–8 digits) with **no carrier name** ? move to `item` and null `tracking_number`
- If `item` holds a long alphanumeric code that matches a carrier tracking pattern ? move to `tracking_number` and null `item`

The carrier detection regex matches: `FedEx`, `UPS`, `DHL`, `USPS`, `OnTrac`, `LaserShip`, `1Z` (UPS prefix), etc.

#### 5. Cross-Field Deduplication (`_dedup_target_fields`)
If the same value appears in two identifier fields (e.g., `item = "SM-923"` and `vend_cat_no = "SM-923"`):
- Determine which field the page schema says this value type belongs to
- Keep it in the correct field, null the duplicate
- If no schema guidance: priority is `vend_cat_no > item > reference_number`

#### 6. Missing Identifier Recovery (`_recover_missing_item_codes`)
Scans all extracted items for:
- Items where `item` is null but `reference_number` (barcode) and price are both present
- Items where `vend_cat_no` is null but price is present

For each affected page, a **targeted re-prompt** is issued:

```python
# Sends:
# - Page image (PNG)
# - Aligned text
# - List of specific items that need recovery: {"match_key": "barcode_xxx", "missing": ["buyer_item_code"]}
# - CSV sample for pattern guidance
# Returns: {"items": [{"match_key": "...", "item_code": "023199", "vend_cat_no": "SM-923"}]}
```

Recovered values are merged back into the main item list by matching on the `match_key`.

#### 7. Hallucination Filter (`_is_hallucination`)
Items are removed if they match known hallucination patterns — e.g., a line item whose description exactly matches a phrase from the prompt examples.

#### 8. Outlier Price Filter
If there are 5+ items, computes the 90th-percentile unit price across all items. Any item with a unit price more than 3× that value is removed.
- Example: 27 items have unit prices between $89–$450. 90th percentile = $430. Threshold = $1,290. An item with `unit_price = 945951.26` is removed — the LLM accidentally read the invoice total as a unit price.
- The threshold has a floor of $1,000 — this filter never fires on legitimate high-value items.

---

### Step 3i — Subtotal Verification

```
extracted_total = sum(item["extended_price"] for all items where extended_price != null)
diff = abs(extracted_total - detected_subtotal)
status = "PASS" if diff < 0.01 else "FAIL"
```

`math_issues` lists rows where `extended_price ? trns_qty × unit_price` beyond 1¢.

> The system **never adjusts values** to force a match. FAIL = "flag for review", not "auto-fix".

---

### Step 3j — Cache Result

Writes `results/N335512.json`:
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

---

## Step 4 — Comparison (User Clicks "Compare")

**Request:** `POST /api/compare/N335512`

### Step 4a — Load Data

- Reads `results/N335512.json`
- The `po_number` field **inside** the JSON is used to filter the CSV (not the URL parameter `N335512`). This handles a subtle case: if the file was uploaded as `1637737.pdf`, the extraction result contains `"po_number": "N335512"`, and the comparison must use `N335512` to find matching CSV rows.
- `pandas` loads `retail.csv` and filters rows where the first column equals `N335512`

### Step 4b — Row Matching

For each extracted invoice item, the comparison engine finds the best matching CSV row using a 3-step fallback:

```
Step 1: Match on line_number
        normalize: strip leading zeros, strip non-alphanumeric chars
        e.g. "002" matches "2"

Step 2: (if step 1 fails) Match on vend_cat_no
        normalize: strip ALL non-alphanumeric chars, lowercase
        e.g. "100-0288" matches "1000288"

Step 3: (if step 2 fails) Match on item
        normalize: same as vend_cat_no

No match: the invoice item is marked "unmatched" in the output
```

### Step 4c — Field-by-Field Comparison

| Invoice field | CSV column | Method |
|---|---|---|
| `line_number` | Line Number | Alphanumeric normalize ? exact string match |
| `item` | Item | Alphanumeric normalize ? exact string match |
| `vend_cat_no` | Vend Cat No | Alphanumeric normalize ? exact string match |
| `order_qty` | Order Qty | Parse both as float ? `abs(a - b) ? 0.02` |
| `unit_price` | Unit Price | Parse both as float ? `abs(a - b) ? 0.02` |
| `inv_uom` | Inv Uom | Lowercase, collapse whitespace ? exact string match |

**Status codes:**

| Status | Meaning |
|---|---|
| `MATCH` | Values agree within tolerance |
| `NO MATCH` | Both present, values differ |
| `NOT FOUND IN CSV` | Invoice has a value but this field has no CSV counterpart |

- **Null fields are skipped** — if the invoice field is null for a specific row, no comparison is done for that field on that row.
- **Always-null fields** — if a field is null across the entire invoice (e.g., `tracking_number`, `batch_number`), it goes into `summary.fields_not_in_invoice` rather than generating noise rows.

### Step 4d — Save Result

Writes `compare/N335512_compare.json`.

---

## Step 5 — View Results in the UI

### PDF Viewer (left panel)
- The backend renders each page as a PNG on demand: `GET /api/pdf-page/N335512?page=1&dpi=150`
- PyMuPDF renders the page at 150 DPI for display (lower than extraction DPI to save bandwidth)
- Navigation arrows let the user step through pages

### CSV Preview (top-right)
- Loads from `GET /api/csv-rows/N335512`
- Uses the **PDF-extracted** PO (`N335512`) for filtering, not the original filename
- Shows column headers and all matching rows in a scrollable table

### Extracted Fields (middle-right)
- Shows the 13-field canonical schema per line item
- Null fields are hidden per row

### Comparison Table (bottom)
- **Grouped** by line item identifier (line_number / item / vend_cat_no)
- **Collapsed by default** — one header row per item with a summary badge
- **Expand** individual groups or use "Expand all"
- **Badges:** `? all match`, `? mismatch`, `? missing`
- **Filter pills:** `All`, `No Match`, `Not in CSV`, `Match`
- **Search:** full-text across all columns
- **NO MATCH rows** highlighted: invoice value in bold red, CSV value in red

---

## Library Responsibilities Summary

| Library | Used in | What it does |
|---|---|---|
| **pdfplumber** | `load_pdf()`, `_extract_po_number()`, `detect_subtotal()` | Extracts text with bounding-box coordinates (`extract_words()`), flat text (`extract_text()`), and renders pages as images as fallback |
| **PyMuPDF (fitz)** | `load_pdf()`, `/api/pdf-page/` | Renders PDF pages as high-quality PNG images at configurable DPI for both the vision LLM and the UI preview |
| **camelot-py** | `_extract_camelot_t1()` | Deterministic table extraction using geometric PDF structure (T1) |
| **openai** | All LLM calls | PO extraction, layout analysis, per-page extraction (T2/T3), subtotal fallback, recovery prompts |
| **pandas** | CSV/Excel loading | Loads reference data, filters rows by PO, provides sample rows |
| **FastAPI** | `backend/api.py` | REST API serving the UI and wrapping the pipeline |

---

## LLM Calls Per Invoice (Typical)

| Step | # Calls | Token cost | When skipped |
|---|---|---|---|
| PO extraction (LLM path) | 0–1 | ~100 | Regex succeeds (most cases) |
| Subtotal detection | 0–1 | ~100 | Regex finds the total |
| Column layout analysis | 1 | ~500 | Never skipped |
| Per-page extraction (T2) | N pages | ~2,000–8,000 per page | No-vision mode |
| Per-page extraction (T3) | 0–N | ~1,000–4,000 per page | T2 succeeded |
| Quality retry | 0–N | same as per-page | Quality check passes |
| Recovery re-prompt | 0–K | ~500 per page | No missing identifiers |

**Typical total for a 3-page invoice:** 4–6 calls, ~15,000–25,000 tokens.
