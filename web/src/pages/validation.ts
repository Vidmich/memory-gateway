/**
 * The arithmetic and the sentences behind the Validation screens (task 103), kept out of
 * the components so a table of inputs can pin them.
 *
 * Two families. The **audit** helpers turn a stored report into what the screen says
 * about it — its age, its worst finding, where a finding's fix lives — and rebuild the
 * server's histogram buckets client-side for Compare, which has the chunks in hand and no
 * report. The **evaluation** helpers format the headline numbers the same way everywhere
 * (a recall of `null` is "—", never "0.00"), and write the one sentence a run's headline
 * has to carry when its labels were not all a person's.
 */

import type { Tone } from '@/components/status'
import type {
  AuditResponse,
  AuditStatusResponse,
  EvaluationItemResponse,
  EvaluationRunSummaryResponse,
  EvaluationSetResponse,
  FindingResponse,
  HistogramResponse,
} from '@/api/types'

export type AuditKind = 'chunking' | 'embedding'

export const AUDIT_KINDS: readonly { kind: AuditKind; label: string }[] = [
  { kind: 'chunking', label: 'Chunking' },
  { kind: 'embedding', label: 'Embeddings' },
]

//  The server's own bucketing: twelve buckets across [0, 1.5 × chunk_size], the last
//  one open. Mirrored here rather than requested, so Compare's histogram and the audit's
//  draw on the same axis for the same setting.
const HISTOGRAM_BUCKETS = 12
const OVER_CEILING_FACTOR = 1.5

// -- audits -----------------------------------------------------------------

export function severityTone(severity: string | null | undefined): Tone {
  if (severity === 'red') return 'error'
  if (severity === 'amber') return 'warn'
  if (severity === 'green') return 'ok'
  return 'neutral'
}

export function severityLabel(severity: string | null | undefined): string {
  if (severity === 'red') return 'needs attention'
  if (severity === 'amber') return 'worth a look'
  if (severity === 'green') return 'healthy'
  return 'not audited'
}

/** "2 hours ago", from the audit's finish (or start, while it runs). */
export function auditAge(audit: AuditResponse | null | undefined, now = Date.now()): string {
  if (!audit) return 'Never run.'
  const when = new Date(audit.finished_at ?? audit.created_at).getTime()
  const minutes = Math.max(0, Math.round((now - when) / 60_000))
  if (audit.status === 'running') return 'Running now.'
  const age =
    minutes < 1
      ? 'just now'
      : minutes < 60
        ? `${minutes} min ago`
        : minutes < 60 * 48
          ? `${Math.round(minutes / 60)} h ago`
          : `${Math.round(minutes / (60 * 24))} days ago`
  return audit.status === 'failed' ? `Failed ${age}.` : `Run ${age}.`
}

/** The sentence beside the Run button: what the audit found, in one line. */
export function auditHeadline(audit: AuditResponse | null | undefined): string {
  if (!audit || !audit.report) return ''
  const red = audit.report.findings.filter((f) => f.severity === 'red').length
  const amber = audit.report.findings.filter((f) => f.severity === 'amber').length
  const points = `${audit.report.points.toLocaleString()} points`
  if (red === 0 && amber === 0) return `${points}, nothing to report.`
  const parts = []
  if (red) parts.push(`${red} red`)
  if (amber) parts.push(`${amber} amber`)
  return `${points}, ${parts.join(' and ')} finding${red + amber === 1 ? '' : 's'}.`
}

/**
 * Where a finding's fix lives, as a link. A badly cut file opens Compare with the
 * document preselected; a mixed index opens the connector's reindex; a model problem
 * opens the platform's; the rest open the document itself.
 */
export function findingLink(
  connectorId: string,
  finding: FindingResponse,
  documentId?: string,
): { to: string; label: string } | null {
  if (finding.action === 'compare' && documentId) {
    return {
      to: `/connectors/${connectorId}?compare=${documentId}`,
      label: 'Open in Compare',
    }
  }
  if (finding.action === 'reindex') {
    return {
      to: `/connectors/${connectorId}#chunking`,
      label: 'Reindex this connector',
    }
  }
  if (finding.action === 'platform') {
    return { to: '/platform/settings', label: 'Platform embedding settings' }
  }
  if (documentId) {
    return {
      to: `/connectors/${connectorId}?document=${documentId}`,
      label: 'Open document',
    }
  }
  return null
}

export function driftCostLine(status: AuditStatusResponse | undefined): string {
  if (!status) return ''
  const { sample, tokens, points } = status.drift_estimate
  if (points === 0) return 'Nothing is indexed to re-embed.'
  return `Re-embeds ${sample.toLocaleString()} of ${points.toLocaleString()} chunks now — about ${tokens.toLocaleString()} tokens at the embedding provider.`
}

