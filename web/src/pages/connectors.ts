/**
 * Pure helpers for the connector screens.
 *
 * Kept out of the components for the usual reason — they are the parts worth testing
 * exhaustively, and a test that has to render a table to check a byte formatter is a test
 * nobody writes.
 *
 * The two that carry real product decisions:
 *
 * :func:`statusSummary` decides what a connector's *health* is in one word. A connector
 * with one failed document out of a thousand is not "failed", and one with a thousand
 * pending is not "ready" — the summary answers "is anything wrong" and "is anything
 * happening", in that order, because those are the two reasons somebody opens the list.
 *
 * :func:`chunkingWarning` decides when to say a reindex is needed. Only when something is
 * actually indexed: a warning on an empty connector is noise, and noise is what makes the
 * real one invisible.
 */

import type { ChunkingCandidate, ChunkingConfig, ConnectorResponse } from '@/api/types'
import { toneFor, type Tone } from '@/components/status'

/** SPEC §9.5, in order. The pipeline's own progression. */
export const DOCUMENT_STATUSES = [
  'pending',
  'extracting',
  'summarizing',
  'chunking',
  'embedding',
  'indexed',
  'failed',
  'skipped',
] as const

/**
 * Statuses that mean the pipeline is working on it right now.
 *
 * The complement — the terminal set — lives in `@/api/connectors`, beside the only thing
 * that asks the question: whether to keep polling. Two copies of one list is one copy
 * that stops being updated.
 */
export const IN_FLIGHT_STATUSES: readonly string[] = [
  'pending',
  'extracting',
  'summarizing',
  'chunking',
  'embedding',
]

export const CHUNK_STRATEGIES = [
  {
    value: 'recursive',
    label: 'Recursive',
    hint: 'Splits on paragraph, then sentence, then word boundaries. The right default for prose.',
    cost: null,
  },
  {
    value: 'by_heading',
    label: 'By heading',
    hint: 'One chunk per section, using the document’s own headings. Falls back to recursive where there are none.',
    cost: null,
  },
  {
    value: 'semantic',
    label: 'Semantic',
    hint: 'Cuts where consecutive sentences stop being about the same thing, measured against this document’s own distribution.',
    // Said once, at the moment of choosing, because it is a real trade-off and the person
    // making it will not meet the consequence for months. Task 20's rule: a cost paid at
    // every ingestion, and a future embedding-model change that has to *recut* this
    // connector from storage rather than re-embed it.
    cost: 'Embeds every sentence on every ingestion, and makes a future change of embedding model more expensive for this connector: its documents have to be re-read and re-cut rather than re-embedded.',
  },
  {
    value: 'sentence_window',
    label: 'Sentence window',
    hint: 'Embeds one sentence and returns it with its neighbours: a small unit to match on, enough context to answer with.',
    cost: null,
  },
  {
    value: 'code',
    label: 'Code-aware',
    hint: 'Function and class bodies as units, with the enclosing declaration carried into each fragment. Python, JavaScript, TypeScript and Go; recursive elsewhere.',
    cost: null,
  },
  {
    value: 'fixed',
    label: 'Fixed',
    hint: 'Exact token windows, ignoring boundaries. For content whose structure means nothing — minified data, single-line logs.',
    cost: null,
  },
] as const

/**
 * Format kinds, in the order the overrides table shows them.
 *
 * The same closed set the server keys overrides by. Listed here rather than read off the
 * response so the table has a stable row order and so a format with no override still
 * appears — a table that only showed the overridden formats would hide the fact that
 * everything else inherits.
 */
export const FORMAT_KINDS = [
  { value: 'pdf', label: 'PDF' },
  { value: 'docx', label: 'Word' },
  { value: 'pptx', label: 'Slides' },
  { value: 'xlsx', label: 'Spreadsheets' },
  { value: 'markdown', label: 'Markdown' },
  { value: 'html', label: 'HTML' },
  { value: 'csv', label: 'CSV' },
  { value: 'json', label: 'JSON' },
  { value: 'text', label: 'Plain text' },
  { value: 'code', label: 'Code' },
  { value: 'other', label: 'Everything else' },
] as const

export function strategyLabel(value: string): string {
  return CHUNK_STRATEGIES.find((entry) => entry.value === value)?.label ?? value
}

