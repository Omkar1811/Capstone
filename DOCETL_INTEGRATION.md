# DocETL features used in `invoice_docetl.py`

This document describes which **DocETL** concepts are used by [invoice_docetl.py](invoice_docetl.py), **why**, and what stays outside DocETL.

**Why `invoice_docetl.py` (not `docetl.py`)**  
A file named `docetl.py` in the project root shadows the installed `docetl` package and breaks `from docetl.api import Pipeline`. The runner is therefore named `invoice_docetl.py`.

---

## Execution style: Python Pipeline API (not YAML CLI)

The pipeline is built with **`docetl.api`**:

- `Pipeline`, `Dataset`, `MapOp`, `ParsingTool`, `PipelineStep`, `PipelineOutput`

and executed with:

```python
cost = pipeline.run()
```

This matches the DocETL **Python API** (`docetl.api`): construct `Pipeline` objects, then call `run()` (no hand-written YAML or `docetl run` subprocess from this script).

---

## 1. Dataset (`Dataset` + `type="file"`)

- One JSON file ? one record per invoice page (aligned text, image base64, metadata).
- **Parsing** is attached with `parsing=[{"function": "vision_page_extractor"}]` so DocETL loads rows and then runs the custom tool per row.

---

## 2. Custom parsing (`ParsingTool` + `function_code`)

- **`vision_page_extractor`** is registered as a `ParsingTool` whose `function_code` is embedded Python (same idea as YAML `parsing_tools`).
- It calls **OpenAI Vision** (`gpt-4o-mini`) per page, adds `extracted_items`, `extraction_error`, and **`llm_validate_block`** (a plain-text bundle of candidate items for the next step).
- **Why**: DocETLùs native map step is text/LLM-oriented; multimodal extraction needs arbitrary Python at load time.

---

## 3. Map operation (`MapOp`)

- **`validate_and_correct`** validates math, identifier roles, credits, and backorders.
- **`output.schema`** constrains `validated_items` to the line-item shape.
- **`gleaning`**: one refinement round with a separate validation prompt (DocETLùs built-in quality loop).
- **`litellm_completion_kwargs`**: `max_tokens=16000`, `temperature=0`.

---

## 4. Prompts without Jinja control-flow

DocETL still requires **at least one** `{{ ... }}` expression in map prompts (its validator). We avoid `{% for %}` / `{% if %}`:

- The map prompt is fixed rules plus **one** substitution: `{{ input.llm_validate_block }}`.
- Per-item lines are built in **Python** inside `vision_page_extractor`, not in the template.

Gleaningùs `validation_prompt` uses `{{ "" }}` as a no-op expression so the string is valid Jinja, with the rest of the instructions in plain text.

---

## 5. System context (`system_prompt` kwargs)

Passed into `Pipeline(..., system_prompt={...})` via `**kwargs` / `other_config`:

- `dataset_description` ù invoice domain.
- `persona` ù accounts-payable style behaviour for all LLM steps.

---

## 6. Caching (`bypass_cache=False`)

DocETL may skip redundant LLM work when inputs and operations are unchanged.

---

## 7. Outside DocETL (plain Python)

| Piece | Why |
|--------|-----|
| `pdf_loader` / `azure_di_loader` | Deterministic PDF ? words, aligned text, PNG, PO/subtotal regex |
| `aggregate_pages`, `_normalize_item_fields`, `verify_total` | Business rules and totals check in code |

---

## Quick reference

| DocETL piece | In code |
|--------------|---------|
| `Pipeline.run()` | `run_docetl_pipeline()` |
| `ParsingTool` | `_VISION_EXTRACT_FUNC` |
| `Dataset` + parsing | `invoice_pages` |
| `MapOp` + gleaning | `validate_and_correct` |
| `system_prompt` | keyword arg to `Pipeline` |
