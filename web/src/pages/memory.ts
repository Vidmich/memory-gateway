/**
 * The Memory section's arithmetic, with nothing rendered.
 *
 * Split out of the components for the same reason as `routing.ts` and `connectors.ts`:
 * every function here can be tested by calling it, and each is somewhere the screen could
 * be quietly wrong — a form that never looks dirty, a validation that disagrees with the
 * server, a score rendered as `0.7100000000000001`.
 */

import type { ConnectorResponse, MemoryConfig, RetrievalPreviewResponse } from '@/api/types'

/** The form, all strings, because that is what an `<input>` holds. */
export type MemoryForm = {
  connectorIds: string[]
  docTopK: string
  docMinScore: string
  docMaxTokens: string
  queryStrategy: string
  queryNTurns: string
  retrievalTimeoutMs: string
  onRetrievalError: string
}

/** Mirrors the server's `MemoryConfig` bounds. Kept in step by `memory.test.ts`. */
export const LIMITS = {
  topK: { min: 1, max: 100 },
  minScore: { min: 0, max: 1 },
  maxTokens: { min: 0, max: 100_000 },
  nTurns: { min: 1, max: 20 },
  timeoutMs: { min: 50, max: 5000 },
} as const

export function memoryForm(config: MemoryConfig): MemoryForm {
  return {
    connectorIds: [...(config.connector_ids ?? [])],
    docTopK: String(config.doc_top_k ?? 6),
    docMinScore: String(config.doc_min_score ?? 0.35),
    docMaxTokens: String(config.doc_max_tokens ?? 2000),
    queryStrategy: config.query_strategy ?? 'last_user_message',
    queryNTurns: String(config.query_n_turns ?? 3),
    retrievalTimeoutMs: String(config.retrieval_timeout_ms ?? 800),
    onRetrievalError: config.on_retrieval_error ?? 'fail_open',
  }
}

/**
 * The partial blob to send. Only the document half — the conversation-memory knobs
 * belong to task 12 and are deep-merged server-side, so not sending them is how this
 * section avoids overwriting a section it does not render.
 */
export function memoryBody(form: MemoryForm): Record<string, unknown> {
  return {
    connector_ids: form.connectorIds,
    doc_top_k: Number(form.docTopK),
    doc_min_score: Number(form.docMinScore),
    doc_max_tokens: Number(form.docMaxTokens),
    query_strategy: form.queryStrategy,
    query_n_turns: Number(form.queryNTurns),
    retrieval_timeout_ms: Number(form.retrievalTimeoutMs),
    on_retrieval_error: form.onRetrievalError,
  }
}

/**
 * Whether anything was actually changed.
 *
 * `connectorIds` is compared by value: a new array on every render would otherwise leave
 * the form permanently dirty and put an "unsaved changes" prompt in front of anyone
 * navigating away.
 */
export function memoryChanged(form: MemoryForm, stored: MemoryConfig): boolean {
  const saved = memoryForm(stored)
  return (
    !sameIds(form.connectorIds, saved.connectorIds) ||
    (Object.keys(saved) as (keyof MemoryForm)[])
      .filter((key) => key !== 'connectorIds')
      .some((key) => form[key] !== saved[key])
  )
}

export function sameIds(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index])
}

/**
 * The first thing wrong with the form, or `null`.
 *
 * Client-side because the server's 422 arrives after a round trip and lands on one field;
 * the wording is deliberately the *reason* rather than the bound, because "between 0 and
 * 1" does not tell you that 0.9 will return nothing.
 */
export function memoryProblem(form: MemoryForm): string | null {
  const topK = Number(form.docTopK)
  if (!Number.isInteger(topK) || topK < LIMITS.topK.min || topK > LIMITS.topK.max) {
    return `Chunks to retrieve is between ${LIMITS.topK.min} and ${LIMITS.topK.max}.`
  }
  const score = Number(form.docMinScore)
  if (!Number.isFinite(score) || score < LIMITS.minScore.min || score > LIMITS.minScore.max) {
    return 'Minimum score is a cosine similarity between 0 and 1.'
  }
  const tokens = Number(form.docMaxTokens)
  if (!Number.isInteger(tokens) || tokens < LIMITS.maxTokens.min) {
    return 'Token budget is a whole number of tokens.'
  }
  const turns = Number(form.queryNTurns)
  if (
    form.queryStrategy === 'last_n_turns' &&
    (!Number.isInteger(turns) || turns < LIMITS.nTurns.min || turns > LIMITS.nTurns.max)
  ) {
    return `Turns to include is between ${LIMITS.nTurns.min} and ${LIMITS.nTurns.max}.`
  }
  const timeout = Number(form.retrievalTimeoutMs)
  if (
    !Number.isInteger(timeout) ||
    timeout < LIMITS.timeoutMs.min ||
    timeout > LIMITS.timeoutMs.max
  ) {
    return `Retrieval timeout is between ${LIMITS.timeoutMs.min} and ${LIMITS.timeoutMs.max} ms.`
  }
  return null
}

