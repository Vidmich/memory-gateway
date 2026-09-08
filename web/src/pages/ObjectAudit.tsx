import { useState } from 'react'

import { useObjectHistory } from '@/api/audit'
import { AuditTrail } from '@/pages/AuditTrail'

/**
 * One object's history, on its own detail screen.
 *
 * **This is where the audit log actually gets read.** The full-list screen is opened once
 * a quarter, usually by somebody preparing an answer for a customer; the question people
 * really have is "what changed on *this* gateway", and they have it while looking at the
 * gateway. So the same endpoint is asked for one target, in a panel beside the form rather
 * than behind a link to a filtered search.
 *
 * Collapsed by default, and it fetches nothing until it is opened. A detail screen already
 * makes several requests, and a history nobody expanded should not be one of them.
 */
export function ObjectAudit({
  targetType,
  targetId,
  noun,
}: {
  targetType: string
  targetId: string | undefined
  /** What the empty state calls this thing: "gateway", "model", "connector". */
  noun: string
}) {
  const [open, setOpen] = useState(false)
  const { data, isLoading } = useObjectHistory(targetType, open ? targetId : undefined)

  if (!targetId) return null

  return (
    <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-start justify-between gap-4 text-left"
      >
        <span>
          <span className="block text-sm font-semibold text-slate-900">History</span>
          <span className="mt-1 block text-sm text-slate-500">
            Every change made to this {noun}, and who made it.
          </span>
        </span>
        <span className="shrink-0 text-sm text-slate-500">{open ? 'Hide' : 'Show'}</span>
      </button>

      {open ? (
        <div className="mt-4">
          <AuditTrail
            events={data?.items ?? []}
            loading={isLoading}
            emptyTitle="No changes recorded"
            emptyDescription={`Nothing has changed on this ${noun} since the audit log began.`}
          />
        </div>
      ) : null}
    </section>
  )
}
