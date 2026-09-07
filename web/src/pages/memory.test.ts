import { describe, expect, it } from 'vitest'

import type { MemoryConfig, RetrievalPreviewResponse } from '@/api/types'
import { makeConnector, makeGateway } from '@/test/factories'
import {
  attachable,
  connectorLabel,
  contextUsage,
  formatScore,
  memoryBody,
  memoryChanged,
  memoryForm,
  memoryProblem,
  memoryWarning,
  retrievalSummary,
  sameIds,
} from '@/pages/memory'

const stored: MemoryConfig = makeGateway().memory_config

function preview(overrides: Partial<RetrievalPreviewResponse> = {}): RetrievalPreviewResponse {
  return {
    query: 'refunds',
    outcome: 'hit',
    latency_ms: 12,
    error: null,
    chunks: [],
    injected_tokens: 0,
    doc_max_tokens: 2000,
    ...overrides,
  }
}

function chunk(injected: boolean) {
  return {
    id: 'c1',
    score: 0.71,
    text: 'Refunds take 14 days.',
    source_name: 'handbook.md',
    page_or_section: null,
    document_id: null,
    connector_id: null,
    chunk_index: 0,
    tokens: 20,
    injected,
  }
}

describe('the memory form', () => {
  it('round-trips the stored configuration', () => {
    expect(memoryBody(memoryForm(stored))).toMatchObject({
      connector_ids: [],
      doc_top_k: 6,
      doc_min_score: 0.35,
      doc_max_tokens: 2000,
      query_strategy: 'last_user_message',
      query_n_turns: 3,
      retrieval_timeout_ms: 800,
      on_retrieval_error: 'fail_open',
    })
  })

  it('sends only the document half, so task 12 can own the rest of the blob', () => {
    // The server deep-merges, so a key this form does not send is a key it cannot wipe.
    expect(Object.keys(memoryBody(memoryForm(stored)))).not.toContain('memory_top_k')
  })

  it('sees no change when nothing was typed', () => {
    // The form holds strings and the response holds numbers; `!==` between them is
    // always true, which would make the save button permanently enabled.
    expect(memoryChanged(memoryForm(stored), stored)).toBe(false)
  })

  it.each([
    ['docTopK', { docTopK: '8' }],
    ['docMinScore', { docMinScore: '0.5' }],
    ['queryStrategy', { queryStrategy: 'last_n_turns' }],
    ['onRetrievalError', { onRetrievalError: 'fail_closed' }],
    ['connectorIds', { connectorIds: ['c1'] }],
  ])('sees a change to %s', (_field, patch) => {
    expect(memoryChanged({ ...memoryForm(stored), ...patch }, stored)).toBe(true)
  })

  it('compares connector ids by value, not by identity', () => {
    expect(sameIds(['a', 'b'], ['a', 'b'])).toBe(true)
    expect(sameIds(['a', 'b'], ['b', 'a'])).toBe(false)
    expect(sameIds(['a'], ['a', 'b'])).toBe(false)
  })
})

describe('memoryProblem', () => {
  const base = memoryForm(stored)

  it('accepts the defaults', () => {
    expect(memoryProblem(base)).toBeNull()
  })

  it.each([['0'], ['101'], ['']])('refuses a top_k of %s', (value) => {
    expect(memoryProblem({ ...base, docTopK: value })).toContain('Chunks to retrieve')
  })

  it.each([['-0.1'], ['1.5'], ['abc']])('refuses a score of %s', (value) => {
    expect(memoryProblem({ ...base, docMinScore: value })).toContain('cosine')
  })

  it('accepts a score of exactly 0 or 1', () => {
    expect(memoryProblem({ ...base, docMinScore: '0' })).toBeNull()
    expect(memoryProblem({ ...base, docMinScore: '1' })).toBeNull()
  })

  it('accepts a token budget of zero, because detaching is a different decision', () => {
    expect(memoryProblem({ ...base, docMaxTokens: '0' })).toBeNull()
  })

  it('checks the turn count only when the strategy uses it', () => {
    expect(memoryProblem({ ...base, queryNTurns: '0' })).toBeNull()
    expect(
      memoryProblem({ ...base, queryStrategy: 'last_n_turns', queryNTurns: '0' }),
    ).toContain('Turns to include')
  })

  it.each([['10'], ['9000']])('refuses a timeout of %s ms', (value) => {
    expect(memoryProblem({ ...base, retrievalTimeoutMs: value })).toContain('timeout')
  })
})

