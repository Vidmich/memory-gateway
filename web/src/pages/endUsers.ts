/**
 * The memory browser's arithmetic, with nothing rendered.
 *
 * Split out of the components for the same reason as `connectors.ts` and `memory.ts`:
 * each of these can be tested by calling it, and each is somewhere the screen could be
 * quietly wrong — a retracted fact shown as live, a confidence rendered as
 * `0.8500000000000001`, a purge confirmation that says more than the purge does.
 */

import type { EndUserResponse, MemoryFactResponse } from '@/api/types'

/** SPEC §6.4's vocabulary, in the order the form offers them. */
export const FACT_KINDS = ['fact', 'preference', 'goal', 'constraint'] as const

export type FactKind = (typeof FACT_KINDS)[number]

/**
 * What each kind means, in the words somebody adding one would use.
 *
 * Worth spelling out on the form: the difference between a *preference* ("prefers
 * Python") and a *constraint* ("must not be shown pricing") is the difference between
 * something the assistant may weigh and something it may not ignore, and nobody guesses
 * that from four one-word labels.
 */
export const KIND_HINTS: Record<FactKind, string> = {
  fact: 'Something true about them. “Works on the billing team.”',
  preference: 'How they like to be answered. “Prefers short answers with code.”',
  goal: 'What they are trying to do. “Migrating from Postgres 14 to 16.”',
  constraint: 'Something the answer must respect. “Works in the EU; needs GDPR answers.”',
}

/** Why a fact is not being used, or `null` when it is. */
export type FactState = 'live' | 'superseded' | 'expired'

export function factState(fact: MemoryFactResponse, now: Date = new Date()): FactState {
  if (fact.superseded_at) return 'superseded'
  if (fact.expires_at && new Date(fact.expires_at) <= now) return 'expired'
  return 'live'
}

/**
 * A sentence for a fact that is not live.
 *
 * The two states need different actions — one was retracted by somebody, the other timed
 * out on its own — so they are not merged into "inactive".
 */
export function stateLabel(state: FactState): string | null {
  if (state === 'superseded') return 'Retracted — no longer used in prompts'
  if (state === 'expired') return 'Expired — no longer used in prompts'
  return null
}

/** A confidence as a percentage. `0.8500000000000001` in a table is noise. */
export function formatConfidence(confidence: number): string {
  return `${Math.round(confidence * 100)}%`
}

/** A cosine similarity, as two decimals. Matches the connector screens. */
export function formatScore(score: number): string {
  return score.toFixed(2)
}

/**
 * What to call somebody on screen.
 *
 * The label if an operator set one, otherwise the id the customer's system uses. An
 * anonymous id is shortened, because sixteen hex characters is not a name and the prefix
 * is the only part that means anything.
 */
export function displayName(endUser: EndUserResponse): string {
  if (endUser.label) return endUser.label
  if (endUser.anonymous) return `Anonymous ${endUser.external_id.replace('anon:', '').slice(0, 6)}`
  return endUser.external_id
}

/**
 * What the list screen says about an end user with no facts.
 *
 * Three different causes, and only one of them is a problem worth acting on: a person who
 * has just arrived has nothing stored yet, a person whose traffic is anonymous will never
 * have anything unless the gateway is changed, and a person with traffic and no facts is
 * waiting for distillation that this build does not have yet.
 */
export function emptyMemoryHint(endUser: EndUserResponse): string | null {
  if (endUser.fact_count > 0) return null
  if (endUser.anonymous) {
    return 'Anonymous callers are identified by address. Send X-Gateway-User to keep memory about a real person.'
  }
  if (endUser.request_count <= 1) return 'Seen once. Nothing has been learned yet.'
  return 'Nothing stored yet. Add a fact by hand, or wait for automatic distillation.'
}

/**
 * The confirmation copy for a purge (SPEC §13.2).
 *
 * It names exactly what goes and what stays, because the thing people expect a "purge" to
 * do — remove the person — is not what this does, and finding that out afterwards is
 * finding it out too late.
 */
export function purgeDescription(
  endUser: EndUserResponse,
  includeTranscripts: boolean,
): string {
  const facts = `${endUser.fact_count} fact${endUser.fact_count === 1 ? '' : 's'}`
  const bodies = includeTranscripts
    ? ' and every stored request and response body from their conversations'
    : ''
  return `This removes ${facts}${bodies}. It cannot be undone. Their request history stays, so past traffic still shows who it belonged to.`
}

/** What the toast says afterwards. Numbers, because "done" is not evidence. */
export function purgeSummary(result: { facts: number; transcripts: number }): string {
  const facts = `${result.facts} fact${result.facts === 1 ? '' : 's'}`
  if (result.transcripts === 0) return `Removed ${facts}.`
  const bodies = `${result.transcripts} transcript${result.transcripts === 1 ? '' : 's'}`
  return `Removed ${facts} and ${bodies}.`
}

/** "12 requests · last seen 3 May" for the list screen's second line. */
export function activity(endUser: EndUserResponse): string {
  const requests = `${endUser.request_count.toLocaleString()} request${
    endUser.request_count === 1 ? '' : 's'
  }`
  return `${requests} · last seen ${new Date(endUser.last_seen_at).toLocaleString()}`
}
