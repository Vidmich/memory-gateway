import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { useGateway, usePromptPreview, useTemplateDefaults, useUpdateGateway } from '@/api/gateways'
import type { PromptPreviewResponse } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { Form, SubmitButton } from '@/components/Form'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { useToast } from '@/components/Toast'
import { PromptResult } from '@/pages/GatewayMemory'
import { TemplateEditor } from '@/pages/TemplateEditor'
import {
  TEMPLATE_NAMES,
  shortFingerprint,
  templateForm,
  templatePatch,
  templatesDiffer,
  type TemplateForm,
} from '@/pages/templates'
import { useUnsavedChanges } from '@/pages/useUnsavedChanges'

/**
 * Gateways → Advanced (task 105, SPEC §13.1): the text the gateway writes around
 * documents, memory and answers, as nine templates.
 *
 * Its own route rather than a seventh section on an already long editor, with its own
 * save and its own unsaved-changes guard. Three things shape it.
 *
 * **The preview is at the top, and it sends the unsaved templates.** Nobody should have
 * to save to see what a heading does to the prompt — the same prompt-preview call the
 * Memory section uses takes the form as a patch, merged server-side by the function the
 * save uses, so what it shows is what saving would do and nothing is written.
 *
 * **The save sends only what changed.** The server merges, so a page that touched one
 * template cannot wipe the other eight — and a validation failure lands under the field
 * it is about, in the server's words.
 *
 * **The fingerprint is shown, because it is what the log will show.** A wording change is
 * a new fingerprint on every row after it; the page names the current one so a person
 * can find those rows under Monitoring.
 */
