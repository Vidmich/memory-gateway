import { useState } from 'react'

import { ApiError } from '@/api/client'
import { useTryRetrieval } from '@/api/gateways'
import type {
  EvaluationItemResponse,
  EvaluationRunResponse,
  EvaluationRunSummaryResponse,
  EvaluationSetResponse,
  RetrievedChunkResponse,
} from '@/api/types'
import {
  runInFlight,
  useAddEvaluationItem,
  useCreateEvaluationSet,
  useDeleteEvaluationItem,
  useDeleteEvaluationSet,
  useEvaluationRun,
  useEvaluationRuns,
  useEvaluationSet,
  useEvaluationSets,
  useGenerateEvaluationItems,
  useImportEvaluationItems,
  useRunDiff,
  useStartEvaluationRun,
  useUpdateEvaluationItem,
} from '@/api/validation'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'
import { memoryBody, type MemoryForm } from '@/pages/memory'
import {
  deltaTone,
  dig,
  formatDelta,
  lastWeek,
  metric,
  runCostLine,
  runHeadline,
  runStatusLine,
  runTone,
  runWarnings,
  setSummary,
  sourceLabel,
} from '@/pages/validation'

/**
 * Gateways → Validation (task 103, SPEC §6.6): evaluation sets, and the runs over them.
 *
 * An evaluation set is labelled questions — imported from this gateway's log with the
 * chunks the answer cited, written by a person from Try retrieval, or written by a model
 * from a chunk — and a run is those questions through the **real** retrieval path, scored.
 * The section keeps the three kinds of label apart and says so on every number, because
 * a number computed over labels a model wrote is a different number from one over labels
 * a person checked.
 *
 * Run sends the Memory form above, unsaved, the way Try retrieval does: change
 * `doc_min_score`, run again, and both runs stay in the history to be diffed.
 */
export function EvaluationSection({
  gatewayId,
  form,
  stored,
  writes,
}: {
  gatewayId: string | undefined
  form: MemoryForm
  stored: MemoryForm
  writes: boolean
}) {
  const [selected, setSelected] = useState<string | null>(null)
  const [name, setName] = useState('')
  const sets = useEvaluationSets(gatewayId)
  const create = useCreateEvaluationSet(gatewayId ?? '')
  const { notify } = useToast()

  if (!gatewayId) {
    return (
      <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold text-slate-900">Validation</h2>
        <p className="mt-1 text-sm text-slate-500">
          Save the gateway first. An evaluation set belongs to a gateway, because its labels only
          mean something against the connectors that gateway reads.
        </p>
      </section>
    )
  }

  const rows = listOf(sets.data)
  const current = rows.find((row) => row.id === selected) ?? null

  return (
    <section
      className="mb-8 rounded-lg border border-slate-200 bg-white p-5"
      data-testid="evaluation-section"
    >
      <h2 className="text-sm font-semibold text-slate-900">Validation</h2>
      <p className="mt-1 text-sm text-slate-500">
        Does retrieval find the right chunks? Build a set of questions with the chunks that answer
        them, run it through this gateway&apos;s retrieval, and read recall, precision and MRR —
        before and after the token budget, at chunk and at document level.
      </p>

      <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_2fr]">
        <div>
          <ul className="space-y-2" aria-label="Evaluation sets">
            {rows.map((row) => (
              <li key={row.id}>
                <button
                  type="button"
                  onClick={() => setSelected(row.id === selected ? null : row.id)}
                  aria-pressed={row.id === selected}
                  className={`w-full rounded-md border p-3 text-left ${
                    row.id === selected
                      ? 'border-slate-900 bg-slate-50'
                      : 'border-slate-200 hover:bg-slate-50'
                  }`}
                >
                  <span className="block text-sm font-medium text-slate-900">{row.name}</span>
                  <span className="block text-xs text-slate-500">{setSummary(row)}</span>
                  {row.last_run ? (
                    <span className="mt-1 block font-mono text-xs text-slate-600">
                      {runHeadline(row.last_run)}
                    </span>
                  ) : null}
                </button>
              </li>
            ))}
            {rows.length === 0 && !sets.isLoading ? (
              <li className="text-sm text-slate-500">No evaluation sets yet.</li>
            ) : null}
          </ul>
          {writes ? (
            <form
              className="mt-3 flex gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                if (!name.trim()) return
                void create
                  .mutateAsync({ name: name.trim() })
                  .then((made) => {
                    setName('')
                    setSelected(made.id)
                  })
                  .catch((caught: unknown) =>
                    notify(
                      caught instanceof ApiError ? caught.message : 'Could not create the set.',
                      'error',
                    ),
                  )
              }}
            >
              <label htmlFor="evaluation-set-name" className="sr-only">
                New set name
              </label>
              <input
                id="evaluation-set-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="New set, e.g. Support questions"
                className="min-w-0 flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
              <button
                type="submit"
                disabled={create.isPending || !name.trim()}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                Create set
              </button>
            </form>
          ) : null}
        </div>

        {current ? (
          <SetPanel
            key={current.id}
            gatewayId={gatewayId}
            set={current}
            form={form}
            stored={stored}
            writes={writes}
            onDeleted={() => setSelected(null)}
          />
        ) : (
          <p className="text-sm text-slate-500">
            {rows.length ? 'Pick a set to see its questions and runs.' : ''}
          </p>
        )}
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// one set
// ---------------------------------------------------------------------------

