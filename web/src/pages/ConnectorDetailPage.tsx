import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { useConnector, useDeleteConnector, useDocuments, useResync } from '@/api/connectors'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { CopyButton } from '@/components/CopyButton'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'
import {
  ChunkingPanel,
  DocumentTable,
  PresignedUpload,
  SearchPanel,
  UploadZone,
} from '@/pages/ConnectorDetail'
import { DOCUMENT_STATUSES, formatBytes, resyncSummary, statusSummary } from '@/pages/connectors'
import { ObjectAudit } from '@/pages/ObjectAudit'
import { SummarizationPanel } from '@/pages/SummarizationPanel'

/**
 * One connector: upload, watch, tune, search (SPEC §13.1).
 *
 * The order of the page is the order somebody uses it. Upload first, because that is why
 * they came; the document table next, because watching it advance is how they learn the
 * thing works; chunking and the debug search below, because those are for the second
 * visit, when the answers are not quite right yet.
 *
 * The table advances on its own — see `useDocuments`, which polls while anything is
 * unfinished and stops when nothing is. The visible progression *is* the demo, and a
 * screen that needed a manual refresh to show it would not be showing it.
 */
export function ConnectorDetailPage() {
  const { connectorId } = useParams<{ connectorId: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const writes = can(user, 'resources:write')
  const { notify } = useToast()

  const [status, setStatus] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  const { data: connector, isLoading } = useConnector(connectorId)
  const documents = useDocuments(connectorId, status)
  const resync = useResync(connectorId)
  const remove = useDeleteConnector()

  if (isLoading || !connector) return <FullPageSpinner label="Loading connector" />

  const summary = statusSummary(connector)
  const deleting = connector.status === 'deleting'

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <Link to="/connectors" className="text-sm text-slate-500 hover:underline">
            ← Connectors
          </Link>
          <h1 className="mt-1 flex items-center gap-3 text-xl font-semibold text-slate-900">
            {connector.name}
            <StatusBadge status={summary.label} tone={summary.tone} />
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            {connector.document_count.toLocaleString()} document
            {connector.document_count === 1 ? '' : 's'} · {formatBytes(connector.total_bytes)}
            {connector.last_synced_at
              ? ` · synced ${new Date(connector.last_synced_at).toLocaleString()}`
              : ''}
          </p>
        </div>
        {writes ? (
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              disabled={resync.isPending || deleting}
              onClick={() => {
                void resync.mutateAsync().then((result) => notify(resyncSummary(result)))
              }}
              className="rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {resync.isPending ? 'Syncing…' : 'Resync'}
            </button>
            <button
              type="button"
              disabled={deleting}
              onClick={() => setConfirming(true)}
              className="rounded-md border border-red-300 px-3 py-2 text-sm font-medium text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {/* Not just "Delete": the document rows have one of those too, and two
                  controls with the same accessible name on one screen is ambiguous to a
                  screen reader as well as to a test. */}
              Delete connector
            </button>
          </div>
        ) : null}
      </header>

      {deleting ? (
        <p
          role="status"
          className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
        >
          This connector is being deleted. Its files and vectors are being removed.
        </p>
      ) : null}

      {writes ? <UploadZone connectorId={connector.id} disabled={deleting} /> : null}

      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="mb-3 flex items-center justify-between gap-4">
          <h2 className="text-sm font-semibold text-slate-900">Documents</h2>
          <label className="flex items-center gap-2 text-xs text-slate-500">
            Status
            <select
              aria-label="Filter by status"
              value={status ?? ''}
              onChange={(event) => setStatus(event.target.value || null)}
              className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs"
            >
              <option value="">All</option>
              {DOCUMENT_STATUSES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
        </div>
        <DocumentTable
          documents={documents.data?.items ?? []}
          loading={documents.isLoading}
          writes={writes}
        />
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-900">Chunking</h2>
          <ChunkingPanel connector={connector} />
        </section>

        <section className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-900">Try retrieval</h2>
          <SearchPanel connectorId={connector.id} />
        </section>

        {writes ? (
          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="mb-3 text-sm font-semibold text-slate-900">Summarization</h2>
            <SummarizationPanel connector={connector} />
          </section>
        ) : null}
      </div>

      {writes ? (
        <section className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-900">Upload from a script</h2>
          <PresignedUpload connectorId={connector.id} />
          {connector.storage_prefix ? (
            <p className="mt-3 flex items-center gap-2 text-xs text-slate-500">
              Objects live under
              <code className="font-mono">{connector.storage_prefix}</code>
              <CopyButton value={connector.storage_prefix} label="Copy prefix" />
            </p>
          ) : null}
        </section>
      ) : null}

      {/* Chunking changes, resyncs and uploads, in one place. A connector that started
          answering badly usually had its chunking changed, and this is what says when. */}
      <ObjectAudit targetType="connector" targetId={connector.id} noun="connector" />

      <ConfirmDialog
        open={confirming}
        title="Delete this connector?"
        description={
          <>
            Its {connector.document_count.toLocaleString()} document
            {connector.document_count === 1 ? '' : 's'}, their files, and everything indexed
            from them are removed. This cannot be undone.
          </>
        }
        resourceName={connector.name}
        onCancel={() => setConfirming(false)}
        busy={remove.isPending}
        onConfirm={() => {
          void remove.mutateAsync(connector.id).then(() => {
            notify(`Deleting ${connector.name}.`)
            setConfirming(false)
            void navigate('/connectors')
          })
        }}
      />
    </div>
  )
}
