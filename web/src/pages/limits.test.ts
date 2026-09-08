/**
 * The Limits section's arithmetic and the sentences it puts on the screen.
 *
 * Three of these are about a screen being *honest* rather than about a calculation: an
 * empty input has to mean unlimited on the wire, a ceiling has to be explained next to the
 * input it lowered, and four bars at zero must not appear on a gateway that has no limits
 * at all.
 */

import { describe, expect, it } from 'vitest'

import { makeGatewayLimits, makeLimitUsage } from '@/test/factories'
import {
  barTone,
  ceilingNote,
  ceilingSummary,
  limitProblems,
  limitsBody,
  limitsChanged,
  limitsForm,
  pressureSummary,
  throttledEmptyHint,
  usageHeadline,
  usageSummary,
} from '@/pages/limits'

const BLANK = limitsForm(undefined)

describe('the form', () => {
  it('shows an unset limit as an empty input', () => {
    // Not "0" and not "null": empty is what a person types to mean "no limit", so it is
    // what the box has to contain when there is none.
    expect(BLANK.gateway.requests_per_minute).toBe('')
  })

  it('reads both scopes out of the stored blob', () => {
    const form = limitsForm({
      requests_per_minute: 60,
      per_end_user: { requests_per_minute: 5 },
    } as never)

    expect(form.gateway.requests_per_minute).toBe('60')
    expect(form.perEndUser.requests_per_minute).toBe('5')
  })

  it('sends null for an empty input rather than leaving the key out', () => {
    // The blob is deep-merged server-side, so an omitted key means "leave it alone" —
    // which would make clearing a limit impossible.
    const body = limitsBody(BLANK)

    expect(body.requests_per_minute).toBeNull()
    expect('requests_per_minute' in body).toBe(true)
  })

  it('sends the per-end-user block as a nested object', () => {
    const form = { ...BLANK, perEndUser: { ...BLANK.perEndUser, requests_per_minute: '5' } }

    expect(limitsBody(form).per_end_user).toMatchObject({ requests_per_minute: 5 })
  })

  it('notices a change in either scope', () => {
    const changed = { ...BLANK, perEndUser: { ...BLANK.perEndUser, tokens_per_minute: '10' } }

    expect(limitsChanged(BLANK, BLANK)).toBe(false)
    expect(limitsChanged(changed, BLANK)).toBe(true)
  })

  it('refuses a fraction and a zero, in either scope', () => {
    const form = {
      gateway: { ...BLANK.gateway, requests_per_minute: '0' },
      perEndUser: { ...BLANK.perEndUser, tokens_per_minute: '1.5' },
    }

    const problems = limitProblems(form)

    expect(problems).toHaveLength(2)
    expect(problems[0]).toContain('Gateway')
    expect(problems[1]).toContain('Per end user')
  })

  it('does not treat an empty input as a problem', () => {
    expect(limitProblems(BLANK)).toEqual([])
  })
})

describe('the platform ceiling', () => {
  it('is explained only next to an input it actually lowered', () => {
    const limits = makeGatewayLimits({
      global_models: true,
      capped: ['requests_per_minute'],
      ceilings: {
        requests_per_minute: 60,
        tokens_per_minute: null,
        requests_per_day: null,
        concurrent_requests: null,
      },
    })

    expect(ceilingNote(limits, 'requests_per_minute')).toContain('60')
    expect(ceilingNote(limits, 'tokens_per_minute')).toBeNull()
  })

  it('says nothing on a gateway that uses only the organization’s own models', () => {
    // A note that appeared whenever a ceiling *existed* would be on every input of every
    // gateway on the platform, which is how a warning stops being read.
    const limits = makeGatewayLimits({
      ceilings: {
        requests_per_minute: 60,
        tokens_per_minute: null,
        requests_per_day: null,
        concurrent_requests: null,
      },
    })

    expect(ceilingSummary(limits)).toBeNull()
  })

  it('names the caps in the summary when it does apply', () => {
    const limits = makeGatewayLimits({
      global_models: true,
      ceilings: {
        requests_per_minute: 60,
        tokens_per_minute: 10000,
        requests_per_day: null,
        concurrent_requests: null,
      },
    })

    const summary = ceilingSummary(limits)

    expect(summary).toContain('requests per minute 60')
    expect(summary).toContain('tokens per minute 10000')
  })
})

describe('the bars', () => {
  it('is green well inside the budget and amber past 80%', () => {
    expect(barTone(makeLimitUsage({ value: 10, remaining: 6 }))).toBe('ok')
    expect(barTone(makeLimitUsage({ value: 10, remaining: 2 }))).toBe('warn')
  })

  it('is red once nothing is left, which is when 429s start', () => {
    expect(barTone(makeLimitUsage({ value: 10, remaining: 0 }))).toBe('full')
  })

  it('says when a window rolls', () => {
    expect(usageSummary(makeLimitUsage({ value: 10, remaining: 6, reset_seconds: 37 }))).toBe(
      '4 of 10 used · resets in 37s',
    )
  })

  it('does not claim concurrency resets at a time', () => {
    // A slot frees when some other request finishes, which is not a moment anybody can
    // name — and a countdown to it would be a lie the client could act on.
    const summary = usageSummary(
      makeLimitUsage({ limit: 'concurrent_requests', value: 4, remaining: 1, reset_seconds: 0 }),
    )

    expect(summary).toBe('3 of 4 used right now')
    expect(summary).not.toContain('resets')
  })
})

describe('the headline', () => {
  it('says a gateway is unlimited rather than drawing four zeroes', () => {
    // Four bars at zero read as "limited and idle", which is the opposite of the truth.
    expect(usageHeadline(makeGatewayLimits())).toContain('not throttled')
  })

  it('says plainly when requests are being refused right now', () => {
    const limits = makeGatewayLimits({
      usage: [makeLimitUsage({ value: 10, remaining: 0, utilization: 1 })],
    })

    expect(usageHeadline(limits)).toContain('429')
  })

  it('warns before that, at the same threshold the dashboard uses', () => {
    const limits = makeGatewayLimits({
      usage: [makeLimitUsage({ value: 10, remaining: 1, utilization: 0.9 })],
    })

    expect(usageHeadline(limits)).toContain('80%')
  })

  it('picks the tightest limit when several are configured', () => {
    const limits = makeGatewayLimits({
      usage: [
        makeLimitUsage({ limit: 'requests_per_day', value: 1000, remaining: 900, utilization: 0.1 }),
        makeLimitUsage({ limit: 'tokens_per_minute', value: 100, remaining: 2, utilization: 0.98 }),
      ],
    })

    expect(usageHeadline(limits)).toContain('tokens per minute')
  })
})

describe('the dashboard card', () => {
  it('names the gateway, the percentage and the limit', () => {
    const summary = pressureSummary(
      'Support Bot',
      makeLimitUsage({ value: 10, remaining: 1, utilization: 0.9 }),
    )

    expect(summary).toBe('Support Bot is at 90% of its requests per minute limit.')
  })
})

describe('the throttled panel', () => {
  it('tells the two empty states apart', () => {
    // "Nothing was throttled" and "things were throttled but nobody was identified" look
    // identical on screen and need different next actions.
    expect(throttledEmptyHint(0)).toContain('Nothing was rate-limited')
    expect(throttledEmptyHint(12)).toContain('X-Gateway-User')
  })
})
