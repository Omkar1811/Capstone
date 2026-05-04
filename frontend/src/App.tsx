import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import type {
  ComparisonResult,
  CSVData,
  ExtractionResult,
  PdfInfo,
} from './types'
import ComparisonTable from './components/ComparisonTable'
import CSVPreview from './components/CSVPreview'
import ExtractedFields from './components/ExtractedFields'
import PDFViewer from './components/PDFViewer'
import Toolbar from './components/Toolbar'

export default function App() {
  const [availablePos, setAvailablePos]       = useState<string[]>([])
  const [selectedPo, setSelectedPo]           = useState<string | null>(null)
  const [pdfInfo, setPdfInfo]                 = useState<PdfInfo | null>(null)
  const [currentPage, setCurrentPage]         = useState(1)
  const [csvData, setCsvData]                 = useState<CSVData | null>(null)
  const [extraction, setExtraction]           = useState<ExtractionResult | null>(null)
  const [comparison, setComparison]           = useState<ComparisonResult | null>(null)
  const [isExtracting, setIsExtracting]       = useState(false)
  const [isComparing, setIsComparing]         = useState(false)
  const [error, setError]                     = useState<string | null>(null)
  const [csvInfo, setCsvInfo]                 = useState<{ filename: string; rows: number } | null>(null)

  // Load available POs on mount
  useEffect(() => {
    api.listPos()
      .then(r => setAvailablePos(r.pos))
      .catch(() => {})
  }, [])

  const selectPo = useCallback(async (po: string) => {
    setSelectedPo(po)
    setCurrentPage(1)
    setExtraction(null)
    setComparison(null)
    setError(null)

    // Load PDF info
    try {
      const info = await api.getPdfInfo(po)
      setPdfInfo(info)
    } catch {
      setPdfInfo(null)
    }

    // Load cached results first — the result may contain a different po_number
    // (the PO extracted from the PDF content) which is the correct key for
    // the CSV lookup (e.g. file keyed as "1637737" but CSV rows are under "N335512").
    let csvPo = po
    try {
      const res = await api.getResults(po)
      setExtraction(res)
      const extractedPo = (res as Record<string, unknown>).po_number as string | null
      if (extractedPo && extractedPo !== po) csvPo = extractedPo
    } catch {
      setExtraction(null)
    }

    // Load CSV rows using the PDF-extracted PO when available
    try {
      const csv = await api.getCsvRows(csvPo)
      setCsvData(csv)
    } catch {
      setCsvData(null)
    }

    // Load cached comparison if it exists
    try {
      const cmp = await api.getComparison(po)
      setComparison(cmp)
    } catch {
      setComparison(null)
    }
  }, [])

  const handlePdfUpload = useCallback(async (file: File) => {
    setError(null)
    try {
      const info = await api.uploadPdf(file)

      if (info.po === null) {
        setError('No PO number found in this PDF. Please check the invoice and try again.')
        return
      }

      const po = info.po
      setPdfInfo({ po, pages: info.pages })
      setSelectedPo(po)
      setCurrentPage(1)
      setExtraction(null)
      setComparison(null)
      setAvailablePos(prev => [...new Set([...prev, po])].sort())
      const csv = await api.getCsvRows(po).catch(() => null)
      setCsvData(csv)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'PDF upload failed')
    }
  }, [])

  const handleCsvUpload = useCallback(async (file: File) => {
    setError(null)
    try {
      const info = await api.uploadCsv(file)
      setCsvInfo({ filename: info.filename, rows: info.rows })

      // Use extraction's po_number when available — it is the authoritative PO
      // that matches CSV rows, even if selectedPo is still a filename-based key.
      const lookupPo =
        (extraction as Record<string, unknown> | null)?.po_number as string | null
        ?? selectedPo
      if (lookupPo) {
        const csv = await api.getCsvRows(lookupPo).catch(() => null)
        setCsvData(csv)
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'CSV upload failed')
    }
  }, [selectedPo, extraction])

  const handleExtract = useCallback(async (force = false) => {
    if (!selectedPo) return
    setError(null)
    setIsExtracting(true)
    try {
      const result = await api.extract(selectedPo, force)
      setExtraction(result)
      setComparison(null)

      // Always refresh CSV after extraction so data shows even if the initial
      // getCsvRows (before any CSV was uploaded) returned empty.
      const extractedPo = (result as Record<string, unknown>).po_number as string | null
      const lookupPo = extractedPo || selectedPo
      const csv = await api.getCsvRows(lookupPo).catch(() => null)
      setCsvData(csv)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Extraction failed')
    } finally {
      setIsExtracting(false)
    }
  }, [selectedPo])

  const handleCompare = useCallback(async () => {
    if (!selectedPo) return
    setError(null)
    setIsComparing(true)
    try {
      const result = await api.compare(selectedPo)
      setComparison(result)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Comparison failed')
    } finally {
      setIsComparing(false)
    }
  }, [selectedPo])

  return (
    <div className="h-screen flex flex-col bg-gray-100 overflow-hidden">
      {/* ── Toolbar ────────────────────────────────────────────────────────── */}
      <Toolbar
        availablePos={availablePos}
        selectedPo={selectedPo}
        extraction={extraction}
        isExtracting={isExtracting}
        isComparing={isComparing}
        onSelectPo={selectPo}
        onPdfUpload={handlePdfUpload}
        onCsvUpload={handleCsvUpload}
        onExtract={handleExtract}
        onCompare={handleCompare}
      />

      {/* ── Error banner ───────────────────────────────────────────────────── */}
      {error && (
        <div className="bg-red-50 border-b border-red-200 px-4 py-2 text-red-700 text-sm flex items-center gap-2">
          <span className="font-medium">Error:</span> {error}
          <button
            onClick={() => setError(null)}
            className="ml-auto text-red-400 hover:text-red-600"
          >
            ✕
          </button>
        </div>
      )}

      {/* ── CSV loaded banner ──────────────────────────────────────────────── */}
      {csvInfo && !error && (
        <div className="bg-green-50 border-b border-green-200 px-4 py-1.5 text-green-700 text-xs flex items-center gap-2">
          <span>✓</span>
          <span><span className="font-medium">{csvInfo.filename}</span> loaded — {csvInfo.rows.toLocaleString()} rows</span>
          <button
            onClick={() => setCsvInfo(null)}
            className="ml-auto text-green-400 hover:text-green-600"
          >
            ✕
          </button>
        </div>
      )}

      {/* ── Main content ───────────────────────────────────────────────────── */}
      {selectedPo ? (
        <div className="flex-1 flex flex-col min-h-0">

          {/* Top row: PDF + Right panels */}
          <div className="flex-1 flex min-h-0">

            {/* Left: PDF Viewer */}
            <div className="w-[55%] min-h-0 bg-white border-r border-gray-200">
              <PDFViewer
                po={selectedPo}
                pages={pdfInfo?.pages ?? 0}
                currentPage={currentPage}
                onPageChange={setCurrentPage}
              />
            </div>

            {/* Right: CSV + Extracted */}
            <div className="w-[45%] flex flex-col min-h-0">
              <div className="flex-1 min-h-0 border-b border-gray-200">
                <CSVPreview data={csvData} />
              </div>
              <div className="flex-1 min-h-0">
                <ExtractedFields
                  extraction={extraction}
                  isLoading={isExtracting}
                />
              </div>
            </div>
          </div>

          {/* Bottom: Comparison table */}
          <div className="h-56 border-t border-gray-200 bg-white">
            <ComparisonTable
              comparison={comparison}
              isLoading={isComparing}
            />
          </div>
        </div>
      ) : (
        <div className="flex-1 flex items-center justify-center text-gray-400">
          <div className="text-center space-y-2">
            <div className="text-5xl">📄</div>
            <p className="text-lg font-medium">No invoice selected</p>
            <p className="text-sm">Upload a PDF or select a PO from the toolbar to get started.</p>
          </div>
        </div>
      )}
    </div>
  )
}
