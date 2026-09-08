import { Fragment, useEffect, useMemo, useRef, useState } from 'react'

import {
  useDeleteDocument,
  useDocumentChunks,
  useReindexConnector,
  useReindexDocument,
  useSearch,
  useUpdateConnector,
  useUpload,
  useUploadUrl,
} from '@/api/connectors'
import type { ConnectorResponse, DocumentResponse, SearchHit } from '@/api/types'
import { CopyButton } from '@/components/CopyButton'
import { Field, Form, Select, SubmitButton, TextInput } from '@/components/Form'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'
import { filesFrom } from '@/pages/dropFiles'
import {
  CHUNK_STRATEGIES,
  chunkingBody,
  chunkingChanged,
  chunkingForm,
  chunkingProblem,
  chunkingWarning,
  documentTone,
  explanationFor,
  formatBytes,
  pageLabel,
  uploadSnippet,
  type ChunkingForm,
} from '@/pages/connectors'

/**
 * The pieces of the connector detail screen, split out of the page so each is readable.
 *
 * The **upload zone** is the one worth reading. It accepts a drop *or* a click, and it
 * handles a dropped folder — `webkitGetAsEntry` is the only way to see into one, and a
 * zone that silently ignored a dragged folder would be the first thing anybody tried.
 */

// ---------------------------------------------------------------------------
// upload
// ---------------------------------------------------------------------------

export function UploadZone({ connectorId, disabled }: { connectorId: string; disabled: boolean }) {
  const [over, setOver] = useState(false)
  const [progress, setProgress] = useState<number | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const upload = useUpload(connectorId)
  const { notify } = useToast()

  const send = async (files: readonly File[]) => {
    if (files.length === 0) return
    setProgress(0)
    try {
      const result = await upload.mutateAsync({
        files,
        onProgress: ({ loaded, total }) => setProgress(total ? loaded / total : 0),
      })
      const rejected = result.files.filter((file) => file.status === 'rejected')
      // Both halves, always. "12 files uploaded" next to a silently dropped video is the
      // report that makes somebody trust a connector that is missing content.
      const accepted = result.files.length - rejected.length
      notify(
        rejected.length === 0
          ? `Uploaded ${accepted} file${accepted === 1 ? '' : 's'}.`
          : `Uploaded ${accepted}, rejected ${rejected.length}: ${rejected[0]?.error ?? ''}`,
        rejected.length === 0 ? 'success' : 'error',
      )
    } catch (error) {
      notify(error instanceof Error ? error.message : 'The upload failed.', 'error')
    } finally {
      setProgress(null)
    }
  }

  return (
    <section
      onDragOver={(event) => {
        event.preventDefault()
        if (!disabled) setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault()
        setOver(false)
        if (!disabled) void filesFrom(event.dataTransfer).then(send)
      }}
      className={`rounded-lg border-2 border-dashed p-8 text-center transition ${
        over ? 'border-slate-900 bg-slate-50' : 'border-slate-300 bg-white'
      } ${disabled ? 'opacity-50' : ''}`}
      aria-label="Upload files"
    >
      <p className="text-sm font-medium text-slate-800">Drop files or a folder here</p>
      <p className="mt-1 text-xs text-slate-500">
        PDF, Word, PowerPoint and Excel, plus text and code: Markdown, HTML, CSV, JSON, YAML
        and source files. Scanned PDFs need OCR and are skipped with an explanation.
      </p>
      <button
        type="button"
        disabled={disabled}
        onClick={() => input.current?.click()}
        className="mt-4 rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed"
      >
        Choose files
      </button>
      <input
        ref={input}
        type="file"
        multiple
        className="hidden"
        aria-label="Choose files"
        onChange={(event) => {
          void send(Array.from(event.target.files ?? []))
          event.target.value = ''
        }}
      />
      {progress !== null ? (
        <div className="mt-4">
          <div
            role="progressbar"
            aria-valuenow={Math.round(progress * 100)}
            aria-valuemin={0}
            aria-valuemax={100}
            className="h-2 overflow-hidden rounded-full bg-slate-200"
          >
            <div
              className="h-full bg-slate-900 transition-all"
              style={{ width: `${Math.round(progress * 100)}%` }}
            />
          </div>
          <p className="mt-1 text-xs text-slate-500">Uploading… {Math.round(progress * 100)}%</p>
        </div>
      ) : null}
    </section>
  )
}

// ---------------------------------------------------------------------------
// documents
// ---------------------------------------------------------------------------

