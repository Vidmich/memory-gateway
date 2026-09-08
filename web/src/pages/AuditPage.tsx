import { useState, type ReactNode } from 'react'

import { exportPath, useAuditEvents, type AuditFilters } from '@/api/audit'
import { useApiClient } from '@/auth/AuthContext'
import { useToast } from '@/components/Toast'
import { AuditTrail } from '@/pages/AuditTrail'
import { TARGET_LABELS, emptyHint, hasFilters } from '@/pages/audit'

/**
 * SPEC §13.1's **Audit log**: who changed what, when, with a before-and-after diff.
 *
 * Three decisions.
 *
 * **There is no default time window**, unlike Monitoring. The two screens are read for
 * opposite reasons: monitoring answers "what is happening now", where an unbounded query
 * would scan every partition retained; this one answers "when did this change", and a
 * silent 24-hour default would hide the answer somebody came here for.
 *
 * **Export goes through the API client, not through a link.** The access token lives in
 * memory, so an `<a href>` carries no `Authorization` header — a download URL that worked
 * without one would be a hole in the only thing standing between an unauthenticated
 * visitor and an organization's entire configuration history.
 *
 * **Filtering is server-side.** A page filtered after it arrives can come back empty while
 * the next one is full, and a log that says "nothing happened" when it means "nothing in
 * the first fifty" is the one answer this screen must never give by accident.
 */
export function AuditPage() {
  const [filters, setFilters] = useState<AuditFilters>({})
  const [cursor, setCursor] = useState<string | null>(null)
  const [previous, setPrevious] = useState<(string | null)[]>([])

  const { data, isLoading } = useAuditEvents({ ...filters, cursor })
  const filtered = hasFilters(filters)

  const update = (patch: Partial<AuditFilters>) => {
    setFilters((current) => ({ ...current, ...patch }))
    setCursor(null)
    setPrevious([])
  }

  return (
    <div>
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Audit log</h1>
          <p className="mt-1 max-w-2xl text-sm text-slate-500">
            Every configuration change, who made it, and what it changed. Entries cannot be
            edited or removed. Secrets are recorded as having changed and never as what
            they changed to.
          </p>
        </div>
        <ExportButton filters={filters} />
      </header>

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Field label="Action">
          <input
            value={filters.action ?? ''}
            onChange={(event) => update({ action: event.target.value || undefined })}
            placeholder="gateway.update"
            className="w-52 rounded-md border border-slate-300 px-3 py-1.5 text-sm"
          />
        </Field>
        <Field label="Kind">
          <select
            value={filters.targetType ?? ''}
            onChange={(event) => update({ targetType: event.target.value || undefined })}
            className="w-44 rounded-md border border-slate-300 px-3 py-1.5 text-sm"
          >
            <option value="">Anything</option>
            {Object.entries(TARGET_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="From">
          <input
            type="date"
            value={filters.from?.slice(0, 10) ?? ''}
            onChange={(event) =>
              update({ from: event.target.value ? `${event.target.value}T00:00:00Z` : undefined })
            }
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm"
          />
        </Field>
        <Field label="To">
          <input
            type="date"
            value={filters.to?.slice(0, 10) ?? ''}
            onChange={(event) =>
              update({ to: event.target.value ? `${event.target.value}T23:59:59Z` : undefined })
            }
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm"
          />
        </Field>
        {filtered ? (
          <button
            type="button"
            onClick={() =>
              update({
                action: undefined,
                targetType: undefined,
                from: undefined,
                to: undefined,
              })
            }
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
          >
            Clear filters
          </button>
        ) : null}
      </div>

      <AuditTrail
        events={data?.items ?? []}
        loading={isLoading}
        emptyTitle="Nothing recorded"
        emptyDescription={emptyHint(filtered)}
      />

      <div className="mt-4 flex justify-end gap-2">
        <button
          type="button"
          disabled={previous.length === 0}
          onClick={() => {
            setCursor(previous.at(-1) ?? null)
            setPrevious((stack) => stack.slice(0, -1))
          }}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 disabled:opacity-40"
        >
          Previous
        </button>
        <button
          type="button"
          disabled={!data?.next_cursor}
          onClick={() => {
            setPrevious((stack) => [...stack, cursor])
            setCursor(data?.next_cursor ?? null)
          }}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 disabled:opacity-40"
        >
          Next
        </button>
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs font-medium text-slate-600">
      {label}
      {children}
    </label>
  )
}

function ExportButton({ filters }: { filters: AuditFilters }) {
  const client = useApiClient()
  const { notify } = useToast()
  const [busy, setBusy] = useState(false)

  const download = async () => {
    setBusy(true)
    try {
      const blob = await client.blob(exportPath(filters))
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'audit-events.csv'
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (error) {
      // Usually the export ceiling. Saying so beats a silent no-op on a button that
      // normally produces a file.
      notify(
        error instanceof Error ? error.message : 'The export could not be produced.',
        'error',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <button
      type="button"
      onClick={() => void download()}
      disabled={busy}
      className="shrink-0 rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
    >
      {busy ? 'Exporting…' : 'Export CSV'}
    </button>
  )
}
