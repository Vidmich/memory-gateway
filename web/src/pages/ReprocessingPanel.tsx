import { useEffect, useState } from 'react'

import {
  useReprocess,
  useReprocessingRuns,
  useRetryFailed,
  useStalePreview,
} from '@/api/reprocessing'
import type {
  ConnectorResponse,
  ConnectorUpdateRequest,
  DocumentResponse,
  ReprocessingRunResponse,
} from '@/api/types'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'
import { FORMAT_KINDS } from '@/pages/connectors'
import {
  durationLabel,
  indexStatusTone,
  outcomeLine,
  progressLine,
  reprocessCostLine,
  rowReason,
  runTone,
  scopeLabel,
  staleHeadline,
  stalePreviewSentence,
  triggerLabel,
  tokensLine,
  type ReprocessScopeKind,
  SCOPES,
} from '@/pages/reprocessing'

/**
 * The connector header's reprocessing block (task 104).
 *
 * Three states, one component. Nothing stale: it renders nothing, because a permanent
 * "everything is current" line is the one people learn to skip. Something stale: the
 * sentence with the count and the formats, **Reprocess** with a scope selector and what
 * the scope will cost, and the history drawer. A run going: the progress bar with the
 * ETA and the failure count, fed by the connector query's own polling — the run is on
 * the connector row, so the header follows the same timer the document table does.
 */
export function ReprocessingHeader({
  connector,
  writes,
}: {
  connector: ConnectorResponse
  writes: boolean
}) {
  const headline = staleHeadline(connector)
  const running = connector.reprocessing ?? null
  const [history, setHistory] = useState(false)
  if (!headline && !running && !history) {
    return (
      <div data-testid="reprocessing-header" className="text-xs text-slate-500">
        <button
          type="button"
          onClick={() => setHistory(true)}
          className="font-medium text-slate-600 hover:underline"
        >
          Reprocessing history
        </button>
      </div>
    )
  }
  return (
    <div
      data-testid="reprocessing-header"
      className={`rounded-md border px-3 py-2 text-sm ${
        running
          ? 'border-sky-200 bg-sky-50 text-sky-900'
          : headline
            ? 'border-amber-200 bg-amber-50 text-amber-900'
            : 'border-slate-200 bg-white text-slate-700'
      }`}
    >
      {running ? <RunProgress run={running} /> : headline ? <p role="status">{headline}</p> : null}
      <div className="mt-2 flex flex-wrap items-center gap-3">
        {writes && !running && headline ? <ReprocessButton connector={connector} /> : null}
        <button
          type="button"
          onClick={() => setHistory((open) => !open)}
          className="text-xs font-medium underline-offset-2 hover:underline"
        >
          {history ? 'Hide history' : 'History'}
        </button>
      </div>
      {history ? <RunHistory connectorId={connector.id} writes={writes} /> : null}
    </div>
  )
}

export function RunProgress({ run }: { run: ReprocessingRunResponse }) {
  const percent = Math.round(run.progress.fraction * 100)
  return (
    <div data-testid="run-progress">
      <div className="flex items-center justify-between gap-3">
        <span className="font-medium">
          Reprocessing {scopeLabel(run)} — {triggerLabel(run.trigger)}
        </span>
        <span className="text-xs">{progressLine(run)}</span>
      </div>
      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-label="Reprocessing progress"
        className="mt-1.5 h-2 w-full overflow-hidden rounded bg-sky-100"
      >
        <div className="h-full bg-sky-500 transition-all" style={{ width: `${percent}%` }} />
      </div>
      <p className="mt-1 text-xs">
        Retrieval keeps working throughout; stale chunks are served and labelled.
      </p>
    </div>
  )
}

/**
 * **Reprocess** with its scope. The scope selector exists because the old button could only
 * re-ingest everything, and re-ingesting a thousand current documents to recut twelve is a
 * bill nobody asked for. The cost line says what the chosen scope will actually re-ingest.
 */
export function ReprocessButton({ connector }: { connector: ConnectorResponse }) {
  const reprocess = useReprocess(connector.id)
  const { notify } = useToast()
  const [scope, setScope] = useState<ReprocessScopeKind>('stale')
  const [formats, setFormats] = useState<string[]>(connector.reindex_formats ?? [])
  const cost = reprocessCostLine(connector, scope, formats)
  const disabled =
    reprocess.isPending ||
    (scope === 'stale' && (connector.stale_documents ?? 0) === 0) ||
    (scope === 'unrecorded' && (connector.unrecorded_documents ?? 0) === 0) ||
    (scope === 'formats' && formats.length === 0)

  return (
    <div data-testid="reprocess" className="flex flex-wrap items-end gap-2">
      <label className="text-xs">
        <span className="mb-0.5 block font-medium">Scope</span>
        <select
          aria-label="Reprocess scope"
          value={scope}
          onChange={(event) => setScope(event.target.value as ReprocessScopeKind)}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs"
        >
          {SCOPES.map((entry) => (
            <option key={entry.kind} value={entry.kind}>
              {entry.label}
            </option>
          ))}
        </select>
      </label>
      {scope === 'formats' ? (
        <fieldset className="flex flex-wrap gap-2 text-xs">
          <legend className="sr-only">Formats</legend>
          {FORMAT_KINDS.map((kind) => (
            <label key={kind.value} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={formats.includes(kind.value)}
                onChange={() =>
                  setFormats((current) =>
                    current.includes(kind.value)
                      ? current.filter((value) => value !== kind.value)
                      : [...current, kind.value],
                  )
                }
                className="rounded border-slate-300"
              />
              {kind.label}
            </label>
          ))}
        </fieldset>
      ) : null}
      <button
        type="button"
        disabled={disabled}
        onClick={() => {
          void reprocess
            .mutateAsync({ scope, formats: scope === 'formats' ? formats : [] })
            .then((run) =>
              notify(
                run.created
                  ? `Reprocessing ${run.total.toLocaleString()} document${run.total === 1 ? '' : 's'}.`
                  : 'A reprocess is already running.',
              ),
            )
        }}
        className="rounded-md border border-amber-300 bg-white px-3 py-1.5 text-sm font-medium text-amber-900 hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {reprocess.isPending ? 'Starting…' : 'Reprocess'}
      </button>
      <p className="basis-full text-xs">{cost}</p>
    </div>
  )
}

