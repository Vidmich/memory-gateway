/**
 * The Advanced page's arithmetic (task 105): which templates there are and where each
 * one's text goes, what a save sends, what the chips insert, and the two inline warnings.
 *
 * Nothing here knows the defaults or the placeholder names — those come from the server
 * (`GET /templates/defaults`), so the chips a person can click are exactly the names the
 * server accepts and the greyed "default" is the string it renders. This module only
 * knows what to *say* about each template, which is the part a screen owns.
 */

import type { TemplateConfig, TemplateUseResponse } from '@/api/types'

export type TemplateName = Exclude<keyof TemplateConfig, 'version'>

export type TemplateForm = Record<TemplateName, string>

export type TemplateFieldSpec = {
  name: TemplateName
  label: string
  /** Where the text goes, in one sentence — the part of the label that earns its keep. */
  where: string
  /** Request side (the prompt) or response side (around the answer). */
  side: 'request' | 'response'
  rows: number
}

/** Every template, in the order the page lists them: the request side, then the response. */
export const TEMPLATE_FIELDS: readonly TemplateFieldSpec[] = [
  {
    name: 'reference_heading',
    label: 'Reference heading',
    where: 'The line above the retrieved excerpts.',
    side: 'request',
    rows: 1,
  },
  {
    name: 'reference_instruction',
    label: 'Reference instruction',
    where:
      'The sentence under the heading that tells the model what the excerpts are and what to do when they do not answer.',
    side: 'request',
    rows: 3,
  },
  {
    name: 'excerpt',
    label: 'Excerpt',
    where:
      'One retrieved excerpt: its citation handle, where it came from, and the text. Repeated once per excerpt, so it is multiplied by the number injected.',
    side: 'request',
    rows: 2,
  },
  {
    name: 'memory_heading',
    label: 'Memory heading',
    where: 'The line above what is known about the person asking.',
    side: 'request',
    rows: 1,
  },
  {
    name: 'fact',
    label: 'Fact',
    where: 'One remembered fact, on one line. Repeated once per fact.',
    side: 'request',
    rows: 1,
  },
  {
    name: 'sources_heading',
    label: 'Sources heading',
    where: 'The line above the footer’s source list, when citations are delivered as a footer.',
    side: 'response',
    rows: 1,
  },
  {
    name: 'source_line',
    label: 'Source line',
    where: 'One cited source in the footer. The handle is the one the model wrote.',
    side: 'response',
    rows: 1,
  },
  {
    name: 'answer_prefix',
    label: 'Answer prefix',
    where:
      'Text the gateway adds before the model’s answer. Streamed as the first content delta, before the answer exists.',
    side: 'response',
    rows: 2,
  },
  {
    name: 'answer_suffix',
    label: 'Answer suffix',
    where: 'Text the gateway adds after the answer and after the footer, before the stream ends.',
    side: 'response',
    rows: 2,
  },
]

export const TEMPLATE_NAMES: readonly TemplateName[] = TEMPLATE_FIELDS.map((field) => field.name)

/** The organization's defaults leave out the two that are per endpoint by nature. */
export const ORGANIZATION_TEMPLATE_NAMES: readonly TemplateName[] = TEMPLATE_NAMES.filter(
  (name) => name !== 'answer_prefix' && name !== 'answer_suffix',
)

/** Where the organization's defaults live inside `organizations.settings`. */
export const TEMPLATE_DEFAULTS_KEY = 'template_defaults'

/** What each placeholder stands for — the chip’s tooltip. */
export const PLACEHOLDER_HINTS: Record<string, string> = {
  handle: 'The citation number, [1], [2], …',
  source_name: 'The document’s name',
  section: '“ (p. 12)” with the space and parentheses, or nothing when there is no section',
  section_raw: 'The bare section, or nothing',
  text: 'The excerpt’s text',
  score: 'The retrieval score, two decimals',
  label: 'Name plus section, linked to the chunk inspector when the deployment has a UI address',
  url: 'The chunk inspector link, or nothing',
  cited_count: 'How many injected excerpts the answer cited (0 in a streamed prefix)',
  injected_count: 'How many excerpts the prompt carried',
  gateway: 'This gateway’s name',
  model: 'The upstream model that answered',
}

/** The form for a saved configuration: the nine strings, nothing else. */
export function templateForm(config: TemplateConfig): TemplateForm {
  const form = {} as TemplateForm
  for (const name of TEMPLATE_NAMES) form[name] = config[name] ?? ''
  return form
}

