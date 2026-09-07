import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import { ApiError } from '@/api/client'
import {
  useCreateGateway,
  useDeleteGateway,
  useGateway,
  useTestGateway,
  useUpdateGateway,
} from '@/api/gateways'
import { useModels } from '@/api/models'
import type { GatewayResponse, GatewayTestResponse } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { CopyButton } from '@/components/CopyButton'
import { Field, Form, Select, SubmitButton, TextArea, TextInput } from '@/components/Form'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { useToast } from '@/components/Toast'
import { GatewayKeys } from '@/pages/GatewayKeys'
import { suggestSlug } from '@/pages/slug'
import { useUnsavedChanges } from '@/pages/useUnsavedChanges'

const MAX_SYSTEM_CONTEXT = 32_000

const ROUTING_MODES = [
  { value: 'single', label: 'Single model' },
  // Present and disabled, so "can this gateway fail over?" is answerable from the screen
  // rather than from the changelog. Task 08 turns them on.
  { value: 'failover', label: 'Failover chain (coming soon)' },
  { value: 'ab_split', label: 'A/B split (coming soon)' },
]

type FormState = {
  name: string
  slug: string
  description: string
  enabled: boolean
  routingMode: string
  modelId: string
  systemContext: string
  paramOverrides: string
  lockedParams: string
}

const BLANK: FormState = {
  name: '',
  slug: '',
  description: '',
  enabled: true,
  routingMode: 'single',
  modelId: '',
  systemContext: '',
  paramOverrides: '{}',
  lockedParams: '{}',
}

/**
 * The gateway editor — one route for create and edit (SPEC §13.1).
 *
 * The section order is the information architecture every later task extends: Identity,
 * Routing, Memory (10), Prompt, Logging (07), Limits (14), Keys. The three unbuilt ones
 * render a real, styled empty state naming the release that fills them, rather than being
 * hidden — a section that appears later moves everything below it, and a demo that shows
 * the shape of the finished screen is worth more than one that hides its gaps.
 *
 * Three details worth stating.
 *
 * **The slug is create-only.** After the first save it is read-only with the reason next
 * to it, because it is in a URL customers have deployed. The escape hatch is *Clone*,
 * which opens the create form pre-filled from this gateway — a new endpoint with a new
 * slug, leaving the old one serving until its callers have moved.
 *
 * **The assembled-prompt preview is rendered locally**, from the same layering rule the
 * server uses, and labelled as a preview. The authoritative answer is the Test panel,
 * which returns the prompt the provider actually received.
 *
 * **Locked parameters are a separate box from overrides**, not a checkbox on each row.
 * They are different policies — a default the client can beat, and a value it cannot —
 * and two labelled inputs say that more clearly than a grid of toggles.
 */
