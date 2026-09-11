import { describe, expect, it } from 'vitest'

import {
  deriveTokenizer,
  describeCalibration,
  driftingTargets,
  formatDrift,
  formatRatio,
  tokenizerKey,
  tokenizerSummary,
} from '@/pages/tokenizers'
import { makeCalibration, makeTokenizers } from '@/test/factories'

const table = makeTokenizers()

describe('deriving a tokenizer in the browser (task 101)', () => {
  it('applies the longest-prefix rule the server applies', () => {
    expect(tokenizerKey(deriveTokenizer(table, 'openai', 'gpt-4o-mini'))).toBe('o200k_base')
    expect(tokenizerKey(deriveTokenizer(table, 'openai', 'gpt-4-turbo'))).toBe('cl100k_base')
    expect(tokenizerKey(deriveTokenizer(table, 'anthropic', 'claude-sonnet-4-5'))).toBe(
      'approximate:3.5',
    )
  })

  it('matches the tail of a vendor-namespaced id', () => {
    expect(tokenizerKey(deriveTokenizer(table, 'openai', 'openai/gpt-4o-mini'))).toBe('o200k_base')
    expect(tokenizerKey(deriveTokenizer(table, 'openai', 'anthropic/claude-3.5-sonnet'))).toBe(
      'approximate:3.5',
    )
  })

  it('falls to the table’s own fallback for anything unknown', () => {
    expect(tokenizerKey(deriveTokenizer(table, 'openai', 'llama3.2'))).toBe('approximate:4')
    expect(tokenizerKey(deriveTokenizer(table, 'hash', 'hash-bow'))).toBe('approximate:4')
  })

  it('claims a whole dialect with an empty prefix', () => {
    expect(tokenizerKey(deriveTokenizer(table, 'anthropic', 'whatever'))).toBe('approximate:3.5')
  })
})

describe('the labels', () => {
  it('prints a ratio the way the server does', () => {
    expect(formatRatio(3.5)).toBe('3.5')
    expect(formatRatio(4)).toBe('4')
    expect(formatRatio(3.365)).toBe('3.365')
    expect(formatDrift(1.04)).toBe('×1.04')
  })

  it('describes a window, or the honest absence of one', () => {
    expect(describeCalibration(makeCalibration({ ratio: 1.04, samples: 3120 }))).toBe(
      "Our count vs. the provider's: ×1.04 over 3,120 requests.",
    )
    expect(describeCalibration(undefined)).toMatch(/no requests measured yet/i)
    expect(describeCalibration(makeCalibration({ ratio: null, samples: 0 }))).toMatch(
      /no requests measured yet/i,
    )
  })

  it('builds the demo’s one-liner for an approximation', () => {
    const row = makeCalibration({ ratio: 1.04, samples: 3120 })
    expect(tokenizerSummary('approximate:3.5 (derived)', true, row)).toBe(
      'approximate:3.5 (derived), calibrated ×1.04 from 3,120 requests',
    )
    expect(tokenizerSummary('o200k_base (derived)', false, row)).toBe('o200k_base (derived)')
  })

  it('picks out the drifting targets and survives a server without the endpoint', () => {
    const rows = [
      makeCalibration({ model_id: 'a', warns: true }),
      makeCalibration({ model_id: 'b', warns: false }),
      makeCalibration({ model_id: 'c', warns: true }),
    ]
    expect(driftingTargets(rows, ['a', 'b']).map((row) => row.model_id)).toEqual(['a'])
    expect(driftingTargets(undefined, ['a'])).toEqual([])
    expect(driftingTargets({ items: [] } as unknown as typeof rows, ['a'])).toEqual([])
  })
})
