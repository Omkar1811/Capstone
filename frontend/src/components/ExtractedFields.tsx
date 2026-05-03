import type { ExtractionResult } from '../types'

interface Props {
  extraction: ExtractionResult | null
  isLoading:  boolean
}

// Fields to show as columns in the preview table (in display order)
const DISPLAY_FIELDS = [
  'line_number', 'item', 'vend_cat_no', 'description',
  'order_qty', 'trns_qty', 'unit_price', 'extended_price', 'inv_uom',
  'tracking_number', 'reference_number',
]

export default function ExtractedFields({ extraction, isLoading }: Props) {
  const items = extraction?.line_items ?? []

  // Only show columns that have at least one non-null value
  const activeCols = DISPLAY_FIELDS.filter(f =>
    items.some(it => it[f] !== null && it[f] !== undefined),
  )

  const vfy = extraction?.verification

  return (
    <div className="h-full flex flex-col bg-white border-t border-gray-100">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-gray-200 bg-gray-50 flex-shrink-0">
        <span className="text-xs font-semibold text-gray-600 uppercase tracking-wide">
          Extracted Fields from Invoice
        </span>
        <div className="flex items-center gap-2">
          {isLoading && (
            <span className="text-xs text-blue-500 animate-pulse">Extracting…</span>
          )}
          {vfy && (
            <span
              className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                vfy.status === 'PASS'
                  ? 'bg-green-100 text-green-700'
                  : 'bg-red-100 text-red-700'
              }`}
            >
              {vfy.status} · {items.length} items
            </span>
          )}
        </div>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-gray-400">
            <div className="w-8 h-8 border-4 border-blue-200 border-t-blue-500 rounded-full animate-spin" />
            <span className="text-xs">Running extraction… this may take a minute</span>
          </div>
        ) : items.length === 0 ? (
          <div className="flex items-center justify-center h-full text-gray-400 text-xs">
            {extraction ? 'No items extracted' : 'Click Extract to run invoice extraction'}
          </div>
        ) : (
          <table className="w-full text-xs border-collapse">
            <thead className="sticky top-0 bg-gray-100 z-10">
              <tr>
                {activeCols.map(f => (
                  <th
                    key={f}
                    className="px-2 py-1.5 text-left font-semibold text-gray-600 border-b border-gray-200 whitespace-nowrap"
                  >
                    {f.replace(/_/g, ' ')}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((item, i) => (
                <tr key={i} className={i % 2 === 0 ? 'bg-white' : 'bg-gray-50'}>
                  {activeCols.map(f => {
                    const val = item[f]
                    return (
                      <td
                        key={f}
                        className={`px-2 py-1 border-b border-gray-100 whitespace-nowrap ${
                          val === null ? 'text-gray-300 italic' : 'text-gray-700'
                        }`}
                      >
                        {val === null ? '—' : String(val)}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
