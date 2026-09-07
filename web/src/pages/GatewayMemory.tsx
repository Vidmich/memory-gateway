import { useState } from 'react'
import { Link } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { useConnectors } from '@/api/connectors'
import { usePromptPreview, useTryRetrieval } from '@/api/gateways'
import type {
  PromptPreviewResponse,
  RetrievalPreviewResponse,
  RetrievedChunkResponse,
} from '@/api/types'
import { Field, Select, TextInput } from '@/components/Form'
import { StatusBadge } from '@/components/StatusBadge'
import {
  attachable,
  connectorLabel,
  contextUsage,
  formatScore,
  memoryBody,
  memoryProblem,
  memoryWarning,
  retrievalSummary,
  type MemoryForm,
} from '@/pages/memory'

/**
 * Section 3 of the gateway editor: which documents this endpoint can read (SPEC §13.1).
 *
 * Four decisions worth stating.
 *
 * **Try retrieval is the point of the screen, so it is on the screen** — not behind a
 * tab, not below the save button. Tuning a score floor by saving, sending traffic and
 * reading the request log is a loop measured in minutes that also changes what live
 * callers get between attempts; this one is measured in seconds and changes nothing.
 *
 * **The knobs are sent unsaved.** The preview requests carry the current form state as a
 * partial `memory_config`, merged server-side by the same function the save uses. So the
 * numbers you are looking at are the numbers being tried, and a value the preview accepts
 * is a value the form can save.
 *
 * **A dropped chunk is shown, struck through, with its score.** The interesting failure
 * is not "nothing was retrieved" — it is "the right passage was retrieved, ranked third,
 * and fell off the end of the token budget". Hiding dropped chunks would hide exactly the
 * case the budget is worth tuning for.
 *
 * **The empty states name their cause.** Four different things produce zero chunks —
 * no connectors, nothing indexed, nothing above the floor, retrieval broken — and they
 * need four different next actions, so `outcome` is rendered rather than a row count.
 */
