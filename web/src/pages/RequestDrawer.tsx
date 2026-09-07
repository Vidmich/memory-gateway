import { useEffect, useMemo, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { useRequestDetail } from '@/api/monitoring'
import type {
  AttemptResponse,
  GatewayResponse,
  RequestDetailResponse,
  RequestLogResponse,
} from '@/api/types'
import { Waterfall } from '@/components/Charts'
import { CopyButton } from '@/components/CopyButton'
import { StatusBadge } from '@/components/StatusBadge'
import {
    asCurl,
    contentOf,
    countInjected,
    droppedReason,
    retrievedChunks,
    roleOf,
    toneFor,
    type RetrievedChunk,
} from '@/pages/requestDetail'

/**
 * SPEC §10.3: the answer to "why did the model say that?".
 *
 * Three things here are the reason the drawer exists at all.
 *
 * **The assembled prompt is shown against the original.** Everything the gateway added —
 * today the system contexts, from task 10 the retrieved documents and facts — is marked
 * as injected, because the difference between what the caller sent and what the provider
 * saw is the single most common thing somebody is trying to establish. The diff is
 * positional rather than textual: assembly *prepends* layers, so the caller's own
 * messages are the tail, and anything before them was added.
 *
 * **A missing panel says why it is missing.** "Not captured" and "the queue was full" and
 * "your redaction pattern timed out" are three different situations with three different
 * fixes, and an empty box is indistinguishable from a request that genuinely had no
 * response. `bodies_omitted` on the row is what makes them tellable apart.
 *
 * **Copy as curl reproduces the request against the gateway**, not against the provider.
 * The key is a placeholder — the real one cannot be read back, and a debugging affordance
 * that printed a live credential into somebody's shell history would be a bad trade for
 * the two seconds it saves.
 */
export function RequestDrawer({
  logId,
  gateways,
  onClose,
}: {
  logId: string
  gateways: readonly GatewayResponse[]
  onClose: () => void
}) {
  const { data, isLoading, error } = useRequestDetail(logId)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button
        type="button"
        aria-label="Close request details"
        onClick={onClose}
        className="flex-1 cursor-default bg-slate-900/20"
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Request details"
        className="flex w-full max-w-2xl flex-col overflow-y-auto bg-white shadow-xl"
      >
        {isLoading ? (
          <p className="p-6 text-sm text-slate-500">Loading the request…</p>
        ) : error ? (
          <p className="p-6 text-sm text-red-700">
            That request could not be loaded. It may have passed its retention window.
          </p>
        ) : data ? (
          <Detail detail={data} gateways={gateways} onClose={onClose} />
        ) : null}
      </aside>
    </div>
  )
}

function Detail({
  detail,
  gateways,
  onClose,
}: {
  detail: RequestDetailResponse
  gateways: readonly GatewayResponse[]
  onClose: () => void
}) {
  const log = detail.log
  const gateway = gateways.find((candidate) => candidate.id === log.gateway_id)
  const injected = useMemo(
    () =>
      countInjected(
        detail.transcript?.assembled_prompt ?? null,
        detail.transcript?.request_body ?? null,
      ),
    [detail.transcript],
  )

  return (
    <>
      <header className="sticky top-0 flex items-start justify-between gap-4 border-b border-slate-200 bg-white px-6 py-4">
        <div>
          <div className="flex items-center gap-2">
            <StatusBadge status={String(log.status_code)} tone={toneFor(log.status_code)} />
            <h2 className="text-sm font-semibold text-slate-900">
              {log.model_name ?? 'Unknown model'}
            </h2>
          </div>
          <p className="mt-1 font-mono text-xs text-slate-500">{log.id}</p>
        </div>
        <div className="flex items-center gap-2">
          <CopyButton
            value={asCurl(log, detail, gateway?.endpoint_url)}
            label="Copy as curl"
          />
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            Close
          </button>
        </div>
      </header>

      <div className="space-y-6 px-6 py-5">
        <Facts log={log} gatewaySlug={gateway?.slug} />

        {log.error_code ? (
          <Panel title="Error">
            <p className="text-sm text-red-700">
              <span className="font-mono text-xs">{log.error_code}</span>
              {log.error_message ? ` — ${log.error_message}` : null}
            </p>
          </Panel>
        ) : null}

        <Panel title="Timing" subtitle="Where the time went.">
          <Waterfall
            total={log.latency_total_ms}
            phases={[
              { label: 'Retrieval', value: log.latency_retrieval_ms, color: '#7c3aed' },
              { label: 'Upstream', value: log.latency_upstream_ms, color: '#2563eb' },
            ]}
          />
          {log.latency_ttft_ms !== null ? (
            <p className="mt-3 text-xs text-slate-500">
              First token after {log.latency_ttft_ms} ms.
            </p>
          ) : null}
          {log.latency_retrieval_ms === null ? (
            <p className="mt-1 text-xs text-slate-400">
              No retrieval ran: this gateway has no connectors attached, or the caller sent
              <code className="mx-1">X-Gateway-Memory: off</code>.
            </p>
          ) : null}
        </Panel>

        <Panel
          title="Client request"
          subtitle="Exactly what the caller sent, before anything was prepended."
        >
          <Messages
            messages={detail.transcript?.request_body ?? null}
            missing={<NotCaptured reason={log.bodies_omitted} field="request bodies" />}
          />
        </Panel>

        <Panel
          title="Assembled prompt"
          subtitle={
            injected > 0
              ? `What the provider received. The first ${injected} message${injected === 1 ? '' : 's'} ${injected === 1 ? 'was' : 'were'} added by the gateway.`
              : 'What the provider received.'
          }
        >
          <Messages
            messages={detail.transcript?.assembled_prompt ?? null}
            injected={injected}
            missing={<NotCaptured reason={log.bodies_omitted} field="assembled prompts" />}
          />
        </Panel>

        <Panel title="Response">
          {detail.transcript?.response_body ? (
            <>
              <pre className="whitespace-pre-wrap break-words rounded-md bg-slate-50 p-3 text-xs text-slate-800">
                {detail.transcript.response_body}
              </pre>
              {log.response_truncated ? (
                <p className="mt-2 text-xs text-amber-700">
                  Stored up to the capture limit; the response continued past this point.
                </p>
              ) : null}
            </>
          ) : (
            <NotCaptured reason={log.bodies_omitted} field="response bodies" />
          )}
        </Panel>

        <Panel
          title="Routing"
          subtitle="Which upstream served this request, and what was tried first."
        >
          <Attempts attempts={detail.failover_attempts} served={log.model_name} />
          {log.failed_after_stream_start ? (
            <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
              The upstream failed after the first chunk had reached the client. The status
              line was already sent, so this could not be failed over and the stream ended
              with an error event.
            </p>
          ) : null}
        </Panel>

        <Panel
          title="Memory"
          subtitle="What retrieval found for this request, and what became of it."
        >
          <Retrieved log={log} entries={detail.retrieved_chunk_ids} />
        </Panel>
      </div>
    </>
  )
}