export function DocumentTable({
  documents,
  loading,
  writes,
}: {
  documents: readonly DocumentResponse[]
  loading: boolean
  writes: boolean
}) {
  const reindex = useReindexDocument()
  const remove = useDeleteDocument()
  const { notify } = useToast()
  // One open at a time. A connector with two hundred documents would otherwise fetch two
  // hundred chunk lists, and nobody compares two of them side by side anyway.
  const [inspecting, setInspecting] = useState<string | null>(null)

  if (loading && documents.length === 0) {
    return <p className="py-8 text-center text-sm text-slate-500">Loading documents…</p>
  }
  if (documents.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-slate-500">
        Nothing here yet. Drop some files above, or upload with a presigned URL and press
        Resync.
      </p>
    )
  }

  return (
    <table className="w-full text-left text-sm">
      <caption className="sr-only">Documents</caption>
      <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
        <tr>
          <th scope="col" className="py-2 pr-3">
            Name
          </th>
          <th scope="col" className="py-2 pr-3">
            Type
          </th>
          <th scope="col" className="py-2 pr-3 text-right">
            Size
          </th>
          <th scope="col" className="py-2 pr-3">
            Status
          </th>
          <th scope="col" className="py-2 pr-3 text-right">
            Chunks
          </th>
          <th scope="col" className="py-2 pr-3 text-right">
            Length
          </th>
          <th scope="col" className="py-2 pr-3">
            Indexed
          </th>
          <th scope="col" className="py-2" />
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {documents.map((document) => (
          <Fragment key={document.id}>
          <tr className="align-top">
            <td className="py-2 pr-3">
              <div className="font-medium text-slate-800">{document.source_name}</div>
              {/* Inline, per SPEC §13.1. A detail view per failed row would mean the
                  table cannot say what is wrong until somebody clicks. An explained
                  state replaces the sentence rather than sitting beside it: two ways of
                  saying the same thing is how somebody reads neither. */}
              <DocumentReason document={document} />
            </td>
            <td className="py-2 pr-3 font-mono text-xs text-slate-500">
              {document.mime_type ?? '—'}
            </td>
            <td className="py-2 pr-3 text-right text-slate-600">
              {formatBytes(document.size_bytes)}
            </td>
            <td className="py-2 pr-3">
              <StatusBadge status={document.status} tone={documentTone(document.status)} />
            </td>
            <td className="py-2 pr-3 text-right text-slate-600">
              {document.chunk_count || '—'}
            </td>
            <td className="py-2 pr-3 text-right text-xs text-slate-500">
              {pageLabel(document)}
            </td>
            <td className="py-2 pr-3 text-xs text-slate-500">
              {document.indexed_at ? new Date(document.indexed_at).toLocaleString() : '—'}
            </td>
            <td className="py-2 text-right whitespace-nowrap">
              {document.chunk_count > 0 ? (
                <button
                  type="button"
                  aria-expanded={inspecting === document.id}
                  onClick={() =>
                    setInspecting((open) => (open === document.id ? null : document.id))
                  }
                  className="text-xs font-medium text-slate-600 hover:underline"
                >
                  {inspecting === document.id ? 'Hide chunks' : 'Chunks'}
                </button>
              ) : null}
              {writes ? (
                <>
                  <button
                    type="button"
                    onClick={() => {
                      void reindex.mutateAsync(document.id).then(() => {
                        notify(`Reindexing ${document.source_name}.`)
                      })
                    }}
                    className="ml-3 text-xs font-medium text-slate-600 hover:underline"
                  >
                    {document.status === 'failed' ? 'Retry' : 'Reindex'}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      void remove.mutateAsync(document.id).then(() => {
                        notify(`Deleted ${document.source_name}.`)
                      })
                    }}
                    className="ml-3 text-xs font-medium text-red-600 hover:underline"
                  >
                    Delete
                  </button>
                </>
              ) : null}
            </td>
          </tr>
          {inspecting === document.id ? (
            <tr>
              <td colSpan={8} className="bg-slate-50 px-3 py-3">
                <ChunkInspector documentId={document.id} expected={document.chunk_count} />
              </td>
            </tr>
          ) : null}
          </Fragment>
        ))}
      </tbody>
    </table>
  )
}

/**
 * The reason a document is not indexed, in the form it deserves.
 *
 * A recognised code becomes a heading and a next step; anything else falls back to the
 * server's sentence, which is what the table showed before this existed and is still the
 * right answer for a code the browser has never heard of.
 */
export function DocumentReason({ document }: { document: DocumentResponse }) {
  const explained = explanationFor(document)
  if (explained) {
    return (
      <div className="mt-1 rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5">
        <div className="text-xs font-medium text-amber-900">{explained.headline}</div>
        <div className="mt-0.5 text-xs text-amber-800">{explained.guidance}</div>
      </div>
    )
  }
  if (!document.error) return null
  return <div className="mt-0.5 text-xs text-amber-700">{document.error}</div>
}

