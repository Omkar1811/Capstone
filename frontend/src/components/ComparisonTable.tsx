import { useMemo, useState } from 'react'
import type { ComparisonResult, ComparisonStatus } from '../types'

interface Props {
  comparison: ComparisonResult | null
  isLoading:  boolean
}

interface FlatRow {
  identifier: string
  field:      string
  invoice:    string
  csv:        string
  status:     ComparisonStatus
}

interface Group {
  id:          string
  rows:        FlatRow[]
  hasNoMatch:  boolean
  hasNotFound: boolean
  allMatch:    boolean
}

// ── Style maps ────────────────────────────────────────────────────────────────

const ROW_BG: Record<ComparisonStatus, string> = {
  'MATCH':                'bg-white',
  'NO MATCH':             'bg-red-50',
  'NOT FOUND IN CSV':     'bg-amber-50',
  'NOT FOUND IN INVOICE': 'bg-gray-50',
}

const BADGE: Record<ComparisonStatus, string> = {
  'MATCH':                'bg-green-100 text-green-700 border-green-300',
  'NO MATCH':             'bg-red-100   text-red-700   border-red-300',
  'NOT FOUND IN CSV':     'bg-amber-100 text-amber-700 border-amber-300',
  'NOT FOUND IN INVOICE': 'bg-gray-100  text-gray-500  border-gray-300',
}

const BADGE_SHORT: Record<ComparisonStatus, string> = {
  'MATCH':                'MATCH',
  'NO MATCH':             'NO MATCH',
  'NOT FOUND IN CSV':     'NOT IN CSV',
  'NOT FOUND IN INVOICE': 'NOT IN INV',
}

type Filter = 'ALL' | ComparisonStatus

// ── Component ─────────────────────────────────────────────────────────────────

