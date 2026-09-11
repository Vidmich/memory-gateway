import { describe, expect, it } from 'vitest'

import {
  auditAge,
  auditHeadline,
  chunkHistogram,
  deltaTone,
  driftCostLine,
  findingLink,
  formatDelta,
  lastWeek,
  metric,
  runCostLine,
  runHeadline,
  runStatusLine,
  runWarnings,
  setSummary,
  severityLabel,
  severityTone,
  sourceLabel,
} from '@/pages/validation'
import {
  makeAudit,
  makeAuditStatus,
  makeEvaluationRun,
  makeEvaluationSet,
  makeFinding,
} from '@/test/factories'

describe('audit sentences', () => {
  it('says how old a report is, and when it never ran or failed', () => {
    const now = new Date('2026-09-06T14:00:00Z').getTime()
    expect(auditAge(null)).toBe('Never run.')
    expect(auditAge(makeAudit(), now)).toBe('Run 2 h ago.')
    expect(auditAge(makeAudit({ status: 'running', finished_at: null }), now)).toBe('Running now.')
    expect(auditAge(makeAudit({ status: 'failed' }), now)).toBe('Failed 2 h ago.')
  })

  it('counts the findings by colour in the headline', () => {
    expect(auditHeadline(makeAudit())).toBe('1,375 points, 1 red and 1 amber findings.')
    expect(auditHeadline(makeAudit({ status: 'running', report: null }))).toBe('')
  })

  it('maps severities to the badge vocabulary', () => {
    expect(severityTone('red')).toBe('error')
    expect(severityTone('amber')).toBe('warn')
    expect(severityTone('green')).toBe('ok')
    expect(severityTone(null)).toBe('neutral')
    expect(severityLabel('red')).toBe('needs attention')
    expect(severityLabel(undefined)).toBe('not audited')
  })

  it('sends a finding to the page that fixes it', () => {
    expect(findingLink('c1', makeFinding(), 'd2')).toEqual({
      to: '/connectors/c1?compare=d2',
      label: 'Open in Compare',
    })
    expect(findingLink('c1', makeFinding({ action: 'reindex' }))?.to).toBe(
      '/connectors/c1#chunking',
    )
    expect(findingLink('c1', makeFinding({ action: 'platform' }))?.to).toBe('/platform/settings')
    expect(findingLink('c1', makeFinding({ action: 'none' }), 'd2')?.to).toBe(
      '/connectors/c1?document=d2',
    )
    expect(findingLink('c1', makeFinding({ action: 'none' }))).toBeNull()
  })

  it('prices the drift check before it runs', () => {
    expect(driftCostLine(makeAuditStatus())).toBe(
      'Re-embeds 100 of 1,375 chunks now — about 52,000 tokens at the embedding provider.',
    )
    expect(
      driftCostLine(makeAuditStatus({ drift_estimate: { points: 0, sample: 0, tokens: 0 } })),
    ).toBe('Nothing is indexed to re-embed.')
  })
})

describe('the histogram', () => {
  it('buckets token counts the way the server does', () => {
    const built = chunkHistogram([10, 60, 60, 399, 400, 5000], 400)

    expect(built.bucket_tokens).toBe(50)
    expect(built.buckets).toHaveLength(12)
    expect(built.buckets[0]?.count).toBe(1)
    expect(built.buckets[1]?.count).toBe(2)
    expect(built.buckets[11]?.count).toBe(1)
    expect(built.buckets.reduce((sum, bucket) => sum + bucket.count, 0)).toBe(6)
  })
})

describe('run sentences', () => {
  it('formats a missing number as a dash, never as zero', () => {
    expect(metric(null)).toBe('—')
    expect(metric(0)).toBe('0.00')
    expect(metric(0.8234)).toBe('0.82')
  })

  it('writes the headline from the chunk column before the budget', () => {
    expect(runHeadline(makeEvaluationRun())).toBe('recall@6 0.82 · precision@6 0.41 · MRR 0.77')
    expect(
      runHeadline(makeEvaluationRun({ status: 'running', completed_items: 12, total_items: 52 })),
    ).toBe('Running: 12 of 52 questions.')
    expect(runStatusLine(makeEvaluationRun({ status: 'failed', error: 'no gateway' }))).toBe(
      'Failed: no gateway.',
    )
  })

  it('carries the server’s warnings and nothing else', () => {
    expect(runWarnings(makeEvaluationRun())).toHaveLength(1)
    expect(runWarnings(makeEvaluationRun({ metrics: {} }))).toEqual([])
  })

  it('describes a set by what it holds', () => {
    expect(setSummary(makeEvaluationSet())).toBe('52 questions · 3 verified · 2 negative')
    expect(
      setSummary(
        makeEvaluationSet({ counts: { total: 10, verified: 0, generated: 10, negatives: 0 } }),
      ),
    ).toBe('10 questions · 0 verified · 10 written by a model')
    expect(
      setSummary(
        makeEvaluationSet({ counts: { total: 0, verified: 0, generated: 0, negatives: 0 } }),
      ),
    ).toBe('No questions yet.')
  })

  it('says what Run will spend', () => {
    expect(runCostLine(makeEvaluationSet())).toMatch(/^52 embedding calls — one per question/)
    expect(runCostLine({ counts: { total: 800 } })).toMatch(/^500 embedding calls/)
    expect(runCostLine({ counts: { total: 0 } })).toBe('Nothing to run.')
  })

  it('formats and colours a delta', () => {
    expect(formatDelta(0.12)).toBe('+0.12')
    expect(formatDelta(-0.3)).toBe('-0.30')
    expect(formatDelta(0.001)).toBe('±0.00')
    expect(formatDelta(null)).toBe('—')
    expect(deltaTone(0.12)).toBe('ok')
    expect(deltaTone(-0.12)).toBe('error')
    expect(deltaTone(0)).toBe('neutral')
  })

  it('names where a label came from', () => {
    expect(sourceLabel('citation')).toBe('from a citation')
    expect(sourceLabel('generated')).toBe('by a model')
  })

  it('defaults the import window to the last seven days', () => {
    const window = lastWeek(new Date('2026-09-08T00:00:00Z'))
    expect(window).toEqual({ from: '2026-09-01T00:00:00.000Z', to: '2026-09-08T00:00:00.000Z' })
  })
})