export function MemorySection({
  gatewayId,
  form,
  stored,
  onChange,
}: {
  /** Undefined while the gateway is being created: there is nothing to retrieve against
   *  until it has been saved once, and the previews say so rather than erroring. */
  gatewayId: string | undefined
  form: MemoryForm
  /** The saved configuration, for the Try-retrieval caveat when the form is dirty. */
  stored: MemoryForm
  onChange: (form: MemoryForm) => void
}) {
  const { data: connectors } = useConnectors()
  const problem = memoryProblem(form)
  const warning = memoryWarning(form)
  const options = attachable(connectors?.items ?? [])

  const set = <K extends keyof MemoryForm>(key: K, value: MemoryForm[K]) =>
    onChange({ ...form, [key]: value })

  const toggle = (id: string) =>
    set(
      'connectorIds',
      form.connectorIds.includes(id)
        ? form.connectorIds.filter((value) => value !== id)
        : [...form.connectorIds, id],
    )

  return (
    <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
      <h2 className="text-sm font-semibold text-slate-900">Memory</h2>
      <p className="mb-4 mt-1 text-sm text-slate-500">
        Which of your documents this endpoint may read, and how much of them ends up in
        each prompt.
      </p>

      <fieldset className="mb-4">
        <legend className="mb-1 text-sm font-medium text-slate-700">Connectors</legend>
        <p className="mb-2 text-xs text-slate-500">
          Only connectors in this organization. With none attached, this gateway does no
          retrieval at all and costs nothing extra per request.
        </p>
        {options.length === 0 ? (
          <p className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-500">
            No connectors yet.{' '}
            <Link to="/connectors" className="font-medium text-slate-700 underline">
              Create one
            </Link>{' '}
            and upload some documents first.
          </p>
        ) : (
          <ul className="space-y-1">
            {options.map((connector) => (
              <li key={connector.id}>
                <label className="flex items-start gap-2 rounded-md border border-slate-200 p-2 text-sm text-slate-700 hover:bg-slate-50">
                  <input
                    type="checkbox"
                    checked={form.connectorIds.includes(connector.id)}
                    onChange={() => toggle(connector.id)}
                    className="mt-0.5 rounded border-slate-300"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="font-medium">{connector.name}</span>
                    <span className="block text-xs text-slate-500">
                      {connectorLabel(connector)}
                    </span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
        )}
      </fieldset>

      {warning ? (
        <p className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          {warning}
        </p>
      ) : null}

      <div className="mb-4 grid gap-4 sm:grid-cols-3">
        <Field
          name="memory_config.doc_top_k"
          label="Chunks to retrieve"
          hint="How many are fetched before the score floor and the token budget cut it down."
        >
          {(props) => (
            <TextInput
              {...props}
              type="number"
              min={1}
              max={100}
              value={form.docTopK}
              onChange={(event) => set('docTopK', event.target.value)}
            />
          )}
        </Field>
        <Field
          name="memory_config.doc_min_score"
          label="Minimum score"
          hint="Cosine similarity, 0 to 1. Raise it to cut noise; too high and nothing is ever retrieved."
        >
          {(props) => (
            <TextInput
              {...props}
              type="number"
              step="0.01"
              min={0}
              max={1}
              value={form.docMinScore}
              onChange={(event) => set('docMinScore', event.target.value)}
            />
          )}
        </Field>
        <Field
          name="memory_config.doc_max_tokens"
          label="Token budget"
          hint="A hard cap on the whole injected block. Chunks are dropped from the lowest score up."
        >
          {(props) => (
            <TextInput
              {...props}
              type="number"
              min={0}
              value={form.docMaxTokens}
              onChange={(event) => set('docMaxTokens', event.target.value)}
            />
          )}
        </Field>
      </div>

      <div className="mb-4 grid gap-4 sm:grid-cols-2">
        <Field
          name="memory_config.query_strategy"
          label="What to search for"
          hint="Dense search on the user's words. Follow-ups like “what about the second one?” retrieve poorly whichever you pick — query rewriting is not in this release."
        >
          {(props) => (
            <Select
              {...props}
              value={form.queryStrategy}
              onChange={(event) => set('queryStrategy', event.target.value)}
            >
              <option value="last_user_message">The last user message</option>
              <option value="last_n_turns">The last few user turns, joined</option>
            </Select>
          )}
        </Field>
        {form.queryStrategy === 'last_n_turns' ? (
          <Field
            name="memory_config.query_n_turns"
            label="Turns to include"
            hint="User turns only. Assistant replies are longer than the question and would dominate the search."
          >
            {(props) => (
              <TextInput
                {...props}
                type="number"
                min={1}
                max={20}
                value={form.queryNTurns}
                onChange={(event) => set('queryNTurns', event.target.value)}
              />
            )}
          </Field>
        ) : null}
      </div>

      <div className="mb-4 grid gap-4 sm:grid-cols-2">
        <Field
          name="memory_config.retrieval_timeout_ms"
          label="Retrieval timeout"
          hint="Milliseconds. Every request through this gateway waits at most this long for its documents."
        >
          {(props) => (
            <TextInput
              {...props}
              type="number"
              min={50}
              max={5000}
              value={form.retrievalTimeoutMs}
              onChange={(event) => set('retrievalTimeoutMs', event.target.value)}
            />
          )}
        </Field>
        <Field
          name="memory_config.on_retrieval_error"
          label="If retrieval fails"
          hint={
            form.onRetrievalError === 'fail_closed'
              ? 'The request is refused with a 503. Right when an ungrounded answer would be worse than no answer.'
              : 'The request is served without documents. Right when a partial answer beats an outage.'
          }
        >
          {(props) => (
            <Select
              {...props}
              value={form.onRetrievalError}
              onChange={(event) => set('onRetrievalError', event.target.value)}
            >
              <option value="fail_open">Answer anyway, without documents</option>
              <option value="fail_closed">Refuse the request</option>
            </Select>
          )}
        </Field>
      </div>

      {problem ? <p className="mb-4 text-sm text-red-700">{problem}</p> : null}

      <TryRetrieval gatewayId={gatewayId} form={form} stored={stored} disabled={problem !== null} />
    </section>
  )
}

/**
 * The tuning loop: a question, the chunks it would inject, and the prompt they land in.
 *
 * One box drives both previews because they answer the same question at two zoom levels,
 * and asking somebody to type the query twice is how the two end up disagreeing.
 */
function TryRetrieval({
  gatewayId,
  form,
  stored,
  disabled,
}: {
  gatewayId: string | undefined
  form: MemoryForm
  stored: MemoryForm
  disabled: boolean
}) {
  const [query, setQuery] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [retrieval, setRetrieval] = useState<RetrievalPreviewResponse | null>(null)
  const [prompt, setPrompt] = useState<PromptPreviewResponse | null>(null)
  const tryRetrieval = useTryRetrieval(gatewayId)
  const previewPrompt = usePromptPreview(gatewayId)

  const body = { query: query.trim(), memory_config: memoryBody(form) }
  const ready = Boolean(gatewayId) && query.trim().length > 0 && !disabled
  //  Shown only when it is true, and it is true often: the previews send the *form*, so
  //  what they report is what saving would do rather than what the live endpoint does.
  const unsaved = JSON.stringify(memoryBody(form)) !== JSON.stringify(memoryBody(stored))

  const run = async (which: 'retrieval' | 'prompt') => {
    setError(null)
    try {
      if (which === 'retrieval') {
        setPrompt(null)
        setRetrieval(await tryRetrieval.mutateAsync(body))
      } else {
        const result = await previewPrompt.mutateAsync(body)
        setPrompt(result)
        setRetrieval(result.retrieval)
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'That could not be run.')
    }
  }

  return (
    <div className="mt-6 rounded-md border border-slate-200 bg-slate-50 p-4">
      <h3 className="text-sm font-semibold text-slate-900">Try retrieval</h3>
      <p className="mb-3 mt-0.5 text-xs text-slate-500">
        Ask something these documents should answer. Nothing is sent to the model and
        nothing is saved.
      </p>

      <label htmlFor="try-retrieval-query" className="sr-only">
        Question
      </label>
      <input
        id="try-retrieval-query"
        value={query}
        placeholder="How do I request a refund?"
        onChange={(event) => setQuery(event.target.value)}
        className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
      />

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={!ready || tryRetrieval.isPending}
          onClick={() => void run('retrieval')}
          className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {tryRetrieval.isPending ? 'Searching…' : 'Try retrieval'}
        </button>
        <button
          type="button"
          disabled={!ready || previewPrompt.isPending}
          onClick={() => void run('prompt')}
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
        >
          {previewPrompt.isPending ? 'Assembling…' : 'Show the whole prompt'}
        </button>
        {gatewayId === undefined ? (
          <span className="text-xs text-slate-500">Save the gateway first.</span>
        ) : unsaved ? (
          <span className="text-xs text-amber-700">
            Using the settings above, which are not saved yet.
          </span>
        ) : null}
      </div>

      {error ? (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {error}
        </p>
      ) : null}

      {retrieval ? <RetrievalResult preview={retrieval} /> : null}
      {prompt ? <PromptResult preview={prompt} /> : null}
    </div>
  )
}

function RetrievalResult({ preview }: { preview: RetrievalPreviewResponse }) {
  const summary = retrievalSummary(preview)

  return (
    <div className="mt-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={preview.outcome} tone={summary.tone} />
        <span className="text-sm text-slate-700">{summary.message}</span>
        <span className="text-xs text-slate-500">{preview.latency_ms} ms</span>
      </div>

      {preview.query && preview.chunks.length === 0 ? (
        <p className="mt-2 font-mono text-xs text-slate-500">searched for: {preview.query}</p>
      ) : null}

      {preview.chunks.length > 0 ? (
        <ol className="mt-3 space-y-2">
          {preview.chunks.map((chunk) => (
            <ChunkRow key={chunk.id} chunk={chunk} />
          ))}
        </ol>
      ) : null}
    </div>
  )
}

function ChunkRow({ chunk }: { chunk: RetrievedChunkResponse }) {
  return (
    <li
      className={`rounded-md border p-3 ${
        chunk.injected ? 'border-slate-200 bg-white' : 'border-slate-200 bg-slate-100 opacity-70'
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-2 text-xs">
        <span className="rounded bg-slate-900 px-1.5 py-0.5 font-mono text-white tabular-nums">
          {formatScore(chunk.score)}
        </span>
        <span className="min-w-0 flex-1 truncate font-medium text-slate-800">
          {chunk.source_name}
          {chunk.page_or_section ? (
            <span className="font-normal text-slate-500"> · {chunk.page_or_section}</span>
          ) : null}
        </span>
        <span className="tabular-nums text-slate-500">{chunk.tokens} tokens</span>
        {chunk.injected ? null : (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-800">
            over budget
          </span>
        )}
      </div>
      <p className="mt-2 line-clamp-4 whitespace-pre-wrap break-words text-xs text-slate-700">
        {chunk.text}
      </p>
    </li>
  )
}

/**
 * The assembled system message, layer by layer.
 *
 * Colour-coded by layer rather than by role, because every one of these is the same
 * `system` message by the time it goes upstream — the layering is the thing that is
 * invisible on the wire and worth drawing.
 */
function PromptResult({ preview }: { preview: PromptPreviewResponse }) {
  const usage = contextUsage(preview.total_tokens, preview.context_window)
  const filled = preview.layers.filter((layer) => layer.text)

  return (
    <div className="mt-4 rounded-md border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-xs font-semibold text-slate-900">Assembled system message</h4>
        <p className="text-xs text-slate-500">
          {preview.total_tokens} tokens
          {preview.context_window ? (
            <>
              {' '}
              of {preview.context_window.toLocaleString()}
              {usage === null ? null : ` · ${usage}%`}
            </>
          ) : (
            <>
              {' '}
              · {preview.model_name ?? 'this model'} has no context window set, so no
              overflow guard runs
            </>
          )}
        </p>
      </div>

      {preview.overflowed ? (
        <p className="mt-2 rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
          This message already fills the context window, so no memory was injected. The
          request would still be sent — without its documents.
        </p>
      ) : null}

      {filled.length === 0 ? (
        <p className="mt-2 text-xs text-slate-500">
          Nothing is prepended. The client&apos;s own messages go through untouched.
        </p>
      ) : (
        <ol className="mt-2 space-y-2">
          {filled.map((layer) => (
            <li key={layer.name} className={`rounded border p-2 ${LAYER_TONE[layer.name] ?? ''}`}>
              <div className="mb-1 flex items-baseline justify-between gap-2 text-[10px] font-medium uppercase tracking-wide">
                <span>{layer.label}</span>
                <span className="tabular-nums">{layer.tokens} tokens</span>
              </div>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-xs">
                {layer.text}
              </pre>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/** One colour per layer, matching the drawer's violet for anything the gateway added. */
const LAYER_TONE: Record<string, string> = {
  'model.system_context': 'border-slate-200 bg-slate-50 text-slate-700',
  'gateway.system_context': 'border-sky-200 bg-sky-50 text-sky-900',
  documents: 'border-violet-200 bg-violet-50 text-violet-900',
  memory: 'border-emerald-200 bg-emerald-50 text-emerald-900',
  'client.system': 'border-slate-200 bg-white text-slate-700',
}