describe('memoryWarning', () => {
  const base = memoryForm(stored)

  it('says a gateway with no connectors retrieves nothing', () => {
    expect(memoryWarning(base)).toContain('retrieves nothing')
  })

  it('says nothing when the configuration will work', () => {
    expect(memoryWarning({ ...base, connectorIds: ['c1'] })).toBeNull()
  })

  it('warns that a zero budget finds chunks and then throws them away', () => {
    const warning = memoryWarning({ ...base, connectorIds: ['c1'], docMaxTokens: '0' })

    expect(warning).toContain('found and then dropped')
  })

  it('warns that a very high score floor rejects almost everything', () => {
    // The single most common cause of "Try retrieval comes back empty", and the one
    // nobody suspects because the number looks like a quality setting.
    const warning = memoryWarning({ ...base, connectorIds: ['c1'], docMinScore: '0.9' })

    expect(warning).toContain('rejects almost everything')
  })
})

describe('retrievalSummary', () => {
  it('says how many chunks would be injected and what they cost', () => {
    const summary = retrievalSummary(
      preview({ chunks: [chunk(true), chunk(true)], injected_tokens: 140 }),
    )

    expect(summary.tone).toBe('ok')
    expect(summary.message).toContain('2 chunks')
    expect(summary.message).toContain('140 tokens')
  })

  it('names the budget when something was dropped', () => {
    // The interesting case: the corpus is fine and the budget is the constraint.
    const summary = retrievalSummary(
      preview({ chunks: [chunk(true), chunk(false)], injected_tokens: 70 }),
    )

    expect(summary.message).toContain('1 dropped')
  })

  it('distinguishes nothing matched from nothing searched', () => {
    expect(retrievalSummary(preview({ outcome: 'empty' })).message).toContain('score floor')
    expect(retrievalSummary(preview({ outcome: 'skipped' })).message).toContain(
      'Attach at least one connector',
    )
  })

  it('shows the server’s own words for a failure', () => {
    const summary = retrievalSummary(
      preview({ outcome: 'timeout', error: 'The knowledge base did not answer within 800 ms.' }),
    )

    expect(summary.tone).toBe('error')
    expect(summary.message).toContain('800 ms')
  })
})

describe('formatScore', () => {
  it('renders two decimals rather than a float artefact', () => {
    expect(formatScore(0.7100000000000001)).toBe('0.71')
    expect(formatScore(1)).toBe('1.00')
  })
})

describe('contextUsage', () => {
  it('is a percentage of the declared window', () => {
    expect(contextUsage(4000, 8192)).toBe(49)
  })

  it('is null when the model has not declared one', () => {
    // A percentage of an invented denominator looks precise and means nothing.
    expect(contextUsage(4000, null)).toBeNull()
    expect(contextUsage(4000, 0)).toBeNull()
  })

  it('never exceeds a hundred', () => {
    expect(contextUsage(20_000, 8192)).toBe(100)
  })
})

describe('the connector list', () => {
  it('does not offer a connector that is being deleted', () => {
    // Its vectors are on their way out; attaching it produces a gateway that silently
    // stops retrieving.
    const list = [makeConnector({ id: 'a' }), makeConnector({ id: 'b', status: 'deleting' })]

    expect(attachable(list).map((connector) => connector.id)).toEqual(['a'])
  })

  it('says what each connector actually has in it', () => {
    expect(connectorLabel(makeConnector({ counts: { indexed: 3 } }))).toBe('3 documents indexed')
    expect(
      connectorLabel(makeConnector({ document_count: 0, counts: {} })),
    ).toBe('No documents yet')
    expect(
      connectorLabel(makeConnector({ document_count: 2, counts: { embedding: 2 } })),
    ).toContain('none indexed yet')
  })
})
