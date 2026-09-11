import { describe, expect, it } from 'vitest'

import {
  describeCost,
  healthSummary,
  summarizationBody,
  summarizationChanged,
  summarizationCost,
  summarizationForm,
  summarizationModelSummary,
  summarizationProblem,
  summarizationWarning,
  summaryStatus,
  waitingSummary,
} from '@/pages/summarization'
import {
  makeConnector,
  makeDocument,
  makeSummarization,
  makeSummarizationHealth,
  makeSummarizationSettings,
} from '@/test/factories'

describe('the cost line', () => {
  it('prices the summarization calls over the documents the connector holds', () => {
    // 4096 bytes over two documents is ~512 tokens each: under the input cap, so the
    // document's own size is what is sent.
    const cost = summarizationCost(makeConnector(), {
      mode: 'summary_chunk',
      max_input_tokens: 12000,
      max_summary_tokens: 150,
    })

    expect(cost).toEqual({ documents: 2, summarizationTokens: 2 * (512 + 150), reembedTokens: 0 })
    expect(describeCost(cost!)).toMatch(/2 documents: about 1,324 tokens/)
    expect(describeCost(cost!)).toMatch(/Nothing already indexed is recut/)
  })

  it('caps what is sent per document at the input limit', () => {
    const connector = makeConnector({ document_count: 1, total_bytes: 400_000 })
    const cost = summarizationCost(connector, {
      mode: 'summary_chunk',
      max_input_tokens: 12000,
      max_summary_tokens: 150,
    })

    expect(cost?.summarizationTokens).toBe(12150)
  })

  it('adds the re-embedding of the whole corpus when contextual is switched on', () => {
    const cost = summarizationCost(makeConnector(), {
      mode: 'contextual',
      max_input_tokens: 12000,
      max_summary_tokens: 150,
    })

    expect(cost?.reembedTokens).toBe(1024)
    expect(describeCost(cost!)).toMatch(/re-embedding every chunk — roughly 1,024 tokens/)
  })

  it('charges no re-embedding to a connector already on contextual', () => {
    const connector = makeConnector({ summarization: makeSummarization({ mode: 'contextual' }) })
    const cost = summarizationCost(connector, {
      mode: 'both',
      max_input_tokens: 12000,
      max_summary_tokens: 150,
    })

    expect(cost?.reembedTokens).toBe(0)
  })

  it('says nothing under off and something honest for an empty connector', () => {
    expect(
      summarizationCost(makeConnector(), { mode: 'off', max_input_tokens: 1, max_summary_tokens: 1 }),
    ).toBeNull()
    const empty = makeConnector({ document_count: 0, counts: {}, total_bytes: 0 })
    const cost = summarizationCost(empty, {
      mode: 'contextual',
      max_input_tokens: 12000,
      max_summary_tokens: 150,
    })
    expect(describeCost(cost!)).toMatch(/costs nothing until documents arrive/)
  })
})

describe('the warning before saving', () => {
  const stored = makeConnector({ counts: { indexed: 3 }, document_count: 3 })

  it('warns that every vector changes when contextual is switched on or off', () => {
    const on = summarizationForm(makeSummarization({ mode: 'contextual' }))
    expect(summarizationWarning(stored, on)).toMatch(/3 documents already indexed become stale/)

    const contextual = makeConnector({
      counts: { indexed: 3 },
      summarization: makeSummarization({ mode: 'contextual' }),
    })
    const off = summarizationForm(makeSummarization({ mode: 'summary_chunk' }))
    expect(summarizationWarning(contextual, off)).toMatch(/embedded with a summary prefix/)
  })

  it('warns when the model moves under contextual, and not under summary_chunk', () => {
    const contextual = makeConnector({
      counts: { indexed: 3 },
      summarization: makeSummarization({ mode: 'contextual', model_id: 'mo1' }),
    })
    expect(
      summarizationWarning(contextual, summarizationForm(makeSummarization({ mode: 'contextual', model_id: 'mo2' }))),
    ).not.toBeNull()

    const chunked = makeConnector({
      counts: { indexed: 3 },
      summarization: makeSummarization({ mode: 'summary_chunk', model_id: 'mo1' }),
    })
    expect(
      summarizationWarning(chunked, summarizationForm(makeSummarization({ mode: 'summary_chunk', model_id: 'mo2' }))),
    ).toBeNull()
  })

  it('says nothing about summary_chunk, and nothing to an empty connector', () => {
    expect(summarizationWarning(stored, summarizationForm(makeSummarization({ mode: 'summary_chunk' })))).toBeNull()
    const empty = makeConnector({ counts: {}, document_count: 0 })
    expect(summarizationWarning(empty, summarizationForm(makeSummarization({ mode: 'contextual' })))).toBeNull()
  })
})