/**
 * The retrieved chunks, with what happened to each.
 *
 * Rendered from the *record* on the log row rather than by looking the chunks up in the
 * vector store, and that is the whole reason the row stores names and scores rather than
 * ids. A request from last week may cite a chunk that has since been reindexed out of
 * existence; what it retrieved at the time is a fact, and it is the fact somebody is
 * asking about.
 */
function Retrieved({
  log,
  entries,
}: {
  log: RequestLogResponse
  entries: readonly unknown[]
}) {
  const chunks = retrievedChunks(entries)

  if (log.latency_retrieval_ms === null) {
    return (
      <p className="text-sm text-slate-500">
        Retrieval did not run for this request, so nothing was injected.
      </p>
    )
  }

  if (chunks.length === 0) {
    return (
      <p className="text-sm text-slate-700">
        Retrieval ran in {log.latency_retrieval_ms} ms and found nothing above this
        gateway&apos;s score floor. The model answered from its own knowledge and the
        system context.
      </p>
    )
  }

  const injected = chunks.filter((chunk) => chunk.injected).length
  return (
    <>
      <p className="text-sm text-slate-700">
        {injected} of {chunks.length} chunk{chunks.length === 1 ? '' : 's'} injected
        {log.memory_tokens ? `, ${log.memory_tokens} tokens` : ''} · {log.latency_retrieval_ms} ms
      </p>
      <ol className="mt-2 space-y-1">
        {chunks.map((chunk, index) => (
          <ChunkRow key={`${chunk.id}-${index}`} chunk={chunk} />
        ))}
      </ol>
    </>
  )
}