function SetPanel({
  gatewayId,
  set,
  form,
  stored,
  writes,
  onDeleted,
}: {
  gatewayId: string
  set: EvaluationSetResponse
  form: MemoryForm
  stored: MemoryForm
  writes: boolean
  onDeleted: () => void
}) {
  const detail = useEvaluationSet(set.id)
  const runs = useEvaluationRuns(set.id)
  const start = useStartEvaluationRun(gatewayId, set.id)
  const importItems = useImportEvaluationItems(gatewayId, set.id)
  const generate = useGenerateEvaluationItems(gatewayId, set.id)
  const remove = useDeleteEvaluationSet(gatewayId)
  const { notify } = useToast()
  const [confirming, setConfirming] = useState(false)
  const [uncited, setUncited] = useState<'' | 'true' | 'false'>('')
  const [count, setCount] = useState(10)
  const [openRun, setOpenRun] = useState<string | null>(null)
  const [against, setAgainst] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const unsaved = JSON.stringify(memoryBody(form)) !== JSON.stringify(memoryBody(stored))
  const inFlight = listOf(runs.data).some(runInFlight)

  const act = async (work: () => Promise<string>) => {
    setError(null)
    try {
      notify(await work())
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'That could not be done.')
    }
  }

  return (
    <div className="space-y-4" data-testid="evaluation-set">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">{set.name}</h3>
          <p className="text-xs text-slate-500">{setSummary(set)}</p>
        </div>
        {writes ? (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="text-xs text-red-700 underline"
          >
            Delete set
          </button>
        ) : null}
      </div>

      {writes ? (
        <div className="flex flex-wrap items-end gap-3 rounded-md border border-slate-200 bg-slate-50 p-3 text-xs">
          <div>
            <button
              type="button"
              disabled={start.isPending || inFlight || set.counts.total === 0}
              onClick={() =>
                void act(async () => {
                  await start.mutateAsync({ memory_config: memoryBody(form) })
                  return 'Run queued.'
                })
              }
              className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400"
            >
              {inFlight ? 'Running…' : 'Run'}
            </button>
            <p className="mt-1 max-w-xs text-slate-500" data-testid="run-cost">
              {runCostLine(set)}
              {unsaved ? ' Uses the Memory settings above, which are not saved yet.' : ''}
            </p>
          </div>
          <div className="flex items-end gap-2">
            <label className="text-slate-600">
              Import from the log
              <select
                value={uncited}
                onChange={(event) => setUncited(event.target.value as '' | 'true' | 'false')}
                className="mt-1 block rounded-md border border-slate-300 bg-white px-2 py-1"
                aria-label="Which requests to import"
              >
                <option value="">Every question, last 7 days</option>
                <option value="false">Only answers that cited something</option>
                <option value="true">Only answers that cited nothing</option>
              </select>
            </label>
            <button
              type="button"
              disabled={importItems.isPending}
              onClick={() =>
                void act(async () => {
                  const result = await importItems.mutateAsync({
                    ...lastWeek(),
                    uncited: uncited === '' ? null : uncited === 'true',
                    limit: 200,
                  })
                  return `Imported ${result.imported} question${result.imported === 1 ? '' : 's'} (${result.labelled} with citations, ${result.duplicates} already here).`
                })
              }
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              Import
            </button>
          </div>
          <div className="flex items-end gap-2">
            <label className="text-slate-600">
              Generate with a model
              <input
                type="number"
                min={1}
                max={25}
                value={count}
                onChange={(event) => setCount(Number(event.target.value))}
                className="mt-1 block w-20 rounded-md border border-slate-300 bg-white px-2 py-1"
                aria-label="How many questions to generate"
              />
            </label>
            <button
              type="button"
              disabled={generate.isPending}
              onClick={() =>
                void act(async () => {
                  const result = await generate.mutateAsync({ count })
                  return `Wrote ${result.generated} question${result.generated === 1 ? '' : 's'} with ${result.model_name ?? 'the model'} (${result.tokens_in + result.tokens_out} tokens).`
                })
              }
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              title="One model call per question, through the summarization model chain. Synthetic questions over-estimate recall."
            >
              Generate
            </button>
          </div>
        </div>
      ) : null}

      {error ? (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      ) : null}

      <ItemsTable
        gatewayId={gatewayId}
        setId={set.id}
        items={listOf(detail.data)}
        writes={writes}
        loading={detail.isLoading}
      />

      <RunsTable
        runs={listOf(runs.data)}
        openRun={openRun}
        against={against}
        onOpen={setOpenRun}
        onAgainst={setAgainst}
      />

      {openRun && against && openRun !== against ? (
        <DiffView runId={openRun} against={against} />
      ) : null}
      {openRun ? <RunDetail runId={openRun} /> : null}

      <ConfirmDialog
        open={confirming}
        title="Delete this evaluation set?"
        description="Its questions, labels and every run over them are removed."
        resourceName={set.name}
        onCancel={() => setConfirming(false)}
        busy={remove.isPending}
        onConfirm={() => {
          void remove.mutateAsync(set.id).then(() => {
            setConfirming(false)
            onDeleted()
          })
        }}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// items
// ---------------------------------------------------------------------------

function ItemsTable({
  gatewayId,
  setId,
  items,
  writes,
  loading,
}: {
  gatewayId: string
  setId: string
  items: readonly EvaluationItemResponse[]
  writes: boolean
  loading: boolean
}) {
  const update = useUpdateEvaluationItem(gatewayId, setId)
  const remove = useDeleteEvaluationItem(gatewayId, setId)
  const [adding, setAdding] = useState(false)

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Questions</h4>
        {writes ? (
          <button
            type="button"
            onClick={() => setAdding((value) => !value)}
            className="text-xs text-slate-700 underline"
          >
            {adding ? 'Close' : 'Add a question'}
          </button>
        ) : null}
      </div>
      {adding ? (
        <AddItem gatewayId={gatewayId} setId={setId} onDone={() => setAdding(false)} />
      ) : null}
      {loading ? <p className="text-sm text-slate-500">Loading…</p> : null}
      {items.length === 0 && !loading ? (
        <p className="text-sm text-slate-500">
          No questions yet. Import last week&apos;s from the log, add one from Try retrieval above,
          or ask a model to write some.
        </p>
      ) : null}
      {items.length > 0 ? (
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Evaluation items</caption>
          <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="py-1 pr-3">
                Question
              </th>
              <th scope="col" className="py-1 pr-3">
                Relevant
              </th>
              <th scope="col" className="py-1 pr-3">
                Source
              </th>
              <th scope="col" className="py-1 pr-3">
                Verified
              </th>
              {writes ? <th scope="col" className="py-1" /> : null}
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr
                key={item.id}
                className="border-b border-slate-100 align-top"
                data-testid="evaluation-item"
              >
                <td className="py-2 pr-3">
                  <ItemQuestion
                    item={item}
                    writes={writes}
                    onSave={(question) =>
                      update.mutateAsync({
                        itemId: item.id,
                        body: { question },
                      })
                    }
                  />
                </td>
                <td className="py-2 pr-3 text-xs text-slate-600">
                  {item.negative ? (
                    <span className="rounded bg-slate-100 px-1.5 py-0.5">nothing — a negative</span>
                  ) : (
                    <>
                      {item.relevant.map((label) => (
                        <span
                          key={label.chunk_id}
                          className="mr-1 inline-block rounded bg-slate-100 px-1.5 py-0.5"
                          title={label.text ?? undefined}
                        >
                          {label.source_name ?? 'chunk'} · chunk
                        </span>
                      ))}
                      {item.relevant_document_ids.map((id) => (
                        <span
                          key={id}
                          className="mr-1 inline-block rounded bg-slate-100 px-1.5 py-0.5"
                        >
                          document {id.slice(0, 8)}…
                        </span>
                      ))}
                    </>
                  )}
                </td>
                <td className="py-2 pr-3 text-xs text-slate-600">{sourceLabel(item.source)}</td>
                <td className="py-2 pr-3">
                  <input
                    type="checkbox"
                    aria-label={`Verified: ${item.question}`}
                    checked={item.verified}
                    disabled={!writes}
                    onChange={(event) =>
                      void update.mutateAsync({
                        itemId: item.id,
                        body: { verified: event.target.checked },
                      })
                    }
                  />
                </td>
                {writes ? (
                  <td className="py-2 text-right">
                    <button
                      type="button"
                      aria-label={`Delete: ${item.question}`}
                      onClick={() => void remove.mutateAsync(item.id)}
                      className="text-xs text-red-700 underline"
                    >
                      Delete
                    </button>
                  </td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  )
}

function ItemQuestion({
  item,
  writes,
  onSave,
}: {
  item: EvaluationItemResponse
  writes: boolean
  onSave: (question: string) => Promise<unknown>
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(item.question)
  if (!editing) {
    return (
      <span>
        {item.question}
        {writes ? (
          <button
            type="button"
            onClick={() => {
              setDraft(item.question)
              setEditing(true)
            }}
            className="ml-2 text-xs text-slate-500 underline"
            aria-label={`Edit: ${item.question}`}
          >
            edit
          </button>
        ) : null}
      </span>
    )
  }
  return (
    <form
      className="flex gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        void onSave(draft.trim()).then(() => setEditing(false))
      }}
    >
      <input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        aria-label="Question"
        className="min-w-0 flex-1 rounded-md border border-slate-300 px-2 py-1 text-sm"
      />
      <button type="submit" className="text-xs text-slate-700 underline">
        Save
      </button>
      <button
        type="button"
        onClick={() => setEditing(false)}
        className="text-xs text-slate-500 underline"
      >
        Cancel
      </button>
    </form>
  )
}

/**
 * A new item, labelled with a chunk picker that *is* Try retrieval: type the question,
 * see what comes back, tick what answers it. Ticking nothing makes a negative.
 */
function AddItem({
  gatewayId,
  setId,
  onDone,
}: {
  gatewayId: string
  setId: string
  onDone: () => void
}) {
  const [question, setQuestion] = useState('')
  const [chunks, setChunks] = useState<RetrievedChunkResponse[]>([])
  const [picked, setPicked] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const tryRetrieval = useTryRetrieval(gatewayId)
  const add = useAddEvaluationItem(gatewayId, setId)

  return (
    <div className="mb-3 rounded-md border border-slate-200 bg-slate-50 p-3" data-testid="add-item">
      <label htmlFor="new-item-question" className="text-xs text-slate-600">
        Question
      </label>
      <div className="mt-1 flex gap-2">
        <input
          id="new-item-question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          className="min-w-0 flex-1 rounded-md border border-slate-300 px-2 py-1 text-sm"
          placeholder="How do I request a refund?"
        />
        <button
          type="button"
          disabled={!question.trim() || tryRetrieval.isPending}
          onClick={() =>
            void tryRetrieval
              .mutateAsync({ query: question.trim(), memory_config: null })
              .then((preview) => {
                setChunks(preview.chunks)
                setPicked([])
              })
          }
          className="rounded-md border border-slate-300 bg-white px-3 py-1 text-xs font-medium text-slate-700"
        >
          Find chunks
        </button>
      </div>
      {chunks.length > 0 ? (
        <ul className="mt-2 space-y-1">
          {chunks.map((chunk) => (
            <li key={chunk.id} className="flex items-start gap-2 text-xs">
              <input
                type="checkbox"
                aria-label={`Relevant: ${chunk.source_name} [${chunk.handle}]`}
                checked={picked.includes(chunk.id)}
                onChange={(event) =>
                  setPicked((value) =>
                    event.target.checked
                      ? [...value, chunk.id]
                      : value.filter((id) => id !== chunk.id),
                  )
                }
              />
              <span>
                <span className="font-medium">{chunk.source_name}</span>
                <span className="text-slate-500">
                  {' '}
                  [{chunk.handle}] · {chunk.score.toFixed(2)}
                </span>
                <span className="block line-clamp-2 text-slate-600">{chunk.text}</span>
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          disabled={!question.trim() || add.isPending}
          onClick={() => {
            setError(null)
            void add
              .mutateAsync({
                question: question.trim(),
                relevant: chunks
                  .filter((chunk) => picked.includes(chunk.id) && chunk.document_id)
                  .map((chunk) => ({
                    chunk_id: chunk.id,
                    document_id: chunk.document_id as string,
                  })),
                verified: true,
              })
              .then(() => {
                setQuestion('')
                setChunks([])
                setPicked([])
                onDone()
              })
              .catch((caught: unknown) =>
                setError(caught instanceof ApiError ? caught.message : 'Could not add the item.'),
              )
          }}
          className="rounded-md bg-slate-900 px-3 py-1 text-xs font-medium text-white disabled:bg-slate-400"
        >
          {picked.length === 0
            ? 'Add as a negative'
            : `Add with ${picked.length} chunk${picked.length === 1 ? '' : 's'}`}
        </button>
        {error ? (
          <span role="alert" className="text-xs text-red-700">
            {error}
          </span>
        ) : null}
      </div>
    </div>
  )
}

/**
 * The link from Try retrieval (task 103): the moment a person tuning a gateway sees the
 * right chunk come back is the moment the label is cheapest. Rendered under the retrieval
 * result with the chunks it just showed.
 */
export function AddToEvaluationSet({
  gatewayId,
  query,
  chunks,
}: {
  gatewayId: string
  query: string
  chunks: readonly RetrievedChunkResponse[]
}) {
  const sets = useEvaluationSets(gatewayId)
  const [setId, setSetId] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [status, setStatus] = useState<string | null>(null)
  const available = listOf(sets.data)
  const chosen = setId || available[0]?.id || ''
  const add = useAddEvaluationItem(gatewayId, chosen)

  if (available.length === 0) return null

  return (
    <div
      className="mt-3 rounded-md border border-dashed border-slate-300 p-3"
      data-testid="add-to-evaluation-set"
    >
      <p className="text-xs font-medium text-slate-700">Add to an evaluation set</p>
      <p className="text-xs text-slate-500">
        Tick the chunks that answer the question. Ticking none records that nothing should.
      </p>
      <ul className="mt-2 space-y-1">
        {chunks.map((chunk) => (
          <li key={chunk.id} className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              aria-label={`Relevant: [${chunk.handle}] ${chunk.source_name}`}
              checked={picked.includes(chunk.id)}
              onChange={(event) =>
                setPicked((value) =>
                  event.target.checked
                    ? [...value, chunk.id]
                    : value.filter((id) => id !== chunk.id),
                )
              }
            />
            <span>
              [{chunk.handle}] {chunk.source_name}
            </span>
          </li>
        ))}
      </ul>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <select
          aria-label="Evaluation set"
          value={chosen}
          onChange={(event) => setSetId(event.target.value)}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs"
        >
          {available.map((row) => (
            <option key={row.id} value={row.id}>
              {row.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={add.isPending || !chosen}
          onClick={() => {
            setStatus(null)
            void add
              .mutateAsync({
                question: query,
                relevant: chunks
                  .filter((chunk) => picked.includes(chunk.id) && chunk.document_id)
                  .map((chunk) => ({
                    chunk_id: chunk.id,
                    document_id: chunk.document_id as string,
                  })),
                verified: true,
              })
              .then(() => setStatus('Added.'))
              .catch((caught: unknown) =>
                setStatus(caught instanceof ApiError ? caught.message : 'Could not add the item.'),
              )
          }}
          className="rounded-md border border-slate-300 bg-white px-3 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          Add to evaluation set
        </button>
        {status ? (
          <span role="status" className="text-xs text-slate-600">
            {status}
          </span>
        ) : null}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// runs
// ---------------------------------------------------------------------------

function RunsTable({
  runs,
  openRun,
  against,
  onOpen,
  onAgainst,
}: {
  runs: readonly EvaluationRunSummaryResponse[]
  openRun: string | null
  against: string | null
  onOpen: (id: string | null) => void
  onAgainst: (id: string | null) => void
}) {
  if (runs.length === 0) {
    return null
  }
  return (
    <div>
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Runs</h4>
      <table className="w-full text-left text-sm" data-testid="runs-table">
        <caption className="sr-only">Evaluation runs, newest first</caption>
        <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th scope="col" className="py-1 pr-3">
              When
            </th>
            <th scope="col" className="py-1 pr-3">
              Status
            </th>
            <th scope="col" className="py-1 pr-3">
              Headline
            </th>
            <th scope="col" className="py-1 pr-3">
              Diff against
            </th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} className="border-b border-slate-100">
              <td className="py-2 pr-3 text-xs text-slate-600">
                <button
                  type="button"
                  onClick={() => onOpen(openRun === run.id ? null : run.id)}
                  className="underline"
                  aria-pressed={openRun === run.id}
                >
                  {new Date(run.created_at).toLocaleString()}
                </button>
                {run.patch ? (
                  <span className="ml-1 rounded bg-amber-100 px-1 py-0.5 text-[10px] text-amber-800">
                    unsaved settings
                  </span>
                ) : null}
              </td>
              <td className="py-2 pr-3">
                <StatusBadge status={run.status} tone={runTone(run.status)} />
              </td>
              <td className="py-2 pr-3 font-mono text-xs text-slate-700">
                {run.status === 'succeeded' ? runHeadline(run) : runStatusLine(run)}
                {runWarnings(run).length > 0 ? (
                  <span className="ml-1 text-amber-700" title={runWarnings(run).join(' ')}>
                    ⚠
                  </span>
                ) : null}
              </td>
              <td className="py-2 pr-3">
                {run.status === 'succeeded' ? (
                  <input
                    type="radio"
                    name="diff-against"
                    aria-label={`Diff against the run of ${new Date(run.created_at).toLocaleString()}`}
                    checked={against === run.id}
                    onChange={() => onAgainst(run.id)}
                  />
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const HEADLINE = [
  ['Chunk recall', 'all', 'chunk', 'recall'],
  ['Chunk precision', 'all', 'chunk', 'precision'],
  ['MRR', 'all', 'chunk', 'mrr'],
  ['Document hit rate', 'all', 'document', 'hit_rate'],
] as const

function RunDetail({ runId }: { runId: string }) {
  const { data: run } = useEvaluationRun(runId)
  if (!run) return null
  const k = dig(run.metrics, 'k') ?? 0
  const warnings = runWarnings(run)
  return (
    <div className="rounded-md border border-slate-200 bg-white p-3" data-testid="run-detail">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-sm font-semibold text-slate-900">
          Run of {new Date(run.created_at).toLocaleString()}
        </h4>
        <span className="text-xs text-slate-500">{runStatusLine(run) || `k = ${k}`}</span>
      </div>
      {warnings.map((warning) => (
        <p
          key={warning}
          className="mt-2 rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900"
        >
          {warning}
        </p>
      ))}
      {run.status === 'succeeded' ? (
        <table className="mt-3 w-full text-left text-xs" data-testid="run-metrics">
          <caption className="sr-only">Headline numbers</caption>
          <thead className="text-slate-500">
            <tr>
              <th scope="col" className="py-1 pr-3 font-medium" />
              <th scope="col" className="py-1 pr-3 font-medium">
                All items
              </th>
              <th scope="col" className="py-1 pr-3 font-medium">
                After budget
              </th>
              <th scope="col" className="py-1 pr-3 font-medium">
                Verified only
              </th>
              <th scope="col" className="py-1 pr-3 font-medium">
                Verified, after budget
              </th>
            </tr>
          </thead>
          <tbody>
            {HEADLINE.map(([label, population, level, field]) => (
              <tr key={label} className="border-t border-slate-100">
                <th scope="row" className="py-1 pr-3 font-medium text-slate-700">
                  {label}
                </th>
                <td className="py-1 pr-3 font-mono">
                  {metric(dig(run.metrics, population, level, field))}
                </td>
                <td className="py-1 pr-3 font-mono">
                  {metric(dig(run.metrics, population, `${level}_injected`, field))}
                </td>
                <td className="py-1 pr-3 font-mono">
                  {metric(dig(run.metrics, 'verified', level, field))}
                </td>
                <td className="py-1 pr-3 font-mono">
                  {metric(dig(run.metrics, 'verified', `${level}_injected`, field))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {run.status === 'succeeded' ? <ItemResults run={run} /> : null}
    </div>
  )
}

function ItemResults({ run }: { run: EvaluationRunResponse }) {
  const [open, setOpen] = useState<string | null>(null)
  const results = run.results as ItemResult[]
  return (
    <ul className="mt-3 divide-y divide-slate-100" data-testid="item-results">
      {results.map((result) => {
        const rank = result.chunk?.first_rank ?? result.document?.first_rank ?? null
        const relevant = new Set(result.relevant_chunk_ids ?? [])
        const documents = new Set(result.relevant_document_ids ?? [])
        return (
          <li key={result.item_id} className="py-2 text-xs">
            <button
              type="button"
              onClick={() => setOpen(open === result.item_id ? null : result.item_id)}
              className="flex w-full items-baseline justify-between gap-2 text-left"
            >
              <span className="text-slate-800">{result.question}</span>
              <span className="shrink-0 font-mono text-slate-500">
                {result.negative
                  ? result.chunk?.precision === 1
                    ? 'nothing returned ✓'
                    : `${result.retrieved?.length ?? 0} returned ✗`
                  : rank
                    ? `first relevant at ${rank}`
                    : 'missed'}
              </span>
            </button>
            {open === result.item_id ? (
              <ol className="mt-1 space-y-1">
                {(result.retrieved ?? []).map((entry, index) => {
                  const hit =
                    relevant.has(entry.chunk_id) ||
                    (entry.document_id
                      ? documents.has(entry.document_id) && relevant.size === 0
                      : false)
                  return (
                    <li
                      key={entry.chunk_id}
                      className={`rounded px-2 py-1 ${hit ? 'bg-emerald-50 text-emerald-900' : 'bg-slate-50 text-slate-600'}`}
                    >
                      <span className="font-mono">[{index + 1}]</span> {entry.source_name}
                      {entry.page_or_section ? ` · ${entry.page_or_section}` : ''} ·{' '}
                      {entry.score.toFixed(2)}
                      {hit ? ' · relevant' : ''}
                      {entry.injected ? '' : ' · over budget'}
                    </li>
                  )
                })}
                {(result.retrieved ?? []).length === 0 ? (
                  <li className="text-slate-500">Nothing came back above the floor.</li>
                ) : null}
              </ol>
            ) : null}
          </li>
        )
      })}
    </ul>
  )
}

type ItemResult = {
  item_id: string
  question: string
  negative?: boolean
  retrieved?: {
    chunk_id: string
    document_id: string | null
    score: number
    injected: boolean
    source_name: string
    page_or_section: string | null
  }[]
  relevant_chunk_ids?: string[]
  relevant_document_ids?: string[]
  chunk?: { first_rank: number | null; precision: number | null }
  document?: { first_rank: number | null }
}

function DiffView({ runId, against }: { runId: string; against: string }) {
  const { data: diff, isLoading } = useRunDiff(runId, against)
  if (isLoading || !diff) return null
  const configChanges = Object.entries(diff.config_changes)
  return (
    <div className="rounded-md border border-slate-200 bg-white p-3" data-testid="run-diff">
      <h4 className="text-sm font-semibold text-slate-900">
        What changed between {new Date(diff.before.created_at).toLocaleString()} and{' '}
        {new Date(diff.after.created_at).toLocaleString()}
      </h4>
      <ul className="mt-2 text-xs text-slate-700">
        {configChanges.map(([key, [before, after]]) => (
          <li key={key}>
            <code>{key}</code>: {JSON.stringify(before)} → {JSON.stringify(after)}
          </li>
        ))}
        {diff.index_changes.map((change) => (
          <li key={change}>{change}</li>
        ))}
        {configChanges.length === 0 && diff.index_changes.length === 0 ? (
          <li>Same settings, same index. Any difference below is retrieval&apos;s own.</li>
        ) : null}
      </ul>
      <table className="mt-3 w-full text-left text-xs">
        <caption className="sr-only">Metric changes</caption>
        <tbody>
          {diff.metrics.map((delta) => (
            <tr key={delta.name} className="border-t border-slate-100">
              <th scope="row" className="py-1 pr-3 font-medium text-slate-700">
                {delta.name}
              </th>
              <td className="py-1 pr-3 font-mono">{metric(delta.before)}</td>
              <td className="py-1 pr-3 font-mono">{metric(delta.after)}</td>
              <td className="py-1 pr-3">
                <StatusBadge status={formatDelta(delta.change)} tone={deltaTone(delta.change)} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {diff.won.length > 0 || diff.lost.length > 0 ? (
        <div className="mt-3 grid gap-3 sm:grid-cols-2 text-xs">
          <div>
            <p className="font-medium text-emerald-800">Now found ({diff.won.length})</p>
            <ul className="mt-1 space-y-0.5 text-slate-700">
              {diff.won.map((entry) => (
                <li key={String(entry.item_id)}>{String(entry.question)}</li>
              ))}
            </ul>
          </div>
          <div>
            <p className="font-medium text-red-800">Now missed ({diff.lost.length})</p>
            <ul className="mt-1 space-y-0.5 text-slate-700">
              {diff.lost.map((entry) => (
                <li key={String(entry.item_id)}>{String(entry.question)}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  )
}

/** A list response's items, or nothing — a screen must not trust a shape it did not get. */
function listOf<T>(data: { items?: T[] } | undefined): T[] {
  return data && Array.isArray(data.items) ? data.items : []
}
