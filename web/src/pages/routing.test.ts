import { describe, expect, it } from 'vitest'

import { makeGateway } from '@/test/factories'
import {
  chainBody,
  chainProblem,
  expectedShare,
  moveRow,
  rowsOf,
  sameChain,
  totalWeight,
  type ChainRow,
} from '@/pages/routing'

/**
 * The routing editor's arithmetic, called rather than clicked.
 *
 * Two things here are worth testing directly instead of through the form: a weight
 * following the wrong model after a reorder, and a total that reads as valid while the
 * server sees something else. Both are silent — the screen looks right either way — so
 * they need assertions on the numbers rather than on the pixels.
 */

const rows = (...pairs: [string, number][]): ChainRow[] =>
  pairs.map(([modelId, weight]) => ({ modelId, weight }))

describe('reordering', () => {
  it('carries each weight with its own model', () => {
    // The failure this exists for: a parallel weights array that reorders independently
    // and silently sends 70% of the traffic to the wrong variant.
    const moved = moveRow(rows(['a', 70], ['b', 30]), 1, 0)

    expect(moved).toEqual(rows(['b', 30], ['a', 70]))
  })

  it('moves a row down as well as up', () => {
    expect(moveRow(rows(['a', 1], ['b', 2], ['c', 3]), 0, 2)).toEqual(
      rows(['b', 2], ['c', 3], ['a', 1]),
    )
  })

  it('leaves the list alone when the move goes nowhere', () => {
    const original = rows(['a', 1], ['b', 2])

    expect(moveRow(original, 0, 0)).toEqual(original)
    expect(moveRow(original, 0, 5)).toEqual(original)
    expect(moveRow(original, -1, 0)).toEqual(original)
  })
})

describe('weights', () => {
  it('totals what is there', () => {
    expect(totalWeight(rows(['a', 70], ['b', 30]))).toBe(100)
  })

  it('treats a cleared number input as zero rather than as NaN', () => {
    // `Number('')` is 0 but `Number('x')` is NaN, and one NaN makes the whole total NaN —
    // which renders as "NaN / 100" and disables the save with no explanation.
    expect(totalWeight([{ modelId: 'a', weight: Number.NaN }])).toBe(0)
  })

  it('reports the share each row actually gets', () => {
    expect(expectedShare(rows(['a', 70], ['b', 20]))).toEqual([
      (70 / 90) * 100,
      (20 / 90) * 100,
    ])
  })

  it('reports zero shares rather than dividing by zero', () => {
    expect(expectedShare(rows(['a', 0], ['b', 0]))).toEqual([0, 0])
  })
})

describe('what can be saved', () => {
  it('allows a gateway with no models at all', () => {
    // A gateway can exist before its models do; the endpoint answers 503 and says so.
    expect(chainProblem('ab_split', [])).toBeNull()
  })

  it('refuses a duplicate model', () => {
    expect(chainProblem('failover', rows(['a', 100], ['a', 100]))).toMatch(/twice/)
  })

  it('refuses a single-model gateway with two targets', () => {
    expect(chainProblem('single', rows(['a', 100], ['b', 100]))).toMatch(/one target/)
  })

  it('refuses a failover chain with nothing to fall back to', () => {
    expect(chainProblem('failover', rows(['a', 100]))).toMatch(/fail over to/)
  })

  it('refuses A/B weights that do not add up to a hundred', () => {
    expect(chainProblem('ab_split', rows(['a', 70], ['b', 20]))).toMatch(/add up to 90/)
  })

  it('accepts a well-formed split', () => {
    expect(chainProblem('ab_split', rows(['a', 70], ['b', 30]))).toBeNull()
  })

  it('ignores a half-filled row while it is being filled in', () => {
    // Adding a target puts an empty row on the screen. Complaining about it before the
    // model has been chosen would make the form shout at somebody mid-edit.
    expect(chainProblem('failover', rows(['a', 100], ['', 100]))).toMatch(/fail over to/)
  })
})

describe('crossing the wire', () => {
  it('reads a saved gateway in the order the server sent it', () => {
    const gateway = makeGateway({
      targets: [
        { id: 'b', name: 'b', dialect: 'openai', enabled: true, organization_id: 'o1', priority: 0, weight: 70 },
        { id: 'a', name: 'a', dialect: 'openai', enabled: true, organization_id: 'o1', priority: 1, weight: 30 },
      ],
    })

    expect(rowsOf(gateway)).toEqual(rows(['b', 70], ['a', 30]))
  })

  it('drops the blank row an unfinished edit leaves behind', () => {
    expect(chainBody(rows(['a', 100], ['', 0]))).toEqual([{ model_id: 'a', weight: 100 }])
  })

  it('compares chains by value so the form is not permanently dirty', () => {
    expect(sameChain(rows(['a', 70]), rows(['a', 70]))).toBe(true)
    expect(sameChain(rows(['a', 70]), rows(['a', 30]))).toBe(false)
    expect(sameChain(rows(['a', 70]), rows(['a', 70], ['b', 30]))).toBe(false)
  })
})
