import { describe, expect, it } from 'vitest'

import type { MemoryConfig, RetrievalPreviewResponse } from '@/api/types'
import { makeConnector, makeGateway } from '@/test/factories'
import {
  attachable,
  connectorLabel,
  contextUsage,
  conversationSummary,
  formatScore,
  identityWarning,
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
    tokenizer: 'o200k_base',
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
    handle: 1,
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

  it('round-trips the citation mode, which lives in this blob but is edited under Prompt', () => {
    expect(memoryBody(memoryForm({ ...stored, citations: 'footer' }))).toMatchObject({
      citations: 'footer',
    })
    // A blob written before the field existed reads as off, which is what the server
    // defaults it to — the form must not invent a different answer.
    const { citations: _omitted, ...older } = stored
    expect(memoryForm(older as typeof stored).citations).toBe('off')
  })

  it('sends both halves of memory, because this section now renders both', () => {
    expect(memoryBody(memoryForm(stored))).toMatchObject({
      memory_enabled: true,
      memory_top_k: 8,
      memory_max_tokens: 600,
      memory_min_score: 0.3,
      allow_anonymous_memory: false,
    })
  })

  it('does not send the per-person fact bound, which belongs to the organization', () => {
    // An end user reaches an organization through however many gateways it has, so a
    // per-endpoint cap on how much may be known about one person is not a cap. It lives in
    // Settings -> Organization, and sending it from here would be a 422.
    expect(Object.keys(memoryBody(memoryForm(stored)))).not.toContain('max_facts_per_user')
  })

  it('still sends a partial, so a field a later task adds is not wiped', () => {
    // The server deep-merges, so a key this form does not send is a key it cannot reset.
    expect(Object.keys(memoryBody(memoryForm(stored)))).not.toContain('dedupe_threshold')
  })

  it('notices a change to the conversation-memory half', () => {
    const form = memoryForm(stored)

    expect(memoryChanged({ ...form, memoryEnabled: false }, stored)).toBe(true)
    expect(memoryChanged({ ...form, memoryTopK: '3' }, stored)).toBe(true)
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

describe('the conversation-memory warning', () => {
  const base = memoryForm(stored)

  it('says nothing when memory is switched off', () => {
    expect(identityWarning({ ...base, memoryEnabled: false })).toBeNull()
  })

  it('names the header when memory is on and only identified callers count', () => {
    // The silent failure it prevents: memory enabled, every caller anonymous, nothing
    // ever stored, and no error anywhere.
    const warning = identityWarning({ ...base, memoryEnabled: true })

    expect(warning).toContain('X-Gateway-User')
  })

  it('says nothing once anonymous callers are remembered too', () => {
    const form = { ...base, memoryEnabled: true, allowAnonymousMemory: true }

    expect(identityWarning(form)).toBeNull()
  })
})

describe('conversationSummary', () => {
  const base = memoryForm(stored)

  it('says what "off" means rather than only that it is off', () => {
    expect(conversationSummary({ ...base, memoryEnabled: false })).toContain(
      'Nothing is recalled',
    )
  })

  it('names the budget and what happens to unidentified callers', () => {
    const line = conversationSummary(base)

    expect(line).toContain('8 facts')
    expect(line).toContain('600 tokens')
    expect(line).toContain('not remembered at all')
  })

  it('changes when anonymous callers are allowed', () => {
    const line = conversationSummary({ ...base, allowAnonymousMemory: true })

    expect(line).toContain('API key and address')
  })
})