/**
 * What to say about a memory configuration that will never retrieve anything.
 *
 * Not an error — an empty connector list is the default and a perfectly valid way to run
 * an endpoint with no memory. But a form where somebody has clearly been tuning scores
 * and has attached no connector is a mistake worth naming before they go looking for it
 * in the request log.
 */
export function memoryWarning(form: MemoryForm): string | null {
  if (form.connectorIds.length === 0) {
    return 'No connectors attached, so this gateway retrieves nothing. Its answers come only from the model and the system context.'
  }
  if (Number(form.docMaxTokens) === 0) {
    return 'The token budget is zero, so retrieved chunks are found and then dropped. Raise it, or detach the connectors instead.'
  }
  if (Number(form.docMinScore) >= 0.8) {
    return 'A minimum score this high rejects almost everything. If Try retrieval comes back empty, this is usually why.'
  }
  return null
}

/** A cosine similarity, as two decimals. `0.7100000000000001` in a table is noise. */
export function formatScore(score: number): string {
  return score.toFixed(2)
}

/**
 * One sentence about what a retrieval attempt did, keyed by outcome.
 *
 * The four kinds of empty are the whole point of the `outcome` field: "nothing matched"
 * and "the index is unreachable" look identical in a list of zero rows, and they need
 * completely different next steps.
 */
export function retrievalSummary(preview: RetrievalPreviewResponse): {
  tone: 'ok' | 'warn' | 'error' | 'neutral'
  message: string
} {
  const injected = preview.chunks.filter((chunk) => chunk.injected).length
  const dropped = preview.chunks.length - injected

  switch (preview.outcome) {
    case 'hit':
      return {
        tone: 'ok',
        message:
          dropped > 0
            ? `${injected} chunk${injected === 1 ? '' : 's'} would be injected (${preview.injected_tokens} tokens); ${dropped} dropped by the ${preview.doc_max_tokens}-token budget.`
            : `${injected} chunk${injected === 1 ? '' : 's'} would be injected, ${preview.injected_tokens} tokens.`,
      }
    case 'empty':
      return {
        tone: 'warn',
        message:
          'Nothing scored above the minimum. Either the documents do not cover this question, or the score floor is too high.',
      }
    case 'timeout':
      return { tone: 'error', message: preview.error ?? 'Retrieval timed out.' }
    case 'error':
      return { tone: 'error', message: preview.error ?? 'Retrieval failed.' }
    default:
      return {
        tone: 'neutral',
        message:
          'Retrieval did not run. Attach at least one connector for this gateway to read.',
      }
  }
}

/** Connectors that can be attached, newest first, with their document counts. */
export function attachable(connectors: readonly ConnectorResponse[]): ConnectorResponse[] {
  //  A connector being torn down cannot be attached: its vectors are on their way out,
  //  and offering it would produce a gateway that silently stops retrieving.
  return connectors.filter((connector) => connector.status !== 'deleting')
}

/** "3 documents · 128 chunks", or what is missing. */
export function connectorLabel(connector: ConnectorResponse): string {
  const indexed = connector.counts.indexed ?? 0
  if (indexed === 0) {
    return connector.document_count === 0
      ? 'No documents yet'
      : `${connector.document_count} document${connector.document_count === 1 ? '' : 's'}, none indexed yet`
  }
  return `${indexed} document${indexed === 1 ? '' : 's'} indexed`
}

/**
 * How much of the model's context window a preview uses, as a percentage — or `null`
 * when the model has not declared one.
 *
 * `null` rather than a guess, matching the server: a percentage of an invented
 * denominator is a number that looks precise and means nothing.
 */
export function contextUsage(total: number, window: number | null | undefined): number | null {
  if (!window || window <= 0) return null
  return Math.min(100, Math.round((total / window) * 100))
}