/**
 * Token counts into the server's buckets. Used by Compare, which has chunks rather than
 * a report; the audit renders the report's own buckets, computed the same way.
 */
export function chunkHistogram(
  tokenCounts: readonly number[],
  chunkSize: number,
): HistogramResponse {
  const width = Math.max(1, Math.ceil((chunkSize * OVER_CEILING_FACTOR) / HISTOGRAM_BUCKETS))
  const counts = new Array<number>(HISTOGRAM_BUCKETS).fill(0)
  for (const tokens of tokenCounts) {
    const index = Math.min(HISTOGRAM_BUCKETS - 1, Math.floor(tokens / width))
    counts[index] = (counts[index] ?? 0) + 1
  }
  return {
    bucket_tokens: width,
    buckets: counts.map((count, index) => ({
      lower: index * width,
      upper: (index + 1) * width,
      count,
    })),
  }
}

// -- evaluation -------------------------------------------------------------

export function metric(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

/** Reads a nested number out of a run's metrics without trusting its shape. */
export function dig(metrics: Record<string, unknown>, ...path: string[]): number | null {
  let value: unknown = metrics
  for (const key of path) {
    if (!value || typeof value !== 'object') return null
    value = (value as Record<string, unknown>)[key]
  }
  return typeof value === 'number' ? value : null
}

/** "recall@6 0.82 · precision@6 0.41 · MRR 0.77" — the chunk column before the budget. */
export function runHeadline(run: EvaluationRunSummaryResponse): string {
  if (run.status !== 'succeeded') return runStatusLine(run)
  const k = dig(run.metrics, 'k') ?? 0
  return [
    `recall@${k} ${metric(dig(run.metrics, 'all', 'chunk', 'recall'))}`,
    `precision@${k} ${metric(dig(run.metrics, 'all', 'chunk', 'precision'))}`,
    `MRR ${metric(dig(run.metrics, 'all', 'chunk', 'mrr'))}`,
  ].join(' · ')
}

export function runStatusLine(run: EvaluationRunSummaryResponse): string {
  if (run.status === 'queued') return 'Queued.'
  if (run.status === 'running') {
    return `Running: ${run.completed_items} of ${run.total_items} questions.`
  }
  if (run.status === 'failed') return `Failed: ${run.error ?? 'no reason recorded'}.`
  return ''
}

export function runTone(status: string): Tone {
  if (status === 'succeeded') return 'ok'
  if (status === 'failed') return 'error'
  return 'info'
}

/** The warnings the server put on the run's headline, if any. */
export function runWarnings(run: EvaluationRunSummaryResponse): string[] {
  const warnings = (run.metrics as { warnings?: unknown }).warnings
  return Array.isArray(warnings) ? warnings.map(String) : []
}

export function sourceLabel(source: EvaluationItemResponse['source']): string {
  return {
    log: 'from the log',
    citation: 'from a citation',
    manual: 'by hand',
    generated: 'by a model',
  }[source]
}

/** The list row's second line: what a set holds, and what that does to its numbers. */
export function setSummary(set: EvaluationSetResponse): string {
  const { total = 0, verified = 0, generated = 0, negatives = 0 } = set.counts ?? {}
  if (total === 0) return 'No questions yet.'
  const parts = [`${total} question${total === 1 ? '' : 's'}`]
  parts.push(`${verified} verified`)
  if (negatives) parts.push(`${negatives} negative`)
  if (generated) parts.push(`${generated} written by a model`)
  return parts.join(' · ')
}

/** What pressing Run will spend: one embedding call per question. */
export function runCostLine(set: EvaluationSetResponse | { counts: { total: number } }): string {
  const total = Math.min(set.counts.total, 500)
  if (total === 0) return 'Nothing to run.'
  return `${total} embedding call${total === 1 ? '' : 's'} — one per question. Nothing is sent to a completion model and nothing is written to the index.`
}

export function formatDelta(change: number | null): string {
  if (change === null) return '—'
  if (Math.abs(change) < 0.005) return '±0.00'
  return `${change > 0 ? '+' : ''}${change.toFixed(2)}`
}

export function deltaTone(change: number | null, higherIsBetter = true): Tone {
  if (change === null || Math.abs(change) < 0.005) return 'neutral'
  return change > 0 === higherIsBetter ? 'ok' : 'error'
}

/** The default import window: the last seven days, as the request body wants them. */
export function lastWeek(now = new Date()): { from: string; to: string } {
  const to = new Date(now)
  const from = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000)
  return { from: from.toISOString(), to: to.toISOString() }
}
