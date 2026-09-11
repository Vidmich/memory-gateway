/**
 * The request drawer's arithmetic, with nothing rendered.
 *
 * Split out of `RequestDrawer.tsx` so each of these can be tested by calling it. They are
 * also the three places the drawer could be quietly wrong — which messages the gateway
 * added, what a multi-part message says, and what a reproduction command should contain.
 */

import type { RequestDetailResponse, RequestLogResponse } from '@/api/types'

/**
 * One retrieved chunk, as the request log stored it.
 *
 * The column is `list[Any]` on the server because it is a jsonb array whose shape has
 * moved once already and will move again when task 12 puts facts beside it. Parsing
 * defensively here rather than typing it as a schema is deliberate: a row written by an
 * older build must render as much as it can rather than blank the panel.
 */
export type RetrievedChunk = {
  id: string
  score: number | null
  sourceName: string
  pageOrSection: string | null
  documentId: string | null
  injected: boolean
  dropped: string | null
  /** Task 100: the answer's handles named this chunk. Only ever true for an injected one. */
  cited: boolean
  /** The `[n]` the prompt numbered it with — positional among the injected chunks. */
  handle: number | null
}

export function retrievedChunks(
  entries: readonly unknown[],
  cited: readonly string[] = [],
): RetrievedChunk[] {
  const citedIds = new Set(cited)
  let handle = 0
  return entries.filter(isRecord).map((entry) => {
    //  Absent means "written before this field existed", and the honest reading of that
    //  is that it went into the prompt: dropping was not a thing the assembler did then.
    const injected = entry.injected !== false
    const id = text(entry.id) ?? ''
    return {
      id,
      score: typeof entry.score === 'number' ? entry.score : null,
      sourceName: text(entry.source_name) ?? '(unknown document)',
      pageOrSection: text(entry.page_or_section),
      documentId: text(entry.document_id),
      injected,
      dropped: text(entry.dropped),
      cited: injected && citedIds.has(id),
      //  The record stores injected chunks first, in prompt order, so counting them here
      //  reproduces the numbering the model saw — the same rule the assembler uses.
      handle: injected ? ++handle : null,
    }
  })
}

/**
 * The one-line account of what the answer did with its documents.
 *
 * Returns nothing when nothing was injected: "0 of 0 cited" is not information, and the
 * drawer already explains why nothing went in.
 */
export function citationSummary(
  chunks: readonly RetrievedChunk[],
  unresolved: number,
): string | null {
  const injected = chunks.filter((chunk) => chunk.injected).length
  if (injected === 0) return null
  const cited = chunks.filter((chunk) => chunk.cited).length
  const parts = [`${cited} of ${injected} cited by the answer`]
  if (unresolved > 0) {
    parts.push(
      `${unresolved} handle${unresolved === 1 ? '' : 's'} pointed at nothing that was injected`,
    )
  }
  return parts.join(' · ')
}

/**
 * Why a chunk or a fact did not make it, in words.
 *
 * The reasons need different actions — a number on this gateway, or a conversation too
 * long for the model — so they are not merged into "dropped".
 */
export function droppedReason(reason: string | null): string | null {
  if (reason === 'doc_max_tokens') return 'over this gateway’s token budget'
  if (reason === 'memory_max_tokens') return 'over this gateway’s memory budget'
  if (reason === 'context_window') return 'no room left in the model’s context window'
  return reason
}

/**
 * One recalled fact, as the request log stored it.
 *
 * The row stores the fact's *text*, not only its id, for the same reason it stores a
 * chunk's source name: the fact may have been edited or erased since, and what the
 * assistant knew at the time is the thing somebody is asking about. Parsed defensively,
 * so a row written by an older build renders as much as it can rather than blanking the
 * panel.
 */
export type RecalledFact = {
  id: string
  text: string
  kind: string | null
  score: number | null
  confidence: number | null
  /** True when it was included because it is recent and confident rather than similar. */
  always: boolean
  injected: boolean
  dropped: string | null
}

export function recalledFacts(entries: readonly unknown[]): RecalledFact[] {
  return entries.filter(isRecord).map((entry) => ({
    id: text(entry.id) ?? '',
    text: text(entry.text) ?? '(no longer recorded)',
    kind: text(entry.kind),
    score: typeof entry.score === 'number' ? entry.score : null,
    confidence: typeof entry.confidence === 'number' ? entry.confidence : null,
    always: entry.always === true,
    injected: entry.injected !== false,
    dropped: text(entry.dropped),
  }))
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

/**
 * How many leading messages the gateway added.
 *
 * Positional, because assembly *prepends*: the caller's own messages are the tail of the
 * assembled list, so anything before them was injected. Falling back to zero when the
 * request body was not stored is right — without the original there is nothing to diff
 * against, and guessing would mark real user messages as added by the gateway.
 */
export function countInjected(
  assembled: Record<string, unknown>[] | null,
  original: Record<string, unknown>[] | null,
): number {
  if (!assembled || !original) return 0
  return Math.max(0, assembled.length - original.length)
}

/** A message's text, whether it is a string or the multi-part array form. */
export function contentOf(message: Record<string, unknown>): string {
  const content = message.content
  if (typeof content === 'string') return content
  if (content === null || content === undefined) return ''
  return JSON.stringify(content, null, 2)
}

export function roleOf(message: Record<string, unknown>): string {
  return typeof message.role === 'string' ? message.role : 'unknown'
}

export function toneFor(status: number): 'ok' | 'warn' | 'error' | 'neutral' {
  if (status < 300) return 'ok'
  if (status < 500) return 'warn'
  return 'error'
}

/**
 * The request as a curl against the gateway.
 *
 * Against the *gateway*, not the provider: reproducing it any other way would skip the
 * prompt assembly and the parameter merge, which are usually what somebody is trying to
 * understand. The key is a placeholder, because it cannot be read back and printing a
 * live credential into a shell history would be a poor trade for the seconds it saves.
 */
export function asCurl(
  log: RequestLogResponse,
  detail: RequestDetailResponse,
  endpointUrl: string | undefined,
): string {
  const body = {
    model: 'YOUR_GATEWAY_SLUG',
    messages: detail.transcript?.request_body ?? [{ role: 'user', content: 'your prompt' }],
    ...(log.streamed ? { stream: true } : {}),
  }
  const url = `${endpointUrl ?? 'https://YOUR_GATEWAY_URL'}/chat/completions`
  return [
    `curl ${url} \\`,
    `  -H "Authorization: Bearer $GATEWAY_API_KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '${JSON.stringify(body)}'`,
  ].join('\n')
}
