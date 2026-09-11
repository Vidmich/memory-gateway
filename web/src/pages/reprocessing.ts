/**
 * The sentences and the small arithmetic behind the reprocessing screens (task 104),
 * kept out of the components so a table of inputs can pin them.
 *
 * Staleness is a stored fact the server counts; what this module decides is how it is
 * *said* — "1,184 of 1,200 documents indexed under a previous configuration", a progress
 * line with an ETA, the reason on a row, what a save will mark before it is saved — so
 * every screen that repeats it repeats the same words.
 */

import type { Tone } from '@/components/status'
import type {
  ConnectorResponse,
  DocumentResponse,
  ReprocessingRunResponse,
  StaleAlertResponse,
  StaleConnectorResponse,
  StalePreviewResponse,
} from '@/api/types'

/** The document table's second axis, in the order the filter chips show them. */
export const INDEX_STATUSES = ['current', 'stale', 'reprocessing'] as const

export type ReprocessScopeKind = 'stale' | 'formats' | 'all' | 'unrecorded'

export const SCOPES: readonly { kind: ReprocessScopeKind; label: string; hint: string }[] = [
  {
    kind: 'stale',
    label: 'Stale documents only',
    hint: 'The documents cut under a previous configuration. The default, and the cheap one.',
  },
  {
    kind: 'formats',
    label: 'These formats',
    hint: 'Every document of the formats the last change touched, current or not.',
  },
  {
    kind: 'all',
    label: 'Everything',
    hint: 'Every document, current ones included. Costs a whole corpus of embedding calls.',
  },
  {
    kind: 'unrecorded',
    label: 'Unrecorded documents',
    hint: 'Documents indexed before the fingerprint existed, so they can be compared from now on.',
  },
]

const REASONS: Record<string, string> = {
  chunking: 'chunking changed',
  embedding_model: 'embedding model changed',
  tokenizer: 'tokenizer changed',
  summarization: 'summarization changed',
  extractor: 'extractor upgraded',
  unrecorded: 'unrecorded',
}

const TRIGGERS: Record<string, string> = {
  chunking: 'chunking change',
  embedding_model: 'embedding model change',
  tokenizer: 'tokenizer change',
  summarization: 'summarization change',
  extractor: 'extractor upgrade',
  manual: 'manual',
}

export function reasonLabel(code: string | null | undefined): string {
  if (!code) return ''
  return REASONS[code] ?? code.replace(/_/g, ' ')
}

export function triggerLabel(trigger: string): string {
  return TRIGGERS[trigger] ?? trigger.replace(/_/g, ' ')
}

export function scopeLabel(run: Pick<ReprocessingRunResponse, 'scope' | 'formats'>): string {
  if (run.scope === 'formats') return run.formats.length ? run.formats.join(', ') : 'formats'
  if (run.scope === 'failed') return 'retry of failures'
  return SCOPES.find((entry) => entry.kind === run.scope)?.label.toLowerCase() ?? run.scope
}

export function indexStatusTone(status: string): Tone {
  if (status === 'stale') return 'warn'
  if (status === 'reprocessing') return 'info'
  return 'ok'
}

export function runTone(status: string): Tone {
  if (status === 'succeeded') return 'ok'
  if (status === 'partial') return 'warn'
  if (status === 'failed') return 'error'
  return 'info'
}

function plural(count: number, noun: string): string {
  return `${count.toLocaleString()} ${noun}${count === 1 ? '' : 's'}`
}

/**
 * The connector header's sentence. Null when nothing is stale, being reprocessed or
 * unrecorded — the header then says nothing, because a permanent "everything is fine"
 * line is the one people learn to skip.
 */
export function staleHeadline(connector: ConnectorResponse): string | null {
  const stale = connector.stale_documents ?? 0
  const busy = connector.reprocessing_documents ?? 0
  const unrecorded = connector.unrecorded_documents ?? 0
  const total = connector.document_count
  const parts: string[] = []
  if (stale > 0) {
    const formats = (connector.reindex_formats ?? []).join(', ')
    parts.push(
      `${stale.toLocaleString()} of ${plural(total, 'document')} indexed under a previous configuration${
        formats ? ` (${formats})` : ''
      }`,
    )
  }
  if (busy > 0) parts.push(`${plural(busy, 'document')} being reprocessed`)
  if (unrecorded > 0) {
    parts.push(`${plural(unrecorded, 'document')} indexed before the fingerprint was recorded`)
  }
  if (parts.length === 0) return null
  return `${parts.join(' · ')}.`
}

/** "~6 min remaining", "~2 h remaining", or null before a rate exists. */
export function etaLabel(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined) return null
  if (seconds < 60) return 'under a minute remaining'
  const minutes = Math.round(seconds / 60)
  if (minutes < 90) return `~${minutes} min remaining`
  const hours = Math.round(minutes / 6) / 10
  return `~${hours} h remaining`
}

