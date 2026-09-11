import { describe, expect, it } from 'vitest'

import { makeConnector } from '@/test/factories'
import {
  FORMAT_KINDS,
  chunkingBody,
  chunkingChanged,
  chunkingForm,
  chunkingProblem,
  comparisonRows,
  formatResolutions,
  strategyCost,
  strategyFields,
  documentTone,
  explanationFor,
  formatBytes,
  inFlight,
  pageLabel,
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
    ['summarizing', 'info'],
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
    expect(resyncSummary({ added: 0, updated: 0, deleted: 0, unchanged: 0, skipped: 5 })).toContain(
      'already in progress',
    )
  })

  it('handles an empty connector', () => {
    expect(resyncSummary({ added: 0, updated: 0, deleted: 0, unchanged: 0, skipped: 0 })).toBe(
      'Nothing to sync.',
    )
  })
})

describe('uploadSnippet', () => {
  it('is a PUT, because that is what a presigned URL accepts', () => {
    const snippet = uploadSnippet('https://storage.test/presigned?sig=abc')

    expect(snippet).toContain('-X PUT')
    expect(snippet).toContain('https://storage.test/presigned?sig=abc')
  })
})

describe('pageLabel', () => {
  const PDF = 'application/pdf'
  const PPTX = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
  const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

  it('names the unit the format actually has', () => {
    expect(pageLabel({ mime_type: PDF, page_count: 147 })).toBe('147 pages')
    expect(pageLabel({ mime_type: PPTX, page_count: 12 })).toBe('12 slides')
    expect(pageLabel({ mime_type: XLSX, page_count: 3 })).toBe('3 sheets')
  })

  it('is singular for one', () => {
    expect(pageLabel({ mime_type: PDF, page_count: 1 })).toBe('1 page')
  })

  it('shows nothing for a format with no such unit', () => {
    // A Word document's pagination is decided by the renderer, so there is no honest
    // number to show and the server sends none.
    expect(pageLabel({ mime_type: 'text/markdown', page_count: null })).toBe('\u2014')
  })
})

describe('explanationFor', () => {
  it('turns a scanned PDF into a state with a next step in it', () => {
    const explained = explanationFor({ reason: 'needs_ocr' })

    expect(explained?.headline).toContain('OCR')
    expect(explained?.guidance).toContain('selectable text')
  })

  it('tells somebody how to get past a password-protected file', () => {
    expect(explanationFor({ reason: 'password_protected' })?.guidance).toContain('unprotected copy')
  })

  it('falls through for a code it does not recognise', () => {
    // The row then shows the server's sentence, which is what every other row shows. A
    // reason added on the server must not blank the explanation out.
    expect(explanationFor({ reason: 'something_new' })).toBeNull()
    expect(explanationFor({ reason: null })).toBeNull()
  })
})

describe('strategy fields', () => {
  it('hides overlap under sentence_window, where the window is the overlap', () => {
    // Offering it would put the same sentence in four chunks instead of three, and
    // nothing on the screen would explain why.
    expect(strategyFields('sentence_window').overlap).toBe(false)
    expect(strategyFields('sentence_window').window).toBe(true)
    expect(strategyFields('recursive').overlap).toBe(true)
  })

  it('shows the breakpoint only for the strategy that uses one', () => {
    expect(strategyFields('semantic').breakpoint).toBe(true)
    expect(strategyFields('code').breakpoint).toBe(false)
  })
})

describe('strategyCost', () => {
  it('warns about semantic where the choice is made, and about nothing else', () => {
    // A trade-off whose consequence arrives months later is one nobody connects to the
    // dropdown that caused it.
    expect(strategyCost('semantic')).toContain('re-cut')
    expect(strategyCost('recursive')).toBeNull()
  })
})

describe('formatResolutions', () => {
  it('reads what each format resolves to off the server rather than recomputing it', () => {
    const connector = makeConnector()
    const code = { ...connector.chunking, strategy: 'code' as const }
    const resolved = formatResolutions({
      ...connector,
      chunking: { ...connector.chunking, overrides: { code: { strategy: 'code' } } },
      effective_chunking: { ...connector.effective_chunking, code },
    })

    const row = resolved.find((entry) => entry.kind === 'code')
    expect(row?.strategy).toBe('code')
    expect(row?.overridden).toBe(true)
    expect(resolved.find((entry) => entry.kind === 'pdf')?.overridden).toBe(false)
  })

  it('lists every format, not only the overridden ones', () => {
    // A table that showed only the overrides would hide the fact that everything else
    // inherits, which is the question somebody opens it to answer.
    expect(formatResolutions(makeConnector())).toHaveLength(FORMAT_KINDS.length)
  })
})

describe('comparisonRows', () => {
  const candidate = (label: string, patch: Record<string, number> = {}) => ({
    label,
    strategy: 'recursive',
    total_chunks: 4,
    embedded_texts: 4,
    best: null,
    chunks: [],
    distribution: {
      chunks: 4,
      min_tokens: 10,
      median_tokens: 40,
      p95_tokens: 60,
      max_tokens: 60,
      at_ceiling: 3,
      mid_sentence: 1,
      ...patch,
    },
  })

  it('puts one column per candidate on every row', () => {
    const rows = comparisonRows([candidate('current'), candidate('proposed')])

    expect(rows.every((row) => row.values.length === 2)).toBe(true)
  })

  it('reports the two numbers that make a strategy look like what it is', () => {
    // How often the size limit decided the boundary, and how often a boundary landed
    // mid-sentence. Without those, two chunkings are just two walls of text.
    const rows = comparisonRows([candidate('current')])

    expect(rows.map((row) => row.label)).toContain('Cut by the size limit')
    expect(rows.map((row) => row.label)).toContain('Boundaries mid-sentence')
  })

  it('shows what one ingestion costs beside what it produces', () => {
    // A comparison that showed quality and hid cost would push every reader toward the
    // most expensive option.
    const rows = comparisonRows([candidate('current')])

    expect(rows.map((row) => row.label)).toContain('Embedding calls per ingestion')
  })
})
