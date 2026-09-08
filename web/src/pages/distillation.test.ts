import { describe, expect, it } from 'vitest'

import {
  capWarning,
  debounceSummary,
  distillationBody,
  distillationChanged,
  distillationForm,
  healthSummary,
  healthWarning,
  modelSummary,
  offSummary,
  percent,
  usageSummary,
} from '@/pages/distillation'
import { makeDistillationSettings, makeMemoryHealth } from '@/test/factories'

const stored = makeDistillationSettings()

describe('the write-back form', () => {
  it('round-trips what is stored', () => {
    expect(distillationBody(distillationForm(stored))).toMatchObject({
      enabled: true,
      debounce_seconds: 30,
      dedupe_threshold: 0.92,
      max_facts_per_user: 500,
      daily_call_cap: 5000,
      per_user_daily_cap: 24,
    })
  })

  it('always sends the model, because clearing it is a value', () => {
    // `model_id: null` means "go back to the platform default". A body that omitted it
    // when empty would make clearing the selector impossible.
    const body = distillationBody({ ...distillationForm(stored), modelId: '' })

    expect(Object.keys(body)).toContain('model_id')
    expect(body.model_id).toBeNull()
  })

  it('notices a change and ignores a save that changes nothing', () => {
    const form = distillationForm(stored)

    expect(distillationChanged(form, stored)).toBe(false)
    expect(distillationChanged({ ...form, dailyCallCap: '10' }, stored)).toBe(true)
  })
})

describe('what the form says about the model', () => {
  it('names the state where nothing will ever be learned', () => {
    expect(modelSummary(stored)).toContain('No distillation model')
  })

  it('says when the platform is providing one, rather than showing an empty selector', () => {
    // An organization that has chosen nothing is not broken — it is using the platform's
    // model — and a blank selector with no sentence reads as "not configured".
    const settings = makeDistillationSettings({
      effective_model_id: 'm1',
      effective_model_name: 'gpt-4o-mini',
      using_platform_default: true,
    })

    expect(modelSummary(settings)).toContain('platform default')
    expect(modelSummary(settings)).toContain('gpt-4o-mini')
  })

  it('names the organization’s own choice', () => {
    const settings = makeDistillationSettings({
      effective_model_id: 'm1',
      effective_model_name: 'cheap-one',
      using_platform_default: false,
    })

    expect(modelSummary(settings)).toContain('cheap-one')
    expect(modelSummary(settings)).not.toContain('platform default')
  })
})

describe('the cost guard', () => {
  it('shows what has been spent against the cap it will be compared against', () => {
    const settings = makeDistillationSettings({
      usage: { calls_today: 1204, daily_call_cap: 5000, day_started_at: '2026-09-06T00:00:00Z' },
    })

    expect(usageSummary(settings)).toContain('1,204')
    expect(usageSummary(settings)).toContain('5,000')
  })

  it('says so when there is no cap at all', () => {
    const settings = makeDistillationSettings({
      usage: { calls_today: 12, daily_call_cap: 0, day_started_at: '2026-09-06T00:00:00Z' },
    })

    expect(usageSummary(settings)).toContain('No daily cap')
    expect(capWarning(settings)).toBeNull()
  })

  it('warns before the budget is gone, not after', () => {
    // A guard that stops memory silently is worse than no cap. The point of the warning is
    // that it appears while there is still something to do about it.
    const settings = makeDistillationSettings({
      usage: { calls_today: 4800, daily_call_cap: 5000, day_started_at: '2026-09-06T00:00:00Z' },
    })

    expect(capWarning(settings)).toContain('nearly spent')
  })

  it('says plainly when nothing more will be learned today', () => {
    const settings = makeDistillationSettings({
      usage: { calls_today: 5000, daily_call_cap: 5000, day_started_at: '2026-09-06T00:00:00Z' },
    })

    expect(capWarning(settings)).toContain('spent')
    expect(capWarning(settings)).toContain('midnight UTC')
  })

  it('says nothing while there is budget left', () => {
    expect(capWarning(stored)).toBeNull()
  })
})

describe('the explanatory copy', () => {
  it('says the wait is measured from the last turn, which nobody guesses from a number', () => {
    const line = debounceSummary(distillationForm(stored))

    expect(line).toContain('30 seconds')
    expect(line).toContain('one pass, not one per turn')
  })

  it('reads a long wait in minutes', () => {
    const form = { ...distillationForm(stored), debounceSeconds: '300' }

    expect(debounceSummary(form)).toContain('5 minutes')
  })

  it('says what "off" means, which is not what it looks like', () => {
    // Facts already stored are still recalled. Turning write-back off stops learning, not
    // remembering, and a checkbox cannot say that on its own.
    const off = offSummary({ ...distillationForm(stored), enabled: false })

    expect(off).toContain('still recalled')
    expect(offSummary(distillationForm(stored))).toBeNull()
  })
})

describe('memory health', () => {
  it('says nothing has run rather than showing a chart of zeroes', () => {
    const warning = healthWarning(makeMemoryHealth())

    expect(warning?.level).toBe('info')
    expect(warning?.text).toContain('No distillation has run')
  })

  it('names a failure rate that is high enough to be the problem', () => {
    const warning = healthWarning(makeMemoryHealth({ runs: 20, failures: 9, failure_rate: 0.45 }))

    expect(warning?.level).toBe('warn')
    expect(warning?.text).toContain('45%')
  })

  it('catches passes that succeed and produce nothing new', () => {
    // The failure that looks exactly like success: green jobs, no errors, facts on the
    // screen, and everything extracted already known.
    const warning = healthWarning(
      makeMemoryHealth({ runs: 30, candidates: 60, deduped: 59, dedupe_rate: 0.98 }),
    )

    expect(warning?.level).toBe('warn')
    expect(warning?.text).toContain('already known')
  })

  it('catches a memory that only ever grows', () => {
    const warning = healthWarning(
      makeMemoryHealth({ runs: 30, candidates: 80, superseded: 0, supersession_rate: 0 }),
    )

    expect(warning?.text).toContain('superseded')
  })

  it('does not cry wolf over one pass that happened to deduplicate', () => {
    // Both rates are only meaningful once there is enough traffic to have a rate.
    const warning = healthWarning(
      makeMemoryHealth({ runs: 1, candidates: 1, deduped: 1, dedupe_rate: 1 }),
    )

    expect(warning).toBeNull()
  })

  it('summarises the window in one line', () => {
    const line = healthSummary(
      makeMemoryHealth({ runs: 4, written: 142, dedupe_rate: 0.38, supersession_rate: 0.09 }),
    )

    expect(line).toContain('142 facts written')
    expect(line).toContain('38% already known')
    expect(line).toContain('9% replaced something')
  })

  it('rounds a rate rather than printing a float', () => {
    expect(percent(0.9166666)).toBe('92%')
  })
})
