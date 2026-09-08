import { describe, expect, it } from 'vitest'

import {
  activity,
  displayName,
  emptyMemoryHint,
  factState,
  formatConfidence,
  formatScore,
  purgeDescription,
  purgeSummary,
  stateLabel,
} from '@/pages/endUsers'
import { makeEndUser, makeFact } from '@/test/factories'

describe('factState', () => {
  it('is live when nothing has retracted or expired it', () => {
    expect(factState(makeFact())).toBe('live')
  })

  it('distinguishes retracted from expired', () => {
    const retracted = makeFact({ superseded_at: '2026-09-01T00:00:00Z' })
    const expired = makeFact({ expires_at: '2020-01-01T00:00:00Z' })

    expect(factState(retracted)).toBe('superseded')
    expect(factState(expired)).toBe('expired')
  })

  it('treats a retraction as final even when the expiry is still ahead', () => {
    const fact = makeFact({
      superseded_at: '2026-09-01T00:00:00Z',
      expires_at: '2099-01-01T00:00:00Z',
    })

    expect(factState(fact)).toBe('superseded')
  })

  it('counts an expiry in the future as still live', () => {
    expect(factState(makeFact({ expires_at: '2099-01-01T00:00:00Z' }))).toBe('live')
  })

  it('gives the two dead states different sentences', () => {
    // They need different actions: one was a person's decision, the other a clock.
    expect(stateLabel('superseded')).not.toBe(stateLabel('expired'))
    expect(stateLabel('live')).toBeNull()
  })
})

describe('formatting', () => {
  it('renders a confidence as a whole percentage', () => {
    expect(formatConfidence(0.85)).toBe('85%')
    expect(formatConfidence(1)).toBe('100%')
  })

  it('renders a score as two decimals', () => {
    expect(formatScore(0.7100000000000001)).toBe('0.71')
  })
})

describe('displayName', () => {
  it('prefers a label somebody set', () => {
    expect(displayName(makeEndUser({ label: 'Ada from Acme' }))).toBe('Ada from Acme')
  })

  it('falls back to the id the application sends', () => {
    expect(displayName(makeEndUser({ external_id: 'customer-4471' }))).toBe('customer-4471')
  })

  it('shortens an anonymous id, because sixteen hex characters is not a name', () => {
    const shown = displayName(
      makeEndUser({ external_id: 'anon:0123456789abcdef', anonymous: true }),
    )

    expect(shown).toBe('Anonymous 012345')
  })
})

describe('emptyMemoryHint', () => {
  it('says nothing when there is memory to show', () => {
    expect(emptyMemoryHint(makeEndUser({ fact_count: 3 }))).toBeNull()
  })

  it('names the integration when the caller is anonymous', () => {
    const hint = emptyMemoryHint(
      makeEndUser({ fact_count: 0, anonymous: true, request_count: 40 }),
    )

    expect(hint).toContain('X-Gateway-User')
  })

  it('does not treat a first sighting as a problem', () => {
    const hint = emptyMemoryHint(makeEndUser({ fact_count: 0, request_count: 1 }))

    expect(hint).toContain('Seen once')
  })

  it('says what is missing for somebody with real traffic and no memory', () => {
    const hint = emptyMemoryHint(makeEndUser({ fact_count: 0, request_count: 40 }))

    expect(hint).toContain('distillation')
  })
})

describe('purge copy', () => {
  it('names what goes and what stays', () => {
    const text = purgeDescription(makeEndUser({ fact_count: 3 }), false)

    expect(text).toContain('3 facts')
    expect(text).toContain('request history stays')
  })

  it('mentions the bodies only when they are included', () => {
    const without = purgeDescription(makeEndUser(), false)
    const withThem = purgeDescription(makeEndUser(), true)

    expect(without).not.toContain('response body')
    expect(withThem).toContain('response body')
  })

  it('reports numbers afterwards rather than "done"', () => {
    expect(purgeSummary({ facts: 4, transcripts: 0 })).toBe('Removed 4 facts.')
    expect(purgeSummary({ facts: 1, transcripts: 2 })).toBe('Removed 1 fact and 2 transcripts.')
  })
})

describe('activity', () => {
  it('reads as one line of traffic', () => {
    const line = activity(makeEndUser({ request_count: 1 }))

    expect(line).toContain('1 request ·')
    expect(line).not.toContain('1 requests')
  })
})
