import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { ApiError } from '@/api/client'
import {
  useCreateModel,
  useModel,
  useTestDraft,
  useTestModel,
  useUpdateModel,
} from '@/api/models'
import type { ModelResponse, ProbeResponse } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { Field, Form, Select, SubmitButton, TextArea, TextInput } from '@/components/Form'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { useToast } from '@/components/Toast'
import { PRESETS, presetById, presetFor } from '@/pages/providerPresets'

const DIALECTS = [
  { value: 'openai', label: 'OpenAI-compatible' },
  // Offered, and refused by the API until task 16 registers the adapter. Hiding it would
  // make "does this gateway support Anthropic?" unanswerable from the screen.
  { value: 'anthropic', label: 'Anthropic (not yet supported)' },
]

const AUTH_TYPES = [
  { value: 'bearer', label: 'Bearer token', hint: 'Authorization: Bearer <key>' },
  { value: 'api_key_header', label: 'API key header', hint: 'x-api-key: <key>' },
  { value: 'azure', label: 'Azure', hint: 'api-key: <key>' },
  { value: 'none', label: 'No authentication', hint: 'Nothing is sent.' },
]

const MAX_SYSTEM_CONTEXT = 8000

type HeaderRow = { key: string; value: string }

type FormState = {
  name: string
  description: string
  baseUrl: string
  dialect: string
  upstreamModelId: string
  authType: string
  headers: HeaderRow[]
  systemContext: string
  defaultParams: string
  timeoutSeconds: number
  //  A string, not a number, because empty is a meaningful value here — it means "I do
  //  not know this model's window", which is a different thing from zero and is what
  //  switches the gateway's overflow guard off.
  contextWindow: string
  enabled: boolean
  scope: string
}

const BLANK: FormState = {
  name: '',
  description: '',
  baseUrl: '',
  dialect: 'openai',
  upstreamModelId: '',
  authType: 'bearer',
  headers: [],
  systemContext: '',
  defaultParams: '{}',
  timeoutSeconds: 60,
  contextWindow: '',
  enabled: true,
  scope: 'org',
}

/**
 * The model editor — one route for create and edit (SPEC §13.1).
 *
 * Three things here are worth stating.
 *
 * **The credential is never loaded.** There is no endpoint that returns one, so the field
 * starts empty even when a key is stored; the hint says one exists. Leaving it blank on
 * save omits it and keeps what is there — the API's `PATCH` rule, mirrored here rather
 * than reimplemented, because sending an empty string would clear it.
 *
 * **Test connection probes whichever configuration it can actually reach.** Typed a key,
 * or auth is off, or nothing is saved yet? Then the values on screen go to the provider.
 * Otherwise the *saved* model is probed, because the stored credential cannot be attached
 * to an arbitrary URL from the browser — that would be a reveal endpoint wearing a
 * different hat. The result panel says which of the two it did.
 *
 * **Validation stays on the server.** The form checks that `default_params` is JSON,
 * because that is a shape the request cannot even carry; every other rule — the parameter
 * allowlist, the name collision, the dialect — comes back as a field error and is
 * rendered where it belongs.
 */