/** The last N runs: what changed, who, when, how long, outcome, and what it cost. */
export function RunHistory({ connectorId, writes }: { connectorId: string; writes: boolean }) {
  const runs = useReprocessingRuns(connectorId)
  const retry = useRetryFailed()
  const { notify } = useToast()
  const rows = runs.data?.items ?? []
  if (runs.isLoading) return <p className="mt-2 text-xs text-slate-500">Loading history…</p>
  if (rows.length === 0) return <p className="mt-2 text-xs text-slate-500">No runs yet.</p>
  return (
    <table data-testid="run-history" className="mt-3 w-full text-left text-xs">
      <caption className="sr-only">Reprocessing runs</caption>
      <thead className="border-b border-slate-200 uppercase tracking-wide text-slate-500">
        <tr>
          <th scope="col" className="py-1 pr-3">
            Trigger
          </th>
          <th scope="col" className="py-1 pr-3">
            Scope
          </th>
          <th scope="col" className="py-1 pr-3">
            Who
          </th>
          <th scope="col" className="py-1 pr-3">
            When
          </th>
          <th scope="col" className="py-1 pr-3">
            Took
          </th>
          <th scope="col" className="py-1 pr-3">
            Outcome
          </th>
          <th scope="col" className="py-1 pr-3">
            Tokens
          </th>
          <th scope="col" className="py-1" />
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {rows.map((run) => (
          <tr key={run.id} data-testid="run-row" className="align-top">
            <td className="py-1.5 pr-3">{triggerLabel(run.trigger)}</td>
            <td className="py-1.5 pr-3">{scopeLabel(run)}</td>
            <td className="py-1.5 pr-3">{run.requested_by_label ?? '—'}</td>
            <td className="py-1.5 pr-3 whitespace-nowrap">
              {new Date(run.started_at).toLocaleString()}
            </td>
            <td className="py-1.5 pr-3">{durationLabel(run)}</td>
            <td className="py-1.5 pr-3">
              <StatusBadge status={run.status} tone={runTone(run.status)} />
              <span className="ml-2">{outcomeLine(run)}</span>
              {run.resumed > 0 ? (
                <span className="ml-2 text-slate-500">
                  continued {run.resumed} time{run.resumed === 1 ? '' : 's'}
                </span>
              ) : null}
            </td>
            <td className="py-1.5 pr-3 whitespace-nowrap">{tokensLine(run)}</td>
            <td className="py-1.5 text-right">
              {writes && run.status === 'partial' && run.failed > 0 ? (
                <button
                  type="button"
                  disabled={retry.isPending}
                  onClick={() => {
                    void retry
                      .mutateAsync(run.id)
                      .then((started) =>
                        notify(
                          `Retrying ${started.total.toLocaleString()} failed document${started.total === 1 ? '' : 's'}.`,
                        ),
                      )
                  }}
                  className="font-medium text-amber-800 hover:underline"
                >
                  Retry failed
                </button>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/**
 * What a settings form says before Save: how many indexed documents the patch as typed
 * would mark stale, per format. Fed by the fingerprint diff on the server, so the chunking
 * form, the summarization form and any later setting get the same sentence for free.
 *
 * Asked only when the form is dirty, and re-asked when the patch changes — a preview is a
 * read, but not one worth making on every keystroke of a field that reaches no chunk.
 */
export function StalePreviewNotice({
  connectorId,
  patch,
  enabled,
}: {
  connectorId: string
  patch: ConnectorUpdateRequest
  enabled: boolean
}) {
  const preview = useStalePreview(connectorId)
  const key = JSON.stringify(patch)
  const { mutate, reset } = preview
  useEffect(() => {
    if (!enabled) {
      reset()
      return
    }
    mutate(JSON.parse(key) as ConnectorUpdateRequest)
  }, [enabled, key, mutate, reset])
  const sentence = enabled ? stalePreviewSentence(preview.data) : null
  if (!sentence) return null
  return (
    <p
      role="status"
      data-testid="stale-preview"
      className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
    >
      {sentence}
    </p>
  )
}

/** The document table's index-status cell: the word, and the reason on hover. */
export function IndexStatusCell({ document }: { document: DocumentResponse }) {
  const status = document.index_status ?? 'current'
  const reason = rowReason(document)
  if (status === 'current' && !reason && document.stale_reason !== 'unrecorded') {
    return <span className="text-xs text-slate-400">current</span>
  }
  const label = document.stale_reason === 'unrecorded' ? 'unrecorded' : status
  return (
    <span title={reason ?? undefined} data-testid="index-status">
      <StatusBadge
        status={label}
        tone={label === 'unrecorded' ? 'neutral' : indexStatusTone(status)}
      />
    </span>
  )
}
