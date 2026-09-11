import { useEffect, useMemo, useState } from 'react'

import { useReindexConnector, useUpdateConnector } from '@/api/connectors'
import { useModels } from '@/api/models'
import { resolveRange } from '@/api/monitoring'
import { useSummarizationHealth } from '@/api/summarization'
import type { ConnectorResponse } from '@/api/types'
import { Field, Form, Select, SubmitButton, TextInput } from '@/components/Form'
import { FORMAT_KINDS, reindexScope } from '@/pages/connectors'
import {
  SUMMARY_MODES,
  describeCost,
  effectiveModeFor,
  modeLabel,
  prefixesContext,
  summarizationBody,
  summarizationChanged,
  summarizationCost,
  summarizationForm,
  summarizationProblem,
  summarizationWarning,
} from '@/pages/summarization'

/**
 * **Connectors → Summarization** (task 102): mode, model, caps, and the cost before saving.
 *
 * The cost line is the reason the panel exists as a form rather than a dropdown. `contextual`
 * roughly doubles embedding spend and adds a model call per document; a person turns it on
 * having seen the number, and the number is shown at the moment of choosing rather than on
 * an invoice.
 *
 * The model select shows the inherited fallback greyed — "cheap-summarizer (inherited)" —
 * because a blank would read as "nothing configured" when the chain has in fact resolved
 * one, and "nothing configured" is the state in which summarization cannot run at all.
 */