/**
 * What a save sends: only the fields that differ from what was saved, because the
 * server merges and a page that sends one template cannot wipe the eight it did not
 * touch. Empty when nothing changed.
 */
export function templatePatch(
  form: TemplateForm,
  saved: TemplateForm,
  names: readonly TemplateName[] = TEMPLATE_NAMES,
): Partial<TemplateForm> {
  const patch: Partial<TemplateForm> = {}
  for (const name of names) {
    if (form[name] !== saved[name]) patch[name] = form[name]
  }
  return patch
}

export function templatesDiffer(
  left: TemplateForm,
  right: TemplateForm,
  names: readonly TemplateName[] = TEMPLATE_NAMES,
): boolean {
  return names.some((name) => left[name] !== right[name])
}

/**
 * The organization's editor form: the platform defaults with the organization's stored
 * overrides on top. Anything in the blob that is not a template string is ignored, the
 * way the server ignores it.
 */
export function organizationTemplateForm(
  settings: Record<string, unknown> | null | undefined,
  platform: TemplateConfig,
): TemplateForm {
  const stored = settings?.[TEMPLATE_DEFAULTS_KEY]
  const overrides = stored && typeof stored === 'object' ? (stored as Record<string, unknown>) : {}
  const form = templateForm(platform)
  for (const name of ORGANIZATION_TEMPLATE_NAMES) {
    const value = overrides[name]
    if (typeof value === 'string') form[name] = value
  }
  return form
}

/**
 * The settings blob to save: every other key untouched, and under `template_defaults`
 * only the fields that differ from the platform defaults — so the organization's blob
 * says what it decided, not what it happened to see. Empty overrides drop the key.
 */
export function organizationTemplateSettings(
  settings: Record<string, unknown> | null | undefined,
  form: TemplateForm,
  platform: TemplateConfig,
): Record<string, unknown> {
  const overrides = templatePatch(form, templateForm(platform), ORGANIZATION_TEMPLATE_NAMES)
  const rest = { ...(settings ?? {}) }
  delete rest[TEMPLATE_DEFAULTS_KEY]
  return Object.keys(overrides).length > 0 ? { ...rest, [TEMPLATE_DEFAULTS_KEY]: overrides } : rest
}

/**
 * A chip click: the placeholder goes where the caret is, replacing a selection, and the
 * caret lands after it. Pure, so the textarea can be told exactly where to put the caret
 * on the next render rather than the page guessing.
 */
export function insertPlaceholder(
  value: string,
  selectionStart: number,
  selectionEnd: number,
  placeholder: string,
): { value: string; caret: number } {
  const token = `{${placeholder}}`
  const start = Math.max(0, Math.min(selectionStart, value.length))
  const end = Math.max(start, Math.min(selectionEnd, value.length))
  return {
    value: value.slice(0, start) + token + value.slice(end),
    caret: start + token.length,
  }
}

/**
 * The two inline warnings, not refusals — the same two sentences the server puts on
 * `template_warnings`, computed here so they show while typing rather than after saving.
 */
export function templateWarnings(form: Pick<TemplateForm, 'reference_instruction' | 'excerpt'>): {
  reference_instruction?: string
  excerpt?: string
} {
  const warnings: { reference_instruction?: string; excerpt?: string } = {}
  if (form.reference_instruction.trim() === '') {
    warnings.reference_instruction =
      'The instruction is empty: the model is no longer told to say when the documents do not answer — that sentence is the difference between a grounded assistant and a confident one.'
  }
  if (!form.excerpt.includes('{source_name}')) {
    warnings.excerpt =
      'The excerpt no longer prints {source_name}: the model can cite, but cannot name the document; the footer and the metadata still can.'
  }
  return warnings
}

/** The fingerprint as the drawer and the filter show it: short, monospace-ready. */
export function shortFingerprint(fingerprint: string | null | undefined): string {
  return fingerprint ? fingerprint.slice(0, 8) : '—'
}

/**
 * One option in the Monitoring **Template** filter: the fingerprint, when it was first
 * seen, and how many requests it worded — enough to tell “the wording from Tuesday” from
 * “the one before it” without opening either.
 */
export function templateUseLabel(use: TemplateUseResponse, now: Date = new Date()): string {
  const first = new Date(use.first_seen)
  const sameDay = first.toDateString() === now.toDateString()
  const when = sameDay
    ? `today ${first.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
    : first.toLocaleDateString()
  const count = `${use.requests.toLocaleString()} request${use.requests === 1 ? '' : 's'}`
  return `${shortFingerprint(use.fingerprint)} · first seen ${when} · ${count}`
}