/**
 * The one-line consequence of a strategy, or null.
 *
 * Shown at the moment of choosing rather than in documentation, because that is the only
 * moment the person deciding is thinking about it.
 */
export function strategyCost(value: string): string | null {
  return CHUNK_STRATEGIES.find((entry) => entry.value === value)?.cost ?? null
}

/**
 * How each format actually resolves, for the table under the form.
 *
 * Read off `effective_chunking` rather than recomputed from `overrides`. The server owns
 * the resolution rule, and a screen that derived it independently would eventually show a
 * configuration the pipeline does not use — which is worse than showing nothing, because
 * it would be believed.
 */
export type FormatResolution = {
  kind: string
  label: string
  strategy: string
  chunkSize: number
  overridden: boolean
}

export function formatResolutions(connector: ConnectorResponse): FormatResolution[] {
  const overrides = connector.chunking.overrides ?? {}
  return FORMAT_KINDS.map((format) => {
    const resolved = connector.effective_chunking?.[format.value]
    return {
      kind: format.value,
      label: format.label,
      strategy: resolved?.strategy ?? connector.chunking.strategy,
      chunkSize: resolved?.chunk_size ?? connector.chunking.chunk_size,
      overridden: Object.hasOwn(overrides, format.value),
    }
  })
}

/**
 * The sentence on the reindex prompt after a chunking change.
 *
 * Names the formats when only some of them moved, which is the entire payoff of per-format
 * overrides: "reindex the code files" is an offer somebody accepts, and "reindex
 * everything" on a corpus of ten thousand PDFs is one they postpone indefinitely.
 */
export function reindexScope(connector: ConnectorResponse): string {
  const formats = connector.reindex_formats ?? []
  const everything = formats.length === 0 || formats.length >= FORMAT_KINDS.length
  if (everything) return 'every document'
  const labels = formats.map(
    (kind) => FORMAT_KINDS.find((format) => format.value === kind)?.label ?? kind,
  )
  return `the ${labels.join(', ')} documents`
}

export type Summary = {
  label: string
  tone: Tone
  detail: string
}

const KILOBYTE = 1024

/** `1.4 MB`. Two significant figures, because nobody reads the third. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unit = 0
  while (value >= KILOBYTE && unit < units.length - 1) {
    value /= KILOBYTE
    unit += 1
  }
  return `${unit === 0 ? value : Number(value.toFixed(value < 10 ? 1 : 0))} ${units[unit]}`
}

export function countOf(connector: ConnectorResponse, status: string): number {
  return connector.counts[status] ?? 0
}

export function inFlight(connector: ConnectorResponse): number {
  return IN_FLIGHT_STATUSES.reduce((total, status) => total + countOf(connector, status), 0)
}

/**
 * One word for a connector's state, and a sentence under it.
 *
 * Order matters and is the whole design: what is *wrong* beats what is *happening*, which
 * beats what is fine. A connector deleting itself outranks both, because nothing else
 * about it is worth acting on.
 */
export function statusSummary(connector: ConnectorResponse): Summary {
  if (connector.status === 'deleting') {
    return { label: 'Deleting', tone: 'warn', detail: 'Removing its files and vectors.' }
  }
  if (connector.status === 'error') {
    return { label: 'Error', tone: 'error', detail: connector.error ?? 'Something went wrong.' }
  }

  const failed = countOf(connector, 'failed')
  const working = inFlight(connector)
  const indexed = countOf(connector, 'indexed')

  if (failed > 0) {
    return {
      label: `${failed} failed`,
      tone: 'error',
      detail: `${failed} of ${connector.document_count} documents could not be read.`,
    }
  }
  if (connector.status === 'syncing' || working > 0) {
    return {
      label: 'Indexing',
      tone: 'info',
      detail: `${working} document${working === 1 ? '' : 's'} still to process.`,
    }
  }
  if (connector.document_count === 0) {
    return { label: 'Empty', tone: 'warn', detail: 'Nothing uploaded yet.' }
  }
  return {
    label: 'Ready',
    tone: 'ok',
    detail: `${indexed} document${indexed === 1 ? '' : 's'} indexed.`,
  }
}

/** The tone for a single document row. Delegates to the shared vocabulary, so a status
 * added on the server renders neutrally rather than breaking the table. */
export function documentTone(status: string): Tone {
  return toneFor(status)
}

