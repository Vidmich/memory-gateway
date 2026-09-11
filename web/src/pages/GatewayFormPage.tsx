import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'

import { ApiError } from '@/api/client'
import {
  useCreateGateway,
  useDeleteGateway,
  useGateway,
  useTestGateway,
  useUpdateGateway,
} from '@/api/gateways'
import { useCalibrations, useModels } from '@/api/models'
import type { GatewayResponse, GatewayTestResponse } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { CopyButton } from '@/components/CopyButton'
import { Field, Form, Select, SubmitButton, TextArea, TextInput } from '@/components/Form'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { useToast } from '@/components/Toast'
import { GatewayKeys } from '@/pages/GatewayKeys'
import { LimitsSection } from '@/pages/GatewayLimits'
import { ObjectAudit } from '@/pages/ObjectAudit'
import { MemorySection } from '@/pages/GatewayMemory'
import { RoutingSection } from '@/pages/GatewayRouting'
import {
  limitProblems,
  limitsBody,
  limitsChanged,
  limitsForm,
  type LimitsForm,
} from '@/pages/limits'
import {
  CITATION_MODES,
  memoryBody,
  memoryChanged,
  memoryForm,
  memoryProblem,
  type MemoryForm,
} from '@/pages/memory'
import { chainBody, chainProblem, rowsOf, sameChain, type ChainRow } from '@/pages/routing'
import { driftingTargets, formatDrift } from '@/pages/tokenizers'
import { suggestSlug } from '@/pages/slug'
import { useUnsavedChanges } from '@/pages/useUnsavedChanges'

const MAX_SYSTEM_CONTEXT = 32_000

type FormState = {
  name: string
  slug: string
  description: string
  enabled: boolean
  routingMode: string
  //  In priority order. Position is what the server stores as `priority`, so the list is
  //  the order and there is no separate number to keep in step with it.
  targets: ChainRow[]
  systemContext: string
  paramOverrides: string
  lockedParams: string
  //  Its own sub-object rather than eight more flat fields, because the Memory section
  //  passes the whole thing to Try retrieval as an unsaved patch and a flat state would
  //  have to be reassembled at every call site.
  memory: MemoryForm
  logRequestBody: boolean
  logAssembledPrompt: boolean
  logResponseBody: boolean
  retentionDays: string
  metadataRetentionDays: string
  //  One pattern per line. A JSON array would be the field's literal shape, and would
  //  also mean every backslash in a regular expression has to be doubled — which is how
  //  a card-number pattern silently stops matching.
  redactionPatterns: string
  enableDistillation: boolean
  //  Its own sub-object for the same reason as `memory`: the Limits section reads the
  //  gateway scope and the per-end-user scope as one shape, and eight more flat fields
  //  would be reassembled at every call site.
  limits: LimitsForm
}

const BLANK: FormState = {
  name: '',
  slug: '',
  description: '',
  enabled: true,
  routingMode: 'single',
  targets: [],
  systemContext: '',
  paramOverrides: '{}',
  lockedParams: '{}',
  memory: memoryForm({} as GatewayResponse['memory_config']),
  logRequestBody: true,
  logAssembledPrompt: true,
  logResponseBody: true,
  retentionDays: '30',
  metadataRetentionDays: '365',
  redactionPatterns: '',
  enableDistillation: true,
  limits: limitsForm(undefined),
}

