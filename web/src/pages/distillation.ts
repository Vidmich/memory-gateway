/**
 * The distillation settings form's arithmetic, and the words the health chart needs.
 *
 * Split out of the components for the same reason as `memory.ts` and `endUsers.ts`: each
 * of these is somewhere the screen could be quietly wrong, and each can be tested by
 * calling it.
 *
 * Two of them earn their place on their own.
 *
 * `healthWarning` is the whole point of the memory-health block. A dedupe rate near 100%
 * and a supersession rate near zero are the two ways this feature fails while looking
 * healthy — green jobs, no errors, facts on the screen — so the screen has to say so in
 * words rather than leave two ratios for somebody to interpret.
 *
 * `capWarning` is the other one. A cost guard that stops memory silently is worse than no
 * cap at all, so the form says how much of today's budget is gone before it is gone.
 */

import type { DistillationSettings, MemoryHealth } from '@/api/types'

export type DistillationForm = {
  enabled: boolean
  modelId: string
  debounceSeconds: string
  dedupeThreshold: string
  maxFactsPerUser: string
  dailyCallCap: string
  perUserDailyCap: string
}

export function distillationForm(settings: DistillationSettings): DistillationForm {
  const config = settings.config
  return {
    enabled: config.enabled ?? true,
    // An empty string is "no selection", which is how the form expresses "use the platform
    // default" — the same value the API takes as `model_id: null`.
    modelId: config.model_id ?? '',
    debounceSeconds: String(config.debounce_seconds ?? 30),
    dedupeThreshold: String(config.dedupe_threshold ?? 0.92),
    maxFactsPerUser: String(config.max_facts_per_user ?? 500),
    dailyCallCap: String(config.daily_call_cap ?? 5000),
    perUserDailyCap: String(config.per_user_daily_cap ?? 24),
  }
}

/** What to send. `model_id` is always present, because clearing it is a value. */
export function distillationBody(form: DistillationForm) {
  return {
    enabled: form.enabled,
    model_id: form.modelId || null,
    debounce_seconds: Number(form.debounceSeconds),
    dedupe_threshold: Number(form.dedupeThreshold),
    max_facts_per_user: Number(form.maxFactsPerUser),
    daily_call_cap: Number(form.dailyCallCap),
    per_user_daily_cap: Number(form.perUserDailyCap),
  }
}

export function distillationChanged(
  form: DistillationForm,
  settings: DistillationSettings,
): boolean {
  const stored = distillationForm(settings)
  return (Object.keys(stored) as (keyof DistillationForm)[]).some(
    (key) => form[key] !== stored[key],
  )
}

/**
 * What the form says under the model selector.
 *
 * Three states, and the middle one is why this exists: an organization that has chosen
 * nothing is not broken — it is using the platform's model — and a screen that showed an
 * empty selector without saying so reads as "distillation is not configured".
 */
export function modelSummary(settings: DistillationSettings): string {
  if (!settings.effective_model_id) {
    return 'No distillation model is configured here or platform-wide. Nothing will be learned from conversations until one is chosen.'
  }
  if (settings.using_platform_default) {
    return `Using the platform default, ${settings.effective_model_name ?? 'an unnamed model'}. Choose one of your own to override it.`
  }
  return `Distilling with ${settings.effective_model_name ?? 'the selected model'}. A cheap model is the point — this reads transcripts, not questions.`
}

/** "1,204 of 5,000 calls used today." Numbers, because a cap is a number. */
export function usageSummary(settings: DistillationSettings): string {
  const { calls_today: used, daily_call_cap: cap } = settings.usage
  if (!cap) return `${used.toLocaleString()} distillation calls today. No daily cap is set.`
  return `${used.toLocaleString()} of ${cap.toLocaleString()} distillation calls used today. Resets at midnight UTC.`
}

/** A warning when today's budget is nearly or entirely gone, otherwise `null`. */
export function capWarning(settings: DistillationSettings): string | null {
  const { calls_today: used, daily_call_cap: cap } = settings.usage
  if (!cap) return null
  if (used >= cap) {
    return 'The daily cap is spent. Nothing more will be learned today; raise the cap or wait for midnight UTC.'
  }
  if (used >= cap * 0.9) {
    return 'Today’s cap is nearly spent. Conversations after it is reached will not be distilled.'
  }
  return null
}

/**
 * What the debounce delay means, in a sentence.
 *
 * Worth spelling out: the delay is measured from the *last* turn, so a long conversation
 * produces one pass at the end rather than one every thirty seconds. Nobody guesses that
 * from a number.
 */
export function debounceSummary(form: DistillationForm): string {
  const seconds = Number(form.debounceSeconds)
  if (!Number.isFinite(seconds)) return ''
  const wait = seconds >= 60 ? `${Math.round(seconds / 60)} minutes` : `${seconds} seconds`
  return `A conversation is distilled once it has been quiet for ${wait}. A burst of turns is one pass, not one per turn.`
}

/** What the org-wide switch means when it is off. */
export function offSummary(form: DistillationForm): string | null {
  if (form.enabled) return null
  return 'No transcript is read and no model is called for any gateway. Facts already stored are still recalled; nothing new is learned.'
}

// ---------------------------------------------------------------------------
// memory health
// ---------------------------------------------------------------------------

/** A ratio as a percentage. `0.9166666` in a table is noise. */
export function percent(rate: number): string {
  return `${Math.round(rate * 100)}%`
}

/** One decimal, because "3.0 facts per person" and "3 facts" read differently. */
export function average(value: number): string {
  return value.toFixed(1)
}

export type HealthWarning = { level: 'warn' | 'info'; text: string }

/**
 * The sentence a rate deserves, or `null` when the numbers are unremarkable.
 *
 * The two thresholds are the ones SPEC §10.1 cannot express and this feature cannot do
 * without. Both are only meaningful once there is enough traffic to have a rate at all,
 * which is why every branch checks the denominator first — a single pass that happened to
 * deduplicate its one candidate is not a 100% dedupe rate worth alarming about.
 */
export function healthWarning(health: MemoryHealth): HealthWarning | null {
  if (health.runs === 0) {
    return {
      level: 'info',
      text: 'No distillation has run in this window. Either no conversations have been logged with an end user, or write-back is switched off.',
    }
  }
  if (health.failure_rate >= 0.25) {
    return {
      level: 'warn',
      text: `${percent(health.failure_rate)} of passes failed. Check the distillation model — an unreachable or misconfigured one fails every pass and affects nothing else, so nothing else will tell you.`,
    }
  }
  if (health.candidates >= 20 && health.dedupe_rate >= 0.95) {
    return {
      level: 'warn',
      text: 'Almost everything extracted was already known. Passes are succeeding and producing nothing new — usually a model paraphrasing the conversation back, or a dedupe threshold set too low.',
    }
  }
  if (health.candidates >= 50 && health.supersession_rate === 0) {
    return {
      level: 'warn',
      text: 'Nothing has been superseded in this window. People change their minds, and a memory that only ever grows ends up holding two contradictory facts with no way to tell which is current.',
    }
  }
  return null
}

/** "142 facts written · 38% already known · 9% replaced something." */
export function healthSummary(health: MemoryHealth): string {
  if (health.runs === 0) return 'Nothing distilled in this window.'
  return [
    `${health.written.toLocaleString()} fact${health.written === 1 ? '' : 's'} written`,
    `${percent(health.dedupe_rate)} already known`,
    `${percent(health.supersession_rate)} replaced something`,
  ].join(' · ')
}
