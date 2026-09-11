import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { Histogram } from '@/components/Histogram'
import { chunkHistogram } from '@/pages/validation'

import {
  useDeleteDocument,
  useDocumentChunks,
  useDocuments,
  usePreviewChunking,
  useReindexDocument,
  useSearch,
  useUpdateConnector,
  useUpload,
  useUploadUrl,
} from '@/api/connectors'
import type {
  ChunkingCandidate,
  ChunkingPreviewResponse,
  ConnectorResponse,
  DocumentResponse,
  SearchHit,
} from '@/api/types'
import { useEditSummary, useRegenerateSummary } from '@/api/summarization'
import { CopyButton } from '@/components/CopyButton'
import { Field, Form, Select, SubmitButton, TextInput } from '@/components/Form'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'
import { filesFrom } from '@/pages/dropFiles'
import { IndexStatusCell, StalePreviewNotice } from '@/pages/ReprocessingPanel'
import { summaryStatus } from '@/pages/summarization'
import {
  CHUNK_STRATEGIES,
  chunkingBody,
  chunkingChanged,
  chunkingForm,
  chunkingProblem,
  comparisonRows,
  documentTone,
  explanationFor,
  formatBytes,
  formatResolutions,
  pageLabel,
  strategyCost,
  strategyFields,
  strategyLabel,
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
        PDF, Word, PowerPoint and Excel, plus text and code: Markdown, HTML, CSV, JSON, YAML and
        source files. Scanned PDFs need OCR and are skipped with an explanation.
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
  // A citation's link (task 100) arrives as `?document=…&chunk=…`: open that document's
  // inspector and let it scroll to the chunk. Read once, at mount, so closing the panel
  // afterwards is not undone by the URL.
  const [params] = useSearchParams()
  const linkedDocument = params.get('document')
  const linkedChunk = params.get('chunk')
  // One open at a time. A connector with two hundred documents would otherwise fetch two
  // hundred chunk lists, and nobody compares two of them side by side anyway.
  const [inspecting, setInspecting] = useState<string | null>(linkedDocument)

  if (loading && documents.length === 0) {
    return <p className="py-8 text-center text-sm text-slate-500">Loading documents…</p>
  }
  if (documents.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-slate-500">
        Nothing here yet. Drop some files above, or upload with a presigned URL and press Resync.
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
          <th scope="col" className="py-2 pr-3">
            Cut with
          </th>
          <th scope="col" className="py-2 pr-3">
            Index
          </th>
          <th scope="col" className="py-2 pr-3">
            Summary
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
              <td className="py-2 pr-3 text-right text-slate-600">{document.chunk_count || '—'}</td>
              <td className="py-2 pr-3 text-right text-xs text-slate-500">{pageLabel(document)}</td>
              <td className="py-2 pr-3 text-xs text-slate-500">
                {document.indexed_at ? new Date(document.indexed_at).toLocaleString() : '—'}
              </td>
              <td className="py-2 pr-3 text-xs">
                {/* Task 101. The tokenizer the sizes were measured with, by the name it
                  gave itself — so a worker whose vocabulary failed to load is visible
                  here rather than in a log line. */}
                <span className="font-mono text-slate-500">{document.tokenizer ?? '—'}</span>
              </td>
              <td className="py-2 pr-3 text-xs">
                {/* Task 104. The second status axis, stored on the row: `current`, `stale`
                  or `reprocessing`, with the reason in the server's words on hover. A
                  document is `indexed` and `stale` at once after a change, and that is
                  the normal state — hence a column of its own rather than a second badge
                  fighting the first for the meaning of "status". */}
                <IndexStatusCell document={document} />
              </td>
              <td className="py-2 pr-3 text-xs">
                {/* Task 102. One word, and the sentence behind it on hover: `capped` is not
                  a failure and a failed summary is not a failed document, and a red badge
                  beside an `indexed` one would send somebody looking for a broken file. */}
                <SummaryCell document={document} />
              </td>
              <td className="py-2 text-right whitespace-nowrap">
                {document.chunk_count > 0 || document.summary ? (
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
                          notify(`Reprocessing ${document.source_name}.`)
                        })
                      }}
                      aria-label={`${document.status === 'failed' ? 'Retry' : 'Reprocess'} ${document.source_name}`}
                      className="ml-3 text-xs font-medium text-slate-600 hover:underline"
                    >
                      {document.status === 'failed' ? 'Retry' : 'Reprocess'}
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
                <td colSpan={11} className="bg-slate-50 px-3 py-3">
                  {document.summary_status || document.summary ? (
                    <SummaryView document={document} writes={writes} />
                  ) : null}
                  <ChunkInspector
                    documentId={document.id}
                    expected={document.chunk_count}
                    highlight={document.id === linkedDocument ? linkedChunk : null}
                  />
                </td>
              </tr>
            ) : null}
          </Fragment>
        ))}
      </tbody>
    </table>
  )
}