/**
 * The gateway editor — one route for create and edit (SPEC §13.1).
 *
 * The section order is the information architecture every task since 06 has extended:
 * Identity, Routing, Memory (10), Prompt, Logging (07), Limits (14), Keys. Each arrived
 * as a styled empty state naming the release that would fill it, rather than being
 * hidden, because a section that appears later moves everything below it. Task 14 filled
 * the last of them, so there are none left.
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
  const routingProblem = chainProblem(state.routingMode, state.targets)
  const memoryIssue = memoryProblem(state.memory)
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
      // The general form, always. `model_id` remains in the API for scripts written
      // against task 06, but a form that can express a chain should not also be sending
      // the shorthand — the two together are a 422.
      targets: chainBody(state.targets),
      system_context: state.systemContext || null,
      param_overrides: overrides,
      locked_params: locked,
      // Both blobs are sent as partial objects and deep-merged server-side, so each
      // section owns its own keys and cannot wipe the conversation-memory half that
      // task 12 adds beside them.
      memory_config: memoryBody(state.memory),
      logging_config: {
        log_request_body: state.logRequestBody,
        log_assembled_prompt: state.logAssembledPrompt,
        log_response_body: state.logResponseBody,
        retention_days: Number(state.retentionDays) || 1,
        metadata_retention_days: Number(state.metadataRetentionDays) || 1,
        redaction_patterns: state.redactionPatterns
          .split(NEWLINE)
          .map((line) => line.trim())
          .filter(Boolean),
        enable_distillation: state.enableDistillation,
      },
      // SPEC §11. Sent whole rather than as a patch of the fields that changed, because
      // an empty input *is* a value here — it means "unlimited" — and a partial body
      // could not express clearing a limit.
      limits: limitsBody(state.limits),
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
          <RoutingSection
            mode={state.routingMode}
            rows={state.targets}
            models={models?.items ?? []}
            onMode={(mode) => set('routingMode', mode)}
            onRows={(targets) => set('targets', targets)}
          />
          <DriftWarning targets={state.targets} models={models?.items ?? []} />

          {/* 3. Memory --------------------------------------------------- */}
          <MemorySection
            gatewayId={gateway?.id}
            form={state.memory}
            stored={saved.memory}
            onChange={(memory) => set('memory', memory)}
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

            {/* Task 100. The prompt already tells the model to cite; this is what the
                gateway does with the citations on the way back. It is stored in the memory
                blob and edited here because the decision is about the answer, not about
                retrieval. */}
            <Field
              name="citations"
              label="Citations"
              hint={
                CITATION_MODES.find((mode) => mode.value === state.memory.citations)?.hint ??
                'How the client learns which documents the answer cited.'
              }
            >
              {(props) => (
                <Select
                  {...props}
                  value={state.memory.citations}
                  onChange={(event) =>
                    set('memory', { ...state.memory, citations: event.target.value })
                  }
                >
                  {CITATION_MODES.map((mode) => (
                    <option key={mode.value} value={mode.value}>
                      {mode.label}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <p className="-mt-2 mb-4 text-xs text-slate-500">
              Whatever the mode, the request log records which injected chunks each answer
              cited. To see what a client would receive under each mode for a real question,
              use <span className="font-medium">Show the whole prompt</span> under Memory → Try
              retrieval.
            </p>

            <PromptPreview
              gatewayContext={state.systemContext}
              modelContext={
                //  The first target: in failover it is the primary, and in A/B it is one
                //  of two. A preview cannot show both without claiming a request goes to
                //  both, so it shows the one a request is most likely to reach and the
                //  Test panel below reports what actually happened.
                (models?.items ?? []).find(
                  (model) => model.id === state.targets[0]?.modelId,
                )?.system_context ?? ''
              }
            />
          </Section>

          {/* 5. Logging -------------------------------------------------- */}
          <LoggingSection state={state} set={set} />

          {/* 6. Limits --------------------------------------------------- */}
          <LimitsSection
            gatewayId={gatewayId}
            form={state.limits}
            onChange={(limits) => set('limits', limits)}
          />
        </fieldset>

        <div className="mb-8 flex flex-wrap items-center gap-3">
          {writes ? (
            <SubmitButton
              busy={create.isPending || update.isPending}
              disabled={
                routingProblem !== null ||
                memoryIssue !== null ||
                limitProblems(state.limits).length > 0
              }
              className="w-auto"
            >
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

      {/* Its own history, which is where the audit log is actually read: somebody
          looking at a misbehaving endpoint wants "what changed here", and they want it
          without leaving the screen they are on. */}
      <ObjectAudit targetType="gateway" targetId={gatewayId} noun="gateway" />

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

/** Splitting the redaction textarea. Named so the escape is written once. */
const NEWLINE = '\n'

/**
 * SPEC §10.2, and the one section of this editor that is about somebody else's data.
 *
 * It says so unprompted, in the first thing you read. Body capture stores what a
 * customer's end users typed; the default is on because that is what makes conversation
 * memory possible; and an organization that has not thought about it should be told here
 * rather than discover it in a subject-access request. The effective retention is a
 * sentence — "kept for 30 days" — rather than a number in a box, because the number is
 * the setting and the sentence is the consequence.
 */
function LoggingSection({
  state,
  set,
}: {
  state: FormState
  set: <K extends keyof FormState>(key: K, value: FormState[K]) => void
}) {
  const anyBody = state.logRequestBody || state.logAssembledPrompt || state.logResponseBody

  return (
    <Section
      title="Logging"
      subtitle="What is recorded about each request, how long it is kept, and what is stripped before it is stored."
    >
      <div
        className={`mb-4 rounded-md border p-3 text-sm ${
          anyBody
            ? 'border-amber-200 bg-amber-50 text-amber-900'
            : 'border-slate-200 bg-slate-50 text-slate-600'
        }`}
      >
        {anyBody ? (
          <>
            <strong className="font-semibold">This stores end-user content.</strong> Prompts
            and responses through this gateway are written to your request log and kept for{' '}
            {state.retentionDays} day{state.retentionDays === '1' ? '' : 's'}. Use redaction
            patterns below for anything that must never be stored, or switch the bodies off.
          </>
        ) : (
          <>
            Bodies are not stored. The request log still records timing, tokens, status and
            errors — kept for {state.metadataRetentionDays} days — but the request detail
            view will have nothing to show, and conversation memory cannot be built.
          </>
        )}
      </div>

      <fieldset className="mb-4 space-y-2">
        <legend className="mb-1 text-sm font-medium text-slate-700">Capture</legend>
        <Toggle
          label="Metadata — timing, tokens, status, errors"
          hint="Always on. It is what the monitoring charts are made of."
          checked
          disabled
          onChange={() => {}}
        />
        <Toggle
          label="The client's request"
          checked={state.logRequestBody}
          onChange={(value) => set('logRequestBody', value)}
        />
        <Toggle
          label="The assembled prompt sent upstream"
          hint="Including anything memory injected."
          checked={state.logAssembledPrompt}
          onChange={(value) => set('logAssembledPrompt', value)}
        />
        <Toggle
          label="The response"
          hint="Streamed responses are reassembled."
          checked={state.logResponseBody}
          onChange={(value) => set('logResponseBody', value)}
        />
      </fieldset>

      <div className="mb-4 grid gap-4 sm:grid-cols-2">
        <Field
          name="retention_days"
          label="Keep bodies for"
          hint="Days. Prompts and responses are deleted after this."
        >
          {(props) => (
            <TextInput
              {...props}
              type="number"
              min={1}
              max={3650}
              value={state.retentionDays}
              onChange={(event) => set('retentionDays', event.target.value)}
            />
          )}
        </Field>
        <Field
          name="metadata_retention_days"
          label="Keep metadata for"
          hint="Days. Must be at least as long as the bodies it describes."
        >
          {(props) => (
            <TextInput
              {...props}
              type="number"
              min={1}
              max={3650}
              value={state.metadataRetentionDays}
              onChange={(event) => set('metadataRetentionDays', event.target.value)}
            />
          )}
        </Field>
      </div>

      <Field
        name="redaction_patterns"
        label="Redaction patterns"
        hint="One regular expression per line, applied to bodies before they are written — never after. A pattern that can backtrack catastrophically is refused when you save."
      >
        {(props) => (
          <TextArea
            {...props}
            rows={3}
            value={state.redactionPatterns}
            placeholder={REDACTION_PLACEHOLDER}
            onChange={(event) => set('redactionPatterns', event.target.value)}
          />
        )}
      </Field>

      <Toggle
        label="Feed transcripts to conversation memory"
        hint="Requires request-body logging; the distillation worker reads what was stored."
        checked={state.enableDistillation}
        onChange={(value) => set('enableDistillation', value)}
      />
      {state.enableDistillation && !state.logRequestBody ? (
        <p className="mt-2 text-sm text-amber-700">
          Distillation reads logged request bodies. Turn body logging back on, or switch
          distillation off — saving with both as they are will be refused.
        </p>
      ) : null}
    </Section>
  )
}

/** An email address and a card number: the two everybody wants first. */
const REDACTION_PLACEHOLDER = [
  String.raw`[\w.+-]+@[\w-]+\.[\w.]+`,
  String.raw`\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}`,
].join(NEWLINE)

function Toggle({
  label,
  hint,
  checked,
  disabled = false,
  onChange,
}: {
  label: string
  hint?: string
  checked: boolean
  disabled?: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <label className="flex items-start gap-2 text-sm text-slate-700">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-0.5 rounded border-slate-300 disabled:opacity-50"
      />
      <span>
        {label}
        {hint ? <span className="block text-xs text-slate-500">{hint}</span> : null}
      </span>
    </label>
  )
}

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
 * The fastest way to see what a change to the prompt did, without waiting for real
 * traffic to show up under Monitoring. It goes through the real proxy path, so a green
 * result here means a customer's request would take the same route with the same prompt.
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
        {result.ok && result.attempts?.length
          ? ` — answered by ${result.model_name ?? 'a fallback'} after ${result.attempts.length - 1} failed attempt(s)`
          : ''}
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

      {result.attempts?.length ? (
        <div className="mt-3">
          {/*  A green tick on a gateway whose primary is dead would be worse than a red
                one, so the probe walks the whole chain and says what it found. */}
          <p className="text-xs font-medium">Attempts</p>
          <ul className="mt-1 space-y-1">
            {result.attempts.map((attempt, index) => (
              <li
                key={`${attempt.target_id}-${index}`}
                className="flex items-center gap-2 text-xs"
              >
                <span className="w-4 text-slate-500">{index + 1}</span>
                <span className="flex-1 truncate font-medium">{attempt.model_name}</span>
                <span className="tabular-nums">{attempt.status}</span>
                {attempt.error_code ? <code>{attempt.error_code}</code> : null}
                <span className="w-14 text-right tabular-nums">{attempt.latency_ms} ms</span>
              </li>
            ))}
          </ul>
        </div>
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

/**
 * Task 101: a target whose tokenizer is more than the warning line off the provider's
 * count. A warning on the screen and not an alert on the pager — it is a configuration
 * problem with a one-click fix, and the person who fixes it is looking at this form.
 */
function DriftWarning({
  targets,
  models,
}: {
  targets: ChainRow[]
  models: { id: string; name: string }[]
}) {
  const modelIds = targets.map((row) => row.modelId).filter(Boolean)
  const calibrations = useCalibrations(modelIds.length > 0)
  const drifting = driftingTargets(calibrations.data, modelIds)
  if (drifting.length === 0) return null
  const named = (id: string) => models.find((model) => model.id === id)?.name ?? id
  return (
    <p
      role="status"
      className="-mt-4 mb-6 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
    >
      Token counts for{' '}
      {drifting.map((row, index) => (
        <span key={row.model_id}>
          {index > 0 ? ', ' : ''}
          <span className="font-medium">{named(row.model_id)}</span> ({formatDrift(row.ratio ?? 1)})
        </span>
      ))}{' '}
      are off the provider&rsquo;s by more than 15%, so this gateway&rsquo;s budgets and rate
      limits are measured in the wrong unit. Fix the tokenizer under Models.
    </p>
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
    targets: rowsOf(gateway),
    systemContext: gateway.system_context ?? '',
    memory: memoryForm(gateway.memory_config),
    paramOverrides: JSON.stringify(gateway.param_overrides, null, 2),
    lockedParams: JSON.stringify(gateway.locked_params, null, 2),
    logRequestBody: gateway.logging_config.log_request_body,
    logAssembledPrompt: gateway.logging_config.log_assembled_prompt,
    logResponseBody: gateway.logging_config.log_response_body,
    retentionDays: String(gateway.logging_config.retention_days),
    metadataRetentionDays: String(gateway.logging_config.metadata_retention_days),
    redactionPatterns: (gateway.logging_config.redaction_patterns ?? []).join(NEWLINE),
    enableDistillation: gateway.logging_config.enable_distillation,
    //  From the gateway row rather than from `GET /limits`: this is what was *saved*,
    //  which is what the inputs show. What is *enforced* — the ceiling, the live bars —
    //  comes from the limits endpoint, and the difference between the two is the thing
    //  the section exists to explain.
    limits: limitsForm(gateway.limits),
  }
}

function sameState(left: FormState, right: FormState): boolean {
  //  Everything is a scalar except the routing chain and the memory block, both of which
  //  have to be compared by value — a new object on every render would otherwise make the
  //  form permanently dirty and put an "unsaved changes" prompt in front of anyone
  //  navigating away.
  return (
    sameChain(left.targets, right.targets) &&
    !memoryChanged(left.memory, memoryConfigOf(right.memory)) &&
    !limitsChanged(left.limits, right.limits) &&
    (Object.keys(left) as (keyof FormState)[])
      .filter((key) => key !== 'targets' && key !== 'memory' && key !== 'limits')
      .every((key) => left[key] === right[key])
  )
}

/** A form back to the shape `memoryChanged` compares against. */
function memoryConfigOf(form: MemoryForm): GatewayResponse['memory_config'] {
  return memoryBody(form) as unknown as GatewayResponse['memory_config']
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
