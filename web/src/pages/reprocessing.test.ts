import { describe, expect, it } from 'vitest'

import {
  compact,
  durationLabel,
  etaLabel,
  indexStatusTone,
  outcomeLine,
  progressLine,
  reasonLabel,
  reprocessCostLine,
  rowReason,
  runTone,
  scopeLabel,
  staleAlertLine,
  staleAmong,
  staleConnectorNotice,
  staleHeadline,
  stalePreviewSentence,
  tokensLine,
  triggerLabel,
} from '@/pages/reprocessing'
import { makeConnector, makeDocument, makeReprocessingRun, makeStaleAlert } from '@/test/factories'

describe('the connector header', () => {
  it('says how many of how many, and why, and nothing when all is current', () => {
    expect(staleHeadline(makeConnector())).toBeNull()
    expect(
      staleHeadline(
        makeConnector({
          document_count: 1200,
          stale_documents: 1184,
          reindex_formats: ['markdown', 'pdf'],
        }),
      ),
    ).toBe('1,184 of 1,200 documents indexed under a previous configuration (markdown, pdf).')
    expect(
      staleHeadline(
        makeConnector({ document_count: 3, reprocessing_documents: 2, unrecorded_documents: 1 }),
      ),
    ).toBe(
      '2 documents being reprocessed · 1 document indexed before the fingerprint was recorded.',
    )
  })

  it('prices each scope from the counts', () => {
    const connector = makeConnector({
      document_count: 40,
      stale_documents: 12,
      unrecorded_documents: 3,
    })
    expect(reprocessCostLine(connector, 'stale')).toMatch(/^Re-ingests the 12 stale documents/)
    expect(reprocessCostLine(makeConnector(), 'stale')).toBe('Nothing is stale.')
    expect(reprocessCostLine(connector, 'unrecorded')).toMatch(/3 unrecorded documents/)
    expect(reprocessCostLine(connector, 'formats')).toBe('Pick at least one format.')
    expect(reprocessCostLine(connector, 'formats', ['code'])).toMatch(/every code document/)
    expect(reprocessCostLine(connector, 'all')).toMatch(/all 40 documents/)
  })
})

describe('progress and history', () => {
  it('writes the progress line with an ETA and the failures', () => {
    expect(progressLine(makeReprocessingRun())).toBe('314 / 1,184, ~6 min remaining, 2 failed')
    expect(
      progressLine(
        makeReprocessingRun({
          failed: 0,
          skipped: 2,
          progress: { done: 314, total: 1184, fraction: 0.26, eta_seconds: null },
        }),
      ),
    ).toBe('314 / 1,184, 2 skipped (source gone)')
  })

  it('formats an ETA at the right unit and never invents one', () => {
    expect(etaLabel(null)).toBeNull()
    expect(etaLabel(30)).toBe('under a minute remaining')
    expect(etaLabel(360)).toBe('~6 min remaining')
    expect(etaLabel(7200)).toBe('~2 h remaining')
  })

  it('summarises an outcome', () => {
    expect(outcomeLine(makeReprocessingRun({ status: 'succeeded', done: 95, failed: 0 }))).toBe(
      '95 documents reprocessed',
    )
    expect(
      outcomeLine(makeReprocessingRun({ status: 'partial', done: 95, failed: 3, skipped: 2 })),
    ).toBe('95 documents reprocessed, 3 failed, 2 skipped')
  })

  it('measures duration to the finish, or to now while running', () => {
    const run = makeReprocessingRun({ status: 'succeeded', finished_at: '2026-09-06T12:02:30Z' })
    expect(durationLabel(run)).toBe('13 min')
    const going = makeReprocessingRun()
    expect(durationLabel(going, new Date('2026-09-06T11:50:20Z').getTime())).toBe('20 s')
    expect(durationLabel(going, new Date('2026-09-06T14:50:00Z').getTime())).toBe('3 h')
  })

  it('puts the estimate beside the spend', () => {
    expect(tokensLine(makeReprocessingRun())).toBe('1.2M estimated · 310k so far spent')
    expect(tokensLine(makeReprocessingRun({ status: 'succeeded', spent_tokens: 1_050_000 }))).toBe(
      '1.2M estimated · 1.1M spent',
    )
    expect(compact(950)).toBe('950')
  })

  it('names triggers, scopes, reasons and tones', () => {
    expect(triggerLabel('embedding_model')).toBe('embedding model change')
    expect(triggerLabel('manual')).toBe('manual')
    expect(scopeLabel({ scope: 'stale', formats: [] })).toBe('stale documents only')
    expect(scopeLabel({ scope: 'formats', formats: ['code', 'pdf'] })).toBe('code, pdf')
    expect(scopeLabel({ scope: 'failed', formats: [] })).toBe('retry of failures')
    expect(reasonLabel('extractor')).toBe('extractor upgraded')
    expect(reasonLabel(null)).toBe('')
    expect(indexStatusTone('stale')).toBe('warn')
    expect(indexStatusTone('reprocessing')).toBe('info')
    expect(indexStatusTone('current')).toBe('ok')
    expect(runTone('partial')).toBe('warn')
    expect(runTone('failed')).toBe('error')
  })
})

describe('what the other screens say', () => {
  it('says before saving how many a change would mark', () => {
    expect(stalePreviewSentence(null)).toBeNull()
    expect(stalePreviewSentence({ formats: {}, total: 0 })).toBeNull()
    expect(stalePreviewSentence({ formats: { markdown: 12, code: 3 }, total: 15 })).toBe(
      'Saving marks 15 indexed documents stale (3 code, 12 markdown). They keep their old chunks until you reprocess them.',
    )
  })

  it('carries the server’s reason onto the row', () => {
    expect(rowReason(makeDocument())).toBeNull()
    expect(
      rowReason(
        makeDocument({
          index_status: 'stale',
          stale_reason: 'tokenizer',
          stale_detail: 'Sized with cl100k_base; the tokenizer is now o200k_base.',
        }),
      ),
    ).toBe('Sized with cl100k_base; the tokenizer is now o200k_base.')
    expect(rowReason(makeDocument({ index_status: 'reprocessing' }))).toMatch(/^Being re-ingested/)
  })

  it('writes the gateway notice and picks the connectors from the form', () => {
    expect(staleConnectorNotice([])).toBeNull()
    expect(
      staleConnectorNotice([
        { id: 'c1', name: 'Docs', stale: 12, reprocessing: 0 },
        { id: 'c2', name: 'Wiki', stale: 0, reprocessing: 4 },
      ]),
    ).toBe(
      'Answers may be drawn from two chunkings until Docs (12 stale) and Wiki (4 reprocessing) are reprocessed.',
    )
    const rows = [
      makeConnector({ id: 'c1', name: 'Docs', stale_documents: 12 }),
      makeConnector({ id: 'c2', name: 'Wiki' }),
      makeConnector({ id: 'c3', name: 'Other', stale_documents: 5 }),
    ]
    expect(staleAmong(rows, ['c1', 'c2'])).toEqual([
      { id: 'c1', name: 'Docs', stale: 12, reprocessing: 0 },
    ])
  })

  it('writes the dashboard entry with the age', () => {
    expect(staleAlertLine(makeStaleAlert())).toBe('1,184 documents stale for 29 h')
    expect(staleAlertLine(makeStaleAlert({ stale_documents: 1, age_hours: 72 }))).toBe(
      '1 document stale for 3 days',
    )
  })
})