export function ModelFormPage() {
  const { modelId } = useParams<{ modelId: string }>()
  const isNew = modelId === undefined
  const navigate = useNavigate()
  const { user } = useAuth()
  const { notify } = useToast()

  const { data: model, isLoading } = useModel(isNew ? undefined : modelId)
  const create = useCreateModel()
  const update = useUpdateModel(modelId)
  const testSaved = useTestModel()
  const testDraft = useTestDraft()

  const [state, setState] = useState<FormState>(BLANK)
  const [preset, setPreset] = useState('openai')
  const [credential, setCredential] = useState('')
  const [clearCredential, setClearCredential] = useState(false)
  const [paramsError, setParamsError] = useState<string | null>(null)
  const [probe, setProbe] = useState<{ result: ProbeResponse; source: 'draft' | 'saved' } | null>(
    null,
  )

  useEffect(() => {
    if (model) {
      setState(stateOf(model))
      setPreset(presetFor(model.base_url))
    }
  }, [model])

  const writes = can(user, 'resources:write')
  const platform = can(user, 'platform:administer')
  const editable = isNew ? writes : Boolean(model?.editable) && writes
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setState((current) => ({ ...current, [key]: value }))

  const applyPreset = (id: string) => {
    setPreset(id)
    const chosen = presetById(id)
    if (!chosen) return
    setState((current) => ({
      ...current,
      baseUrl: chosen.baseUrl,
      authType: chosen.authType,
      // Only fill a model id that has not been typed into yet, so choosing a preset
      // after entering one does not silently discard it.
      upstreamModelId: current.upstreamModelId || chosen.modelId,
    }))
  }

  /** The draft can be probed only when the browser holds a usable credential for it. */
  const probesDraft =
    isNew || Boolean(credential) || state.authType === 'none' || !model?.credential.configured

  const saving = create.isPending || update.isPending
  const testing = testSaved.isPending || testDraft.isPending

  if (!isNew && isLoading) return <FullPageSpinner label="Loading the model…" />

  const submit = async () => {
    const params = parseParams(state.defaultParams)
    if (params === undefined) {
      setParamsError('Not valid JSON. It should look like {"temperature": 0.2}.')
      return
    }
    setParamsError(null)

    const body = {
      name: state.name,
      description: state.description || null,
      base_url: state.baseUrl,
      dialect: state.dialect,
      upstream_model_id: state.upstreamModelId,
      auth_type: state.authType,
      extra_headers: headersToObject(state.headers),
      system_context: state.systemContext || null,
      default_params: params,
      timeout_seconds: state.timeoutSeconds,
      //  `null` rather than omitted, so clearing the field is a change the PATCH applies
      //  — the field is genuinely nullable, unlike the rest of this body.
      context_window: state.contextWindow ? Number(state.contextWindow) : null,
      enabled: state.enabled,
    }

    try {
      if (isNew) {
        const created = await create.mutateAsync({
          ...body,
          scope: state.scope,
          ...(credential ? { credential } : {}),
        })
        notify(`${created.name} created.`)
        void navigate(`/models/${created.id}`, { replace: true })
        return
      }
      // Absent keeps the stored credential; `null` clears it. The two are different
      // requests, and only one of them can be spelled with an empty string.
      const changed = credential
        ? { credential }
        : clearCredential
          ? { credential: null }
          : {}
      await update.mutateAsync({ ...body, ...changed })
      setCredential('')
      setClearCredential(false)
      notify('Saved.')
    } catch {
      // Rendered by `Form` from the mutation's error, field by field.
    }
  }

  const runTest = async () => {
    setProbe(null)
    try {
      if (probesDraft) {
        const result = await testDraft.mutateAsync({
          name: state.name || 'draft',
          base_url: state.baseUrl,
          upstream_model_id: state.upstreamModelId,
          dialect: state.dialect,
          auth_type: state.authType,
          credential: credential || null,
          extra_headers: headersToObject(state.headers),
          timeout_seconds: state.timeoutSeconds,
          // Both ignored by the probe — a connection does not care whether the row it
          // would become is enabled, or who would own it — but the body is the create
          // body, which is what makes "test exactly what you are about to save" true.
          enabled: state.enabled,
          scope: state.scope,
        })
        setProbe({ result, source: 'draft' })
      } else {
        const result = await testSaved.mutateAsync(modelId)
        setProbe({ result, source: 'saved' })
      }
    } catch (error) {
      notify(
        error instanceof ApiError ? error.message : 'Could not run the test.',
        'error',
      )
    }
  }

  return (
    <div className="max-w-3xl">
      <header className="mb-6">
        <Link to="/models" className="text-sm text-slate-500 hover:text-slate-700">
          ← Models
        </Link>
        <h1 className="mt-2 text-xl font-semibold text-slate-900">
          {isNew ? 'New model' : model?.name}
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          A provider endpoint plus the credential and defaults used to call it.
        </p>
      </header>

      {!editable && !isNew ? (
        <p className="mb-4 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">
          This model belongs to the global catalog. Only a platform administrator can change
          it — everything below is read-only.
        </p>
      ) : null}

      <Form onSubmit={submit} error={create.error ?? update.error}>
        <fieldset disabled={!editable} className="contents">
          <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
            <h2 className="mb-4 text-sm font-semibold text-slate-900">Provider</h2>

            <Field
              name="preset"
              label="Preset"
              hint="Fills in the base URL and auth style. Every field stays editable."
            >
              {(props) => (
                <Select
                  {...props}
                  value={preset}
                  onChange={(event) => applyPreset(event.target.value)}
                >
                  {PRESETS.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                  <option value="custom">Custom</option>
                </Select>
              )}
            </Field>

            <Field
              name="base_url"
              label="Base URL"
              hint={presetById(preset)?.hint ?? 'Everything before /chat/completions.'}
            >
              {(props) => (
                <TextInput
                  {...props}
                  value={state.baseUrl}
                  onChange={(event) => set('baseUrl', event.target.value)}
                  placeholder="https://api.openai.com/v1"
                />
              )}
            </Field>

            <Field
              name="upstream_model_id"
              label="Upstream model id"
              hint="The provider's own name for the model, e.g. gpt-4o-mini."
            >
              {(props) => (
                <TextInput
                  {...props}
                  value={state.upstreamModelId}
                  onChange={(event) => set('upstreamModelId', event.target.value)}
                />
              )}
            </Field>

            <Field name="dialect" label="Dialect">
              {(props) => (
                <Select
                  {...props}
                  value={state.dialect}
                  onChange={(event) => set('dialect', event.target.value)}
                >
                  {DIALECTS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </section>

          <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
            <h2 className="mb-4 text-sm font-semibold text-slate-900">Authentication</h2>

            <Field
              name="auth_type"
              label="Auth type"
              hint={AUTH_TYPES.find((option) => option.value === state.authType)?.hint}
            >
              {(props) => (
                <Select
                  {...props}
                  value={state.authType}
                  onChange={(event) => set('authType', event.target.value)}
                >
                  {AUTH_TYPES.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </Select>
              )}
            </Field>

            {state.authType === 'none' ? null : (
              <CredentialField
                model={model}
                value={credential}
                onChange={(next) => {
                  setCredential(next)
                  if (next) setClearCredential(false)
                }}
                clearing={clearCredential}
                onClearingChange={setClearCredential}
              />
            )}

            <HeadersField
              rows={state.headers}
              onChange={(rows) => set('headers', rows)}
              disabled={!editable}
            />
          </section>

          <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
            <h2 className="mb-4 text-sm font-semibold text-slate-900">Identity and behaviour</h2>

            <Field name="name" label="Name" hint="How this model is referred to in this UI.">
              {(props) => (
                <TextInput
                  {...props}
                  value={state.name}
                  onChange={(event) => set('name', event.target.value)}
                  placeholder="production-gpt-4o"
                />
              )}
            </Field>

            <Field name="description" label="Description">
              {(props) => (
                <TextInput
                  {...props}
                  value={state.description}
                  onChange={(event) => set('description', event.target.value)}
                />
              )}
            </Field>

            <Field
              name="system_context"
              label="System context"
              hint={`Prepended to every request through this model. ${state.systemContext.length} / ${MAX_SYSTEM_CONTEXT} characters.`}
            >
              {(props) => (
                <TextArea
                  {...props}
                  rows={4}
                  maxLength={MAX_SYSTEM_CONTEXT}
                  value={state.systemContext}
                  onChange={(event) => set('systemContext', event.target.value)}
                />
              )}
            </Field>

            <Field
              name="default_params"
              label="Default parameters"
              hint={
                paramsError ??
                'JSON. Applied first, then the gateway’s overrides, then the client’s request.'
              }
            >
              {(props) => (
                <TextArea
                  {...props}
                  rows={4}
                  invalid={props.invalid || Boolean(paramsError)}
                  value={state.defaultParams}
                  onChange={(event) => set('defaultParams', event.target.value)}
                />
              )}
            </Field>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field
                name="timeout_seconds"
                label="Timeout (seconds)"
                hint="How long to wait for the first byte."
              >
                {(props) => (
                  <TextInput
                    {...props}
                    type="number"
                    min={1}
                    max={600}
                    value={state.timeoutSeconds}
                    onChange={(event) => set('timeoutSeconds', Number(event.target.value))}
                  />
                )}
              </Field>

              <Field
                name="context_window"
                label="Context window (tokens)"
                hint="Optional. When set, a gateway will not inject retrieved documents that would overflow it. Left blank, no such check runs."
              >
                {(props) => (
                  <TextInput
                    {...props}
                    type="number"
                    min={256}
                    placeholder="not set"
                    value={state.contextWindow}
                    onChange={(event) => set('contextWindow', event.target.value)}
                  />
                )}
              </Field>
            </div>

            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={state.enabled}
                onChange={(event) => set('enabled', event.target.checked)}
                className="rounded border-slate-300"
              />
              Enabled — a disabled model is skipped, and its gateways answer 503 saying so.
            </label>

            {isNew && platform ? (
              <div className="mt-4">
                <Field
                  name="scope"
                  label="Availability"
                  hint="A catalog model is offered to every organization, and only you can edit it."
                >
                  {(props) => (
                    <Select
                      {...props}
                      value={state.scope}
                      onChange={(event) => set('scope', event.target.value)}
                    >
                      <option value="org">This organization only</option>
                      <option value="global">Global catalog</option>
                    </Select>
                  )}
                </Field>
              </div>
            ) : null}
          </section>
        </fieldset>

        <div className="flex flex-wrap items-center gap-3">
          {editable ? (
            <SubmitButton busy={saving} className="w-auto">
              {isNew ? 'Create model' : 'Save changes'}
            </SubmitButton>
          ) : null}
          <button
            type="button"
            onClick={() => void runTest()}
            disabled={testing || (!probesDraft && isNew)}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
          >
            {testing ? 'Testing…' : 'Test connection'}
          </button>
        </div>
      </Form>

      {probe ? <ProbeResult result={probe.result} source={probe.source} /> : null}
    </div>
  )
}

// ---------------------------------------------------------------------------

/**
 * Write-only, and it says so.
 *
 * There is no endpoint that returns a stored credential, so the input starts empty and
 * the hint is all the evidence that one exists. Leaving it blank keeps what is stored;
 * clearing is a separate, explicit act, because the two cannot both be "empty".
 */
function CredentialField({
  model,
  value,
  onChange,
  clearing,
  onClearingChange,
}: {
  model: ModelResponse | undefined
  value: string
  onChange: (value: string) => void
  clearing: boolean
  onClearingChange: (clearing: boolean) => void
}) {
  const configured = Boolean(model?.credential.configured)
  const hint = model?.credential.hint

  return (
    <>
      <Field
        name="credential"
        label="Credential"
        hint={
          configured
            ? `A credential is stored${hint ? ` (${hint})` : ''}. Leave blank to keep it — it cannot be shown again.`
            : 'Sent to the provider on every request. Encrypted at rest and never returned.'
        }
      >
        {(props) => (
          <TextInput
            {...props}
            type="password"
            autoComplete="off"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            placeholder={configured ? 'Enter a new value to replace it' : 'sk-…'}
          />
        )}
      </Field>

      {configured ? (
        <label className="mb-4 -mt-2 flex items-center gap-2 text-sm text-slate-600">
          <input
            type="checkbox"
            checked={clearing}
            disabled={Boolean(value)}
            onChange={(event) => onClearingChange(event.target.checked)}
            className="rounded border-slate-300"
          />
          Remove the stored credential
        </label>
      ) : null}
    </>
  )
}

function HeadersField({
  rows,
  onChange,
  disabled,
}: {
  rows: HeaderRow[]
  onChange: (rows: HeaderRow[]) => void
  disabled: boolean
}) {
  const update = (index: number, patch: Partial<HeaderRow>) =>
    onChange(rows.map((row, at) => (at === index ? { ...row, ...patch } : row)))

  return (
    <Field
      name="extra_headers"
      label="Extra headers"
      hint="Sent with every request, applied last. Use the credential field for the API key."
    >
      {(props) => (
        <div id={props.id} aria-describedby={props.describedBy}>
          {rows.map((row, index) => (
            <div key={index} className="mb-2 flex gap-2">
              <input
                aria-label={`Header ${index + 1} name`}
                value={row.key}
                onChange={(event) => update(index, { key: event.target.value })}
                placeholder="x-example"
                className="w-1/3 rounded-md border border-slate-300 px-3 py-2 font-mono text-sm"
              />
              <input
                aria-label={`Header ${index + 1} value`}
                value={row.value}
                onChange={(event) => update(index, { value: event.target.value })}
                className="flex-1 rounded-md border border-slate-300 px-3 py-2 font-mono text-sm"
              />
              <button
                type="button"
                disabled={disabled}
                onClick={() => onChange(rows.filter((_, at) => at !== index))}
                className="rounded-md border border-slate-300 px-2 text-xs font-medium text-red-700"
              >
                Remove
              </button>
            </div>
          ))}
          <button
            type="button"
            disabled={disabled}
            onClick={() => onChange([...rows, { key: '', value: '' }])}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700"
          >
            Add header
          </button>
        </div>
      )}
    </Field>
  )
}

/**
 * Green with a latency, or red with the provider's own words.
 *
 * The upstream error is shown verbatim on purpose: "401 invalid_api_key" is the whole
 * answer, and paraphrasing it into "authentication failed" loses the part somebody can
 * search for.
 */
function ProbeResult({
  result,
  source,
}: {
  result: ProbeResponse
  source: 'draft' | 'saved'
}) {
  const tone = result.ok
    ? 'border-emerald-200 bg-emerald-50 text-emerald-900'
    : 'border-red-200 bg-red-50 text-red-900'

  return (
    <div role="status" className={`mt-6 rounded-md border p-4 text-sm ${tone}`}>
      <p className="font-medium">
        {result.ok
          ? `OK, ${result.latency_ms} ms`
          : `Failed${result.upstream_status ? ` — HTTP ${result.upstream_status}` : ''}`}
      </p>
      {result.error_message ? (
        <p className="mt-1 break-words font-mono text-xs">{result.error_message}</p>
      ) : null}
      {result.ok && result.model_echo ? (
        <p className="mt-1 text-xs">The provider answered as {result.model_echo}.</p>
      ) : null}
      <p className="mt-2 text-xs opacity-80">
        {source === 'saved'
          ? 'Tested the saved configuration. Enter a credential to test unsaved changes.'
          : 'Tested the values on screen.'}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------

function stateOf(model: ModelResponse): FormState {
  return {
    name: model.name,
    description: model.description ?? '',
    baseUrl: model.base_url,
    dialect: model.dialect,
    upstreamModelId: model.upstream_model_id,
    authType: model.auth_type,
    headers: Object.entries(model.extra_headers).map(([key, value]) => ({ key, value })),
    systemContext: model.system_context ?? '',
    defaultParams: JSON.stringify(model.default_params, null, 2),
    timeoutSeconds: model.timeout_seconds,
    contextWindow: model.context_window === null ? '' : String(model.context_window),
    enabled: model.enabled,
    scope: model.scope,
  }
}

function headersToObject(rows: HeaderRow[]): Record<string, string> {
  return Object.fromEntries(rows.filter((row) => row.key.trim()).map((row) => [row.key, row.value]))
}

/** `undefined` means "not JSON" — distinct from `{}`, which is a valid empty object. */
function parseParams(text: string): Record<string, unknown> | undefined {
  const trimmed = text.trim()
  if (!trimmed) return {}
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return undefined
    return parsed as Record<string, unknown>
  } catch {
    return undefined
  }
}