function SummaryCell({ document }: { document: DocumentResponse }) {
  const state = summaryStatus(document)
  if (!state) return <span className="text-slate-400">—</span>
  return (
    <span title={state.detail ?? undefined}>
      <StatusBadge status={state.label} tone={state.tone} />
    </span>
  )
}

/**
 * The document's summary, at the top of its inspector, with **Edit** and **Regenerate**
 * (task 102).
 *
 * The summary is content an operator can own. An edit becomes `manual`, is never charged to
 * a cap, and survives later reindexes of the same bytes; a regeneration asks the model again
 * and is charged like any other call. Both re-embed what depends on the summary — the
 * summary chunk, and under `contextual` every chunk — through the same jobs ingestion runs.
 */
export function SummaryView({ document, writes }: { document: DocumentResponse; writes: boolean }) {
  const edit = useEditSummary()
  const regenerate = useRegenerateSummary()
  const { notify } = useToast()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(document.summary ?? '')
  const state = summaryStatus(document)

  useEffect(() => {
    if (!editing) setDraft(document.summary ?? '')
  }, [document.summary, editing])

  return (
    <section
      data-testid="document-summary"
      className="mb-3 rounded-md border border-slate-200 bg-white p-3"
    >
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
        <span className="font-medium text-slate-700">
          Summary
          {state ? (
            <span className="ml-2">
              <StatusBadge status={state.label} tone={state.tone} />
            </span>
          ) : null}
          {document.summary_model && document.summary_status === 'summarized' ? (
            <span className="ml-2 font-normal">
              {document.summary_model === 'manual'
                ? 'written by hand'
                : `by ${document.summary_model}, ${(
                    (document.summary_tokens_in ?? 0) + (document.summary_tokens_out ?? 0)
                  ).toLocaleString()} tokens`}
            </span>
          ) : null}
        </span>
        {writes ? (
          <span className="flex gap-3">
            {!editing ? (
              <button
                type="button"
                onClick={() => setEditing(true)}
                className="font-medium text-slate-600 hover:underline"
              >
                Edit
              </button>
            ) : null}
            <button
              type="button"
              disabled={regenerate.isPending}
              onClick={() => {
                void regenerate.mutateAsync(document.id).then(() => {
                  notify(`Summarizing ${document.source_name} again.`)
                })
              }}
              className="font-medium text-slate-600 hover:underline disabled:opacity-50"
            >
              {document.summary_status === 'failed' ? 'Summarize' : 'Regenerate'}
            </button>
          </span>
        ) : null}
      </div>
      {editing ? (
        <div>
          <textarea
            aria-label="Summary"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            rows={4}
            className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
          />
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              disabled={edit.isPending || draft.trim().length === 0}
              onClick={() => {
                void edit
                  .mutateAsync({ documentId: document.id, summary: draft.trim() })
                  .then(() => {
                    setEditing(false)
                    notify('Summary saved; re-embedding what depends on it.')
                  })
              }}
              className="rounded-md bg-slate-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-50"
            >
              Save summary
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="rounded-md border border-slate-300 px-3 py-1 text-xs font-medium text-slate-700"
            >
              Cancel
            </button>
          </div>
          {edit.isError ? (
            <p role="alert" className="mt-1 text-xs text-red-600">
              The summary could not be saved.
            </p>
          ) : null}
        </div>
      ) : document.summary ? (
        <p className="whitespace-pre-wrap text-sm text-slate-700">{document.summary}</p>
      ) : (
        <p className="text-sm text-slate-500">{document.summary_error ?? 'No summary yet.'}</p>
      )}
    </section>
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
  highlight = null,
}: {
  documentId: string
  expected: number
  /** A chunk id to scroll to and mark — what a citation's URL points at (task 100). */
  highlight?: string | null
}) {
  const chunks = useDocumentChunks(documentId)
  const highlighted = useRef<HTMLLIElement | null>(null)
  useEffect(() => {
    // Optional-called: jsdom has no `scrollIntoView`, and a missing scroll is not a
    // failure worth a crash outside a browser either.
    highlighted.current?.scrollIntoView?.({ block: 'center' })
  }, [chunks.data, highlight])

  if (chunks.isPending) {
    return <p className="text-xs text-slate-500">Loading chunks…</p>
  }
  if (chunks.isError) {
    return <p className="text-xs text-red-600">The chunks could not be loaded.</p>
  }

  const all = chunks.data?.chunks ?? []
  // The summary point is not a chunk of the file: it is listed, labelled, and left out of
  // the count the row is compared against.
  const items = all.filter((chunk) => chunk.kind !== 'summary')
  const summaryPoint = all.find((chunk) => chunk.kind === 'summary')
  return (
    <div>
      <p className="mb-2 text-xs text-slate-500">
        {items.length} chunk{items.length === 1 ? '' : 's'} in the index
        {summaryPoint ? ', plus the summary' : ''}.
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
        {summaryPoint ? (
          <li
            key={summaryPoint.id}
            ref={summaryPoint.id === highlight ? highlighted : null}
            data-highlighted={summaryPoint.id === highlight || undefined}
            data-testid="summary-point"
            className={`rounded-md border border-dashed bg-white p-2 ${
              summaryPoint.id === highlight
                ? 'border-violet-400 ring-2 ring-violet-200'
                : 'border-slate-300'
            }`}
          >
            <div className="mb-1 flex items-center justify-between gap-2 text-xs text-slate-500">
              <span className="font-medium text-slate-700">
                Summary
                <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-600">
                  summary point
                </span>
                {summaryPoint.id === highlight ? (
                  <span className="ml-2 rounded bg-violet-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-violet-800">
                    cited
                  </span>
                ) : null}
              </span>
              <span className="font-mono">{summaryPoint.token_count ?? 0} tokens</span>
            </div>
            <p className="whitespace-pre-wrap text-xs text-slate-600">{summaryPoint.text}</p>
            <p className="mt-1 text-[10px] text-slate-400">
              Retrievable like any chunk, and rendered in the prompt as a summary, never as a
              source.
            </p>
          </li>
        ) : null}
        {items.map((chunk) => (
          <li
            key={chunk.id}
            ref={chunk.id === highlight ? highlighted : null}
            data-highlighted={chunk.id === highlight || undefined}
            className={`rounded-md border bg-white p-2 ${
              chunk.id === highlight
                ? 'border-violet-400 ring-2 ring-violet-200'
                : 'border-slate-200'
            }`}
          >
            <div className="mb-1 flex items-center justify-between gap-2 text-xs text-slate-500">
              <span className="font-medium text-slate-700">
                {chunk.page_or_section ?? `Chunk ${(chunk.chunk_index ?? 0) + 1}`}
                {chunk.id === highlight ? (
                  <span className="ml-2 rounded bg-violet-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-violet-800">
                    cited chunk
                  </span>
                ) : null}
              </span>
              <span className="font-mono">{chunk.token_count ?? 0} tokens</span>
            </div>
            {chunk.context ? (
              /* Task 102's `contextual` mode: the prefix that was embedded, visually
                 distinct and above the text that is returned — the same split the
                 windowed highlight makes for a different reason. */
              <p
                data-testid="embedded-context"
                className="mb-1 rounded border border-dashed border-slate-300 bg-slate-50 px-2 py-1 text-[11px] italic text-slate-500"
              >
                {chunk.context}
              </p>
            ) : null}
            <p className="line-clamp-4 whitespace-pre-wrap text-xs text-slate-600">
              {/* Under `sentence_window` the chunk's text is not what was embedded, and
                  without marking the difference the first debugging session is "why does
                  this chunk not contain the words I searched for" — with the answer
                  nowhere on this screen. */}
              {chunk.embedded_text ? (
                <Highlighted text={chunk.text} matched={chunk.embedded_text} />
              ) : (
                chunk.text
              )}
            </p>
            {chunk.embedded_text || chunk.context ? (
              <p className="mt-1 text-[10px] text-slate-400">
                {embeddedNote(chunk.embedded_because)}
              </p>
            ) : null}
          </li>
        ))}
      </ol>
    </div>
  )
}

