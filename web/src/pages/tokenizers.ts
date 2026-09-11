/**
 * The tokenizer follows the model (task 101) — the browser's half.
 *
 * The derivation table is *served* by `GET /api/v1/tokenizers` rather than copied here, so
 * the one thing this module has to get right is the matching rule, and it is the same rule
 * `app/services/tokenizers.py` applies: longest matching prefix wins, the model id is
 * matched whole and after its last `/`, a `null` dialect matches every dialect, and nothing
 * matching falls to the table's own fallback. Two copies of a *table* would disagree within
 * a month; two copies of a five-line rule are checked by one test each.
 */

import type {
  CalibrationResponse,
  DerivationResponse,
  TokenizerSpec,
  TokenizersResponse,
} from '@/api/types'

export function tokenizerKey(spec: TokenizerSpec): string {
  return spec.name === 'approximate' ? `approximate:${formatRatio(spec.ratio ?? 0)}` : spec.name
}

/** `3.5`, `4`, `3.365` — the way Python's `:g` prints it, so labels match the server's. */
export function formatRatio(ratio: number): string {
  return String(Number(ratio.toPrecision(6)))
}

export function deriveTokenizer(
  table: TokenizersResponse,
  dialect: string,
  modelId: string,
): TokenizerSpec {
  const whole = modelId.trim().toLowerCase()
  const tail = whole.split('/').pop() ?? whole
  let best: DerivationResponse | null = null
  for (const row of table.derivations) {
    if (row.dialect !== null && row.dialect !== dialect) continue
    if (!whole.startsWith(row.prefix) && !tail.startsWith(row.prefix)) continue
    if (best === null || row.prefix.length > best.prefix.length) best = row
  }
  return best?.spec ?? table.fallback
}

/** `×1.04` — provider over ours, two decimals, the sign a reader expects. */
export function formatDrift(ratio: number): string {
  return `×${ratio.toFixed(2)}`
}

/**
 * One line for the model page: "our count vs. the provider's: ×1.04 over 3 120 requests",
 * or the honest absence of one.
 */
export function describeCalibration(row: CalibrationResponse | undefined): string {
  if (!row || row.ratio === null || row.samples === 0) {
    return 'No requests measured yet — the ratio appears once the provider has reported usage.'
  }
  return `Our count vs. the provider's: ${formatDrift(row.ratio)} over ${row.samples.toLocaleString()} request${row.samples === 1 ? '' : 's'}.`
}

/**
 * "anthropic (approximate, calibrated ×1.04 from 3 120 requests)" — the demo's line, built
 * from the effective tokenizer and its window.
 */
export function tokenizerSummary(
  label: string,
  approximate: boolean,
  row: CalibrationResponse | undefined,
): string {
  if (!approximate || !row || row.ratio === null) return label
  return `${label}, calibrated ${formatDrift(row.ratio)} from ${row.samples.toLocaleString()} requests`
}

/** The models on a gateway whose tokenizer drift is past the warning line. */
export function driftingTargets(
  rows: CalibrationResponse[] | undefined,
  modelIds: readonly string[],
): CalibrationResponse[] {
  //  `Array.isArray`, not a truthiness check: a server that predates the endpoint (or a
  //  test double that answers the models prefix wholesale) hands back a page object, and a
  //  missing warning is the right degradation for a missing measurement.
  if (!Array.isArray(rows)) return []
  const wanted = new Set(modelIds)
  return rows.filter((row) => wanted.has(row.model_id) && row.warns)
}
