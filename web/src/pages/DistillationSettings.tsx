import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { useDistillationSettings, useUpdateDistillation } from '@/api/distillation'
import { useModels } from '@/api/models'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { Field, Form, Select, SubmitButton, TextInput } from '@/components/Form'
import { useToast } from '@/components/Toast'
import {
  capWarning,
  debounceSummary,
  distillationBody,
  distillationChanged,
  distillationForm,
  modelSummary,
  offSummary,
  usageSummary,
  type DistillationForm,
} from '@/pages/distillation'

/**
 * Settings → Organization → Memory write-back (SPEC §6.4).
 *
 * Everything here is per *organization* rather than per gateway, and the section says why:
 * an end user reaches an organization through however many endpoints it has, so a bound on
 * how much may be known about one person is not a bound if it is set per endpoint.
 *
 * Three things on this form are unlike the gateway's memory settings.
 *
 * **The model selector is a spending decision.** It is the only setting here that costs
 * money per conversation, so the hint says "a cheap model is the point" and the summary
 * underneath says which model would actually be used — including "the platform's", which
 * an empty selector otherwise reads as "none".
 *
 * **The daily cap shows what it has spent.** A guard that stops memory silently is worse
 * than no guard; the usage line comes from the same table the cap is enforced against, so
 * the number on screen is the number that will refuse the next pass.
 *
 * **The off switch says what "off" means.** Facts already stored are still recalled —
 * turning write-back off stops learning, not remembering — and nobody guesses that from a
 * checkbox.
 */
export function DistillationSettings() {
  const { user } = useAuth()
  const editable = can(user, 'org:administer')
  const { data: settings, isLoading } = useDistillationSettings()
  const { data: models } = useModels(null)
  const update = useUpdateDistillation()
  const { notify } = useToast()

  const [form, setForm] = useState<DistillationForm | null>(null)

  // Seeded from the server rather than kept in sync with it: a change in another tab
  // should not overwrite what this user is currently typing.
  useEffect(() => {
    if (settings && form === null) setForm(distillationForm(settings))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings])

  if (isLoading || !settings || !form) {
    return (
      <section className="mt-8 rounded-lg border border-slate-200 bg-white p-6">
        <h2 className="text-sm font-semibold text-slate-900">Memory write-back</h2>
        <p className="mt-2 text-sm text-slate-500">Loading…</p>
      </section>
    )
  }

  const set = <K extends keyof DistillationForm>(key: K, value: DistillationForm[K]) =>
    setForm((current) => (current ? { ...current, [key]: value } : current))

  const capNote = capWarning(settings)
  const offNote = offSummary(form)

  return (
    <section className="mt-8 rounded-lg border border-slate-200 bg-white p-6">
      <h2 className="text-sm font-semibold text-slate-900">Memory write-back</h2>
      <p className="mb-4 mt-1 max-w-2xl text-sm text-slate-600">
        Conversations through your gateways are read in the background and turned into
        durable facts about the person who had them. What is learned appears in{' '}
        <Link to="/memory" className="font-medium underline">
          Memory
        </Link>
        , and is injected into that person’s later prompts.
      </p>

      <Form
        onSubmit={() => {
          update.mutate(distillationBody(form), {
            onSuccess: () => notify('Saved.'),
          })
        }}
        error={update.error}
      >
        <label className="mb-4 flex items-start gap-2">
          <input
            type="checkbox"
            checked={form.enabled}
            disabled={!editable}
            onChange={(event) => set('enabled', event.target.checked)}
            className="mt-1 rounded border-slate-300"
          />
          <span>
            <span className="text-sm font-medium text-slate-700">
              Learn from conversations
            </span>
            <span className="block text-sm text-slate-500">
              {offNote ??
                'Every gateway with body logging and distillation enabled feeds memory.'}
            </span>
          </span>
        </label>

        <Field
          name="distillation.model_id"
          label="Distillation model"
          hint="A cheap model is the point — this reads whole transcripts, not questions."
        >
          {(props) => (
            <Select
              {...props}
              value={form.modelId}
              disabled={!editable}
              onChange={(event) => set('modelId', event.target.value)}
            >
              <option value="">Platform default</option>
              {(models?.items ?? []).map((model) => (
                <option key={model.id} value={model.id}>
                  {model.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <p className="-mt-2 mb-4 text-sm text-slate-500">{modelSummary(settings)}</p>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            name="distillation.debounce_seconds"
            label="Wait before distilling"
            hint="Seconds of quiet before a conversation is read."
          >
            {(props) => (
              <TextInput
                {...props}
                type="number"
                min={5}
                max={3600}
                value={form.debounceSeconds}
                disabled={!editable}
                onChange={(event) => set('debounceSeconds', event.target.value)}
              />
            )}
          </Field>
          <Field
            name="distillation.dedupe_threshold"
            label="Duplicate threshold"
            hint="Above this similarity, a new fact reinforces an existing one instead of being stored beside it."
          >
            {(props) => (
              <TextInput
                {...props}
                type="number"
                step="0.01"
                min={0.5}
                max={1}
                value={form.dedupeThreshold}
                disabled={!editable}
                onChange={(event) => set('dedupeThreshold', event.target.value)}
              />
            )}
          </Field>
        </div>
        <p className="-mt-2 mb-4 text-sm text-slate-500">{debounceSummary(form)}</p>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            name="distillation.max_facts_per_user"
            label="Facts kept per person"
            hint="Past this, the lowest-scoring facts are forgotten — retracted ones first. A memory of more than a few hundred sentences has stopped being memory."
          >
            {(props) => (
              <TextInput
                {...props}
                type="number"
                min={1}
                max={10000}
                value={form.maxFactsPerUser}
                disabled={!editable}
                onChange={(event) => set('maxFactsPerUser', event.target.value)}
              />
            )}
          </Field>
          <Field
            name="distillation.per_user_daily_cap"
            label="Passes per person per day"
            hint="Bounds a very chatty person. The wait above already coalesces a burst; this bounds a conversation that goes on all day."
          >
            {(props) => (
              <TextInput
                {...props}
                type="number"
                min={0}
                max={10000}
                value={form.perUserDailyCap}
                disabled={!editable}
                onChange={(event) => set('perUserDailyCap', event.target.value)}
              />
            )}
          </Field>
        </div>

        <Field
          name="distillation.daily_call_cap"
          label="Model calls per day"
          hint="The cost guard for the whole organization. Zero removes it."
        >
          {(props) => (
            <TextInput
              {...props}
              type="number"
              min={0}
              max={1000000}
              value={form.dailyCallCap}
              disabled={!editable}
              onChange={(event) => set('dailyCallCap', event.target.value)}
            />
          )}
        </Field>
        <p className="-mt-2 mb-4 text-sm text-slate-500">{usageSummary(settings)}</p>
        {capNote ? (
          <p
            role="status"
            className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
          >
            {capNote}
          </p>
        ) : null}

        {editable ? (
          <SubmitButton
            busy={update.isPending}
            disabled={!distillationChanged(form, settings)}
            className="w-auto"
          >
            Save write-back settings
          </SubmitButton>
        ) : (
          <p className="text-sm text-slate-500">
            Your role can view these settings but not change them.
          </p>
        )}
      </Form>
    </section>
  )
}