export function GatewayAdvancedPage() {
  const { gatewayId } = useParams<{ gatewayId: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const { notify } = useToast()
  const writes = can(user, 'resources:write')

  const { data: gateway, isLoading } = useGateway(gatewayId)
  const { data: defaults } = useTemplateDefaults()
  const update = useUpdateGateway(gatewayId)

  const [form, setForm] = useState<TemplateForm | null>(null)
  const [saved, setSaved] = useState<TemplateForm | null>(null)

  // Seeded from the server rather than kept in sync with it: an edit in another tab
  // should not overwrite what this user is currently typing.
  useEffect(() => {
    if (gateway && form === null) {
      const loaded = templateForm(gateway.template_config)
      setForm(loaded)
      setSaved(loaded)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gateway])

  const dirty = useMemo(() => Boolean(form && saved && templatesDiffer(form, saved)), [form, saved])
  const { confirmLeave } = useUnsavedChanges(dirty && writes)

  if (isLoading || !gateway || !defaults || !form || !saved) {
    return <FullPageSpinner label="Loading the gateway…" />
  }

  const patch = templatePatch(form, saved)

  const submit = async () => {
    try {
      const result = await update.mutateAsync({ template_config: patch })
      const loaded = templateForm(result.template_config)
      setForm(loaded)
      setSaved(loaded)
      notify('Saved. The next request uses the new wording.')
    } catch {
      // Rendered by `Form` from the mutation's error, field by field.
    }
  }

  const leave = () => {
    if (confirmLeave()) void navigate(`/gateways/${gatewayId}`)
  }

  return (
    <div className="max-w-3xl">
      <header className="mb-6">
        <button
          type="button"
          onClick={leave}
          className="text-sm text-slate-500 hover:text-slate-700"
        >
          ← {gateway.name}
        </button>
        <h1 className="mt-2 text-xl font-semibold text-slate-900">Advanced</h1>
        <p className="mt-1 text-sm text-slate-500">
          The text this gateway writes around documents, memory and answers. Placeholders in braces
          are substituted by name and nothing else; a literal brace is{' '}
          <code className="font-mono">{'{{'}</code>.
        </p>
        <p className="mt-1 text-xs text-slate-500" data-testid="template-fingerprint">
          Current wording:{' '}
          <code className="font-mono">{shortFingerprint(gateway.template_fingerprint)}</code> — the
          request log records it on every row, so a change here is a filter under Monitoring.
          {dirty ? ' Unsaved edits will produce a new one.' : ''}
        </p>
      </header>

      <TemplatePreview gatewayId={gatewayId} patch={patch} dirty={dirty} />

      <Form
        onSubmit={submit}
        error={update.error}
        className="mt-6 rounded-lg border border-slate-200 bg-white p-5"
      >
        <fieldset disabled={!writes} className="contents">
          <TemplateEditor
            form={form}
            defaults={defaults}
            names={TEMPLATE_NAMES}
            disabled={!writes}
            onChange={(name, value) =>
              setForm((current) => (current ? { ...current, [name]: value } : current))
            }
          />
        </fieldset>

        {writes ? (
          <div className="mt-2 flex items-center gap-3">
            <SubmitButton busy={update.isPending} className="w-auto">
              Save templates
            </SubmitButton>
            {dirty ? (
              <span className="text-xs text-amber-700">Unsaved changes.</span>
            ) : (
              <span className="text-xs text-slate-500">Nothing to save.</span>
            )}
          </div>
        ) : (
          <p className="mt-2 text-sm text-slate-500">
            Your role can view these templates but not change them.
          </p>
        )}
      </Form>
    </div>
  )
}

/**
 * One question, the assembled prompt below, the citation examples rendered with the
 * templates as they are on the page — and what would go around the answer.
 */
function TemplatePreview({
  gatewayId,
  patch,
  dirty,
}: {
  gatewayId: string | undefined
  patch: Partial<TemplateForm>
  dirty: boolean
}) {
  const [query, setQuery] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<PromptPreviewResponse | null>(null)
  const previewPrompt = usePromptPreview(gatewayId)
  const ready = Boolean(gatewayId) && query.trim().length > 0

  const run = async () => {
    setError(null)
    try {
      setPreview(await previewPrompt.mutateAsync({ query: query.trim(), template_config: patch }))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'That could not be run.')
    }
  }

  return (
    <section
      className="rounded-lg border border-slate-200 bg-slate-50 p-4"
      data-testid="template-preview"
    >
      <h2 className="text-sm font-semibold text-slate-900">Preview</h2>
      <p className="mb-3 mt-0.5 text-xs text-slate-500">
        Type a question this gateway’s documents should answer. The prompt is assembled with the
        templates below as they are now — saved or not — and nothing is sent to the model or saved.
      </p>
      <label htmlFor="template-preview-query" className="sr-only">
        Question
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <input
          id="template-preview-query"
          value={query}
          placeholder="How do I request a refund?"
          onChange={(event) => setQuery(event.target.value)}
          className="min-w-64 flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
        />
        <button
          type="button"
          disabled={!ready || previewPrompt.isPending}
          onClick={() => void run()}
          className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {previewPrompt.isPending ? 'Assembling…' : 'Preview'}
        </button>
        {dirty ? (
          <span className="text-xs text-amber-700">
            Using the templates below, which are not saved yet.
          </span>
        ) : null}
      </div>

      {error ? (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {error}
        </p>
      ) : null}

      {preview ? (
        <>
          <PromptResult preview={preview} />
          <AnswerWrapping preview={preview} />
        </>
      ) : null}
    </section>
  )
}

function AnswerWrapping({ preview }: { preview: PromptPreviewResponse }) {
  const prefix = preview.answer_prefix ?? ''
  const suffix = preview.answer_suffix ?? ''
  return (
    <div
      className="mt-3 rounded-md border border-slate-200 bg-white p-3"
      data-testid="answer-wrapping"
    >
      <h4 className="text-xs font-semibold text-slate-900">Around the answer</h4>
      {prefix === '' && suffix === '' ? (
        <p className="mt-1 text-xs text-slate-500">
          Nothing is added before or after the model’s answer.
        </p>
      ) : (
        <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-slate-700">
          {prefix}
          <span className="italic text-slate-400">{'<the model’s answer, footer included>'}</span>
          {suffix}
        </pre>
      )}
      <p className="mt-2 text-xs text-slate-500">
        This wording’s fingerprint:{' '}
        <code className="font-mono">{shortFingerprint(preview.template_fingerprint)}</code>
      </p>
    </div>
  )
}
