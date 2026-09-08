/**
 * The audit log's vocabulary and the sentences it needs.
 *
 * Split out of the components for the same reason as `limits.ts`: each of these is
 * somewhere the screen could be quietly wrong, and each can be tested by calling it.
 *
 * Two earn their place on their own.
 *
 * `describeEvent` is what turns `member.update` into something a person reads without
 * learning the schema. An audit log whose rows say `gateway.update` and a UUID is a log
 * people stop opening, which defeats the point of having one.
 *
 * `renderValue` is the difference between a diff and a puzzle. A change from `null` to
 * `"***"` is a credential being *set*; a change from absent to `6` is a field that did not
 * exist before. Rendering both as an empty cell would make the two look identical, and the
 * first of them is a security event.
 */

import type { AuditChange, AuditEvent } from '@/api/types'

/** What the server stores for a value it deliberately did not keep. */
export const REDACTED = '***'

/** Shown where a side of a change does not exist, rather than an empty cell. */
export const ABSENT = '—'

/**
 * Human names for the target types, for the filter and the row.
 *
 * A partial map on purpose: a target type this build has not been taught renders as
 * itself, which is ugly and readable, rather than as blank.
 */
export const TARGET_LABELS: Record<string, string> = {
  organization: 'Organization',
  user: 'Member',
  invitation: 'Invitation',
  upstream_model: 'Model',
  connector: 'Connector',
  document: 'Document',
  gateway: 'Gateway',
  api_key: 'API key',
  end_user: 'End user',
  memory_fact: 'Memory fact',
  platform_settings: 'Platform settings',
}

/** The verbs, keyed by the suffix of an action. Same partial-map reasoning as above. */
const VERBS: Record<string, string> = {
  create: 'created',
  update: 'updated',
  delete: 'deleted',
  remove: 'removed',
  revoke: 'revoked',
  resend: 'resent',
  accept: 'accepted',
  assume: 'opened',
  purge: 'purged',
  resync: 'resynced',
  reindex: 'reindexed',
  upload: 'uploaded to',
  distil: 'distilled',
  password_change: 'changed their password',
}

export function targetLabel(targetType: string): string {
  return TARGET_LABELS[targetType] ?? targetType
}

/**
 * A sentence for one event: who did what to which thing.
 *
 * Built from the action rather than from a table of thirty hand-written strings, so an
 * action added by a later task reads sensibly on the day it is added instead of on the
 * day somebody notices it does not.
 */
export function describeEvent(event: AuditEvent): string {
  const verb = VERBS[event.action.split('.').slice(1).join('.')] ?? event.action
  const who = event.actor ?? 'Somebody'
  if (verb === 'changed their password') return `${who} changed their password`
  const noun = targetLabel(event.target_type).toLowerCase()
  const name = event.target ?? ''
  return `${who} ${verb} ${name ? `${noun} ${name}` : noun}`
}

/**
 * A value as the diff shows it.
 *
 * `kind` decides whether a side exists at all, because "the field held null" and "the
 * field was not there" are different facts, and the second is how a credential being set
 * for the first time appears.
 */
export function renderValue(value: unknown, present: boolean): string {
  if (!present) return ABSENT
  if (value === null) return 'null'
  if (value === undefined) return ABSENT
  if (typeof value === 'string') return value === '' ? '(empty)' : value
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  return JSON.stringify(value)
}

export function beforeOf(change: AuditChange): string {
  return renderValue(change.before, change.kind !== 'added')
}

export function afterOf(change: AuditChange): string {
  return renderValue(change.after, change.kind !== 'removed')
}

/** True when this change is one whose value the log deliberately refused to keep. */
export function isRedacted(change: AuditChange): boolean {
  return change.before === REDACTED || change.after === REDACTED
}

/**
 * The one-line summary on a collapsed row.
 *
 * Names the fields rather than counting them: "system_context, memory_config.doc_top_k"
 * answers "is this the change I am looking for" without expanding, and "2 fields changed"
 * does not.
 */
export function changeSummary(event: AuditEvent): string {
  if (event.summary) return summaryLine(event.summary)
  const paths = event.changes.map((change) => change.path)
  if (paths.length === 0) return 'No field-level detail'
  const shown = paths.slice(0, 3).join(', ')
  const rest = paths.length - 3 + (event.omitted ?? 0)
  return rest > 0 ? `${shown} and ${rest} more` : shown
}

/** A bulk operation's counts, as a sentence. */
export function summaryLine(summary: Record<string, unknown>): string {
  const count = typeof summary.count === 'number' ? summary.count : 0
  const sample = Array.isArray(summary.sample) ? summary.sample.map(String) : []
  const extras = Object.entries(summary)
    .filter(([key, value]) => key !== 'count' && key !== 'sample' && Boolean(value))
    .map(([key, value]) => `${key}: ${String(value)}`)
  const head = `${count} ${count === 1 ? 'item' : 'items'}`
  const tail = [sample.join(', '), extras.join(', ')].filter(Boolean).join(' · ')
  return tail ? `${head} — ${tail}` : head
}

export type ActorTone = 'person' | 'support' | 'system'

/**
 * How an actor is drawn.
 *
 * Support access is visually distinct because that is the whole point of recording it: a
 * customer reviewing their own log should be able to see the vendor's visits without
 * reading every row, and a badge that looks like every other badge does not do that.
 */
export function actorTone(event: AuditEvent): ActorTone {
  if (event.actor_type === 'superadmin_impersonation') return 'support'
  if (event.actor_type === 'system') return 'system'
  return 'person'
}

export function actorLabel(event: AuditEvent): string {
  if (event.actor_type === 'system') return `${event.actor ?? 'job'} (automated)`
  return event.actor ?? 'Unknown'
}

/** Whether a filter is narrowing anything, so the screen can offer to clear it. */
export function hasFilters(filters: Record<string, string | null | undefined>): boolean {
  return Object.values(filters).some((value) => Boolean(value))
}

/**
 * What an empty list means, which depends on why it is empty.
 *
 * "Nothing has happened yet" and "nothing matches this filter" send somebody in opposite
 * directions, and a single "No results" sends half of them the wrong way.
 */
export function emptyHint(filtered: boolean): string {
  return filtered
    ? 'No changes match these filters. Widen the date range, or clear the action filter.'
    : 'No configuration changes recorded yet. Every change made from here on appears in this list.'
}