export function SummarizationPanel({ connector }: { connector: ConnectorResponse }) {
  const update = useUpdateConnector(connector.id)
  const reindex = useReindexConnector(connector.id)
  const { data: models } = useModels(null)
  const [form, setForm] = useState(() => summarizationForm(connector.summarization))

  useEffect(() => {
    setForm(summarizationForm(connector.summarization))
  }, [connector.summarization])

  const changed = summarizationChanged(form, connector.summarization)
  const problem = summarizationProblem(form)
  const warning = summarizationWarning(connector, form)
  const cost = summarizationCost(connector, {
    mode: form.mode,
    max_input_tokens: Number(form.maxInputTokens) || 0,
    max_summary_tokens: Number(form.maxSummaryTokens) || 0,
  })
  const mode = SUMMARY_MODES.find((entry) => entry.value === form.mode)
  const inherited = connector.summary_model
  const overrides = FORMAT_KINDS.filter(
    (kind) => effectiveModeFor(connector.summarization, kind.value) !== connector.summarization.mode,
  )

  const submit = async () => {
    await update.mutateAsync({ summarization: summarizationBody(form) })
  }

  return (
    <Form onSubmit={submit} error={update.error}>
      <Field name="summarization.mode" label="Mode" hint={mode?.hint}>
        {({ id, invalid, describedBy }) => (
          <Select
            id={id}
            invalid={invalid}
            describedBy={describedBy}
            value={form.mode}
            onChange={(event) => setForm({ ...form, mode: event.target.value })}
          >
            {SUMMARY_MODES.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </Select>
        )}
      </Field>

      {form.mode !== 'off' ? (
        <>
          <Field
            name="summarization.model_id"
            label="Summarization model"
            hint="Leave it inherited to use the organization's default, then the distillation model, then the platform's."
          >
            {({ id, invalid, describedBy }) => (
              <Select
                id={id}
                invalid={invalid}
                describedBy={describedBy}
                value={form.modelId}
                onChange={(event) => setForm({ ...form, modelId: event.target.value })}
              >
                <option value="">
                  {inherited
                    ? `${inherited.name} (inherited)`
                    : 'Inherited — nothing configured anywhere'}
                </option>
                {(models?.items ?? []).map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {!inherited && !form.modelId ? (
            <p role="status" className="-mt-2 mb-4 text-sm text-amber-800">
              No summarization model resolves for this connector. Pick one here, under
              Settings, or as the platform default, or every summary will fail.
            </p>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-3">
            <Field name="summarization.max_summary_tokens" label="Summary length (tokens)">
              {({ id, invalid, describedBy }) => (
                <TextInput
                  id={id}
                  type="number"
                  invalid={invalid}
                  describedBy={describedBy}
                  value={form.maxSummaryTokens}
                  onChange={(event) => setForm({ ...form, maxSummaryTokens: event.target.value })}
                />
              )}
            </Field>
            <Field
              name="summarization.max_input_tokens"
              label="Input sent (tokens)"
              hint="The head of the document, and the tail if it fits."
            >
              {({ id, invalid, describedBy }) => (
                <TextInput
                  id={id}
                  type="number"
                  invalid={invalid}
                  describedBy={describedBy}
                  value={form.maxInputTokens}
                  onChange={(event) => setForm({ ...form, maxInputTokens: event.target.value })}
                />
              )}
            </Field>
            <Field
              name="summarization.daily_document_cap"
              label="Daily cap (documents)"
              hint="Empty means no cap."
            >
              {({ id, invalid, describedBy }) => (
                <TextInput
                  id={id}
                  type="number"
                  invalid={invalid}
                  describedBy={describedBy}
                  value={form.dailyDocumentCap}
                  onChange={(event) => setForm({ ...form, dailyDocumentCap: event.target.value })}
                />
              )}
            </Field>
          </div>
        </>
      ) : null}

      {cost ? (
        /* Said once, where the choice is made. */
        <p
          data-testid="summarization-cost"
          className="mb-4 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700"
        >
          {describeCost(cost)}
        </p>
      ) : null}

      {problem ? (
        <p role="alert" className="mb-4 text-sm text-red-600">
          {problem}
        </p>
      ) : null}
      {warning ? (
        <p
          role="status"
          className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
        >
          {warning}
        </p>
      ) : null}

      {connector.reindex_required && prefixesContext(connector.summarization.mode) ? (
        <div className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          <p>
            Every chunk's embedding now depends on the summary, so the stored vectors are
            stale. Reindexing re-embeds {reindexScope(connector)}, reusing each document's
            summary where it already has one.
          </p>
          <button
            type="button"
            onClick={() => reindex.mutate(connector.reindex_formats ?? [])}
            className="mt-2 rounded-md border border-amber-300 bg-white px-3 py-1.5 text-sm font-medium text-amber-900 hover:bg-amber-100"
          >
            {reindex.isPending ? 'Queueing…' : `Re-embed ${reindexScope(connector)}`}
          </button>
        </div>
      ) : null}

      <SubmitButton busy={update.isPending} disabled={!changed || problem !== null}>
        Save summarization
      </SubmitButton>

      {overrides.length > 0 ? (
        <div className="mt-4">
          <p className="mb-1 text-xs font-medium text-slate-600">Per-format overrides</p>
          <ul className="divide-y divide-slate-200 rounded-md border border-slate-200 bg-white text-xs">
            {overrides.map((kind) => (
              <li key={kind.value} className="flex items-center justify-between gap-2 px-2 py-1.5">
                <span className="font-medium text-slate-800">{kind.label}</span>
                <span className="text-slate-600">
                  {modeLabel(effectiveModeFor(connector.summarization, kind.value))}
                  <span className="ml-1 rounded bg-slate-100 px-1 text-slate-500">override</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="mt-4 text-xs text-slate-500">
          Every format is summarized the same way. Per-format overrides are set through the API.
        </p>
      )}

      <ConnectorSpend connectorId={connector.id} />
    </Form>
  )
}

/** This connector's slice of the Monitoring panel's numbers, over the last 24 hours. */
function ConnectorSpend({ connectorId }: { connectorId: string }) {
  const window = useMemo(() => resolveRange('24h'), [])
  const { data } = useSummarizationHealth(window, connectorId)
  if (!data || data.runs === 0) return null
  const tokens = data.tokens_in + data.tokens_out
  return (
    <dl
      data-testid="connector-summarization-spend"
      className="mt-4 grid grid-cols-2 gap-3 rounded-md border border-slate-200 bg-white p-3 text-sm sm:grid-cols-4"
    >
      <Stat label="Summarized (24 h)" value={data.documents.toLocaleString()} />
      <Stat label="Tokens" value={tokens.toLocaleString()} />
      <Stat label="Failed" value={data.failures.toLocaleString()} />
      <Stat label="Waiting on cap" value={data.waiting_documents.toLocaleString()} />
    </dl>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-base font-semibold tabular-nums text-slate-900">{value}</dd>
    </div>
  )
}
