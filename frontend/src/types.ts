export interface LineItem {
  line_number:    string | null
  reference_number: string | null
  item:           string | null
  vend_cat_no:    string | null
  description:    string | null
  order_qty:      number | null
  trns_qty:       number | null
  inv_uom:        string | null
  unit_price:     number | null
  extended_price: number | null
  pack_size:      string | null
  tracking_number: string | null
  batch_number:   string | null
  [key: string]: unknown
}

export interface Verification {
  extracted_total:   number
  detected_subtotal: number | null
  subtotal_match:    boolean
  diff:              number | null
  line_count:        number
  status:            'PASS' | 'FAIL'
}

export interface ExtractionResult {
  po_number:  string
  tier:       string
  line_items: LineItem[]
  verification: Verification
  _cached?:   boolean
}

export interface CSVData {
  po:      string
  columns: string[]
  rows:    Record<string, string>[]
  total:   number
}

export type ComparisonStatus =
  | 'MATCH'
  | 'NO MATCH'
  | 'NOT FOUND IN CSV'
  | 'NOT FOUND IN INVOICE'

export interface ComparisonField {
  invoice: unknown
  csv:     string | null
  status:  ComparisonStatus
}

export interface ComparisonRow {
  match_key:    Record<string, string>
  csv_row_found: boolean
  fields:       Record<string, ComparisonField>
}

export interface ComparisonSummary {
  total_invoice_items:    number
  csv_rows_available:     number
  items_matched_to_csv:   number
  items_unmatched_to_csv: number
  fields_not_in_invoice:  string[]
  field_stats:            Record<string, number>
}

export interface ComparisonResult {
  po_number:  string
  comparison: ComparisonRow[]
  summary:    ComparisonSummary
}

export interface PdfInfo {
  po:    string
  pages: number
}
