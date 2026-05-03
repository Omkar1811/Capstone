import type {
  ComparisonResult,
  CSVData,
  ExtractionResult,
  PdfInfo,
} from './types'

// Vite proxy forwards /api → http://localhost:8000
const BASE = ''

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText })) as { detail?: string }
    throw new Error(err.detail ?? res.statusText)
  }
  return res.json() as Promise<T>
}

export const api = {
  // ── PO list ────────────────────────────────────────────────────────────────
  listPos: () =>
    fetch(`${BASE}/api/list-pos`).then(r => handle<{ pos: string[] }>(r)),

  // ── PDF ────────────────────────────────────────────────────────────────────
  uploadPdf: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return fetch(`${BASE}/api/upload-pdf`, { method: 'POST', body: form })
      .then(r => handle<{ po: string | null; pages: number; error?: string }>(r))
  },

  getPdfInfo: (po: string) =>
    fetch(`${BASE}/api/pdf-info/${po}`).then(r => handle<PdfInfo>(r)),

  /** Returns the URL to use as <img src> for a given page (1-indexed). */
  pdfPageUrl: (po: string, page: number, dpi = 150): string =>
    `${BASE}/api/pdf-page/${po}?page=${page}&dpi=${dpi}`,

  // ── CSV ────────────────────────────────────────────────────────────────────
  uploadCsv: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return fetch(`${BASE}/api/upload-csv`, { method: 'POST', body: form })
      .then(r => handle<{ filename: string; rows: number; pos: string[] }>(r))
  },

  getCsvRows: (po: string) =>
    fetch(`${BASE}/api/csv-rows/${po}`).then(r => handle<CSVData>(r)),

  // ── Extraction ─────────────────────────────────────────────────────────────
  extract: (po: string, force = false) =>
    fetch(`${BASE}/api/extract/${po}?force=${force}`, { method: 'POST' })
      .then(r => handle<ExtractionResult>(r)),

  getResults: (po: string) =>
    fetch(`${BASE}/api/results/${po}`).then(r => handle<ExtractionResult>(r)),

  // ── Comparison ─────────────────────────────────────────────────────────────
  compare: (po: string) =>
    fetch(`${BASE}/api/compare/${po}`, { method: 'POST' })
      .then(r => handle<ComparisonResult>(r)),

  getComparison: (po: string) =>
    fetch(`${BASE}/api/compare/${po}`).then(r => handle<ComparisonResult>(r)),
}
