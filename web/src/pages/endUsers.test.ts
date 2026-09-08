import { describe, expect, it } from 'vitest'

import {
  activity,
  displayName,
  emptyMemoryHint,
  factOrigin,
  factState,
  formatConfidence,
  formatScore,
  groupFacts,
  passSummary,
  provenanceLink,
  purgeDescription,
  purgeSummary,
  stateLabel,
} from '@/pages/endUsers'
import { makeEndUser, makeFact } from '@/test/factories'

const NOW = '2026-09-06T12:00:00Z'

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

  it('offers an action for somebody with real traffic and no memory', () => {
    // Not "wait": distillation exists now, and the two things that produce a fact — typing
    // one, and running a pass — are both one click away on this person's page.
    const hint = emptyMemoryHint(makeEndUser({ fact_count: 0, request_count: 40 }))

    expect(hint).toContain('distil')
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

describe('folding a retracted fact under its replacement', () => {
  it('shows the pair as a pair', () => {
    // "Why did it say that last month" is answered by the fact that has since been
    // replaced, and the answer is only complete if the two are together.
    const replacement = makeFact({ id: 'new', text: 'Works in Go.' })
    const old = makeFact({
      id: 'old',
      text: 'Works in Rust.',
      superseded_at: '2026-09-01T00:00:00Z',
      superseded_by_id: 'new',
    })

    const groups = groupFacts([replacement, old])

    expect(groups).toHaveLength(1)
    expect(groups[0]?.fact.id).toBe('new')
    expect(groups[0]?.replaced.map((fact) => fact.id)).toEqual(['old'])
  })

  it('keeps a retracted fact whose replacement is not on the page', () => {
    // Deleted, or on the next page. Folding it under something absent would make it
    // vanish from a list it is genuinely part of.
    const orphan = makeFact({
      id: 'old',
      superseded_at: '2026-09-01T00:00:00Z',
      superseded_by_id: 'gone',
    })

    expect(groupFacts([orphan]).map((group) => group.fact.id)).toEqual(['old'])
  })

  it('keeps a fact retracted by hand at the top level', () => {
    // Nothing replaced it — somebody said it was wrong — so there is no pair to show.
    const retracted = makeFact({ superseded_at: '2026-09-01T00:00:00Z' })

    expect(groupFacts([retracted])).toEqual([{ fact: retracted, replaced: [] }])
  })

  it('folds several generations under the one that is current', () => {
    const current = makeFact({ id: 'c' })
    const first = makeFact({ id: 'a', superseded_by_id: 'c', superseded_at: NOW })
    const second = makeFact({ id: 'b', superseded_by_id: 'c', superseded_at: NOW })

    expect(groupFacts([current, first, second])[0]?.replaced).toHaveLength(2)
  })
})

describe('provenance', () => {
  it('links a distilled fact to the request it was learned from', () => {
    const fact = makeFact({ source_log_id: 'log-1' })

    expect(provenanceLink(fact)).toBe('/monitoring?request=log-1')
    expect(factOrigin(fact)).toContain('conversation')
  })

  it('says a hand-written fact was written by hand rather than linking nowhere', () => {
    expect(provenanceLink(makeFact())).toBeNull()
    expect(factOrigin(makeFact())).toBe('Added by hand')
  })
})

describe('what "distil now" says afterwards', () => {
  it('reports the dispositions, because a count alone is not evidence', () => {
    const line = passSummary({ sessions: 2, inserted: 1, deduped: 3, superseded: 1 })

    expect(line).toContain('2 conversations')
    expect(line).toContain('1 new')
    expect(line).toContain('3 already known')
  })

  it('distinguishes "nothing new to read" from "nothing was learned"', () => {
    // Zero facts written has several causes and they need different actions. "0" alone is
    // the answer that sends somebody to read logs.
    const line = passSummary({ sessions: 0, inserted: 0, deduped: 0, superseded: 0 })

    expect(line).toContain('already been distilled')
  })
})
