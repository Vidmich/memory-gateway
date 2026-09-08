import { useEffect, useState } from 'react'

import { usePlatformSettings, useUpdatePlatformSettings } from '@/api/platform'
import type { PlatformSettingsPatch, PlatformSettingsResponse } from '@/api/types'
import { Field, Form, SubmitButton, Select, TextInput } from '@/components/Form'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { useToast } from '@/components/Toast'
import { EmbeddingChange } from '@/pages/EmbeddingChange'

/**
 * Platform → Settings (SPEC §13.1, §15.3).
 *
 * Everything an operator can change without a deploy. Two things about this screen are
 * deliberate and would otherwise look like omissions.
 *
 * **A section says where its value came from.** "From the environment" and "set to the
 * same value" look identical on screen and are not the same state: the first changes when
 * a pod is redeployed. The badge is the difference.
 *
 * **The embedding model is not saved by this form's Save button.** It has its own panel,
 * its own confirmation, and its own cost estimate, because pressing it starts a re-embed
 * of every collection on the platform. Folding it into a form with six other fields is how
 * somebody spends a four-figure embedding bill while changing a storage cap.
 */
export function PlatformSettingsPage() {
  const { data, isLoading } = usePlatformSettings()
  if (isLoading || !data) return <FullPageSpinner label="Loading platform settings…" />
  return <PlatformSettingsForm settings={data} />
}

type Draft = {
  maxBodyDays: string
  maxMetadataDays: string
  maxFileBytes: string
  quotaBytes: string
  ceilingRpm: string
  ceilingTpm: string
  ceilingRpd: string
  ceilingConcurrent: string
  distillationModelId: string
  logRetentionDays: string
  logMetadataRetentionDays: string
  logRequestBody: string
}

function draftOf(data: PlatformSettingsResponse): Draft {
  const settings = data.settings
  return {
    maxBodyDays: text(settings.retention?.max_body_days),
    maxMetadataDays: text(settings.retention?.max_metadata_days),
    maxFileBytes: text(settings.storage?.max_file_bytes),
    quotaBytes: text(settings.storage?.quota_bytes),
    ceilingRpm: text(settings.limits?.global_model_ceilings?.requests_per_minute),
    ceilingTpm: text(settings.limits?.global_model_ceilings?.tokens_per_minute),
    ceilingRpd: text(settings.limits?.global_model_ceilings?.requests_per_day),
    ceilingConcurrent: text(settings.limits?.global_model_ceilings?.concurrent_requests),
    distillationModelId: text(settings.distillation?.model_id),
    logRetentionDays: text(settings.logging?.retention_days),
    logMetadataRetentionDays: text(settings.logging?.metadata_retention_days),
    logRequestBody: settings.logging?.log_request_body === false ? 'off' : 'on',
  }
}

/** A stored value as an input's string. Narrowed to the scalars these fields hold, so a
 *  section that grows an object cannot silently render `[object Object]` in a text box. */
function text(value: string | number | null | undefined): string {
  return value === null || value === undefined ? '' : String(value)
}

/** Empty means "no ceiling", which is a different value from zero and has to stay null. */
function optionalNumber(value: string): number | null {
  const trimmed = value.trim()
  return trimmed === '' ? null : Number(trimmed)
}

