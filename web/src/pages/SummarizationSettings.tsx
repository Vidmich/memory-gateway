import { useEffect, useState } from 'react'

import { useModels } from '@/api/models'
import { useSummarizationSettings, useUpdateSummarizationSettings } from '@/api/summarization'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { Field, Form, Select, SubmitButton } from '@/components/Form'
import { useToast } from '@/components/Toast'
import { summarizationModelSummary } from '@/pages/summarization'

/**
 * **Settings → Summarization model** (task 102), beside the distillation model.
 *
 * One select. A connector with no model of its own uses this; an organization that leaves
 * it empty summarizes with its distillation model; and with neither, the platform default.
 * The sentence under the select says which link of that chain is currently answering,
 * because a blank select over a working fallback would read as "nothing configured".
 */
export function SummarizationSettings() {
  const { user } = useAuth()
  const editable = can(user, 'org:administer')
  const { data: settings, isLoading } = useSummarizationSettings()
  const { data: models } = useModels(null)
  const update = useUpdateSummarizationSettings()
  const { notify } = useToast()
  const [modelId, setModelId] = useState<string | null>(null)

  useEffect(() => {
    if (settings && modelId === null) setModelId(settings.config.model_id ?? '')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings])

  if (isLoading || !settings || modelId === null) {
    return (
      <section className="mt-8 rounded-lg border border-slate-200 bg-white p-6">
        <h2 className="text-sm font-semibold text-slate-900">Document summaries</h2>
        <p className="mt-2 text-sm text-slate-500">Loading…</p>
      </section>
    )
  }

  const changed = (modelId || null) !== (settings.config.model_id ?? null)

  return (
    <section className="mt-8 rounded-lg border border-slate-200 bg-white p-6">
      <h2 className="text-sm font-semibold text-slate-900">Document summaries</h2>
      <p className="mb-4 mt-1 max-w-2xl text-sm text-slate-600">
        A connector can have a model summarize each document as it is ingested. This is the
        model those connectors use unless they name one of their own.
      </p>

      <Form
        onSubmit={() => {
          update.mutate({ model_id: modelId || null }, { onSuccess: () => notify('Saved.') })
        }}
        error={update.error}
      >
        <Field
          name="summarization.model_id"
          label="Summarization model"
          hint="Empty means the distillation model, then the platform default."
        >
          {(props) => (
            <Select
              {...props}
              value={modelId}
              disabled={!editable}
              onChange={(event) => setModelId(event.target.value)}
            >
              <option value="">Same as distillation</option>
              {(models?.items ?? []).map((model) => (
                <option key={model.id} value={model.id}>
                  {model.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <p className="-mt-2 mb-4 text-sm text-slate-500">{summarizationModelSummary(settings)}</p>

        {editable ? (
          <SubmitButton busy={update.isPending} disabled={!changed} className="w-auto">
            Save summarization model
          </SubmitButton>
        ) : null}
      </Form>
    </section>
  )
}
