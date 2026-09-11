import { useRef, type ReactNode } from 'react'

import type { TemplateDefaultsResponse } from '@/api/types'
import { Field } from '@/components/Form'
import {
  insertPlaceholder,
  PLACEHOLDER_HINTS,
  TEMPLATE_FIELDS,
  templateWarnings,
  type TemplateForm,
  type TemplateName,
} from '@/pages/templates'

/**
 * The nine templates as a form (task 105), shared by Gateways → Advanced and by the
 * organization's defaults under Settings.
 *
 * Each field says where its text goes, offers the placeholders *that* template takes as
 * chips that insert at the caret, shows the default greyed once the value differs from
 * it, and has a Reset. A field that fails validation shows the server's message under it
 * — the `Field` picks it out of the mutation error by name, so the sentence on the page
 * is the sentence the API gave, not a second rule written here.
 *
 * Two warnings are inline and do not block: they are computed here so they appear while
 * typing, and they are the same two sentences the server puts on `template_warnings`.
 */
export function TemplateEditor({
  form,
  defaults,
  names,
  disabled,
  onChange,
}: {
  form: TemplateForm
  defaults: TemplateDefaultsResponse
  /** Which templates to show, in order. The organization's editor omits the two per-endpoint ones. */
  names: readonly TemplateName[]
  disabled: boolean
  onChange: (name: TemplateName, value: string) => void
}) {
  const warnings = templateWarnings(form)
  const shown = TEMPLATE_FIELDS.filter((field) => names.includes(field.name))
  const request = shown.filter((field) => field.side === 'request')
  const response = shown.filter((field) => field.side === 'response')

  return (
    <div className="space-y-8">
      <Group
        title="In the prompt"
        intro="What the gateway writes into the system message around retrieved documents and remembered facts."
      >
        {request.map((field) => (
          <TemplateField
            key={field.name}
            name={field.name}
            value={form[field.name]}
            defaultValue={defaults.defaults[field.name] ?? ''}
            placeholders={defaults.placeholders[field.name] ?? []}
            warning={warnings[field.name as 'reference_instruction' | 'excerpt']}
            disabled={disabled}
            onChange={(value) => onChange(field.name, value)}
          />
        ))}
      </Group>
      {response.length > 0 ? (
        <Group
          title="Around the answer"
          intro="What the gateway adds to the response: the citation footer, and text before and after the model’s answer."
        >
          {response.map((field) => (
            <TemplateField
              key={field.name}
              name={field.name}
              value={form[field.name]}
              defaultValue={defaults.defaults[field.name] ?? ''}
              placeholders={defaults.placeholders[field.name] ?? []}
              disabled={disabled}
              onChange={(value) => onChange(field.name, value)}
            />
          ))}
        </Group>
      ) : null}
    </div>
  )
}

function Group({ title, intro, children }: { title: string; intro: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
      <p className="mb-4 mt-1 text-sm text-slate-500">{intro}</p>
      {children}
    </section>
  )
}

function TemplateField({
  name,
  value,
  defaultValue,
  placeholders,
  warning,
  disabled,
  onChange,
}: {
  name: TemplateName
  value: string
  defaultValue: string
  placeholders: string[]
  warning?: string | undefined
  disabled: boolean
  onChange: (value: string) => void
}) {
  const spec = TEMPLATE_FIELDS.find((field) => field.name === name)
  const area = useRef<HTMLTextAreaElement | null>(null)
  const changed = value !== defaultValue

  const insert = (placeholder: string) => {
    const element = area.current
    const start = element?.selectionStart ?? value.length
    const end = element?.selectionEnd ?? start
    const next = insertPlaceholder(value, start, end, placeholder)
    onChange(next.value)
    // After React has written the new value: focus the textarea and put the caret after
    // the placeholder, so the next chip lands after this one.
    requestAnimationFrame(() => {
      const target = area.current
      if (!target) return
      target.focus()
      target.setSelectionRange(next.caret, next.caret)
    })
  }

  return (
    <div data-testid={`template-${name}`}>
      <Field name={name} label={spec?.label ?? name} hint={spec?.where}>
        {(props) => (
          // A bare textarea rather than `TextArea`: the chips need the element to read
          // the caret from, and the shared component does not forward a ref.
          <textarea
            id={props.id}
            ref={area}
            aria-invalid={props.invalid || undefined}
            aria-describedby={props.describedBy}
            rows={spec?.rows ?? 2}
            value={value}
            disabled={disabled}
            onChange={(event) => onChange(event.target.value)}
            className={`w-full rounded-md border px-3 py-2 font-mono text-sm focus:outline-none ${
              props.invalid
                ? 'border-red-400 focus:border-red-500'
                : 'border-slate-300 focus:border-slate-500'
            }`}
          />
        )}
      </Field>

      {placeholders.length > 0 ? (
        <div
          className="-mt-2 mb-2 flex flex-wrap items-center gap-1.5"
          role="group"
          aria-label={`Placeholders for ${spec?.label ?? name}`}
        >
          {placeholders.map((placeholder) => (
            <button
              key={placeholder}
              type="button"
              disabled={disabled}
              title={PLACEHOLDER_HINTS[placeholder]}
              aria-label={`Insert {${placeholder}} into ${spec?.label ?? name}`}
              onClick={() => insert(placeholder)}
              className="rounded-full border border-slate-300 bg-white px-2 py-0.5 font-mono text-[11px] text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:text-slate-400"
            >
              {`{${placeholder}}`}
            </button>
          ))}
        </div>
      ) : null}

      {warning ? (
        <p
          role="note"
          className="mb-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900"
        >
          {warning}
        </p>
      ) : null}

      {changed ? (
        <p className="mb-4 text-xs text-slate-400" data-testid={`default-${name}`}>
          Default:{' '}
          <code className="whitespace-pre-wrap break-words font-mono">
            {defaultValue === '' ? '(empty)' : defaultValue}
          </code>
          {disabled ? null : (
            <>
              {' · '}
              <button
                type="button"
                onClick={() => onChange(defaultValue)}
                aria-label={`Reset ${spec?.label ?? name}`}
                className="font-medium text-slate-600 underline hover:text-slate-900"
              >
                Reset
              </button>
            </>
          )}
        </p>
      ) : (
        <div className="mb-4" />
      )}
    </div>
  )
}