function PlatformSettingsForm({ settings }: { settings: PlatformSettingsResponse }) {
  const update = useUpdatePlatformSettings()
  const { notify } = useToast()
  const [draft, setDraft] = useState<Draft>(() => draftOf(settings))

  // Seeded, not synchronised: a background refetch must not overwrite what somebody is
  // halfway through typing. The dependency is the settings *object* rather than the
  // response, because the response also carries a reindex whose progress changes every
  // few seconds — and re-seeding on that would wipe the form while it polls.
  const stored = settings.settings
  useEffect(() => {
    setDraft(draftOf({ ...settings, settings: stored }))
    // eslint-disable-next-line react-hooks/exhaustive-deps -- see above: seeding, not syncing
  }, [stored])

  const set = (key: keyof Draft, value: string) =>
    setDraft((current) => ({ ...current, [key]: value }))

  const body = (): PlatformSettingsPatch => ({
    retention: {
      max_body_days: optionalNumber(draft.maxBodyDays),
      max_metadata_days: optionalNumber(draft.maxMetadataDays),
    },
    storage: {
      max_file_bytes: Number(draft.maxFileBytes) || undefined,
      quota_bytes: optionalNumber(draft.quotaBytes),
    },
    limits: {
      global_model_ceilings: {
        requests_per_minute: optionalNumber(draft.ceilingRpm),
        tokens_per_minute: optionalNumber(draft.ceilingTpm),
        requests_per_day: optionalNumber(draft.ceilingRpd),
        concurrent_requests: optionalNumber(draft.ceilingConcurrent),
      },
    },
    distillation: { model_id: draft.distillationModelId.trim() || null },
    logging: {
      retention_days: Number(draft.logRetentionDays) || undefined,
      metadata_retention_days: Number(draft.logMetadataRetentionDays) || undefined,
      log_request_body: draft.logRequestBody === 'on',
    },
  })

  const save = () =>
    update.mutate(body(), { onSuccess: () => notify('Platform settings saved.') })

  // Warned about rather than confirmed with a dialog. Lowering a ceiling is not
  // destructive *now* — it caps gateways and tonight's retention pass acts on the new
  // number — so the honest thing is to say what will happen, next to the button, rather
  // than to make somebody type a word before a change that is still reversible until 03:05.
  const proposedBodyDays = optionalNumber(draft.maxBodyDays)
  const currentBodyDays = settings.settings.retention?.max_body_days
  const lowersRetention =
    proposedBodyDays !== null &&
    proposedBodyDays < (currentBodyDays ?? Number.POSITIVE_INFINITY)

  return (
    <div className="max-w-3xl">
      <header className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Platform settings</h1>
        <p className="mt-1 text-sm text-slate-500">
          Configuration for every organization on this deployment. A section with no stored
          value is running on its environment variable, and will change if that variable
          does.
        </p>
      </header>

      <EmbeddingChange settings={settings} />

      <div className="mt-8 rounded-lg border border-slate-200 bg-white p-6">
        <Form onSubmit={save} error={update.error}>
          <Section
            title="Retention ceilings"
            source={sourceOf(settings, 'retention')}
            hint="The longest an organization may keep data. An organization can be stricter, never longer — a gateway asking for more is capped, tonight, by the retention job."
          >
            <Field
              name="retention.max_body_days"
              label="Maximum body retention (days)"
              hint="Blank means the platform sets no ceiling."
            >
              {(props) => (
                <TextInput
                  {...props}
                  inputMode="numeric"
                  value={draft.maxBodyDays}
                  onChange={(event) => set('maxBodyDays', event.target.value)}
                />
              )}
            </Field>
            <Field name="retention.max_metadata_days" label="Maximum metadata retention (days)">
              {(props) => (
                <TextInput
                  {...props}
                  inputMode="numeric"
                  value={draft.maxMetadataDays}
                  onChange={(event) => set('maxMetadataDays', event.target.value)}
                />
              )}
            </Field>
          </Section>

          <Section
            title="Logging defaults"
            source={sourceOf(settings, 'logging')}
            hint="What a new gateway starts from. Changing these does not touch gateways that already exist."
          >
            <Field name="logging.retention_days" label="Default body retention (days)">
              {(props) => (
                <TextInput
                  {...props}
                  inputMode="numeric"
                  value={draft.logRetentionDays}
                  onChange={(event) => set('logRetentionDays', event.target.value)}
                />
              )}
            </Field>
            <Field
              name="logging.metadata_retention_days"
              label="Default metadata retention (days)"
            >
              {(props) => (
                <TextInput
                  {...props}
                  inputMode="numeric"
                  value={draft.logMetadataRetentionDays}
                  onChange={(event) => set('logMetadataRetentionDays', event.target.value)}
                />
              )}
            </Field>
            <Field name="logging.log_request_body" label="Capture request bodies by default">
              {(props) => (
                <Select
                  {...props}
                  value={draft.logRequestBody}
                  onChange={(event) => set('logRequestBody', event.target.value)}
                >
                  <option value="on">Yes</option>
                  <option value="off">No</option>
                </Select>
              )}
            </Field>
          </Section>

          <Section
            title="Rate-limit ceilings"
            source={sourceOf(settings, 'limits')}
            hint="Maxima for a gateway routed at a model on the operator's own credential. Blank means unlimited."
          >
            <Field name="limits.requests_per_minute" label="Requests per minute">
              {(props) => (
                <TextInput
                  {...props}
                  inputMode="numeric"
                  value={draft.ceilingRpm}
                  onChange={(event) => set('ceilingRpm', event.target.value)}
                />
              )}
            </Field>
            <Field name="limits.tokens_per_minute" label="Tokens per minute">
              {(props) => (
                <TextInput
                  {...props}
                  inputMode="numeric"
                  value={draft.ceilingTpm}
                  onChange={(event) => set('ceilingTpm', event.target.value)}
                />
              )}
            </Field>
            <Field name="limits.requests_per_day" label="Requests per day">
              {(props) => (
                <TextInput
                  {...props}
                  inputMode="numeric"
                  value={draft.ceilingRpd}
                  onChange={(event) => set('ceilingRpd', event.target.value)}
                />
              )}
            </Field>
            <Field name="limits.concurrent_requests" label="Concurrent requests">
              {(props) => (
                <TextInput
                  {...props}
                  inputMode="numeric"
                  value={draft.ceilingConcurrent}
                  onChange={(event) => set('ceilingConcurrent', event.target.value)}
                />
              )}
            </Field>
          </Section>

          <Section title="Storage" source={sourceOf(settings, 'storage')}>
            <Field name="storage.max_file_bytes" label="Maximum file size (bytes)">
              {(props) => (
                <TextInput
                  {...props}
                  inputMode="numeric"
                  value={draft.maxFileBytes}
                  onChange={(event) => set('maxFileBytes', event.target.value)}
                />
              )}
            </Field>
            <Field
              name="storage.quota_bytes"
              label="Per-organization quota (bytes)"
              hint="Blank means unlimited."
            >
              {(props) => (
                <TextInput
                  {...props}
                  inputMode="numeric"
                  value={draft.quotaBytes}
                  onChange={(event) => set('quotaBytes', event.target.value)}
                />
              )}
            </Field>
          </Section>

          <Section
            title="Distillation"
            source={sourceOf(settings, 'distillation')}
            hint="The global catalog model organizations distil with when they have not chosen one."
          >
            <Field name="distillation.model_id" label="Default distillation model id">
              {(props) => (
                <TextInput
                  {...props}
                  value={draft.distillationModelId}
                  onChange={(event) => set('distillationModelId', event.target.value)}
                  placeholder="Blank to disable the platform default"
                />
              )}
            </Field>
          </Section>

          {lowersRetention ? (
            <p
              role="status"
              className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
            >
              Gateways keeping bodies for longer than {proposedBodyDays} days will be capped,
              and tonight&rsquo;s retention pass will delete anything already past the new
              ceiling. That deletion cannot be undone.
            </p>
          ) : null}

          <SubmitButton busy={update.isPending} className="w-auto">
            Save settings
          </SubmitButton>
        </Form>
      </div>

    </div>
  )
}

function sourceOf(settings: PlatformSettingsResponse, key: string): string | null {
  if (settings.from_environment?.includes(key)) return 'From the environment'
  const entry = settings.attribution?.find((item) => item.key === key)
  if (!entry) return null
  const who = entry.updated_by_label ?? 'a superadmin'
  return `Set by ${who} on ${new Date(entry.updated_at).toLocaleDateString()}`
}

function Section({
  title,
  hint,
  source,
  children,
}: {
  title: string
  hint?: string
  source: string | null
  children: React.ReactNode
}) {
  return (
    <section className="mb-8 border-b border-slate-100 pb-6 last:border-0">
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {source ? <span className="text-xs text-slate-500">{source}</span> : null}
      </div>
      {hint ? <p className="mb-4 text-sm text-slate-600">{hint}</p> : null}
      {children}
    </section>
  )
}