/**
 * Whether the chunking form differs from what is stored.
 *
 * Compared field by field rather than by identity, because the form holds strings from
 * inputs and the response holds numbers, and `!==` on those is always true.
 */
export function chunkingChanged(form: ChunkingForm, stored: ChunkingConfig): boolean {
  return (
    form.strategy !== stored.strategy ||
    Number(form.chunkSize) !== stored.chunk_size ||
    Number(form.overlap) !== stored.overlap ||
    form.respectBoundaries !== stored.respect_boundaries ||
    Number(form.windowSentences) !== stored.window_sentences ||
    Number(form.breakpointPercentile) !== stored.breakpoint_percentile
  )
}

export type ChunkingForm = {
  strategy: string
  chunkSize: string
  overlap: string
  respectBoundaries: boolean
  windowSentences: string
  breakpointPercentile: string
}

export function chunkingForm(config: ChunkingConfig): ChunkingForm {
  return {
    strategy: config.strategy,
    chunkSize: String(config.chunk_size),
    overlap: String(config.overlap),
    respectBoundaries: config.respect_boundaries,
    windowSentences: String(config.window_sentences),
    breakpointPercentile: String(config.breakpoint_percentile),
  }
}

export function chunkingBody(form: ChunkingForm): Record<string, unknown> {
  return {
    strategy: form.strategy,
    chunk_size: Number(form.chunkSize),
    overlap: Number(form.overlap),
    respect_boundaries: form.respectBoundaries,
    window_sentences: Number(form.windowSentences),
    breakpoint_percentile: Number(form.breakpointPercentile),
  }
}

/** Which extra inputs a strategy actually uses, so the form shows only those. */
export function strategyFields(strategy: string): {
  window: boolean
  breakpoint: boolean
  overlap: boolean
} {
  return {
    window: strategy === 'sentence_window',
    breakpoint: strategy === 'semantic',
    // `sentence_window` deliberately ignores overlap — the window *is* the overlap, and a
    // second one would put the same sentence in four chunks instead of three. Hiding the
    // input is how that stops being a surprise.
    overlap: strategy !== 'sentence_window',
  }
}

/**
 * The client-side half of the server's rule, so a mistake is caught while typing rather
 * than on save. The server refuses the same values — this only decides what the button
 * looks like.
 */
export function chunkingProblem(form: ChunkingForm): string | null {
  const size = Number(form.chunkSize)
  const overlap = Number(form.overlap)
  if (!Number.isFinite(size) || size < 50 || size > 4000) {
    return 'Chunk size must be between 50 and 4000 tokens.'
  }
  if (!Number.isFinite(overlap) || overlap < 0) return 'Overlap cannot be negative.'
  if (overlap * 2 > size) {
    return 'Overlap must be at most half the chunk size, or nearly every chunk is a copy of its neighbour.'
  }
  const window = Number(form.windowSentences)
  if (!Number.isFinite(window) || window < 0 || window > 10) {
    return 'The window must be between 0 and 10 sentences either side.'
  }
  const breakpoint = Number(form.breakpointPercentile)
  if (!Number.isFinite(breakpoint) || breakpoint < 50 || breakpoint > 99) {
    return 'The breakpoint percentile must be between 50 and 99.'
  }
  return null
}

/**
 * Whether to tell somebody a reindex is needed.
 *
 * Only when there is an index to invalidate. A warning on an empty connector is noise,
 * and noise is what makes the real warning invisible.
 */
export function chunkingWarning(connector: ConnectorResponse, changed: boolean): string | null {
  if (!changed) return null
  const indexed = countOf(connector, 'indexed')
  if (indexed === 0) return null
  return `Saving re-chunks nothing on its own. The ${indexed} document${
    indexed === 1 ? '' : 's'
  } already indexed keep their old chunks until you reindex them.`
}

/**
 * The four numbers that make two candidate chunkings comparable, as rows.
 *
 * Four, and not a wall of chunk text: nobody reads two documents side by side and
 * concludes anything. `at_ceiling` says how often the size limit decided the boundary
 * rather than the strategy, which is what catches a `semantic` configuration that is not
 * earning what it costs; `mid_sentence` is what makes `fixed` look like what it is.
 */
export type ComparisonRow = {
  label: string
  values: string[]
}