export default function ComparisonTable({ comparison, isLoading }: Props) {
  const [filter,   setFilter]   = useState<Filter>('ALL')
  const [search,   setSearch]   = useState('')
  // Tracks which groups are open; empty = all closed (default)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  // Flatten all comparison rows
  const flatRows: FlatRow[] = useMemo(() => {
    if (!comparison) return []
    const rows: FlatRow[] = []
    for (const row of comparison.comparison) {
      const id = Object.values(row.match_key).filter(Boolean).join(' / ')
      for (const [field, data] of Object.entries(row.fields)) {
        rows.push({
          identifier: id,
          field,
          invoice: data.invoice !== null && data.invoice !== undefined
            ? String(data.invoice) : '—',
          csv:    data.csv ?? '—',
          status: data.status,
        })
      }
    }
    return rows
  }, [comparison])

  // Apply filter + search
  const filtered = useMemo(() => {
    let rows = flatRows
    if (filter !== 'ALL') rows = rows.filter(r => r.status === filter)
    if (search.trim()) {
      const q = search.toLowerCase()
      rows = rows.filter(r =>
        r.identifier.toLowerCase().includes(q) ||
        r.field.toLowerCase().includes(q) ||
        r.invoice.toLowerCase().includes(q) ||
        r.csv.toLowerCase().includes(q),
      )
    }
    return rows
  }, [flatRows, filter, search])

  // Group by identifier
  const groups = useMemo((): Group[] => {
    const map = new Map<string, FlatRow[]>()
    for (const row of filtered) {
      const existing = map.get(row.identifier) ?? []
      existing.push(row)
      map.set(row.identifier, existing)
    }
    return [...map.entries()].map(([id, rows]) => ({
      id,
      rows,
      hasNoMatch:  rows.some(r => r.status === 'NO MATCH'),
      hasNotFound: rows.some(r => r.status === 'NOT FOUND IN CSV'),
      allMatch:    rows.every(r => r.status === 'MATCH'),
    }))
  }, [filtered])

  const stats   = comparison?.summary.field_stats ?? {}
  const nItems  = comparison?.summary.total_invoice_items ?? 0
  const nMatch  = stats['MATCH']         ?? 0
  const nNoMatch= stats['NO MATCH']      ?? 0
  const nNotCSV = stats['NOT FOUND IN CSV'] ?? 0

  const toggleGroup = (id: string) =>
    setExpanded(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  const collapseAll = () => setExpanded(new Set())
  const expandAll   = () => setExpanded(new Set(groups.map(g => g.id)))

  return (
    <div className="h-full flex flex-col bg-white">

      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-gray-200 bg-gray-50 flex-shrink-0 flex-wrap">
        <span className="text-xs font-semibold text-gray-600 uppercase tracking-wide">
          Comparison — CSV vs Extracted
        </span>

        {/* Filter pills */}
        {comparison && (
          <div className="flex gap-1 ml-2">
            {([
              ['ALL',              `All · ${nItems}`,     'bg-gray-100  text-gray-700  border-gray-300'],
              ['NO MATCH',         `No Match · ${nNoMatch}`, 'bg-red-100 text-red-700 border-red-300'],
              ['NOT FOUND IN CSV', `Not in CSV · ${nNotCSV}`, 'bg-amber-100 text-amber-700 border-amber-300'],
              ['MATCH',            `Match · ${nMatch}`,   'bg-green-100 text-green-700 border-green-300'],
            ] as [Filter, string, string][]).map(([key, label, cls]) => (
              <button
                key={key}
                onClick={() => setFilter(filter === key && key !== 'ALL' ? 'ALL' : key)}
                className={`text-xs px-2 py-0.5 rounded-full border font-medium transition whitespace-nowrap
                  ${cls}
                  ${filter === key ? 'ring-2 ring-offset-1 ring-blue-400' : 'opacity-60 hover:opacity-100'}`}
              >
                {label}
              </button>
            ))}
          </div>
        )}

        {/* Right side controls */}
        <div className="ml-auto flex items-center gap-2">
          {groups.length > 0 && (
            <>
              <button onClick={collapseAll} className="text-xs text-gray-400 hover:text-gray-700">Collapse all</button>
              <span className="text-gray-300 text-xs">|</span>
              <button onClick={expandAll}   className="text-xs text-gray-400 hover:text-gray-700">Expand all</button>
              <span className="text-gray-300 text-xs">|</span>
            </>
          )}
          <input
            type="text"
            placeholder="Search…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="text-xs border border-gray-300 rounded px-2 py-1 w-36 focus:outline-none focus:ring-1 focus:ring-blue-400"
          />
          <span className="text-xs text-gray-400 whitespace-nowrap">
            {groups.length} item{groups.length !== 1 ? 's' : ''}
          </span>
        </div>
      </div>

      {/* ── Body ───────────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-gray-400">
            <div className="w-7 h-7 border-4 border-indigo-200 border-t-indigo-500 rounded-full animate-spin" />
            <span className="text-xs">Running comparison…</span>
          </div>
        ) : flatRows.length === 0 ? (
          <div className="flex items-center justify-center h-full text-gray-400 text-xs">
            {comparison
              ? 'No rows match the current filter'
              : 'Click Compare after extracting to see field-level results'}
          </div>
        ) : (
          <table className="w-full text-xs border-collapse table-fixed">
            <colgroup>
              <col style={{ width: '18%' }} />  {/* Field */}
              <col style={{ width: '34%' }} />  {/* Invoice Value */}
              <col style={{ width: '34%' }} />  {/* CSV Value */}
              <col style={{ width: '14%' }} />  {/* Status */}
            </colgroup>
            <thead className="sticky top-0 bg-gray-100 z-10">
              <tr>
                <th className="px-3 py-1.5 text-left font-semibold text-gray-600 border-b border-gray-300">Field</th>
                <th className="px-3 py-1.5 text-left font-semibold text-gray-600 border-b border-gray-300">Invoice Value</th>
                <th className="px-3 py-1.5 text-left font-semibold text-gray-600 border-b border-gray-300">CSV Value</th>
                <th className="px-3 py-1.5 text-left font-semibold text-gray-600 border-b border-gray-300">Status</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => (
                <GroupRows
                  key={group.id}
                  group={group}
                  isOpen={expanded.has(group.id)}
                  onToggle={() => toggleGroup(group.id)}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* ── Footer ─────────────────────────────────────────────────────── */}
      {comparison?.summary.fields_not_in_invoice?.length ? (
        <div className="border-t border-gray-200 bg-gray-50 px-3 py-1 text-xs text-gray-400 flex-shrink-0">
          <span className="font-medium text-gray-500">Not in invoice:</span>{' '}
          {comparison.summary.fields_not_in_invoice.join(', ')}
        </div>
      ) : null}
    </div>
  )
}

// ── GroupRows sub-component ────────────────────────────────────────────────────

function GroupRows({
  group, isOpen, onToggle,
}: {
  group:    Group
  isOpen:   boolean
  onToggle: () => void
}) {
  return (
    <>
      {/* Group header row */}
      <tr
        onClick={onToggle}
        className="bg-gray-100 hover:bg-gray-200 cursor-pointer select-none border-b border-gray-300"
      >
        <td colSpan={3} className="px-3 py-1.5 font-semibold text-gray-700">
          <span className="mr-1.5 text-gray-400 text-xs">{isOpen ? '▼' : '▶'}</span>
          <span className="font-mono text-gray-800">{group.id}</span>
        </td>
        <td className="px-3 py-1.5">
          <div className="flex gap-1">
            {group.hasNoMatch && (
              <span className="px-1.5 py-0.5 rounded text-xs font-semibold bg-red-100 text-red-700 border border-red-300">
                ✗ mismatch
              </span>
            )}
            {group.hasNotFound && (
              <span className="px-1.5 py-0.5 rounded text-xs font-semibold bg-amber-100 text-amber-700 border border-amber-300">
                ? missing
              </span>
            )}
            {group.allMatch && (
              <span className="px-1.5 py-0.5 rounded text-xs font-semibold bg-green-100 text-green-700 border border-green-300">
                ✓ all match
              </span>
            )}
          </div>
        </td>
      </tr>

      {/* Field rows — only rendered when expanded */}
      {isOpen && group.rows.map((row, i) => (
        <tr key={i} className={`${ROW_BG[row.status]} border-b border-gray-100`}>
          <td className="px-3 py-1 pl-8 font-medium text-gray-600 capitalize truncate">
            {row.field.replace(/_/g, ' ')}
          </td>
          <td className="px-3 py-1 font-mono truncate">
            {row.status === 'NO MATCH'
              ? <span className="text-red-700 font-semibold">{row.invoice}</span>
              : <span className="text-gray-800">{row.invoice}</span>}
          </td>
          <td className="px-3 py-1 font-mono truncate">
            {row.status === 'NO MATCH'
              ? <span className="text-red-500">{row.csv}</span>
              : <span className="text-gray-500">{row.csv}</span>}
          </td>
          <td className="px-3 py-1">
            <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium border ${BADGE[row.status]}`}>
              {BADGE_SHORT[row.status]}
            </span>
          </td>
        </tr>
      ))}
    </>
  )
}
