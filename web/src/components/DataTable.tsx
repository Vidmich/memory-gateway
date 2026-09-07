import { useMemo, useState, type ReactNode } from 'react'

import { EmptyState } from '@/components/EmptyState'

/**
 * The table every list screen from task 05 on renders into.
 *
 * Two things it deliberately does *not* do. It does not fetch — the caller owns the
 * query, because pagination is server-side and the cursor belongs to whoever holds the
 * request. And it does not page by offset: SPEC §12.2 makes every list endpoint
 * cursor-paginated, so this exposes next/previous, not page numbers, and a page count is
 * simply not available.
 *
 * Sorting is a callback when `onSortChange` is given (the server sorts, which is the
 * only correct option across pages) and local otherwise, for the small fixed lists that
 * arrive whole.
 */

export type Column<Row> = {
  key: string
  header: ReactNode
  /** Cell contents. */
  render: (row: Row) => ReactNode
  /** Omit to make the column unsortable. */
  sortValue?: (row: Row) => string | number
  align?: 'left' | 'right'
  width?: string
}

export type SortState = { key: string; direction: 'asc' | 'desc' }

export type DataTableProps<Row> = {
  rows: readonly Row[]
  columns: readonly Column<Row>[]
  rowKey: (row: Row) => string
  caption?: string
  loading?: boolean
  emptyTitle?: string
  emptyDescription?: ReactNode
  emptyAction?: ReactNode
  sort?: SortState | null
  /** Provide to sort server-side; without it the table sorts the rows it was given. */
  onSortChange?: (sort: SortState) => void
  onRowClick?: (row: Row) => void
  /** Cursor pagination. `null` means there is nothing in that direction. */
  onNextPage?: (() => void) | null
  onPreviousPage?: (() => void) | null
}

export function DataTable<Row>({
  rows,
  columns,
  rowKey,
  caption,
  loading = false,
  emptyTitle = 'Nothing here yet',
  emptyDescription = 'Items will appear here once they exist.',
  emptyAction,
  sort = null,
  onSortChange,
  onRowClick,
  onNextPage = null,
  onPreviousPage = null,
}: DataTableProps<Row>) {
  const [localSort, setLocalSort] = useState<SortState | null>(null)
  const activeSort = onSortChange ? sort : (sort ?? localSort)

  const visible = useMemo(() => {
    if (onSortChange || !activeSort) return rows
    const column = columns.find((candidate) => candidate.key === activeSort.key)
    if (!column?.sortValue) return rows

    const sortValue = column.sortValue
    const factor = activeSort.direction === 'asc' ? 1 : -1
    return [...rows].sort((left, right) => {
      const a = sortValue(left)
      const b = sortValue(right)
      if (a === b) return 0
      return (a < b ? -1 : 1) * factor
    })
  }, [rows, columns, activeSort, onSortChange])

  const toggle = (column: Column<Row>) => {
    if (!column.sortValue && !onSortChange) return
    const direction =
      activeSort?.key === column.key && activeSort.direction === 'asc' ? 'desc' : 'asc'
    const next: SortState = { key: column.key, direction }
    if (onSortChange) onSortChange(next)
    else setLocalSort(next)
  }

  if (!loading && rows.length === 0) {
    return (
      <EmptyState title={emptyTitle} description={emptyDescription} action={emptyAction} />
    )
  }

  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          {caption ? <caption className="sr-only">{caption}</caption> : null}
          <thead className="bg-slate-50">
            <tr>
              {columns.map((column) => {
                const sortable = Boolean(column.sortValue ?? onSortChange)
                const isSorted = activeSort?.key === column.key
                return (
                  <th
                    key={column.key}
                    scope="col"
                    style={column.width ? { width: column.width } : undefined}
                    aria-sort={
                      isSorted
                        ? activeSort.direction === 'asc'
                          ? 'ascending'
                          : 'descending'
                        : sortable
                          ? 'none'
                          : undefined
                    }
                    className={`px-4 py-2.5 font-medium text-slate-600 ${
                      column.align === 'right' ? 'text-right' : 'text-left'
                    }`}
                  >
                    {sortable ? (
                      <button
                        type="button"
                        onClick={() => toggle(column)}
                        className="inline-flex items-center gap-1 hover:text-slate-900"
                      >
                        {column.header}
                        <span aria-hidden="true" className="text-xs text-slate-400">
                          {isSorted ? (activeSort.direction === 'asc' ? '▲' : '▼') : '↕'}
                        </span>
                      </button>
                    ) : (
                      column.header
                    )}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {loading && rows.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="px-4 py-8 text-center text-slate-500">
                  Loading…
                </td>
              </tr>
            ) : (
              visible.map((row) => (
                <tr
                  key={rowKey(row)}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={onRowClick ? 'cursor-pointer hover:bg-slate-50' : undefined}
                >
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      className={`px-4 py-2.5 text-slate-700 ${
                        column.align === 'right' ? 'text-right' : 'text-left'
                      }`}
                    >
                      {column.render(row)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {onNextPage || onPreviousPage ? (
        <div className="flex items-center justify-end gap-2 border-t border-slate-200 px-4 py-2">
          <button
            type="button"
            onClick={onPreviousPage ?? undefined}
            disabled={!onPreviousPage}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 disabled:cursor-not-allowed disabled:text-slate-300"
          >
            Previous
          </button>
          <button
            type="button"
            onClick={onNextPage ?? undefined}
            disabled={!onNextPage}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 disabled:cursor-not-allowed disabled:text-slate-300"
          >
            Next
          </button>
        </div>
      ) : null}
    </div>
  )
}
