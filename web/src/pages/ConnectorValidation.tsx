import { useState } from 'react'
import { Link } from 'react-router-dom'

import { useAudits, useStartAudit } from '@/api/validation'
import type {
  ChunkingReportResponse,
  ConnectorResponse,
  EmbeddingReportResponse,
  FindingResponse,
} from '@/api/types'
import { ApiError } from '@/api/client'
import { Histogram } from '@/components/Histogram'
import { StatusBadge } from '@/components/StatusBadge'
import {
  AUDIT_KINDS,
  auditAge,
  auditHeadline,
  driftCostLine,
  findingLink,
  severityLabel,
  severityTone,
  type AuditKind,
} from '@/pages/validation'

/**
 * Connectors → Validation (task 103, SPEC §6.6): two tabs, Chunking and Embeddings.
 *
 * Compare shows how one document is cut. This shows how the *connector* is cut, over
 * every point in the index, and whether the vectors are the vectors of that text — with a
 * findings list where each entry is a retrieval defect with a count, the documents behind
 * it, and a link to the page that fixes it. A report is a job, so the section shows the
 * last one and its age and polls while a new one runs.
 *
 * The one button here that spends is the drift check, and it says what it will spend
 * before it is pressed, the way Compare does.
 */
export function ConnectorValidation({
  connector,
  writes,
}: {
  connector: ConnectorResponse
  writes: boolean
}) {
  const [kind, setKind] = useState<AuditKind>('chunking')
  const [drift, setDrift] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { data: status, isLoading } = useAudits(connector.id)
  const start = useStartAudit(connector.id)

  const audit = status && typeof status === 'object' ? (status[kind] ?? null) : null
  const running = audit?.status === 'running'

  const run = async () => {
    setError(null)
    try {
      await start.mutateAsync({
        kind,
        body:
          kind === 'embedding' && drift
            ? { drift_sample: status?.drift_estimate.sample || 100 }
            : {},
      })
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'The audit could not be started.')
    }
  }

  return (
    <div data-testid="connector-validation">
      <div
        role="tablist"
        aria-label="Validation"
        className="mb-3 flex gap-1 border-b border-slate-200"
      >
        {AUDIT_KINDS.map((entry) => {
          const own = status?.[entry.kind]
          return (
            <button
              key={entry.kind}
              type="button"
              role="tab"
              aria-selected={kind === entry.kind}
              onClick={() => setKind(entry.kind)}
              className={`-mb-px flex items-center gap-2 border-b-2 px-3 py-2 text-sm ${
                kind === entry.kind
                  ? 'border-slate-900 font-medium text-slate-900'
                  : 'border-transparent text-slate-500 hover:text-slate-700'
              }`}
            >
              {entry.label}
              {own?.severity ? (
                <span
                  aria-label={severityLabel(own.severity)}
                  className={`inline-block h-2 w-2 rounded-full ${DOT[own.severity]}`}
                />
              ) : null}
            </button>
          )
        })}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        {writes ? (
          <button
            type="button"
            onClick={() => void run()}
            disabled={running || start.isPending}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400"
          >
            {running ? 'Running…' : audit ? 'Run again' : 'Run'}
          </button>
        ) : null}
        <span className="text-xs text-slate-500" data-testid="audit-age">
          {isLoading ? 'Loading…' : auditAge(audit)} {auditHeadline(audit)}
        </span>
      </div>

      {kind === 'embedding' && writes ? (
        <label className="mt-2 flex items-start gap-2 text-xs text-slate-600">
          <input
            type="checkbox"
            checked={drift}
            onChange={(event) => setDrift(event.target.checked)}
            className="mt-0.5"
          />
          <span>
            Also re-embed a sample and compare it with what is stored.{' '}
            <span data-testid="drift-cost">{driftCostLine(status)}</span>
          </span>
        </label>
      ) : null}

      {error ? (
        <p role="alert" className="mt-2 text-sm text-red-700">
          {error}
        </p>
      ) : null}

      {audit?.status === 'failed' ? (
        <p
          role="alert"
          className="mt-3 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800"
        >
          The last audit failed: {audit.error ?? 'no reason recorded'}.
        </p>
      ) : null}

      {audit?.report?.kind === 'chunking' ? (
        <ChunkingReport report={audit.report} connectorId={connector.id} />
      ) : null}
      {audit?.report?.kind === 'embedding' ? (
        <EmbeddingReport report={audit.report} connectorId={connector.id} />
      ) : null}
      {!audit && !isLoading ? (
        <p className="mt-3 text-sm text-slate-500">
          {kind === 'chunking'
            ? 'Scrolls every point in this connector’s index and reports the chunk-size distribution, the five numbers from Compare over the whole index, and what is wrong — per format, with the documents behind each finding.'
            : 'Checks the stored vectors: their width against the platform setting, padding returned as vectors, whether a chunk’s nearest neighbour is its own document, and — if asked — whether a fresh embedding of a sample still matches what is stored.'}
        </p>
      ) : null}
    </div>
  )
}