/** "312 / 1,184, ~6 min remaining, 2 failed" — the progress bar's line. */
export function progressLine(run: ReprocessingRunResponse): string {
  const settled = run.done + run.failed + run.skipped
  const parts = [`${settled.toLocaleString()} / ${run.total.toLocaleString()}`]
  const eta = etaLabel(run.progress.eta_seconds)
  if (run.status === 'running' && eta) parts.push(eta)
  if (run.failed > 0) parts.push(`${run.failed.toLocaleString()} failed`)
  if (run.skipped > 0) parts.push(`${run.skipped.toLocaleString()} skipped (source gone)`)
  return parts.join(', ')
}

/** How the history row summarises an outcome. */
export function outcomeLine(run: ReprocessingRunResponse): string {
  if (run.status === 'running') return progressLine(run)
  const parts = [`${plural(run.done, 'document')} reprocessed`]
  if (run.failed > 0) parts.push(`${run.failed.toLocaleString()} failed`)
  if (run.skipped > 0) parts.push(`${run.skipped.toLocaleString()} skipped`)
  return parts.join(', ')
}

export function durationLabel(run: ReprocessingRunResponse, now = Date.now()): string {
  const started = new Date(run.started_at).getTime()
  const finished = run.finished_at ? new Date(run.finished_at).getTime() : now
  const seconds = Math.max(0, Math.round((finished - started) / 1000))
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.round(seconds / 60)
  if (minutes < 90) return `${minutes} min`
  return `${Math.round(minutes / 6) / 10} h`
}

/** "1.2M estimated · 310k spent" — the calibration column. */
export function tokensLine(run: ReprocessingRunResponse): string {
  const spent =
    run.status === 'running' ? `${compact(run.spent_tokens)} so far` : compact(run.spent_tokens)
  return `${compact(run.estimated_tokens)} estimated · ${spent} spent`
}

export function compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`
  return value.toLocaleString()
}

/** The run cost sentence under the Reprocess button, from the connector's counts. */
export function reprocessCostLine(
  connector: ConnectorResponse,
  scope: ReprocessScopeKind,
  formats: readonly string[] = [],
): string {
  const stale = connector.stale_documents ?? 0
  const unrecorded = connector.unrecorded_documents ?? 0
  if (scope === 'stale') {
    return stale === 0
      ? 'Nothing is stale.'
      : `Re-ingests the ${plural(stale, 'stale document')}: extraction and one embedding pass each.`
  }
  if (scope === 'unrecorded') {
    return unrecorded === 0
      ? 'Every indexed document has a fingerprint.'
      : `Re-ingests the ${plural(unrecorded, 'unrecorded document')} so they can be compared from now on.`
  }
  if (scope === 'formats') {
    return formats.length === 0
      ? 'Pick at least one format.'
      : `Re-ingests every ${formats.join(', ')} document, current ones included.`
  }
  return `Re-ingests all ${plural(connector.document_count, 'document')}, current ones included — a whole corpus of embedding calls.`
}

/** What every settings form says before Save, fed by the fingerprint diff. */
export function stalePreviewSentence(
  preview: StalePreviewResponse | null | undefined,
): string | null {
  if (!preview || typeof preview !== 'object' || !preview.total) return null
  const formats = Object.entries(preview.formats ?? {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([kind, count]) => `${count.toLocaleString()} ${kind}`)
    .join(', ')
  return `Saving marks ${plural(preview.total, 'indexed document')} stale (${formats}). They keep their old chunks until you reprocess them.`
}

/** The document table's hover text: the reason in the server's words. */
export function rowReason(document: DocumentResponse): string | null {
  if (document.stale_detail) return document.stale_detail
  if (document.index_status === 'reprocessing') return 'Being re-ingested by a reprocessing run.'
  return null
}

/** The gateway's notice about the connectors it reads. */
export function staleConnectorNotice(rows: readonly StaleConnectorResponse[]): string | null {
  if (rows.length === 0) return null
  const names = rows.map((row) => {
    const bits: string[] = []
    if (row.stale > 0) bits.push(`${row.stale.toLocaleString()} stale`)
    if (row.reprocessing > 0) bits.push(`${row.reprocessing.toLocaleString()} reprocessing`)
    return `${row.name} (${bits.join(', ')})`
  })
  const verb = rows.length === 1 ? 'is' : 'are'
  return `Answers may be drawn from two chunkings until ${names.join(' and ')} ${verb} reprocessed.`
}

/** What the dashboard's degraded entry says. */
export function staleAlertLine(alert: StaleAlertResponse): string {
  const age =
    alert.age_hours >= 48
      ? `${Math.round(alert.age_hours / 24)} days`
      : `${Math.round(alert.age_hours)} h`
  return `${plural(alert.stale_documents, 'document')} stale for ${age}`
}

/** The connectors this gateway's form reads that are not wholly current. */
export function staleAmong(
  connectors: readonly ConnectorResponse[],
  connectorIds: readonly string[],
): StaleConnectorResponse[] {
  return connectors
    .filter(
      (row) =>
        connectorIds.includes(row.id) &&
        ((row.stale_documents ?? 0) > 0 || (row.reprocessing_documents ?? 0) > 0),
    )
    .map((row) => ({
      id: row.id,
      name: row.name,
      stale: row.stale_documents ?? 0,
      reprocessing: row.reprocessing_documents ?? 0,
    }))
}
