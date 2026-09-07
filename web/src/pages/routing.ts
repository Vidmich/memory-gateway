/**
 * The routing section's arithmetic, away from its rendering.
 *
 * Pure functions over a list of `{modelId, weight}` rows: reordering, totalling, turning
 * a saved gateway into rows and rows back into a request body. None of it renders, so all
 * of it is tested by calling it — which matters here more than usual, because the two
 * things most likely to go wrong (a weight ending up on the wrong model after a reorder,
 * and a total that reads 100 while the server sees 99) are arithmetic rather than layout.
 */

import type { GatewayResponse } from '@/api/types'

export type RoutingMode = 'single' | 'failover' | 'ab_split'

/** One row of the editor: which model, and what share of the traffic. */
export type ChainRow = { modelId: string; weight: number }

export const TOTAL_WEIGHT = 100

/** SPEC §8.1, in the operator's terms. Each line is about what happens when it breaks. */
export const MODES: { value: RoutingMode; label: string; failure: string }[] = [
  {
    value: 'single',
    label: 'Single model',
    failure: 'One target. If it fails, the caller gets the error.',
  },
  {
    value: 'failover',
    label: 'Failover chain',
    failure:
      'Targets are tried in order. A timeout, a 429 or a 5xx moves to the next one; a 400, 401, 403, 404 or 422 is returned straight away, because the next target would reject it too.',
  },
  {
    value: 'ab_split',
    label: 'A/B split',
    failure:
      'One target per request, chosen by weight. There is no retry: a retried request would land on the other variant and quietly bias the comparison.',
  },
]

export function modeInfo(mode: string): { label: string; failure: string } {
  return MODES.find((entry) => entry.value === mode) ?? MODES[0]!
}

/** How many rows each mode needs before it means anything. Mirrors the server's check. */
export function minimumTargets(mode: string): number {
  return mode === 'single' ? 1 : 2
}

export function totalWeight(rows: readonly ChainRow[]): number {
  return rows.reduce((sum, row) => sum + (Number.isFinite(row.weight) ? row.weight : 0), 0)
}

/**
 * Whether this chain can be saved, and why not when it cannot.
 *
 * `null` means yes. The message is the same sentence the server would answer with, said
 * before the round trip rather than after it — the server is still the authority, and
 * `tests/test_gateway_api.py` is what proves a chain that gets past this is still refused.
 */
export function chainProblem(mode: string, rows: readonly ChainRow[]): string | null {
  const filled = rows.filter((row) => row.modelId)
  if (filled.length === 0) return null // A gateway may exist before its models do.

  const ids = filled.map((row) => row.modelId)
  if (new Set(ids).size !== ids.length) {
    return 'The same model appears twice. A duplicate in a failover chain retries the upstream that just failed.'
  }
  if (mode === 'single' && filled.length > 1) {
    return 'Single-model routing takes one target.'
  }
  if (mode === 'failover' && filled.length < 2) {
    return 'A failover chain needs something to fail over to. Add a second model.'
  }
  if (mode === 'ab_split') {
    if (filled.length < 2) return 'An A/B split needs at least two models to split between.'
    const total = totalWeight(filled)
    if (total !== TOTAL_WEIGHT) {
      return `Weights are percentages and must add up to ${TOTAL_WEIGHT}. These add up to ${total}.`
    }
  }
  return null
}

/** The share each row actually gets, which is only the weight when the total is 100. */
export function expectedShare(rows: readonly ChainRow[]): number[] {
  const total = totalWeight(rows)
  if (total <= 0) return rows.map(() => 0)
  return rows.map((row) => (row.weight / total) * 100)
}

export function moveRow(rows: readonly ChainRow[], from: number, to: number): ChainRow[] {
  if (from === to || from < 0 || to < 0 || from >= rows.length || to >= rows.length) {
    return [...rows]
  }
  const next = [...rows]
  const [moved] = next.splice(from, 1)
  if (moved) next.splice(to, 0, moved)
  return next
}

/**
 * A saved gateway as editor rows.
 *
 * `TargetSummary.id` is the *model* id and the list arrives in priority order, so this is
 * a map rather than a sort — the server already decided the order, and re-deriving it here
 * would be a second opinion nobody asked for.
 */
export function rowsOf(gateway: GatewayResponse): ChainRow[] {
  return gateway.targets.map((target) => ({
    modelId: target.id,
    weight: target.weight ?? TOTAL_WEIGHT,
  }))
}

/** The rows as the API's `targets`, dropping the blanks an unfinished row leaves. */
export function chainBody(rows: readonly ChainRow[]): { model_id: string; weight: number }[] {
  return rows
    .filter((row) => row.modelId)
    .map((row) => ({ model_id: row.modelId, weight: row.weight }))
}

/** Rows compare by value; the form's dirty check is otherwise always true. */
export function sameChain(left: readonly ChainRow[], right: readonly ChainRow[]): boolean {
  return (
    left.length === right.length &&
    left.every(
      (row, index) =>
        row.modelId === right[index]?.modelId && row.weight === right[index]?.weight,
    )
  )
}