describe('the form', () => {
  it('round-trips the stored section and sends the whole of it', () => {
    const config = makeSummarization({ mode: 'both', model_id: 'mo1', daily_document_cap: 40 })
    const form = summarizationForm(config)

    expect(summarizationChanged(form, config)).toBe(false)
    expect(summarizationBody({ ...form, dailyDocumentCap: '' })).toEqual({
      mode: 'both',
      model_id: 'mo1',
      max_summary_tokens: 150,
      max_input_tokens: 12000,
      daily_document_cap: null,
    })
    expect(summarizationChanged({ ...form, modelId: '' }, config)).toBe(true)
  })

  it('refuses the numbers the server would refuse', () => {
    const form = summarizationForm(makeSummarization({ mode: 'summary_chunk' }))
    expect(summarizationProblem(form)).toBeNull()
    expect(summarizationProblem({ ...form, maxSummaryTokens: '10' })).toMatch(/between 30 and 1000/)
    expect(summarizationProblem({ ...form, maxInputTokens: '100' })).toMatch(/between 500/)
    expect(summarizationProblem({ ...form, dailyDocumentCap: '-1' })).toMatch(/whole number/)
  })
})

describe('what the table says about a summary', () => {
  it('names each state without calling any of them a document failure', () => {
    expect(summaryStatus(makeDocument())).toBeNull()
    expect(
      summaryStatus(
        makeDocument({
          summary_status: 'summarized',
          summary_model: 'cheap',
          summary_tokens_in: 100,
          summary_tokens_out: 20,
        }),
      ),
    ).toEqual({ label: 'summarized', tone: 'ok', detail: 'cheap, 120 tokens.' })
    expect(summaryStatus(makeDocument({ summary_status: 'summarized', summary_model: 'manual' }))?.label).toBe(
      'edited',
    )
    expect(summaryStatus(makeDocument({ summary_status: 'failed', summary_error: 'no' }))).toEqual({
      label: 'summary failed',
      tone: 'warn',
      detail: 'no',
    })
    expect(summaryStatus(makeDocument({ summary_status: 'capped' }))?.tone).toBe('neutral')
  })
})

describe('the panel sentences', () => {
  it('sums the window and flags estimates', () => {
    expect(healthSummary(makeSummarizationHealth())).toBe('12 summarized, 1 failed — 25,800 tokens.')
    expect(healthSummary(makeSummarizationHealth({ capped: 3, estimated_runs: 2 }))).toMatch(
      /3 refused by a cap — 25,800 tokens \(some estimated\)/,
    )
  })

  it('names the waiting documents only when there are any', () => {
    expect(waitingSummary(undefined)).toBeNull()
    expect(waitingSummary(makeSummarizationHealth())).toBeNull()
    expect(
      waitingSummary(
        makeSummarizationHealth({
          waiting_documents: 4,
          waiting: [{ connector_id: 'c1', name: 'Product docs', documents: 4 }],
        }),
      ),
    ).toBe('4 documents waiting on the summarization cap across 1 connector')
  })

  it('says which link of the model chain is answering', () => {
    expect(summarizationModelSummary(makeSummarizationSettings())).toMatch(/platform default, acme-gpt/)
    expect(
      summarizationModelSummary(makeSummarizationSettings({ effective_model_source: 'distillation' })),
    ).toMatch(/distillation model, acme-gpt/)
    expect(
      summarizationModelSummary(makeSummarizationSettings({ effective_model_source: 'summarization' })),
    ).toBe('Summarizing with acme-gpt.')
    expect(
      summarizationModelSummary(makeSummarizationSettings({ effective_model_id: null, effective_model_name: null })),
    ).toMatch(/No summarization model resolves anywhere/)
  })
})
