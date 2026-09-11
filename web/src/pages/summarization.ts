/**
 * Pure helpers for document summarization (task 102).
 *
 * Two carry product decisions.
 *
 * :func:`summarizationCost` is the cost line the connector panel shows *before* saving —
 * documents × (input tokens + summary tokens), and under `contextual` the re-embedding on
 * top. It is an estimate from what the connector already knows (document count, stored
 * bytes) and says so; the point is the order of magnitude, shown at the moment of choosing,
 * because `contextual` roughly doubles embedding spend and a person turns it on having seen
 * the number.
 *
 * :func:`summaryStatus` decides what the document table says about a summary in one word.
 * `capped` is not a failure and `failed` is not a document failure, and rendering either
 * red beside an `indexed` badge would send somebody looking for a broken file.
 */

import type {
  ConnectorResponse,
  DocumentResponse,
  SummarizationConfig,
  SummarizationHealth,
  SummarizationSettings,
} from '@/api/types'
import type { Tone } from '@/components/status'

export const SUMMARY_MODES = [
  {
    value: 'off',
    label: 'Off',
    hint: 'Documents are chunked and embedded as they are.',
  },
  {
    value: 'summary_chunk',
    label: 'Summary chunk',
    hint: 'One extra retrievable chunk per document, labelled as a summary, so a question about the document itself finds something. Cheap: one point per document, and nothing is recut.',
  },
  {
    value: 'contextual',
    label: 'Contextual',
    hint: 'The summary is prefixed to every chunk when it is embedded — not when it is returned — so a chunk about “the second option” embeds as the second option of this policy. The largest retrieval-quality lever short of reranking; roughly doubles embedding spend.',
  },
  {
    value: 'both',
    label: 'Both',
    hint: 'A summary chunk and contextual embedding. They do different things, and a corpus can want either.',
  },
] as const

export type SummaryMode = (typeof SUMMARY_MODES)[number]['value']

export function prefixesContext(mode: string): boolean {
  return mode === 'contextual' || mode === 'both'
}

export function addsSummaryChunk(mode: string): boolean {
  return mode === 'summary_chunk' || mode === 'both'
}

export function modeLabel(mode: string): string {
  return SUMMARY_MODES.find((entry) => entry.value === mode)?.label ?? mode
}

/** Roughly how many tokens a byte count is, for a cost line. Four characters a token. */
const BYTES_PER_TOKEN = 4

export type CostEstimate = {
  documents: number
  /** Tokens the summarization calls would send and receive, over every document. */
  summarizationTokens: number
  /** Tokens embedded again because every chunk's vector changes — `contextual` only. */
  reembedTokens: number
}

/**
 * What turning the proposed settings on would cost, over the documents the connector holds.
 *
 * Input per document is the smaller of the cap and the document's own size, because a
 * two-page memo does not send twelve thousand tokens. The re-embedding term is the whole
 * corpus once, which is what a re-embed of every chunk costs; it is present only when the
 * proposal *starts* prefixing, since a connector already on `contextual` has paid it.
 */
export function summarizationCost(
  connector: ConnectorResponse,
  proposed: { mode: string; max_input_tokens: number; max_summary_tokens: number },
): CostEstimate | null {
  if (proposed.mode === 'off') return null
  const documents = connector.document_count
  if (documents === 0) return { documents: 0, summarizationTokens: 0, reembedTokens: 0 }
  const corpusTokens = Math.ceil(connector.total_bytes / BYTES_PER_TOKEN)
  const perDocument = Math.min(proposed.max_input_tokens, Math.ceil(corpusTokens / documents))
  const summarizationTokens = documents * (perDocument + proposed.max_summary_tokens)
  const startsPrefixing =
    prefixesContext(proposed.mode) && !prefixesContext(connector.summarization.mode)
  return {
    documents,
    summarizationTokens,
    reembedTokens: startsPrefixing ? corpusTokens : 0,
  }
}

export function describeCost(estimate: CostEstimate): string {
  if (estimate.documents === 0) {
    return 'Nothing is indexed yet, so this costs nothing until documents arrive — then one model call per document.'
  }
  const documents = `${estimate.documents.toLocaleString()} document${estimate.documents === 1 ? '' : 's'}`
  const calls = `about ${estimate.summarizationTokens.toLocaleString()} tokens at the summarization model`
  if (estimate.reembedTokens > 0) {
    return `${documents}: ${calls}, plus re-embedding every chunk — roughly ${estimate.reembedTokens.toLocaleString()} tokens at the embedding provider — because every vector changes.`
  }
  return `${documents}: ${calls}. Nothing already indexed is recut.`
}

export type SummaryState = {
  label: string
  tone: Tone
  detail: string | null
}

/** One word for the document table's summary column, and the sentence behind it. */
export function summaryStatus(document: DocumentResponse): SummaryState | null {
  switch (document.summary_status) {
    case 'summarized':
      return {
        label: document.summary_model === 'manual' ? 'edited' : 'summarized',
        tone: 'ok',
        detail:
          document.summary_model === 'manual'
            ? 'Written by hand.'
            : `${document.summary_model ?? 'a model'}, ${(
                (document.summary_tokens_in ?? 0) + (document.summary_tokens_out ?? 0)
              ).toLocaleString()} tokens.`,
      }
    case 'failed':
      return { label: 'summary failed', tone: 'warn', detail: document.summary_error ?? null }
    case 'capped':
      return { label: 'waiting on cap', tone: 'neutral', detail: document.summary_error ?? null }
    default:
      return null
  }
}

