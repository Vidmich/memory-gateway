import { useState } from 'react'

import { useMaintenance, useReindexRun, useRunMaintenance, useSweep } from '@/api/platform'
import type { MaintenanceRun, PartitionRunway, ReindexRun, SweepResponse } from '@/api/types'
import { EmptyState } from '@/components/EmptyState'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { useToast } from '@/components/Toast'
import { outcomeLine, scopeLabel, triggerLabel } from '@/pages/reprocessing'

/**
 * Platform → Maintenance (task 17).
 *
 * Four questions, in the order an operator asks them: is logging about to start failing,
 * did retention run and what did it take, is a reindex in flight, and is anything leaking.
 *
 * The sweep is two buttons rather than one with a checkbox. A checkbox next to a button is
 * a thing you can miss; "Find orphans" and "Delete the 12 orphans found" cannot be
 * confused, and the second only appears once the first has produced a set to talk about.
 */
export function PlatformMaintenancePage() {
  const { data, isLoading } = useMaintenance()
  const partitions = useRunMaintenance('partitions')
  const retention = useRunMaintenance('retention')
  const sweep = useSweep()
  const { notify } = useToast()
  const [report, setReport] = useState<SweepResponse | null>(null)

  if (isLoading || !data) return <FullPageSpinner label="Loading maintenance…" />

  const runs = data.last_runs ?? []
  const recent = data.recent_reindexes ?? []

  const runSweep = (apply: boolean) =>
    sweep.mutate(
      { apply },
      {
        onSuccess: (result) => {
          setReport(result)
          notify(
            apply
              ? `Deleted ${result.deleted} orphan${result.deleted === 1 ? '' : 's'}.`
              : `Found ${total(result)} orphan${total(result) === 1 ? '' : 's'}.`,
          )
        },
      },
    )

  return (
    <div className="max-w-4xl space-y-8">
      <header>
        <h1 className="text-xl font-semibold text-slate-900">Maintenance</h1>
        <p className="mt-1 text-sm text-slate-500">
          The jobs that keep the data lifecycle honest. All of them run nightly; these buttons run
          one now.
        </p>
      </header>

      <section className="rounded-lg border border-slate-200 bg-white p-6">
        <div className="mb-4 flex items-baseline justify-between gap-4">
          <h2 className="text-sm font-semibold text-slate-900">Partition runway</h2>
          <button
            type="button"
            onClick={() => partitions.mutate()}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            {partitions.isPending ? 'Creating…' : 'Create missing partitions'}
          </button>
        </div>
        <p className="mb-4 text-sm text-slate-600">
          Days of partitions that exist ahead of today. When this reaches zero, request logging
          stops writing — so it is an alert rather than a statistic.
        </p>
        <ul className="space-y-2">
          {(data.runway ?? []).map((entry) => (
            <RunwayRow key={entry.table} entry={entry} threshold={data.runway_threshold_days} />
          ))}
        </ul>
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-6">
        <div className="mb-4 flex items-baseline justify-between gap-4">
          <h2 className="text-sm font-semibold text-slate-900">Last runs</h2>
          <button
            type="button"
            onClick={() => retention.mutate()}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            {retention.isPending ? 'Pruning…' : 'Run retention now'}
          </button>
        </div>
        {runs.length === 0 ? (
          <EmptyState
            title="Nothing has run yet"
            description="The scheduled jobs run at 03:05 UTC. Until then, this is empty rather than broken."
          />
        ) : (
          <ul className="divide-y divide-slate-100">
            {runs.map((run) => (
              <RunRow key={run.id} run={run} />
            ))}
          </ul>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-6">
        <h2 className="mb-4 text-sm font-semibold text-slate-900">Reindex</h2>
        {data.reindex ? (
          <ReindexProgress run={data.reindex} />
        ) : recent.length > 0 ? (
          <ul className="divide-y divide-slate-100">
            {recent.map((run) => (
              <li key={run.id} className="py-3 text-sm">
                <span className="font-medium text-slate-900">{run.status}</span>{' '}
                <span className="text-slate-500">
                  — {run.from_model ?? 'unset'} → {run.to_model} ({run.to_dimension}d),{' '}
                  {new Date(run.started_at).toLocaleString()}
                </span>
                {run.error ? <p className="mt-1 text-red-600">{run.error}</p> : null}
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState
            title="No reindex has run"
            description="Changing the embedding model on Platform → Settings starts one."
          />
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-6">
        <div className="mb-4 flex items-baseline justify-between gap-4">
          <h2 className="text-sm font-semibold text-slate-900">Orphan sweep</h2>
          <button
            type="button"
            onClick={() => runSweep(false)}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            {sweep.isPending ? 'Scanning…' : 'Find orphans'}
          </button>
        </div>
        <p className="mb-4 text-sm text-slate-600">
          Vectors with no row and stored files with no document. Nothing is deleted until you have
          seen the list — an upload in flight looks exactly like an orphan, which is why anything
          written in the last hour is left alone.
        </p>
        {report ? <SweepReport report={report} onApply={() => runSweep(true)} /> : null}
      </section>
    </div>
  )
}

function total(report: SweepResponse): number {
  return (report.groups ?? []).reduce((sum, group) => sum + group.count, 0)
}

function RunwayRow({ entry, threshold }: { entry: PartitionRunway; threshold: number }) {
  return (
    <li className="flex items-center justify-between rounded-md border border-slate-100 px-3 py-2 text-sm">
      <span className="font-mono text-slate-700">{entry.table}</span>
      <span className={entry.low ? 'font-semibold text-red-600' : 'text-slate-600'}>
        {entry.days_ahead} day{entry.days_ahead === 1 ? '' : 's'} ahead
        {entry.low ? ` — below ${threshold}` : ''}
      </span>
    </li>
  )
}

function RunRow({ run }: { run: MaintenanceRun }) {
  const report = run.report ?? {}
  return (
    <li className="py-3 text-sm">
      <div className="flex items-baseline justify-between gap-4">
        <span className="font-medium text-slate-900">{run.job}</span>
        <span className="text-xs text-slate-500">
          {run.status} · {new Date(run.started_at).toLocaleString()}
        </span>
      </div>
      <p className="mt-1 text-slate-600">{summarize(run.job, report)}</p>
      {run.error ? <p className="mt-1 text-red-600">{run.error}</p> : null}
    </li>
  )
}

/** One sentence per job, because a JSON blob on a dashboard is not a report. */
function summarize(job: string, report: Record<string, unknown>): string {
  if (job === 'retention') {
    const rows = Number(report.rows_removed ?? 0)
    const bodies = Number(report.bodies_removed ?? 0)
    const bytes = Number(report.bytes_reclaimed ?? 0)
    const facts = Number(report.facts_expired ?? 0)
    return `${bodies.toLocaleString()} bodies and ${rows.toLocaleString()} rows removed, about ${format(bytes)} reclaimed, ${facts.toLocaleString()} expired facts purged.`
  }
  if (job === 'partitions') {
    const created = Object.values((report.created ?? {}) as Record<string, string[]>).flat()
    const dropped = Object.values((report.dropped ?? {}) as Record<string, string[]>).flat()
    return `${created.length} partition(s) created, ${dropped.length} dropped.`
  }
  const orphans = Number(report.orphans ?? 0)
  const deleted = Number(report.deleted ?? 0)
  return `${orphans.toLocaleString()} orphan(s) found, ${deleted.toLocaleString()} deleted.`
}

function format(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${Math.round(bytes / (1024 * 1024))} MB`
}

function ReindexProgress({ run }: { run: ReindexRun }) {
  const targets = run.targets ?? []
  // Task 104. The single-run read carries the per-connector runs the reindex spawned for
  // the connectors it is recutting; the maintenance summary does not, so it is fetched.
  const detail = useReindexRun(run.id)
  const spawned = detail.data?.reprocessing_runs ?? []
  const done = targets.reduce((sum, target) => sum + target.done_points, 0)
  const expected = targets.reduce((sum, target) => sum + target.total_points, 0)
  const percent = expected ? Math.round((done / expected) * 100) : 0
  return (
    <div>
      <p className="text-sm text-slate-600">
        {run.from_model ?? 'unset'} → <span className="font-mono">{run.to_model}</span> (
        {run.to_dimension}d). {done.toLocaleString()} of {expected.toLocaleString()} chunks.
        {run.eta_seconds !== null && run.eta_seconds !== undefined
          ? ` About ${Math.ceil(run.eta_seconds / 60)} minute(s) left.`
          : ''}
      </p>
      <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-slate-100">
        <div className="h-full bg-sky-500" style={{ width: `${percent}%` }} />
      </div>
      <ul className="mt-4 divide-y divide-slate-100">
        {targets.map((target) => (
          <li key={target.collection} className="flex justify-between py-2 text-sm">
            <span className="font-mono text-xs text-slate-600">{target.collection}</span>
            <span className="text-slate-500">
              {target.status} · {target.done_points.toLocaleString()} /{' '}
              {target.total_points.toLocaleString()}
            </span>
          </li>
        ))}
      </ul>
      {spawned.length > 0 ? (
        <div className="mt-4" data-testid="spawned-runs">
          <p className="text-xs font-medium text-slate-600">
            Connectors being recut from object storage
          </p>
          <ul className="mt-1 divide-y divide-slate-100">
            {spawned.map((row) => (
              <li key={row.id} className="flex justify-between py-2 text-sm">
                <span className="font-mono text-xs text-slate-600">{row.connector_id}</span>
                <span className="text-slate-500">
                  {triggerLabel(row.trigger)} · {scopeLabel(row)} · {row.status} ·{' '}
                  {outcomeLine(row)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  )
}

function SweepReport({ report, onApply }: { report: SweepResponse; onApply: () => void }) {
  const found = total(report)
  if (found === 0) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Nothing orphaned across {report.organizations} organization
        {report.organizations === 1 ? '' : 's'}.
      </p>
    )
  }
  return (
    <div>
      <ul className="divide-y divide-slate-100">
        {(report.groups ?? []).map((group) => (
          <li key={`${group.store}:${group.kind}`} className="py-2 text-sm">
            <span className="font-medium text-slate-900">
              {group.count} {group.kind.replace(/_/g, ' ')}
            </span>{' '}
            <span className="text-slate-500">in {group.store}</span>
            {(group.sample ?? []).length > 0 ? (
              <p className="mt-1 truncate font-mono text-xs text-slate-500">
                {(group.sample ?? []).join(', ')}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
      {report.applied ? (
        <p className="mt-4 text-sm text-slate-600">Deleted {report.deleted}.</p>
      ) : (
        <button
          type="button"
          onClick={onApply}
          className="mt-4 rounded-md bg-red-600 px-3 py-2 text-sm font-medium text-white hover:bg-red-500"
        >
          Delete the {found} orphan{found === 1 ? '' : 's'} listed above
        </button>
      )}
    </div>
  )
}