/** What the inspector says under a chunk whose vector was not made from its text alone. */
function embeddedNote(because: string | null | undefined): string {
  switch (because) {
    case 'context':
      return 'The italic prefix was embedded with this chunk and is not returned; the text below is what the prompt receives.'
    case 'window+context':
      return 'The highlighted sentence, behind the italic prefix, is what was embedded; the rest is context this chunk carries into the prompt.'
    default:
      return 'The highlighted sentence is what was embedded; the rest is context this chunk carries into the prompt.'
  }
}

// ---------------------------------------------------------------------------
// chunking
// ---------------------------------------------------------------------------

export function ChunkingPanel({ connector }: { connector: ConnectorResponse }) {
  const [form, setForm] = useState<ChunkingForm>(() => chunkingForm(connector.chunking))
  // A finding on the Validation section links here as `?compare=<document>` (task 103):
  // the page that found a badly cut file opens the page that recuts it, on that file.
  const [params] = useSearchParams()
  const [comparing, setComparing] = useState(Boolean(params.get('compare')))
  const update = useUpdateConnector(connector.id)
  const { notify } = useToast()

  // Re-seeded when the server's copy changes, so a poll landing mid-edit does not fight
  // the person typing — the effect only fires when the *stored* value moves.
  useEffect(() => {
    setForm(chunkingForm(connector.chunking))
  }, [connector.chunking])

  const changed = chunkingChanged(form, connector.chunking)
  const problem = chunkingProblem(form)
  const strategy = CHUNK_STRATEGIES.find((entry) => entry.value === form.strategy)
  const shows = strategyFields(form.strategy)
  const cost = strategyCost(form.strategy)

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
        {shows.overlap ? (
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
        ) : null}
        {shows.window ? (
          <Field
            name="chunking.window_sentences"
            label="Window (sentences either side)"
            hint="The sentence is what a query matches; the window is what goes into the prompt."
          >
            {({ id, invalid, describedBy }) => (
              <TextInput
                id={id}
                type="number"
                invalid={invalid}
                describedBy={describedBy}
                value={form.windowSentences}
                onChange={(event) => setForm({ ...form, windowSentences: event.target.value })}
              />
            )}
          </Field>
        ) : null}
        {shows.breakpoint ? (
          <Field
            name="chunking.breakpoint_percentile"
            label="Breakpoint percentile"
            hint="A percentile of this document's own distances, not an absolute number — the scale is a property of the embedding model."
          >
            {({ id, invalid, describedBy }) => (
              <TextInput
                id={id}
                type="number"
                invalid={invalid}
                describedBy={describedBy}
                value={form.breakpointPercentile}
                onChange={(event) => setForm({ ...form, breakpointPercentile: event.target.value })}
              />
            )}
          </Field>
        ) : null}
      </div>

      {cost ? (
        /* Said once, where the choice is made. A trade-off whose consequence arrives
           months later is one nobody connects to the dropdown that caused it. */
        <p className="mb-4 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
          {cost}
        </p>
      ) : null}

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
      {/* Task 104. What this save will mark stale, per format, from the server's own
          fingerprint diff — the same sentence the summarization form gets, for free. The
          reprocess itself lives in the header, where the stale count is, so a person
          reads the consequence and the remedy in one place. */}
      <StalePreviewNotice
        connectorId={connector.id}
        patch={{ chunking: chunkingBody(form) }}
        enabled={changed && problem === null}
      />

      <div className="flex flex-wrap items-center gap-3">
        <SubmitButton busy={update.isPending} disabled={!changed || problem !== null}>
          Save chunking
        </SubmitButton>
        <button
          type="button"
          onClick={() => setComparing((open) => !open)}
          className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
        >
          {comparing ? 'Hide comparison' : 'Compare'}
        </button>
      </div>

      <FormatResolutions connector={connector} />
      {comparing ? <ChunkingCompare connector={connector} form={form} /> : null}
    </Form>
  )
}