const DOT: Record<string, string> = {
  red: 'bg-red-500',
  amber: 'bg-amber-400',
  green: 'bg-emerald-500',
}

function ChunkingReport({
  report,
  connectorId,
}: {
  report: ChunkingReportResponse
  connectorId: string
}) {
  const [format, setFormat] = useState<string>('all')
  const shown = format === 'all' ? null : report.formats.find((entry) => entry.kind === format)
  const distribution = shown ? shown.distribution : report.distribution
  const histogram = shown ? shown.histogram : report.histogram
  const chunkSize = shown ? shown.chunk_size : report.chunk_size
  const findings = shown ? shown.findings : report.findings

  return (
    <div className="mt-4 space-y-4" data-testid="chunking-report">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-slate-700">
          <span className="font-medium">{report.points.toLocaleString()}</span> chunks over{' '}
          <span className="font-medium">{report.documents.toLocaleString()}</span> documents
          {report.summary_points ? `, plus ${report.summary_points} summary points` : ''}
          {report.documents_without_points
            ? ` · ${report.documents_without_points} documents have no points`
            : ''}
          .
        </p>
        {report.formats.length > 1 ? (
          <label className="text-xs text-slate-600">
            Format
            <select
              value={format}
              onChange={(event) => setFormat(event.target.value)}
              className="ml-2 rounded-md border border-slate-300 bg-white px-2 py-1 text-xs"
            >
              <option value="all">All formats</option>
              {report.formats.map((entry) => (
                <option key={entry.kind} value={entry.kind}>
                  {entry.kind} ({entry.points.toLocaleString()})
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>

      <div className="grid gap-4 md:grid-cols-[2fr_3fr]">
        <div className="rounded-md border border-slate-200 bg-white p-3">
          <p className="mb-2 text-xs font-medium text-slate-700">Chunk sizes (tokens)</p>
          <Histogram histogram={histogram} chunkSize={chunkSize} />
        </div>
        <dl className="grid grid-cols-3 gap-3 rounded-md border border-slate-200 bg-white p-3 text-sm">
          <Stat label="Chunks" value={distribution.chunks.toLocaleString()} />
          <Stat label="Median tokens" value={String(distribution.median_tokens)} />
          <Stat label="p95 tokens" value={String(distribution.p95_tokens)} />
          <Stat label="Smallest" value={String(distribution.min_tokens)} />
          <Stat label="Largest" value={String(distribution.max_tokens)} />
          <Stat label="At the ceiling" value={distribution.at_ceiling.toLocaleString()} />
          <Stat label="Mid-sentence cuts" value={distribution.mid_sentence.toLocaleString()} />
          <Stat
            label="Fingerprints"
            value={String(Object.keys(report.fingerprints ?? {}).length)}
            hint={
              Object.keys(report.fingerprints ?? {}).length > 1
                ? 'more than one cutting in this index'
                : undefined
            }
          />
        </dl>
      </div>

      <Findings findings={findings} connectorId={connectorId} />
    </div>
  )
}

function EmbeddingReport({
  report,
  connectorId,
}: {
  report: EmbeddingReportResponse
  connectorId: string
}) {
  const agreement = report.agreement
  const drift = report.drift
  return (
    <div className="mt-4 space-y-4" data-testid="embedding-report">
      <dl className="grid grid-cols-2 gap-3 rounded-md border border-slate-200 bg-white p-3 text-sm sm:grid-cols-4">
        <Stat
          label="Dimension"
          value={Object.keys(report.dimensions).join(' / ') || String(report.expected_dimension)}
          hint={`platform setting ${report.expected_dimension}`}
        />
        <Stat
          label="Scanned"
          value={`${report.scanned.toLocaleString()} of ${report.points.toLocaleString()}`}
        />
        <Stat
          label="Own document nearest"
          value={
            agreement?.rate === null || agreement?.rate === undefined
              ? '—'
              : `${Math.round(agreement.rate * 100)}%`
          }
          hint={agreement ? `${agreement.sampled} chunks sampled` : 'no document has two chunks'}
        />
        <Stat
          label="Fresh sample agrees"
          value={drift ? drift.mean.toFixed(2) : '—'}
          hint={drift ? `${drift.sampled} re-embedded, ${drift.shape}` : 'not re-embedded'}
        />
        <Stat label="Zero vectors" value={report.zero_vectors.toLocaleString()} />
        <Stat label="Identical vectors" value={report.identical_vectors.toLocaleString()} />
        <Stat
          label="Norms"
          value={
            report.norms ? `${report.norms.min.toFixed(2)}–${report.norms.max.toFixed(2)}` : '—'
          }
          hint={report.norms ? `median ${report.norms.median.toFixed(2)}` : undefined}
        />
        <Stat
          label="Model on rows"
          value={Object.keys(report.document_models).join(', ') || '—'}
          hint={`platform: ${report.expected_model}`}
        />
      </dl>

      <Findings findings={report.findings} connectorId={connectorId} />
    </div>
  )
}

function Findings({
  findings,
  connectorId,
}: {
  findings: readonly FindingResponse[]
  connectorId: string
}) {
  if (findings.length === 0) {
    return (
      <p className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
        Nothing to report.
      </p>
    )
  }
  const documentsOf = (finding: FindingResponse) => finding.documents ?? []
  return (
    <ul className="space-y-2" data-testid="findings">
      {findings.map((finding) => (
        <li key={finding.code} className="rounded-md border border-slate-200 bg-white p-3">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge
              status={severityLabel(finding.severity)}
              tone={severityTone(finding.severity)}
            />
            <span className="text-sm font-medium text-slate-900">{finding.title}</span>
            {finding.action === 'reindex' || finding.action === 'platform' ? (
              <FindingAction connectorId={connectorId} finding={finding} />
            ) : null}
          </div>
          <p className="mt-1 text-xs text-slate-600">{finding.detail}</p>
          {documentsOf(finding).length > 0 ? (
            <ul className="mt-2 flex flex-wrap gap-2 text-xs">
              {documentsOf(finding).map((document) => {
                const link = findingLink(connectorId, finding, document.id)
                return (
                  <li key={document.id} className="rounded bg-slate-100 px-2 py-1">
                    {link ? (
                      <Link to={link.to} className="text-slate-800 underline" title={link.label}>
                        {document.source_name}
                      </Link>
                    ) : (
                      <span className="text-slate-800">{document.source_name}</span>
                    )}
                    <span className="text-slate-500"> · {document.count}</span>
                  </li>
                )
              })}
              {(finding.document_count ?? 0) > documentsOf(finding).length ? (
                <li className="px-2 py-1 text-slate-500">
                  and {(finding.document_count ?? 0) - documentsOf(finding).length} more
                </li>
              ) : null}
            </ul>
          ) : null}
        </li>
      ))}
    </ul>
  )
}

function FindingAction({
  connectorId,
  finding,
}: {
  connectorId: string
  finding: FindingResponse
}) {
  const link = findingLink(connectorId, finding)
  if (!link) return null
  return (
    <Link to={link.to} className="text-xs text-slate-700 underline">
      {link.label}
    </Link>
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string | undefined }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-0.5 font-semibold tabular-nums text-slate-900">{value}</dd>
      {hint ? <dd className="text-[10px] text-slate-400">{hint}</dd> : null}
    </div>
  )
}