/**
 * What one document actually became.
 *
 * The fastest way to see whether extraction produced text worth embedding — a PDF whose
 * every chunk opens with the same page header, a spreadsheet indexed as bare cells, a Word
 * file that came out as its pre-review draft. All three report `indexed` with a plausible
 * chunk count, and nothing else on this screen tells them apart from a healthy document.
 */
export function ChunkInspector({
  documentId,
  expected,
}: {
  documentId: string
  expected: number
}) {
  const chunks = useDocumentChunks(documentId)

  if (chunks.isPending) {
    return <p className="text-xs text-slate-500">Loading chunks…</p>
  }
  if (chunks.isError) {
    return <p className="text-xs text-red-600">The chunks could not be loaded.</p>
  }

  const items = chunks.data?.chunks ?? []
  return (
    <div>
      <p className="mb-2 text-xs text-slate-500">
        {items.length} chunk{items.length === 1 ? '' : 's'} in the index.
        {items.length !== expected ? (
          /* The two disagreeing is the finding, not a rendering detail: a row claiming
             twelve chunks with three in the index was written into a collection that has
             since been dropped, and "retrieval is bad" is how that otherwise presents. */
          <span className="ml-1 font-medium text-amber-700">
            The document row says {expected} — reindex to rebuild it.
          </span>
        ) : null}
      </p>
      <ol className="space-y-2">
        {items.map((chunk) => (
          <li key={chunk.id} className="rounded-md border border-slate-200 bg-white p-2">
            <div className="mb-1 flex items-center justify-between gap-2 text-xs text-slate-500">
              <span className="font-medium text-slate-700">
                {chunk.page_or_section ?? `Chunk ${(chunk.chunk_index ?? 0) + 1}`}
              </span>
              <span className="font-mono">{chunk.token_count ?? 0} tokens</span>
            </div>
            <p className="line-clamp-4 whitespace-pre-wrap text-xs text-slate-600">
              {chunk.text}
            </p>
          </li>
        ))}
      </ol>
    </div>
  )
}

// ---------------------------------------------------------------------------
// chunking
// ---------------------------------------------------------------------------