export function GatewayFormPage() {
  const { gatewayId } = useParams<{ gatewayId: string }>()
  const [search] = useSearchParams()
  const isNew = gatewayId === undefined
  //  The escape hatch for the immutable slug: a new gateway pre-filled from an existing
  //  one. Everything except the slug and the keys, because neither can be copied — one is
  //  the thing being changed, and the other cannot be read back.
  const cloneOf = isNew ? search.get('clone') : null
  const navigate = useNavigate()
  const { user } = useAuth()
  const { notify } = useToast()

  const { data: gateway, isLoading } = useGateway(isNew ? undefined : gatewayId)
  const { data: source } = useGateway(cloneOf ?? undefined)
  const { data: models } = useModels(null)
  const create = useCreateGateway()
  const update = useUpdateGateway(gatewayId)
  const remove = useDeleteGateway()

  const [state, setState] = useState<FormState>(BLANK)
  const [slugTouched, setSlugTouched] = useState(false)
  const [saved, setSaved] = useState<FormState>(BLANK)
  const [paramsError, setParamsError] = useState<string | null>(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  useEffect(() => {
    if (gateway) {
      const loaded = stateOf(gateway)
      setState(loaded)
      setSaved(loaded)
      setSlugTouched(true)
    }
  }, [gateway])

  useEffect(() => {
    if (source) {
      // A blank slug and a name that says what happened, so the one field that has to be
      // different is the one field that is empty.
      setState({ ...stateOf(source), slug: '', name: `${source.name} (copy)` })
    }
  }, [source])

  const writes = can(user, 'resources:write')
  const dirty = useMemo(() => !sameState(state, saved), [state, saved])
  const { confirmLeave } = useUnsavedChanges(dirty && writes)

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setState((current) => ({ ...current, [key]: value }))

  if (!isNew && isLoading) return <FullPageSpinner label="Loading the gateway…" />

  const submit = async () => {
    const overrides = parseJsonObject(state.paramOverrides)
    const locked = parseJsonObject(state.lockedParams)
    if (overrides === undefined || locked === undefined) {
      setParamsError('Not valid JSON. It should look like {"temperature": 0.2}.')
      return
    }
    setParamsError(null)

    const body = {
      name: state.name,
      description: state.description || null,
      enabled: state.enabled,
      routing_mode: state.routingMode,
      model_id: state.modelId || null,
      system_context: state.systemContext || null,
      param_overrides: overrides,
      locked_params: locked,
    }

    try {
      if (isNew) {
        const created = await create.mutateAsync({ ...body, slug: state.slug })
        notify(`${created.name} created.`)
        setSaved(state)
        void navigate(`/gateways/${created.id}`, { replace: true })
        return
      }
      // No `slug`: the API refuses it, and sending one the user cannot have changed would
      // turn every save into a 422.
      await update.mutateAsync(body)
      setSaved(state)
      notify('Saved. The next request uses the new configuration.')
    } catch {
      // Rendered by `Form` from the mutation's error, field by field.
    }
  }

  const leave = (to: string) => {
    if (confirmLeave()) void navigate(to)
  }

  return (
    <div className="max-w-3xl">
      <header className="mb-6">
        <button
          type="button"
          onClick={() => leave('/gateways')}
          className="text-sm text-slate-500 hover:text-slate-700"
        >
          ← Gateways
        </button>
        <h1 className="mt-2 text-xl font-semibold text-slate-900">
          {isNew ? 'New gateway' : gateway?.name}
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          A published endpoint: its own URL, its own keys, its own prompt.
        </p>
      </header>

      {gateway ? <EndpointBanner gateway={gateway} /> : null}

      <Form onSubmit={submit} error={create.error ?? update.error}>
        <fieldset disabled={!writes} className="contents">
          {/* 1. Identity ------------------------------------------------- */}
          <Section title="Identity" subtitle="What this endpoint is called, and where it lives.">
            <Field name="name" label="Name" hint="Shown in this UI. Change it whenever you like.">
              {(props) => (
                <TextInput
                  {...props}
                  value={state.name}
                  placeholder="Support Bot"
                  onChange={(event) => {
                    const name = event.target.value
                    setState((current) => ({
                      ...current,
                      name,
                      // Suggest a slug until the field is touched. After that it is the
                      // user's, because it becomes a URL they have to live with.
                      slug: isNew && !slugTouched ? suggestSlug(name) : current.slug,
                    }))
                  }}
                />
              )}
            </Field>

            <Field
              name="slug"
              label="Slug"
              hint={
                isNew
                  ? 'Lower-case letters, digits and hyphens. It becomes part of the endpoint URL and cannot be changed afterwards.'
                  : 'Fixed after creation: it is in the URL your clients already use. Clone this gateway to get one with a different slug.'
              }
            >
              {(props) => (
                <TextInput
                  {...props}
                  value={state.slug}
                  readOnly={!isNew}
                  placeholder="acme-support"
                  onChange={(event) => {
                    setSlugTouched(true)
                    set('slug', event.target.value)
                  }}
                />
              )}
            </Field>

            {isNew && state.slug ? (
              <p className="-mt-2 mb-4 font-mono text-xs text-slate-500">
                …/g/{state.slug}/v1
              </p>
            ) : null}

            <Field name="description" label="Description">
              {(props) => (
                <TextInput
                  {...props}
                  value={state.description}
                  onChange={(event) => set('description', event.target.value)}
                />
              )}
            </Field>

            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={state.enabled}
                onChange={(event) => set('enabled', event.target.checked)}
                className="rounded border-slate-300"
              />
              Enabled — a disabled gateway answers 403 and says so.
            </label>
          </Section>

          {/* 2. Routing -------------------------------------------------- */}
          <Section title="Routing" subtitle="Where completions are sent.">
            <Field
              name="routing_mode"
              label="Mode"
              hint="Failover and A/B split arrive with routing modes. Until then a gateway has exactly one target."
            >
              {(props) => (
                <Select
                  {...props}
                  value={state.routingMode}
                  onChange={(event) => set('routingMode', event.target.value)}
                >
                  {ROUTING_MODES.map((option) => (
                    <option
                      key={option.value}
                      value={option.value}
                      disabled={option.value !== 'single'}
                    >
                      {option.label}
                    </option>
                  ))}
                </Select>
              )}
            </Field>

            <Field
              name="model_id"
              label="Model"
              hint={
                <>
                  Your own models and the shared catalog. Add one under{' '}
                  <Link to="/models" className="underline">
                    Models
                  </Link>
                  .
                </>
              }
            >
              {(props) => (
                <Select
                  {...props}
                  value={state.modelId}
                  onChange={(event) => set('modelId', event.target.value)}
                >
                  <option value="">No model — requests will fail</option>
                  {(models?.items ?? []).map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.name}
                      {model.organization_id ? '' : ' (shared)'}
                      {model.enabled ? '' : ' — disabled'}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </Section>

          {/* 3. Memory --------------------------------------------------- */}
          <Placeholder
            title="Memory"
            what="Which connectors this gateway may read, retrieval knobs, and a Try retrieval box that shows exactly which chunks and facts would be injected."
          />

          {/* 4. Prompt --------------------------------------------------- */}
          <Section
            title="Prompt"
            subtitle="What is prepended to every request, and which generation parameters this endpoint sets."
          >
            <Field
              name="system_context"
              label="System context"
              hint={`Prepended to every request through this gateway. ${state.systemContext.length} / ${MAX_SYSTEM_CONTEXT} characters.`}
            >
              {(props) => (
                <TextArea
                  {...props}
                  rows={5}
                  maxLength={MAX_SYSTEM_CONTEXT}
                  value={state.systemContext}
                  placeholder="You are Acme's support assistant. Be concise."
                  onChange={(event) => set('systemContext', event.target.value)}
                />
              )}
            </Field>

            <Field
              name="param_overrides"
              label="Parameter defaults"
              hint={
                paramsError ??
                'JSON. Applied over the model’s defaults, and beaten by whatever the client sends.'
              }
            >
              {(props) => (
                <TextArea
                  {...props}
                  rows={3}
                  invalid={props.invalid || Boolean(paramsError)}
                  value={state.paramOverrides}
                  onChange={(event) => set('paramOverrides', event.target.value)}
                />
              )}
            </Field>

            <Field
              name="locked_params"
              label="Locked parameters"
              hint="JSON. Applied last, so a client cannot change these. Responses carry X-Gateway-Locked-Params when one was overridden."
            >
              {(props) => (
                <TextArea
                  {...props}
                  rows={3}
                  invalid={props.invalid || Boolean(paramsError)}
                  value={state.lockedParams}
                  onChange={(event) => set('lockedParams', event.target.value)}
                />
              )}
            </Field>

            <PromptPreview
              gatewayContext={state.systemContext}
              modelContext={
                (models?.items ?? []).find((model) => model.id === state.modelId)
                  ?.system_context ?? ''
              }
            />
          </Section>

          {/* 5. Logging -------------------------------------------------- */}
          <Placeholder
            title="Logging"
            what="Which parts of a request are captured, how long they are kept, redaction patterns, and whether transcripts feed conversation memory."
          />

          {/* 6. Limits --------------------------------------------------- */}
          <Placeholder
            title="Limits"
            what="Requests and tokens per minute, concurrent requests, and daily quotas — per gateway and per end user."
          />
        </fieldset>

        <div className="mb-8 flex flex-wrap items-center gap-3">
          {writes ? (
            <SubmitButton busy={create.isPending || update.isPending} className="w-auto">
              {isNew ? 'Create gateway' : 'Save changes'}
            </SubmitButton>
          ) : null}
          {dirty && writes ? (
            <span className="text-sm text-amber-700">Unsaved changes</span>
          ) : null}
        </div>
      </Form>

      {/* 7. Keys ------------------------------------------------------- */}
      {gatewayId ? <GatewayKeys gatewayId={gatewayId} /> : null}

      {gateway ? <TestPanel gateway={gateway} /> : null}

      {gateway && writes ? (
        <section className="mb-8 rounded-lg border border-red-200 bg-white p-5">
          <h2 className="text-sm font-semibold text-slate-900">Danger zone</h2>
          <p className="mt-1 text-sm text-slate-500">
            Deleting a gateway takes its keys with it. Every deployed client stops working
            immediately, and the slug can be claimed by anyone afterwards.
          </p>
          <div className="mt-4 flex gap-2">
            <button
              type="button"
              onClick={() => leave(`/gateways/new?clone=${gateway.id}`)}
              className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              Clone with a new slug
            </button>
            <button
              type="button"
              onClick={() => setConfirmingDelete(true)}
              className="rounded-md border border-red-300 bg-white px-3 py-2 text-sm font-medium text-red-700 hover:bg-red-50"
            >
              Delete gateway
            </button>
          </div>

          <ConfirmDialog
            open={confirmingDelete}
            title="Delete this gateway?"
            description={
              <>
                Every application using <code className="text-xs">{gateway.endpoint_url}</code>{' '}
                stops working on its next request, and its keys are deleted with it. This
                cannot be undone.
              </>
            }
            resourceName={gateway.slug}
            confirmLabel="Delete gateway"
            busy={remove.isPending}
            onConfirm={() =>
              remove.mutate(gateway.id, {
                onSuccess: () => {
                  notify(`${gateway.name} deleted.`)
                  setSaved(state)
                  void navigate('/gateways')
                },
                onError: (error) =>
                  notify(
                    error instanceof ApiError ? error.message : 'Could not delete it.',
                    'error',
                  ),
              })
            }
            onCancel={() => setConfirmingDelete(false)}
          />
        </section>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------

function Section({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle: string
  children: React.ReactNode
}) {
  return (
    <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
      <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
      <p className="mb-4 mt-1 text-sm text-slate-500">{subtitle}</p>
      {children}
    </section>
  )
}

/**
 * A real, styled empty state naming what will fill it.
 *
 * Hidden sections would make the editor look finished and then move everything below them
 * when they arrive. Naming the contents also makes the gap reviewable: if a later task
 * ships something different from what this promised, the difference is visible here.
 */
function Placeholder({ title, what }: { title: string; what: string }) {
  return (
    <section className="mb-8 rounded-lg border border-dashed border-slate-300 bg-slate-50 p-5">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold text-slate-500">{title}</h2>
        <span className="rounded bg-slate-200 px-1.5 py-0.5 text-xs font-medium text-slate-600">
          Coming soon
        </span>
      </div>
      <p className="mt-1 text-sm text-slate-500">{what}</p>
    </section>
  )
}

function EndpointBanner({ gateway }: { gateway: GatewayResponse }) {
  return (
    <div className="mb-6 rounded-lg border border-slate-200 bg-slate-50 p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Endpoint</p>
      <div className="mt-1 flex items-center gap-2">
        <code className="min-w-0 flex-1 break-all font-mono text-sm text-slate-800">
          {gateway.endpoint_url}
        </code>
        <CopyButton value={gateway.endpoint_url} label="Copy URL" />
      </div>
      <p className="mt-2 text-xs text-slate-500">
        Use it as <code>base_url</code> in any OpenAI client, with a key from below as the
        API key, and <code>{gateway.slug}</code> as the model.
      </p>
    </div>
  )
}

/**
 * The prompt as it will be assembled, rendered from the same rule the server uses:
 * model context, then gateway context, then whatever the client sends.
 *
 * Labelled a preview, and it says where the authoritative answer is. Tasks 10 and 12 add
 * layers between these two, and a preview claiming to be exact would then be wrong.
 */
function PromptPreview({
  gatewayContext,
  modelContext,
}: {
  gatewayContext: string
  modelContext: string
}) {
  const layers = [modelContext, gatewayContext].map((layer) => layer.trim()).filter(Boolean)

  return (
    <div className="mt-2 rounded-md border border-slate-200 bg-slate-50 p-3">
      <p className="text-xs font-medium text-slate-600">Assembled system message</p>
      {layers.length === 0 ? (
        <p className="mt-1 text-xs text-slate-500">
          Nothing is prepended. The client&apos;s own messages go through untouched.
        </p>
      ) : (
        <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-slate-700">
          {layers.join('\n\n')}
        </pre>
      )}
      <p className="mt-2 text-xs text-slate-500">
        A preview. Test gateway below shows the prompt the provider actually received.
      </p>
    </div>
  )
}

/**
 * Type a message, see the assembled prompt, the answer, and where the time went.
 *
 * The most useful debugging affordance in the product until task 07 has request logs, and
 * it goes through the real proxy path — so a green result here means a customer's request
 * would take the same route with the same prompt.
 */
function TestPanel({ gateway }: { gateway: GatewayResponse }) {
  const [message, setMessage] = useState('Hello! Reply with one short sentence.')
  const [result, setResult] = useState<GatewayTestResponse | null>(null)
  const test = useTestGateway(gateway.id)
  const { notify } = useToast()

  const run = async () => {
    setResult(null)
    try {
      setResult(await test.mutateAsync({ message }))
    } catch (error) {
      notify(error instanceof ApiError ? error.message : 'Could not run the test.', 'error')
    }
  }

  return (
    <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
      <h2 className="text-sm font-semibold text-slate-900">Test gateway</h2>
      <p className="mb-4 mt-1 text-sm text-slate-500">
        Sends a real completion through this gateway — same prompt assembly, same model,
        same credentials — and shows you everything it did.
      </p>

      <label htmlFor="probe-message" className="block text-sm font-medium text-slate-700">
        Message
      </label>
      <TextArea
        id="probe-message"
        invalid={false}
        describedBy={undefined}
        rows={2}
        value={message}
        onChange={(event) => setMessage(event.target.value)}
      />

      <button
        type="button"
        onClick={() => void run()}
        disabled={test.isPending || !message.trim()}
        className="mt-3 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
      >
        {test.isPending ? 'Testing…' : 'Send test message'}
      </button>

      {result ? <TestResult result={result} /> : null}
    </section>
  )
}

function TestResult({ result }: { result: GatewayTestResponse }) {
  const tone = result.ok
    ? 'border-emerald-200 bg-emerald-50 text-emerald-900'
    : 'border-red-200 bg-red-50 text-red-900'

  return (
    <div role="status" className={`mt-4 rounded-md border p-4 text-sm ${tone}`}>
      <p className="font-medium">
        {result.ok
          ? `OK, ${result.total_ms} ms`
          : `Failed${result.upstream_status ? ` — HTTP ${result.upstream_status}` : ''}`}
      </p>
      {result.error_message ? (
        <p className="mt-1 break-words font-mono text-xs">{result.error_message}</p>
      ) : null}

      {result.ok ? (
        <p className="mt-1 text-xs">
          {result.upstream_ms} ms upstream, {Math.max(0, result.total_ms - result.upstream_ms)} ms
          in the gateway
          {result.model_name ? ` · ${result.model_name}` : ''}
        </p>
      ) : null}

      {result.locked_overrides?.length ? (
        <p className="mt-1 text-xs">
          Locked by this gateway: {result.locked_overrides.join(', ')}
        </p>
      ) : null}

      {result.assembled_prompt.length > 0 ? (
        <div className="mt-3">
          <p className="text-xs font-medium">Assembled prompt</p>
          <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-black/10 bg-white/60 p-2 font-mono text-xs">
            {result.assembled_prompt
              .map((entry) => `${entry.role}: ${entry.content}`)
              .join('\n\n')}
          </pre>
        </div>
      ) : null}

      {result.content ? (
        <div className="mt-3">
          <p className="text-xs font-medium">Response</p>
          <p className="mt-1 whitespace-pre-wrap break-words rounded border border-black/10 bg-white/60 p-2 text-xs">
            {result.content}
          </p>
        </div>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------

function stateOf(gateway: GatewayResponse): FormState {
  return {
    name: gateway.name,
    slug: gateway.slug,
    description: gateway.description ?? '',
    enabled: gateway.enabled,
    routingMode: gateway.routing_mode,
    modelId: gateway.targets[0]?.id ?? '',
    systemContext: gateway.system_context ?? '',
    paramOverrides: JSON.stringify(gateway.param_overrides, null, 2),
    lockedParams: JSON.stringify(gateway.locked_params, null, 2),
  }
}

function sameState(left: FormState, right: FormState): boolean {
  return (Object.keys(left) as (keyof FormState)[]).every((key) => left[key] === right[key])
}

/** `undefined` means "not JSON" — distinct from `{}`, which is a valid empty object. */
function parseJsonObject(text: string): Record<string, unknown> | undefined {
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
