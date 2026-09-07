import { describe, expect, it } from 'vitest'

import { makeConnector } from '@/test/factories'
import {
  chunkingBody,
  chunkingChanged,
  chunkingForm,
  chunkingProblem,
  chunkingWarning,
  documentTone,
  formatBytes,
  inFlight,
  resyncSummary,
  statusSummary,
  uploadSnippet,
} from '@/pages/connectors'

describe('formatBytes', () => {
  it.each([
    [0, '0 B'],
    [512, '512 B'],
    [1024, '1 KB'],
    [1536, '1.5 KB'],
    [4 * 1024 * 1024, '4 MB'],
    [1024 * 1024 * 1024, '1 GB'],
  ])('renders %i as %s', (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected)
  })

  it('never renders a negative or nonsense size', () => {
    expect(formatBytes(-1)).toBe('0 B')
    expect(formatBytes(Number.NaN)).toBe('0 B')
  })
})

describe('statusSummary', () => {
  it('leads with what is wrong', () => {
    const summary = statusSummary(
      makeConnector({ document_count: 100, counts: { indexed: 99, failed: 1 } }),
    )

    expect(summary.tone).toBe('error')
    expect(summary.label).toBe('1 failed')
    expect(summary.detail).toContain('1 of 100')
  })

  it('says what is happening when nothing is wrong', () => {
    const summary = statusSummary(
      makeConnector({ document_count: 3, counts: { indexed: 1, embedding: 2 } }),
    )

    expect(summary.label).toBe('Indexing')
    expect(summary.detail).toContain('2 documents still to process')
  })

  it('prefers a failure to a job in flight', () => {
    // Both are true at once during a large upload. The failure is the one somebody has
    // to act on; "Indexing" would hide it until the very end.
    const summary = statusSummary(
      makeConnector({ document_count: 3, counts: { failed: 1, embedding: 2 } }),
    )

    expect(summary.label).toBe('1 failed')
  })

  it('says a connector being deleted is being deleted, whatever else is true', () => {
    const summary = statusSummary(
      makeConnector({ status: 'deleting', counts: { failed: 4, indexed: 2 } }),
    )

    expect(summary.label).toBe('Deleting')
  })

  it('distinguishes empty from ready', () => {
    // Both have nothing wrong and nothing running, and they need different next steps.
    expect(statusSummary(makeConnector({ document_count: 0, counts: {} })).label).toBe('Empty')
    expect(statusSummary(makeConnector({ document_count: 2, counts: { indexed: 2 } })).label).toBe(
      'Ready',
    )
  })

  it('counts a syncing connector as busy even before any document moves', () => {
    const summary = statusSummary(makeConnector({ status: 'syncing', counts: { indexed: 2 } }))

    expect(summary.label).toBe('Indexing')
  })

  it('surfaces a connector-level error verbatim', () => {
    const summary = statusSummary(
      makeConnector({ status: 'error', error: 'Storage refused the listing.' }),
    )

    expect(summary.detail).toBe('Storage refused the listing.')
  })

  it('does not count a skipped document as work outstanding', () => {
    // `skipped` is terminal. Counting it as in-flight would leave a connector reading
    // "Indexing" forever after somebody dropped in a video.
    expect(inFlight(makeConnector({ counts: { skipped: 3, indexed: 1 } }))).toBe(0)
  })
})

describe('documentTone', () => {
  it.each([
    ['indexed', 'ok'],
    ['failed', 'error'],
    ['skipped', 'warn'],
    ['pending', 'info'],
    ['extracting', 'info'],
    ['embedding', 'info'],
  ])('renders %s as %s', (status, tone) => {
    expect(documentTone(status)).toBe(tone)
  })

  it('renders an unrecognised status neutrally rather than breaking', () => {
    expect(documentTone('transmogrifying')).toBe('neutral')
  })
})