export function comparisonRows(
  candidates: readonly ChunkingCandidate[],
): ComparisonRow[] {
  const at = (pick: (candidate: ChunkingCandidate) => string) => candidates.map(pick)
  return [
    { label: 'Chunks', values: at((one) => String(one.distribution.chunks)) },
    {
      label: 'Tokens (min / median / p95 / max)',
      values: at(
        (one) =>
          `${one.distribution.min_tokens} / ${one.distribution.median_tokens} / ` +
          `${one.distribution.p95_tokens} / ${one.distribution.max_tokens}`,
      ),
    },
    {
      label: 'Cut by the size limit',
      values: at((one) => `${one.distribution.at_ceiling} of ${one.distribution.chunks}`),
    },
    {
      label: 'Boundaries mid-sentence',
      values: at((one) => String(one.distribution.mid_sentence)),
    },
    {
      label: 'Embedding calls per ingestion',
      values: at((one) => String(one.embedded_texts)),
    },
  ]
}

/**
 * What a format's `page_count` counts, from the media type.
 *
 * The noun is derived here rather than stored beside the number, because it is a fact
 * about the format: storing both would be two columns that can disagree, and the one that
 * would be wrong is the one nobody looks at.
 */
const PAGE_UNITS: Record<string, [string, string]> = {
  'application/pdf': ['page', 'pages'],
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': [
    'slide',
    'slides',
  ],
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['sheet', 'sheets'],
}

/** `147 pages`, `12 slides`, `3 sheets`, or an em dash where the format has no such unit. */
export function pageLabel(document: {
  mime_type: string | null
  page_count: number | null
}): string {
  const unit = document.mime_type ? PAGE_UNITS[document.mime_type] : undefined
  if (!unit || document.page_count === null) return '—'
  return `${document.page_count} ${document.page_count === 1 ? unit[0] : unit[1]}`
}

export type Explanation = {
  headline: string
  guidance: string
}

/**
 * The states that are not really failures, spelled out.
 *
 * A scanned PDF and a password-protected file are the two things a customer uploads that
 * cannot be indexed *and* can be fixed by the customer. Rendering them as a red row with a
 * paragraph in it makes them look like a defect in the product; rendering them as an
 * explained state with the next step in it makes them a task.
 *
 * Keyed on `reason`, never on the message: the message is written for a person and gets
 * rewritten as the wording improves, and a UI matching on its text breaks silently when it
 * does. An unrecognised code falls through to showing the sentence, which is what every
 * other row shows anyway.
 */
const EXPLANATIONS: Record<string, Explanation> = {
  needs_ocr: {
    headline: 'Scanned — needs OCR',
    guidance:
      'This PDF is images of pages with no text layer, so there is nothing to index. Upload a version with selectable text, or run it through OCR first.',
  },
  password_protected: {
    headline: 'Password-protected',
    guidance:
      'Save an unprotected copy and upload that. The gateway does not store document passwords.',
  },
  not_yet_supported: {
    headline: 'Not supported yet',
    guidance:
      'The format is recognised. This file is safe to leave here — a resync will pick it up when support arrives.',
  },
  extraction_timeout: {
    headline: 'Took too long to read',
    guidance:
      'Reading this file hit the time limit. That is usually a very large document, or one with unusual internal structure; splitting it up is the reliable fix.',
  },
  extraction_out_of_memory: {
    headline: 'Too large to read',
    guidance:
      'Reading this file needed more memory than one document is allowed. Splitting it into smaller files is the reliable fix.',
  },
}

export function explanationFor(document: { reason: string | null }): Explanation | null {
  return document.reason ? (EXPLANATIONS[document.reason] ?? null) : null
}

/** `aws s3 cp` is not the instruction; a presigned PUT is. */
export function uploadSnippet(url: string): string {
  return `curl -X PUT --upload-file ./your-file.md \\\n  "${url}"`
}

/** A short label for a resync result, for the toast. */
export function resyncSummary(result: {
  added: number
  updated: number
  deleted: number
  unchanged: number
  skipped: number
}): string {
  const parts: string[] = []
  if (result.added) parts.push(`${result.added} added`)
  if (result.updated) parts.push(`${result.updated} updated`)
  if (result.deleted) parts.push(`${result.deleted} removed`)
  if (result.skipped) parts.push(`${result.skipped} already in progress`)
  if (parts.length === 0) {
    return result.unchanged > 0
      ? `Everything is up to date (${result.unchanged} unchanged).`
      : 'Nothing to sync.'
  }
  return `${parts.join(', ')}.`
}
