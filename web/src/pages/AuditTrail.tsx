import { useState } from 'react'

import type { AuditChange, AuditEvent } from '@/api/types'
import { EmptyState } from '@/components/EmptyState'
import {
  actorLabel,
  actorTone,
  afterOf,
  beforeOf,
  changeSummary,
  describeEvent,
  isRedacted,
  targetLabel,
  type ActorTone,
} from '@/pages/audit'

/**
 * The list of events, shared by the Audit log screen and the per-object panels.
 *
 * A list of expandable rows rather than a `DataTable`. That component is built for one
 * row per record with a fixed set of columns, and the interesting half of an audit event
 * is a *variable-length* diff — expanding a row has to change its height, which a column
 * layout cannot do without the columns dancing.
 *
 * Rows are collapsed by default and their summary names the fields that changed rather
 * than counting them: "system_context, memory_config.doc_top_k" answers "is this the
 * change I am looking for" without a click, and "2 fields changed" does not.
 */
export function AuditTrail({
  events,
  loading,
  emptyTitle,
  emptyDescription,
}: {
  events: readonly AuditEvent[]
  loading?: boolean
  emptyTitle: string
  emptyDescription: string
}) {
  if (loading) {
    return <p className="py-8 text-center text-sm text-slate-400">Loading…</p>
  }
  if (events.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} />
  }

  return (
    <ul className="divide-y divide-slate-200 overflow-hidden rounded-lg border border-slate-200 bg-white">
      {events.map((event) => (
        <AuditRow key={event.id} event={event} />
      ))}
    </ul>
  )
}

const ACTOR_TONES: Record<ActorTone, string> = {
  person: 'bg-slate-100 text-slate-600 ring-slate-500/20',
  // A colour used nowhere else in this list, for the same reason the support banner is
  // amber: a customer scanning their own log should see the vendor's visits without
  // reading a single row.
  support: 'bg-amber-50 text-amber-800 ring-amber-600/20',
  system: 'bg-sky-50 text-sky-700 ring-sky-600/20',
}

const ACTOR_WORDS: Record<ActorTone, string> = {
  person: '',
  support: 'Support access',
  system: 'Automated',
}

export function AuditRow({ event }: { event: AuditEvent }) {
  const [open, setOpen] = useState(false)
  const tone = actorTone(event)
  const expandable = event.changes.length > 0 || event.summary !== null

  return (
    <li className={tone === 'support' ? 'bg-amber-50/40' : undefined}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        disabled={!expandable}
        className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-slate-50 disabled:hover:bg-transparent"
      >
        <span className="w-40 shrink-0 text-xs text-slate-500">
          {new Date(event.created_at).toLocaleString()}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm text-slate-900">{describeEvent(event)}</span>
          <span className="mt-0.5 block truncate font-mono text-xs text-slate-500">
            {changeSummary(event)}
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-2">
          {ACTOR_WORDS[tone] ? (
            <span
              className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${ACTOR_TONES[tone]}`}
            >
              {ACTOR_WORDS[tone]}
            </span>
          ) : null}
          <span className="text-xs text-slate-400">{targetLabel(event.target_type)}</span>
        </span>
      </button>

      {open ? <AuditDetail event={event} /> : null}
    </li>
  )
}

function AuditDetail({ event }: { event: AuditEvent }) {
  return (
    <div className="border-t border-slate-100 bg-slate-50/60 px-4 py-3">
      {event.summary ? (
        <pre className="mb-3 overflow-x-auto rounded border border-slate-200 bg-white p-2 text-xs text-slate-700">
          {JSON.stringify(event.summary, null, 2)}
        </pre>
      ) : null}

      {event.changes.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-slate-500">
                <th className="py-1 pr-4 font-medium">Field</th>
                <th className="py-1 pr-4 font-medium">Before</th>
                <th className="py-1 font-medium">After</th>
              </tr>
            </thead>
            <tbody className="align-top">
              {event.changes.map((change) => (
                <ChangeRow key={change.path} change={change} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {event.omitted > 0 ? (
        <p className="mt-2 text-xs text-slate-500">
          {event.omitted} more fields changed and were not recorded individually.
        </p>
      ) : null}

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-500">
        <div>
          <dt className="inline font-medium">Actor: </dt>
          <dd className="inline">{actorLabel(event)}</dd>
        </div>
        {event.ip ? (
          <div>
            <dt className="inline font-medium">From: </dt>
            <dd className="inline font-mono">{event.ip}</dd>
          </div>
        ) : null}
        {event.request_id ? (
          <div>
            <dt className="inline font-medium">Request: </dt>
            <dd className="inline font-mono">{event.request_id}</dd>
          </div>
        ) : null}
      </dl>
    </div>
  )
}

function ChangeRow({ change }: { change: AuditChange }) {
  return (
    <tr className="border-t border-slate-100">
      <td className="py-1 pr-4 font-mono text-slate-700">{change.path}</td>
      <td className="py-1 pr-4 font-mono text-slate-500">{beforeOf(change)}</td>
      <td className="py-1 font-mono text-slate-900">
        {afterOf(change)}
        {change.truncated ? (
          <span className="ml-1 text-slate-400" title="Shown cut; the stored value is longer">
            …
          </span>
        ) : null}
        {isRedacted(change) ? (
          <span className="ml-2 text-slate-400">(value never recorded)</span>
        ) : null}
      </td>
    </tr>
  )
}