describe('chunking form', () => {
  const stored = makeConnector().chunking

  it('round-trips the stored configuration', () => {
    expect(chunkingBody(chunkingForm(stored))).toMatchObject({
      strategy: 'recursive',
      chunk_size: 1000,
      overlap: 150,
      respect_boundaries: true,
    })
  })

  it('sees no change when nothing was typed', () => {
    // The form holds strings and the response holds numbers; `!==` between them is
    // always true, which would make the save button permanently enabled.
    expect(chunkingChanged(chunkingForm(stored), stored)).toBe(false)
  })

  it.each([
    ['strategy', { strategy: 'fixed' }],
    ['chunkSize', { chunkSize: '500' }],
    ['overlap', { overlap: '20' }],
    ['respectBoundaries', { respectBoundaries: false }],
  ])('sees a change to %s', (_field, patch) => {
    expect(chunkingChanged({ ...chunkingForm(stored), ...patch }, stored)).toBe(true)
  })
})

describe('chunkingProblem', () => {
  const base = chunkingForm(makeConnector().chunking)

  it('accepts the defaults', () => {
    expect(chunkingProblem(base)).toBeNull()
  })

  it('refuses an overlap over half the chunk size', () => {
    // Not an aesthetic limit: at overlap >= size the splitter cannot advance at all.
    expect(chunkingProblem({ ...base, chunkSize: '100', overlap: '60' })).toContain('half')
  })

  it('accepts an overlap of exactly half', () => {
    expect(chunkingProblem({ ...base, chunkSize: '100', overlap: '50' })).toBeNull()
  })

  it.each([['10'], ['9000'], ['']])('refuses a chunk size of %s', (size) => {
    expect(chunkingProblem({ ...base, chunkSize: size, overlap: '0' })).toContain('Chunk size')
  })

  it('refuses a negative overlap', () => {
    expect(chunkingProblem({ ...base, overlap: '-1' })).toContain('negative')
  })
})

describe('chunkingWarning', () => {
  it('warns when a change would leave indexed documents stale', () => {
    const warning = chunkingWarning(makeConnector({ counts: { indexed: 4 } }), true)

    expect(warning).toContain('4 documents')
    expect(warning).toContain('reindex')
  })

  it('says nothing when nothing changed', () => {
    expect(chunkingWarning(makeConnector({ counts: { indexed: 4 } }), false)).toBeNull()
  })

  it('says nothing when there is no index to invalidate', () => {
    // A warning on an empty connector is noise, and noise is what makes the real one
    // invisible.
    expect(chunkingWarning(makeConnector({ counts: {}, document_count: 0 }), true)).toBeNull()
  })
})

describe('resyncSummary', () => {
  it('names only what actually happened', () => {
    const message = resyncSummary({ added: 2, updated: 1, deleted: 0, unchanged: 7, skipped: 0 })

    expect(message).toBe('2 added, 1 updated.')
  })

  it('says everything is up to date when nothing moved', () => {
    const message = resyncSummary({ added: 0, updated: 0, deleted: 0, unchanged: 9, skipped: 0 })

    expect(message).toContain('up to date')
    expect(message).toContain('9')
  })

  it('reports work already under way separately from work not needed', () => {
    // "Nothing to sync" next to five documents stuck mid-pipeline would make a broken
    // connector look healthy.
    expect(
      resyncSummary({ added: 0, updated: 0, deleted: 0, unchanged: 0, skipped: 5 }),
    ).toContain('already in progress')
  })

  it('handles an empty connector', () => {
    expect(
      resyncSummary({ added: 0, updated: 0, deleted: 0, unchanged: 0, skipped: 0 }),
    ).toBe('Nothing to sync.')
  })
})

describe('uploadSnippet', () => {
  it('is a PUT, because that is what a presigned URL accepts', () => {
    const snippet = uploadSnippet('https://storage.test/presigned?sig=abc')

    expect(snippet).toContain('-X PUT')
    expect(snippet).toContain('https://storage.test/presigned?sig=abc')
  })
})