/**
 * What each format this connector could hold actually resolves to.
 *
 * Displayed rather than left to be inferred, because a resolution rule nobody can see is a
 * rule everybody guesses at — and the guess that a per-format override applies to a format
 * it does not is invisible until retrieval quietly gets worse.
 */
export function FormatResolutions({ connector }: { connector: ConnectorResponse }) {
  const rows = formatResolutions(connector)
  const overridden = rows.filter((row) => row.overridden)
  if (overridden.length === 0) {
    return (
      <p className="mt-4 text-xs text-slate-500">
        Every format is cut the same way. Per-format overrides are set through the API.
      </p>
    )
  }
  return (
    <div className="mt-4">
      <p className="mb-1 text-xs font-medium text-slate-600">What each format resolves to</p>
      <ul className="divide-y divide-slate-200 rounded-md border border-slate-200 bg-white text-xs">
        {rows.map((row) => (
          <li key={row.kind} className="flex items-center justify-between gap-2 px-2 py-1.5">
            <span className={row.overridden ? 'font-medium text-slate-800' : 'text-slate-600'}>
              {row.label}
            </span>
            <span className="text-slate-600">
              {strategyLabel(row.strategy)}, {row.chunkSize} tokens
              {row.overridden ? (
                <span className="ml-1 rounded bg-slate-100 px-1 text-slate-500">override</span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * **Compare**: the same document cut several ways, side by side.
 *
 * Nobody can pick a chunking strategy from a description — the right answer depends on the
 * corpus. Without this, every user picks by name, which in practice means picking
 * `semantic` because it sounds better, paying for it at every ingestion, and never finding
 * out whether it helped.
 *
 * The candidate sent is the *form's* current state, so what is compared is the change
 * somebody is about to save rather than a hypothetical.
 */
export function ChunkingCompare({
  connector,
  form,
}: {
  connector: ConnectorResponse
  form: ChunkingForm
}) {
  const documents = useDocuments(connector.id, 'indexed')
  const preview = usePreviewChunking(connector.id)
  const [params] = useSearchParams()
  const [documentId, setDocumentId] = useState(params.get('compare') ?? '')
  const [query, setQuery] = useState('')

  const rows = documents.data?.items ?? []
  const chosen = documentId || rows[0]?.id || ''

  const run = () => {
    if (!chosen) return
    preview.mutate({
      document_id: chosen,
      candidates: [{ label: 'proposed', ...chunkingBody(form) }],
      query: query.trim() || null,
    })
  }

  return (
    <div className="mt-6 rounded-md border border-slate-200 bg-slate-50 p-3">
      <p className="mb-2 text-sm font-medium text-slate-800">Compare</p>
      <p className="mb-3 text-xs text-slate-600">
        Runs the settings above against one document beside what this connector does today. Nothing
        is saved, and nothing is indexed — but it does embed the document, so it costs what one
        ingestion would.
      </p>

      <div className="mb-3 grid gap-2 sm:grid-cols-2">
        <label className="text-xs text-slate-600">
          Document
          <select
            value={chosen}
            onChange={(event) => setDocumentId(event.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            {rows.length === 0 ? <option value="">Nothing indexed yet</option> : null}
            {rows.map((document: DocumentResponse) => (
              <option key={document.id} value={document.id}>
                {document.source_name}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-slate-600">
          Question (optional)
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="what does the travel policy cover?"
            className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
      </div>

      <button
        type="button"
        onClick={run}
        disabled={!chosen || preview.isPending}
        className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
      >
        {preview.isPending ? 'Running…' : 'Run comparison'}
      </button>

      {preview.isError ? (
        <p role="alert" className="mt-2 text-sm text-red-600">
          The comparison could not be run.
        </p>
      ) : null}
      {preview.data ? <ComparisonResult result={preview.data} /> : null}
    </div>
  )
}

function ComparisonResult({ result }: { result: ChunkingPreviewResponse }) {
  const candidates = result.candidates
  const summarization = result.summarization
  return (
    <div className="mt-4">
      {summarization ? (
        /* Task 102. The cost line includes the summarization call, because a comparison
           that hid half the embedding cost is the thing this panel refused to be; and
           under `contextual` the candidates below carry the prefix each chunk would be
           embedded behind. */
        <p
          data-testid="comparison-summarization"
          className="mb-3 rounded-md border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600"
        >
          Summarization ({summarization.mode}): one model call of about{' '}
          {summarization.tokens_in.toLocaleString()} tokens in and up to{' '}
          {summarization.tokens_out.toLocaleString()} out, on top of the embedding calls below.
          {summarization.prefixes
            ? summarization.summary
              ? ' Each chunk is shown behind the summary it would be embedded with.'
              : ' This document has no summary yet, so the prefix cannot be shown.'
            : ''}
        </p>
      ) : null}
      <table className="w-full table-fixed border-collapse text-xs">
        <thead>
          <tr className="text-left text-slate-500">
            <th className="w-48 py-1 font-medium">
              {result.source_name}
              <span className="ml-1 font-normal text-slate-400">({result.format_kind})</span>
            </th>
            {candidates.map((candidate: ChunkingCandidate) => (
              <th key={candidate.label} className="py-1 font-medium text-slate-700">
                {candidate.label}
                <span className="ml-1 font-normal text-slate-400">
                  {strategyLabel(candidate.strategy)}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200">
          {comparisonRows(candidates).map((row) => (
            <tr key={row.label}>
              <td className="py-1 pr-2 text-slate-500">{row.label}</td>
              {row.values.map((value, index) => (
                <td key={`${row.label}-${index}`} className="py-1 font-mono text-slate-700">
                  {value}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        {candidates.map((candidate: ChunkingCandidate) => (
          <div key={candidate.label}>
            <p className="mb-1 text-xs font-medium text-slate-600">{candidate.label}</p>
            {/* Task 103's histogram, on the candidate's own chunks: the shape of a cutting
                is something a list of twelve boxes does not show. */}
            <div className="mb-2 rounded-md border border-slate-200 bg-white p-2">
              <Histogram
                histogram={chunkHistogram(
                  candidate.chunks.map((chunk) => chunk.token_count),
                  candidateChunkSize(candidate, result),
                )}
                chunkSize={candidateChunkSize(candidate, result)}
                compact
              />
            </div>
            <ol className="space-y-1">
              {candidate.chunks.slice(0, 12).map((chunk) => (
                <li
                  key={chunk.index}
                  className={
                    'rounded-md border bg-white p-2 ' +
                    (candidate.best === chunk.index
                      ? 'border-emerald-400 ring-1 ring-emerald-200'
                      : 'border-slate-200')
                  }
                >
                  <div className="mb-0.5 flex justify-between text-[10px] text-slate-400">
                    <span>{chunk.section ?? `Chunk ${chunk.index + 1}`}</span>
                    <span className="font-mono">
                      {chunk.token_count} tok
                      {chunk.score !== null && chunk.score !== undefined
                        ? ` · ${chunk.score.toFixed(2)}`
                        : ''}
                    </span>
                  </div>
                  {/* The boundaries are drawn by the blocks themselves: one box per chunk,
                      in cut order, is the same information as lines over the text and
                      cannot disagree with what the splitter actually returned. */}
                  {chunk.context ? (
                    <p
                      data-testid="preview-context"
                      className="mb-1 rounded border border-dashed border-slate-300 bg-slate-50 px-1.5 py-0.5 text-[10px] italic text-slate-500 line-clamp-2"
                    >
                      {chunk.context}
                    </p>
                  ) : null}
                  <p className="line-clamp-3 whitespace-pre-wrap text-[11px] text-slate-600">
                    {chunk.embedded_text ? (
                      <Highlighted text={chunk.text} matched={chunk.embedded_text} />
                    ) : (
                      chunk.text
                    )}
                  </p>
                </li>
              ))}
            </ol>
            {candidate.total_chunks > 12 ? (
              <p className="mt-1 text-[10px] text-slate-400">
                {candidate.total_chunks - 12} more. The numbers above cover all of them.
              </p>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * The matched sentence, marked inside its window.
 *
 * Only `sentence_window` produces a chunk whose text is not what was embedded, and without
 * this the first debugging session under it is "why does this chunk not contain the words I
 * searched for" — with the answer nowhere on the screen.
 */
function Highlighted({ text, matched }: { text: string; matched: string }) {
  const at = text.indexOf(matched)
  if (at < 0) return <>{text}</>
  return (
    <>
      {text.slice(0, at)}
      <mark className="bg-amber-100 text-slate-800">{matched}</mark>
      {text.slice(at + matched.length)}
    </>
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
        Ask what a gateway would ask. This searches the chunks that are actually indexed, so it
        answers "is my file in there" before anything is wired up to use it.
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

/**
 * The ceiling a candidate was cut under, for the histogram's axis. The preview does not
 * echo each candidate's configuration back, so the largest chunk is the honest upper
 * bound: a candidate whose biggest chunk is 380 tokens was not cut at 1000.
 */
function candidateChunkSize(candidate: ChunkingCandidate, result: ChunkingPreviewResponse): number {
  const largest = Math.max(1, ...candidate.chunks.map((chunk) => chunk.token_count))
  const ceilings = [200, 400, 600, 800, 1000, 1500, 2000, 4000, 8000]
  return (
    ceilings.find((ceiling) => ceiling >= largest) ?? Math.max(largest, result.candidates.length)
  )
}
