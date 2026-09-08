/**
 * The Limits section's arithmetic and the sentences it needs.
 *
 * Split out of the components for the same reason as `distillation.ts`: each of these is
 * somewhere the screen could be quietly wrong, and each can be tested by calling it.
 *
 * Two earn their place on their own.
 *
 * `ceilingNote` is what stops the editor from looking broken. An organization types 5000,
 * the platform caps the gateway at 600 because it routes to a global catalog model, and
 * without a sentence next to the input the form appears to have ignored the save.
 *
 * `barTone` is the difference between a chart and a warning. A bar at 82% and a bar at 12%
 * are the same shape in a screenshot; only the colour and the word beside it say that one
 * of them is about to start returning 429s.
 */

import type { GatewayLimits, LimitQuota, LimitUsage } from '@/api/types'

/** SPEC §11's four caps, in the order they are checked and shown. */
export const LIMIT_NAMES = [
  'requests_per_minute',
  'requests_per_day',
  'tokens_per_minute',
  'concurrent_requests',
] as const

export type LimitName = (typeof LIMIT_NAMES)[number]

export const LIMIT_LABELS: Record<LimitName, string> = {
  requests_per_minute: 'Requests per minute',
  requests_per_day: 'Requests per day',
  tokens_per_minute: 'Tokens per minute',
  concurrent_requests: 'Concurrent requests',
}

export const LIMIT_HINTS: Record<LimitName, string> = {
  requests_per_minute: 'Counted over a sliding minute, so a burst across the boundary cannot double it.',
  requests_per_day: 'A daily ceiling, for the runaway loop nobody notices until the invoice.',
  tokens_per_minute:
    'Prompt plus completion, and it counts the memory this gateway injects — not only what the client sent.',
  concurrent_requests: 'Requests in flight upstream at once. Freed as soon as each one finishes.',
}

/** Above this fraction of a limit, a gateway is running hot. Matches the server. */
export const NEAR_LIMIT = 0.8

export type QuotaForm = Record<LimitName, string>

export type LimitsForm = {
  gateway: QuotaForm
  perEndUser: QuotaForm
}

/** An unset cap is an empty input, which is what "unlimited" looks like in a form. */
export function quotaForm(quota: LimitQuota | undefined): QuotaForm {
  const value = (name: LimitName) => {
    const stored = quota?.[name]
    return stored === null || stored === undefined ? '' : String(stored)
  }
  return {
    requests_per_minute: value('requests_per_minute'),
    requests_per_day: value('requests_per_day'),
    tokens_per_minute: value('tokens_per_minute'),
    concurrent_requests: value('concurrent_requests'),
  }
}

/** The form for a gateway's stored `limits` blob — what the inputs show. */
export function limitsForm(stored: GatewayLimits['configured'] | undefined): LimitsForm {
  return {
    gateway: quotaForm(stored),
    perEndUser: quotaForm(
      (stored as { per_end_user?: LimitQuota } | undefined)?.per_end_user,
    ),
  }
}

export type QuotaBody = Record<LimitName, number | null>
export type LimitsBody = QuotaBody & { per_end_user: QuotaBody }

/**
 * Every key is present, always. An empty input means "unlimited", and the blob is
 * deep-merged server-side — so a body that left the key out would mean "leave it as it
 * was", which makes clearing a limit impossible.
 */
function quotaBody(form: QuotaForm): QuotaBody {
  const value = (name: LimitName) => {
    const raw = form[name].trim()
    return raw === '' ? null : Number(raw)
  }
  return {
    requests_per_minute: value('requests_per_minute'),
    requests_per_day: value('requests_per_day'),
    tokens_per_minute: value('tokens_per_minute'),
    concurrent_requests: value('concurrent_requests'),
  }
}

export function limitsBody(form: LimitsForm): LimitsBody {
  return { ...quotaBody(form.gateway), per_end_user: quotaBody(form.perEndUser) }
}

export function limitsChanged(left: LimitsForm, right: LimitsForm): boolean {
  return LIMIT_NAMES.some(
    (name) =>
      left.gateway[name] !== right.gateway[name] ||
      left.perEndUser[name] !== right.perEndUser[name],
  )
}