/** The dashboard's line, or nothing: a card that says "0 waiting" every day is one nobody
 * reads on the day it changes. */
export function waitingSummary(health: SummarizationHealth | undefined): string | null {
  // `Array.isArray`, not a truthiness check: a server that predates the endpoint hands
  // back something else entirely, and a missing card is the right degradation.
  if (!health || !Array.isArray(health.waiting) || !health.waiting_documents) return null
  const connectors = health.waiting.length
  return `${health.waiting_documents.toLocaleString()} document${
    health.waiting_documents === 1 ? '' : 's'
  } waiting on the summarization cap across ${connectors} connector${connectors === 1 ? '' : 's'}`
}

/** "3 120 summarized, 2 failed, 40 waiting — 812 k tokens" for a panel subtitle. */
export function healthSummary(health: SummarizationHealth): string {
  const parts = [`${health.documents.toLocaleString()} summarized`]
  if (health.failures) parts.push(`${health.failures.toLocaleString()} failed`)
  if (health.capped) parts.push(`${health.capped.toLocaleString()} refused by a cap`)
  const tokens = health.tokens_in + health.tokens_out
  const estimate = health.estimated_runs > 0 ? ' (some estimated)' : ''
  return `${parts.join(', ')} — ${tokens.toLocaleString()} tokens${estimate}.`
}

export function effectiveModeFor(config: SummarizationConfig, kind: string): string {
  return config.overrides?.[kind]?.mode ?? config.mode
}

export type SummarizationForm = {
  mode: string
  modelId: string
  maxSummaryTokens: string
  maxInputTokens: string
  dailyDocumentCap: string
}

export function summarizationForm(config: SummarizationConfig): SummarizationForm {
  return {
    mode: config.mode,
    modelId: config.model_id ?? '',
    maxSummaryTokens: String(config.max_summary_tokens),
    maxInputTokens: String(config.max_input_tokens),
    dailyDocumentCap: config.daily_document_cap === null ? '' : String(config.daily_document_cap),
  }
}

export function summarizationBody(form: SummarizationForm): Record<string, unknown> {
  return {
    mode: form.mode,
    model_id: form.modelId || null,
    max_summary_tokens: Number(form.maxSummaryTokens),
    max_input_tokens: Number(form.maxInputTokens),
    daily_document_cap: form.dailyDocumentCap === '' ? null : Number(form.dailyDocumentCap),
  }
}

export function summarizationChanged(form: SummarizationForm, stored: SummarizationConfig): boolean {
  const proposed = summarizationBody(form)
  return (
    proposed.mode !== stored.mode ||
    proposed.model_id !== (stored.model_id ?? null) ||
    proposed.max_summary_tokens !== stored.max_summary_tokens ||
    proposed.max_input_tokens !== stored.max_input_tokens ||
    proposed.daily_document_cap !== stored.daily_document_cap
  )
}

export function summarizationProblem(form: SummarizationForm): string | null {
  const summary = Number(form.maxSummaryTokens)
  const input = Number(form.maxInputTokens)
  if (!Number.isFinite(summary) || summary < 30 || summary > 1000) {
    return 'Summary length must be between 30 and 1000 tokens.'
  }
  if (!Number.isFinite(input) || input < 500 || input > 100000) {
    return 'Input must be between 500 and 100 000 tokens.'
  }
  if (form.dailyDocumentCap !== '' && (!Number.isFinite(Number(form.dailyDocumentCap)) || Number(form.dailyDocumentCap) < 0)) {
    return 'The daily cap must be a whole number, or empty for no cap.'
  }
  return null
}

/**
 * The warning before saving: what the change will do to the index. Prefix on or off, or a
 * model change while prefixing, re-embeds; anything else is free. Only worth saying when
 * something is indexed.
 */
export function summarizationWarning(
  connector: ConnectorResponse,
  form: SummarizationForm,
): string | null {
  const stored = connector.summarization
  const indexed = connector.counts.indexed ?? 0
  if (indexed === 0) return null
  const was = prefixesContext(stored.mode)
  const will = prefixesContext(form.mode)
  const modelMoved = will && (form.modelId || null) !== (stored.model_id ?? null)
  if (was === will && !modelMoved) return null
  const noun = `${indexed.toLocaleString()} document${indexed === 1 ? '' : 's'}`
  if (will) {
    return `Every chunk's embedding will change, so the ${noun} already indexed become stale until you reindex them.`
  }
  return `The ${noun} already indexed were embedded with a summary prefix; they become stale until reindexed without it.`
}

/** The sentence under the Settings select: which link of the chain is answering. */
export function summarizationModelSummary(settings: SummarizationSettings): string {
  if (!settings.effective_model_id) {
    return 'No summarization model resolves anywhere — not here, not as the distillation model, not platform-wide. Connectors with summarization on will record every summary as failed until one is chosen.'
  }
  const name = settings.effective_model_name ?? 'an unnamed model'
  switch (settings.effective_model_source) {
    case 'summarization':
      return `Summarizing with ${name}.`
    case 'distillation':
      return `Using the distillation model, ${name}. Choose one here to summarize with something different.`
    default:
      return `Using the platform default, ${name}. Choose one here to override it.`
  }
}
