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
}

export function retrievedChunks(entries: readonly unknown[]): RetrievedChunk[] {
  return entries.filter(isRecord).map((entry) => ({
    id: text(entry.id) ?? '',
    score: typeof entry.score === 'number' ? entry.score : null,
    sourceName: text(entry.source_name) ?? '(unknown document)',
    pageOrSection: text(entry.page_or_section),
    documentId: text(entry.document_id),
    //  Absent means "written before this field existed", and the honest reading of that
    //  is that it went into the prompt: dropping was not a thing the assembler did then.
    injected: entry.injected !== false,
    dropped: text(entry.dropped),
  }))
}

/**
 * Why a chunk did not make it, in words.
 *
 * The two reasons need different actions — one is a number on this gateway, the other is
 * a conversation that is too long for the model — so they are not merged into "dropped".
 */
export function droppedReason(reason: string | null): string | null {
  if (reason === 'doc_max_tokens') return 'over this gateway’s token budget'
  if (reason === 'context_window') return 'no room left in the model’s context window'
  return reason
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