/** Every value that is not a whole number above zero, so the save can be refused. */
export function limitProblems(form: LimitsForm): string[] {
  const problems: string[] = []
  for (const [scope, quota] of [
    ['Gateway', form.gateway],
    ['Per end user', form.perEndUser],
  ] as const) {
    for (const name of LIMIT_NAMES) {
      const raw = quota[name].trim()
      if (raw === '') continue
      const value = Number(raw)
      if (!Number.isInteger(value) || value < 1) {
        problems.push(`${scope}: ${LIMIT_LABELS[name].toLowerCase()} must be a whole number of 1 or more.`)
      }
    }
  }
  return problems
}

/**
 * What the editor says next to an input the platform ceiling has lowered.
 *
 * Only when it actually bites. A note that appeared whenever a ceiling *existed* would be
 * on every input of every gateway on the platform, which is how a warning stops being read.
 */
export function ceilingNote(limits: GatewayLimits | undefined, name: LimitName): string | null {
  if (!limits || !(limits.capped ?? []).includes(name)) return null
  const ceiling = limits.ceilings[name]
  return (
    `This gateway routes to a model from the global catalog, so the platform enforces ` +
    `${ceiling} — whatever is set here.`
  )
}

/** The line above the section, when the ceiling applies at all. */
export function ceilingSummary(limits: GatewayLimits | undefined): string | null {
  if (!limits || !limits.global_models) return null
  const set = LIMIT_NAMES.filter((name) => (limits.ceilings ?? {})[name] != null)
  if (set.length === 0) return null
  const words = set.map((name) => `${LIMIT_LABELS[name].toLowerCase()} ${limits.ceilings[name]}`)
  return `This gateway uses a model from the global catalog, which runs on the platform's own credential. The platform caps ${words.join(', ')}.`
}

export function utilization(usage: LimitUsage): number {
  return Math.max(0, Math.min(1, usage.utilization))
}

export type Tone = 'ok' | 'warn' | 'full'

export function barTone(usage: LimitUsage): Tone {
  if (usage.remaining === 0) return 'full'
  return utilization(usage) >= NEAR_LIMIT ? 'warn' : 'ok'
}

/** "4 of 10 used · resets in 37s", or without the reset for concurrency. */
export function usageSummary(usage: LimitUsage): string {
  const spent = `${usage.used} of ${usage.value} used`
  if (usage.limit === 'concurrent_requests') {
    // No window, so no reset: a slot frees when some other request finishes, which is not
    // a time anybody can name.
    return `${spent} right now`
  }
  return `${spent} · resets in ${usage.reset_seconds}s`
}

/**
 * The sentence under the bars. Says nothing when nothing is configured, because four bars
 * at zero would read as "limited and idle" rather than "not limited".
 */
export function usageHeadline(limits: GatewayLimits | undefined): string {
  if (!limits || (limits.usage ?? []).length === 0) {
    return 'No limits are set, so this gateway is not throttled. Requests are still bounded by whatever the upstream provider allows.'
  }
  const worst = [...(limits.usage ?? [])].sort((a, b) => b.utilization - a.utilization)[0]
  if (worst && worst.remaining === 0) {
    return `This gateway is at its ${LIMIT_LABELS[worst.limit as LimitName].toLowerCase()} limit. Requests are being refused with 429 right now.`
  }
  if (worst && utilization(worst) >= NEAR_LIMIT) {
    return `This gateway is above ${Math.round(NEAR_LIMIT * 100)}% of its ${LIMIT_LABELS[worst.limit as LimitName].toLowerCase()} limit.`
  }
  return 'Live usage, read from the same counters a request is checked against.'
}

/** The dashboard card's sentence for one gateway under pressure. */
export function pressureSummary(name: string, usage: LimitUsage): string {
  const percent = Math.round(utilization(usage) * 100)
  return `${name} is at ${percent}% of its ${LIMIT_LABELS[usage.limit as LimitName].toLowerCase()} limit.`
}

/**
 * What the throttled-callers panel says when it is empty.
 *
 * Two different empties, and the difference matters: nothing throttled at all, or nothing
 * throttled *that anybody identified*. The second is a note about the integration rather
 * than about the traffic.
 */
export function throttledEmptyHint(rateLimited: number): string {
  if (rateLimited === 0) return 'Nothing was rate-limited in this window.'
  return 'Requests were rate-limited, but none of them identified a caller. Send X-Gateway-User to see who.'
}
