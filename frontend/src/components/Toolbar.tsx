import { useRef } from 'react'
import type { ExtractionResult } from '../types'

interface Props {
  availablePos:  string[]
  selectedPo:    string | null
  extraction:    ExtractionResult | null
  isExtracting:  boolean
  isComparing:   boolean
  onSelectPo:    (po: string) => void
  onPdfUpload:   (file: File) => void
  onCsvUpload:   (file: File) => void
  onExtract:     (force?: boolean) => void
  onCompare:     () => void
}

export default function Toolbar({
  availablePos, selectedPo, extraction,
  isExtracting, isComparing,
  onSelectPo, onPdfUpload, onCsvUpload, onExtract, onCompare,
}: Props) {
  const pdfInput = useRef<HTMLInputElement>(null)
  const csvInput = useRef<HTMLInputElement>(null)

  const vfy = extraction?.verification
  const statusColor = !vfy
    ? ''
    : vfy.status === 'PASS'
    ? 'text-green-600 bg-green-50 border-green-300'
    : 'text-red-600 bg-red-50 border-red-300'

  return (
    <header className="bg-white border-b border-gray-200 px-4 py-2 flex items-center gap-3 flex-wrap shadow-sm z-10">
      {/* Brand */}
      <div className="flex items-center gap-2 mr-2">
        <span className="text-xl">🧾</span>
        <span className="font-semibold text-gray-800 text-sm">Invoice Processor</span>
      </div>

      <div className="w-px h-6 bg-gray-200" />

      {/* PO selector */}
      <div className="flex items-center gap-1.5">
        <label className="text-xs text-gray-500 font-medium">PO</label>
        <select
          value={selectedPo ?? ''}
          onChange={e => e.target.value && onSelectPo(e.target.value)}
          className="text-sm border border-gray-300 rounded-md px-2 py-1 bg-white focus:outline-none focus:ring-2 focus:ring-blue-400"
        >
          <option value="">Select PO…</option>
          {availablePos.map(po => (
            <option key={po} value={po}>{po}</option>
          ))}
        </select>
      </div>

      <div className="w-px h-6 bg-gray-200" />

      {/* Upload PDF */}
      <input
        ref={pdfInput}
        type="file"
        accept=".pdf"
        className="hidden"
        onChange={e => e.target.files?.[0] && onPdfUpload(e.target.files[0])}
      />
      <button
        onClick={() => pdfInput.current?.click()}
        className="flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-md border border-gray-300 hover:bg-gray-50 transition"
      >
        <span>📤</span> Upload PDF
      </button>

      {/* Upload CSV */}
      <input
        ref={csvInput}
        type="file"
        accept=".csv,.xlsx,.xls"
        className="hidden"
        onChange={e => e.target.files?.[0] && onCsvUpload(e.target.files[0])}
      />
      <button
        onClick={() => csvInput.current?.click()}
        className="flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-md border border-gray-300 hover:bg-gray-50 transition"
      >
        <span>📊</span> Upload CSV
      </button>

      <div className="w-px h-6 bg-gray-200" />

      {/* Extract button */}
      <button
        disabled={!selectedPo || isExtracting}
        onClick={() => onExtract(false)}
        className="flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition"
      >
        {isExtracting ? (
          <><span className="animate-spin">⟳</span> Extracting…</>
        ) : (
          <><span>⚡</span> Extract</>
        )}
      </button>

      {/* Re-extract (force) */}
      {extraction && (
        <button
          disabled={!selectedPo || isExtracting}
          onClick={() => onExtract(true)}
          title="Re-run extraction (ignore cache)"
          className="text-xs px-2 py-1.5 rounded-md border border-blue-300 text-blue-600 hover:bg-blue-50 disabled:opacity-40 transition"
        >
          ↺ Re-extract
        </button>
      )}

      {/* Compare button */}
      <button
        disabled={!extraction || isComparing || isExtracting}
        onClick={onCompare}
        className="flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40 disabled:cursor-not-allowed transition"
      >
        {isComparing ? (
          <><span className="animate-spin">⟳</span> Comparing…</>
        ) : (
          <><span>🔍</span> Compare</>
        )}
      </button>

      {/* Verification badge */}
      {vfy && (
        <div className={`ml-auto flex items-center gap-2 text-xs font-medium px-3 py-1 rounded-full border ${statusColor}`}>
          <span>{vfy.status === 'PASS' ? '✓' : '✗'}</span>
          <span>{vfy.status}</span>
          {vfy.diff !== null && (
            <span className="text-gray-500">
              diff: ${Math.abs(vfy.diff).toFixed(2)}
            </span>
          )}
          {extraction?._cached && (
            <span className="text-gray-400 font-normal">(cached)</span>
          )}
        </div>
      )}
    </header>
  )
}
