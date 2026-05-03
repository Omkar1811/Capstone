import { api } from '../api'

interface Props {
  po:           string
  pages:        number
  currentPage:  number
  onPageChange: (page: number) => void
}

export default function PDFViewer({ po, pages, currentPage, onPageChange }: Props) {
  const imgUrl = pages > 0 ? api.pdfPageUrl(po, currentPage) : null

  return (
    <div className="h-full flex flex-col">
      {/* Panel header */}
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-gray-200 bg-gray-50 flex-shrink-0">
        <span className="text-xs font-semibold text-gray-600 uppercase tracking-wide">
          Invoice PDF Preview
        </span>
        {pages > 0 && (
          <div className="flex items-center gap-2">
            <button
              disabled={currentPage <= 1}
              onClick={() => onPageChange(currentPage - 1)}
              className="text-sm px-2 py-0.5 rounded border border-gray-300 hover:bg-gray-100 disabled:opacity-40"
            >
              ‹
            </button>
            <span className="text-xs text-gray-600">
              {currentPage} / {pages}
            </span>
            <button
              disabled={currentPage >= pages}
              onClick={() => onPageChange(currentPage + 1)}
              className="text-sm px-2 py-0.5 rounded border border-gray-300 hover:bg-gray-100 disabled:opacity-40"
            >
              ›
            </button>
          </div>
        )}
      </div>

      {/* PDF page image */}
      <div className="flex-1 overflow-auto bg-gray-200 flex items-start justify-center p-4">
        {imgUrl ? (
          <img
            key={`${po}-${currentPage}`}
            src={imgUrl}
            alt={`Page ${currentPage}`}
            className="max-w-full shadow-lg bg-white"
            style={{ display: 'block' }}
          />
        ) : (
          <div className="flex items-center justify-center h-full text-gray-400 text-sm">
            No PDF loaded
          </div>
        )}
      </div>
    </div>
  )
}