function ChunkRow({ chunk }: { chunk: RetrievedChunk }) {
  const reason = droppedReason(chunk.dropped)
  return (
    <li
      className={`rounded-md border p-2 text-xs ${
        chunk.injected
          ? 'border-violet-200 bg-violet-50 text-violet-900'
          : 'border-slate-200 bg-slate-50 text-slate-500'
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono tabular-nums">
          {chunk.score === null ? '—' : chunk.score.toFixed(2)}
        </span>
        <span className="min-w-0 flex-1 truncate font-medium">
          {chunk.documentId ? (
            <Link to={`/connectors?document=${chunk.documentId}`} className="underline">
              {chunk.sourceName}
            </Link>
          ) : (
            chunk.sourceName
          )}
          {chunk.pageOrSection ? (
            <span className="font-normal opacity-70"> · {chunk.pageOrSection}</span>
          ) : null}
        </span>
        {chunk.injected ? null : (
          <span className="rounded bg-slate-200 px-1.5 py-0.5 font-medium text-slate-700">
            dropped{reason ? ` — ${reason}` : ''}
          </span>
        )}
      </div>
    </li>
  )
}

/**
 * The chain, when there was one.
 *
 * An empty list is not a gap: it means one target answered, which the header and the
 * facts above already say. Drawing a one-row timeline for every request would make every
 * gateway look like a failover chain, which is the opposite of the point — the timeline
 * exists so that the rare request that *did* move between upstreams is unmistakable.
 */
function Attempts({
  attempts,
  served,
}: {
  attempts: readonly AttemptResponse[]
  served: string | null
}) {
  if (attempts.length === 0) {
    return (
      <p className="text-sm text-slate-700">
        {served ?? 'Unknown model'} — served on the first attempt.
      </p>
    )
  }

  return (
    <ol className="space-y-2">
      {attempts.map((attempt, index) => {
        const ok = attempt.status < 400
        return (
          <li
            key={`${attempt.target_id}-${index}`}
            className={`flex items-center gap-3 rounded-md border p-2 text-xs ${
              ok ? 'border-emerald-200 bg-emerald-50' : 'border-red-200 bg-red-50'
            }`}
          >
            <span className="w-4 shrink-0 text-center font-medium text-slate-400">
              {index + 1}
            </span>
            <span className="min-w-0 flex-1 truncate font-medium text-slate-800">
              {attempt.model_name}
            </span>
            <span className={`tabular-nums ${ok ? 'text-emerald-700' : 'text-red-700'}`}>
              {attempt.status}
            </span>
            {attempt.error_code ? (
              <code className="text-red-700">{attempt.error_code}</code>
            ) : null}
            <span className="w-16 shrink-0 text-right tabular-nums text-slate-500">
              {attempt.latency_ms} ms
            </span>
          </li>
        )
      })}
      {attempts.every((attempt) => attempt.status >= 400) ? (
        <li className="text-xs text-slate-500">
          Every target failed, so the caller received the last error. A{' '}
          <span className="font-medium">retryable</span> failure on the last attempt means
          the chain was too short, not that the error was final.
        </li>
      ) : null}
    </ol>
  )
}

function Facts({
  log,
  gatewaySlug,
}: {
  log: RequestLogResponse
  gatewaySlug: string | undefined
}) {
  const entries: [string, ReactNode][] = [
    ['When', new Date(log.created_at).toLocaleString()],
    ['Gateway', gatewaySlug ?? '(deleted)'],
    ['Model', log.model_name ?? '(deleted)'],
    ['Mode', log.streamed ? 'Streamed' : 'Single response'],
    ['Total', `${log.latency_total_ms} ms`],
    [
      'Tokens',
      log.prompt_tokens === null && log.completion_tokens === null
        ? 'Not reported'
        : `${log.prompt_tokens ?? 0} in / ${log.completion_tokens ?? 0} out`,
    ],
    ['Key', log.api_key_id ? <code className="text-xs">{log.api_key_id}</code> : '—'],
    [
      'Request id',
      log.request_id ? <code className="text-xs">{log.request_id}</code> : '—',
    ],
  ]

  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
      {entries.map(([label, value]) => (
        <div key={label}>
          <dt className="text-xs text-slate-500">{label}</dt>
          <dd className="truncate text-slate-800">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function Panel({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: string
  children: ReactNode
}) {
  return (
    <section>
      <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
      {subtitle ? <p className="mb-2 mt-0.5 text-xs text-slate-500">{subtitle}</p> : null}
      <div className={subtitle ? '' : 'mt-2'}>{children}</div>
    </section>
  )
}

function Messages({
  messages,
  injected = 0,
  missing,
}: {
  messages: Record<string, unknown>[] | null
  injected?: number
  missing: ReactNode
}) {
  if (!messages) return <>{missing}</>
  if (messages.length === 0) {
    return <p className="text-sm text-slate-500">No messages.</p>
  }

  return (
    <ol className="space-y-2">
      {messages.map((message, index) => {
        const added = index < injected
        return (
          <li
            key={index}
            className={`rounded-md border p-3 ${
              added ? 'border-violet-200 bg-violet-50' : 'border-slate-200 bg-white'
            }`}
          >
            <div className="mb-1 flex items-center gap-2 text-xs">
              <span className="font-medium uppercase tracking-wide text-slate-500">
                {roleOf(message)}
              </span>
              {added ? (
                <span className="rounded bg-violet-200 px-1.5 py-0.5 text-[10px] font-medium text-violet-800">
                  added by the gateway
                </span>
              ) : null}
            </div>
            <pre className="whitespace-pre-wrap break-words text-xs text-slate-800">
              {contentOf(message)}
            </pre>
          </li>
        )
      })}
    </ol>
  )
}

function NotCaptured({ reason, field }: { reason: string | null; field: string }) {
  if (reason === 'queue_pressure') {
    return (
      <p className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
        Not stored — the log queue was saturated when this request finished, so its bodies
        were dropped to keep the metadata. The <code>logs_dropped_total</code> metric
        counts these.
      </p>
    )
  }
  if (reason === 'redaction_budget') {
    return (
      <p className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
        Not stored — redaction did not finish within its budget, so the bodies were dropped
        rather than saved half-redacted. Check this gateway’s redaction patterns.
      </p>
    )
  }
  return (
    <p className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-500">
      Not captured. Logging {field} is switched off for this gateway.
    </p>
  )
}