export function ChunkingPanel({ connector }: { connector: ConnectorResponse }) {
  const reindex = useReindexConnector(connector.id)
  const [form, setForm] = useState<ChunkingForm>(() => chunkingForm(connector.chunking))
  const update = useUpdateConnector(connector.id)
  const { notify } = useToast()

  // Re-seeded when the server's copy changes, so a poll landing mid-edit does not fight
  // the person typing — the effect only fires when the *stored* value moves.
  useEffect(() => {
    setForm(chunkingForm(connector.chunking))
  }, [connector.chunking])

  const changed = chunkingChanged(form, connector.chunking)
  const problem = chunkingProblem(form)
  const warning = chunkingWarning(connector, changed)
  const strategy = CHUNK_STRATEGIES.find((entry) => entry.value === form.strategy)

  const submit = async () => {
    await update.mutateAsync({ chunking: chunkingBody(form) })
    notify('Chunking saved.')
  }

  return (
    <Form onSubmit={submit} error={update.error}>
      <Field name="chunking.strategy" label="Strategy" hint={strategy?.hint}>
        {({ id, invalid, describedBy }) => (
          <Select
            id={id}
            invalid={invalid}
            describedBy={describedBy}
            value={form.strategy}
            onChange={(event) => setForm({ ...form, strategy: event.target.value })}
          >
            {CHUNK_STRATEGIES.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </Select>
        )}
      </Field>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field name="chunking.chunk_size" label="Chunk size (tokens)">
          {({ id, invalid, describedBy }) => (
            <TextInput
              id={id}
              type="number"
              invalid={invalid}
              describedBy={describedBy}
              value={form.chunkSize}
              onChange={(event) => setForm({ ...form, chunkSize: event.target.value })}
            />
          )}
        </Field>
        <Field name="chunking.overlap" label="Overlap (tokens)">
          {({ id, invalid, describedBy }) => (
            <TextInput
              id={id}
              type="number"
              invalid={invalid}
              describedBy={describedBy}
              value={form.overlap}
              onChange={(event) => setForm({ ...form, overlap: event.target.value })}
            />
          )}
        </Field>
      </div>

      <label className="mb-4 flex items-center gap-2 text-sm text-slate-700">
        <input
          type="checkbox"
          checked={form.respectBoundaries}
          onChange={(event) => setForm({ ...form, respectBoundaries: event.target.checked })}
          className="rounded border-slate-300"
        />
        Do not split mid-sentence or mid-code-block
      </label>

      {problem ? (
        <p role="alert" className="mb-4 text-sm text-red-600">
          {problem}
        </p>
      ) : null}
      {warning ? (
        <p className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          {warning}
        </p>
      ) : null}

      {connector.reindex_required ? (
        <div className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          <p>
            The chunking has changed since these documents were indexed, so their chunks are
            stale. Reindexing runs them through the pipeline again.
          </p>
          <button
            type="button"
            onClick={() =>
              reindex.mutate(undefined, {
                onSuccess: (result: { documents: number }) =>
                  notify(
                    `Reindexing ${result.documents} document${
                      result.documents === 1 ? '' : 's'
                    }.`,
                  ),
              })
            }
            className="mt-2 rounded-md border border-amber-300 bg-white px-3 py-1.5 text-sm font-medium text-amber-900 hover:bg-amber-100"
          >
            {reindex.isPending ? 'Queueing…' : 'Reindex every document'}
          </button>
        </div>
      ) : null}

      <SubmitButton busy={update.isPending} disabled={!changed || problem !== null}>
        Save chunking
      </SubmitButton>
    </Form>
  )
}

// ---------------------------------------------------------------------------
// presigned uploads
// ---------------------------------------------------------------------------

export function PresignedUpload({ connectorId }: { connectorId: string }) {
  const [filename, setFilename] = useState('handbook.md')
  const mint = useUploadUrl(connectorId)
  const snippet = mint.data ? uploadSnippet(mint.data.url) : null

  return (
    <div>
      <p className="mb-3 text-sm text-slate-600">
        Mint a short-lived URL and <code className="font-mono text-xs">PUT</code> to it from a
        script. The file is picked up by the next <strong>Resync</strong>.
      </p>
      <div className="flex items-end gap-2">
        <div className="flex-1">
          <Field name="filename" label="File name">
            {({ id, invalid, describedBy }) => (
              <TextInput
                id={id}
                invalid={invalid}
                describedBy={describedBy}
                value={filename}
                onChange={(event) => setFilename(event.target.value)}
              />
            )}
          </Field>
        </div>
        <button
          type="button"
          onClick={() => mint.mutate(filename)}
          disabled={mint.isPending || !filename.trim()}
          className="mb-4 rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed"
        >
          Get URL
        </button>
      </div>
      {snippet ? (
        <div className="rounded-md bg-slate-900 p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs text-slate-400">
              Expires in {Math.round((mint.data?.expires_in ?? 0) / 60)} minutes
            </span>
            <CopyButton value={snippet} label="Copy" />
          </div>
          <pre className="overflow-x-auto text-xs text-slate-100">{snippet}</pre>
        </div>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// debug search
// ---------------------------------------------------------------------------

export function SearchPanel({ connectorId }: { connectorId: string }) {
  const [query, setQuery] = useState('')
  const search = useSearch(connectorId)
  const hits: readonly SearchHit[] = useMemo(() => search.data?.hits ?? [], [search.data])

  return (
    <div>
      <p className="mb-3 text-sm text-slate-600">
        Ask what a gateway would ask. This searches the chunks that are actually indexed, so
        it answers "is my file in there" before anything is wired up to use it.
      </p>
      <Form
        onSubmit={async () => {
          await search.mutateAsync({ query, limit: 10 })
        }}
        error={search.error}
      >
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <Field name="query" label="Question">
              {({ id, invalid, describedBy }) => (
                <TextInput
                  id={id}
                  invalid={invalid}
                  describedBy={describedBy}
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="How much annual leave do we get?"
                />
              )}
            </Field>
          </div>
          <div className="mb-4">
            <SubmitButton busy={search.isPending} disabled={!query.trim()} className="w-auto px-4">
              Search
            </SubmitButton>
          </div>
        </div>
      </Form>

      {search.isSuccess && hits.length === 0 ? (
        <p className="text-sm text-slate-500">
          Nothing matched. Check that the documents you expect are <em>indexed</em> above.
        </p>
      ) : null}

      {hits.length > 0 ? (
        <>
          <p className="mb-2 text-xs text-slate-500">
            Scored by <span className="font-mono">{search.data?.embedding_model}</span>.
          </p>
          <ol className="space-y-3">
            {hits.map((hit) => (
              <li key={hit.id} className="rounded-md border border-slate-200 p-3">
                <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                  <span className="font-medium text-slate-700">
                    {hit.source_name}
                    {hit.page_or_section ? (
                      <span className="text-slate-400"> · {hit.page_or_section}</span>
                    ) : null}
                  </span>
                  <span className="font-mono text-slate-500">{hit.score.toFixed(3)}</span>
                </div>
                <p className="whitespace-pre-wrap text-sm text-slate-600">{hit.text}</p>
              </li>
            ))}
          </ol>
        </>
      ) : null}
    </div>
  )
}
