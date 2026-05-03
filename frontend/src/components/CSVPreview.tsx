import type { CSVData } from '../types'

interface Props {
  data: CSVData | null
}

export default function CSVPreview({ data }: Props) {
  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-gray-200 bg-gray-50 flex-shrink-0">
        <span className="text-xs font-semibold text-gray-600 uppercase tracking-wide">
          CSV File Preview
        </span>
        {data && (
          <span className="text-xs text-gray-400">{data.total} rows</span>
        )}
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto">
        {!data || data.rows.length === 0 ? (
          <div className="flex items-center justify-center h-full text-gray-400 text-xs">
            {data ? 'No CSV rows for this PO' : 'No CSV loaded'}
          </div>
        ) : (
          <table className="w-full text-xs border-collapse">
            <thead className="sticky top-0 bg-gray-100 z-10">
              <tr>
                {data.columns.map(col => (
                  <th
                    key={col}
                    className="px-2 py-1.5 text-left font-semibold text-gray-600 border-b border-gray-200 whitespace-nowrap"
                  >
                    {col}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row, i) => (
                <tr
                  key={i}
                  className={i % 2 === 0 ? 'bg-white' : 'bg-gray-50'}
                >
                  {data.columns.map(col => (
                    <td
                      key={col}
                      className="px-2 py-1 border-b border-gray-100 text-gray-700 whitespace-nowrap"
                    >
                      {row[col] ?? '—'}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
