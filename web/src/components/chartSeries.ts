/**
 * Turning the API's buckets into something a chart can draw.
 *
 * Separate from `Charts.tsx` because none of it renders: these are pure functions over
 * the server's `{name: value}` buckets, and keeping them out of the component file means
 * they can be tested by calling them, without mounting anything.
 */

import type {
  BucketResponse,
  GatewayResponse,
  SummaryResponse,
} from '@/api/types'
import type { Slice } from '@/components/Charts'

/** One line, or one layer of a stack. */
export type Series = {
  name: string
  label: string
  color: string
  values: (number | null)[]
}

/** One position on the x-axis. `short` is what fits under a 640-pixel chart. */
export type Point = { label: string; short: string }

const PALETTE = [
  '#2563eb',
  '#16a34a',
  '#d97706',
  '#dc2626',
  '#7c3aed',
  '#0891b2',
  '#db2777',
  '#65a30d',
]

/**
 * A stable colour for a series name.
 *
 * Status classes are pinned, so 5xx is the same red on every chart and in every
 * screenshot in a support thread. Everything else takes its turn from the palette.
 */
export function colorFor(name: string, index: number): string {
  if (name.startsWith('2xx')) return '#16a34a'
  if (name.startsWith('3xx')) return '#0891b2'
  if (name.startsWith('4xx')) return '#d97706'
  if (name.startsWith('5xx')) return '#dc2626'
  return PALETTE[index % PALETTE.length] ?? '#2563eb'
}

export function pointsOf(buckets: readonly BucketResponse[]): Point[] {
  return buckets.map((bucket) => ({
    label: new Date(bucket.start).toLocaleString(),
    short: new Date(bucket.start).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
  }))
}

/**
 * One series per named value, over the same buckets.
 *
 * Two rules, both about absence. A name that appears in *no* bucket is dropped entirely —
 * a gateway that never streamed has no first-token line rather than a flat line at zero.
 * A name missing from *one* bucket becomes `null` there, so the line breaks instead of
 * dipping to a latency nothing took.
 */
export function seriesFrom(
  buckets: readonly BucketResponse[],
  names: readonly { name: string; label: string }[],
): Series[] {
  return names
    .filter(({ name }) => buckets.some((bucket) => name in bucket.series))
    .map(({ name, label }, index) => ({
      name,
      label,
      color: colorFor(name, index),
      values: buckets.map((bucket) => bucket.series[name] ?? null),
    }))
}

/** The request-rate chart's series, one per status class the window actually saw. */
export function statusSeries(buckets: readonly BucketResponse[]): Series[] {
  const names = Array.from(new Set(buckets.flatMap((bucket) => Object.keys(bucket.series)))).sort()
  return seriesFrom(
    buckets,
    names.map((name) => ({ name, label: name.replace('.requests', '') })),
  )
}

export function latencySeries(buckets: readonly BucketResponse[]): Series[] {
  return seriesFrom(buckets, [
    { name: 'total_p50', label: 'p50' },
    { name: 'total_p95', label: 'p95' },
    { name: 'total_p99', label: 'p99' },
    { name: 'ttft_p95', label: 'first token p95' },
    { name: 'retrieval_p95', label: 'retrieval p95' },
  ])
}

export function tokenSeries(buckets: readonly BucketResponse[]): Series[] {
  return seriesFrom(buckets, [
    { name: 'prompt', label: 'Prompt' },
    { name: 'completion', label: 'Completion' },
    { name: 'memory', label: 'Memory' },
  ])
}

/**
 * Traffic per model, with the configured A/B weight overlaid when there is one.
 *
 * The drift between an intended 70/30 and an actual 68/32 is the whole reason the mode
 * exists to be checked on, and it is invisible unless the two numbers are on the same
 * chart. The overlay appears only for a single gateway in `ab_split`: a failover chain's
 * weights are not a target share — a healthy chain sends everything to its primary — and
 * a mark at 100% would suggest something had gone wrong.
 */
export function modelSlices(
  summary: SummaryResponse | undefined,
  gateway: GatewayResponse | undefined,
): Slice[] {
  const weights =
    gateway?.routing_mode === 'ab_split'
      ? new Map(gateway.targets.map((target) => [target.id, target.weight]))
      : null

  return (summary?.models ?? []).map((model) => {
    const expected = model.upstream_model_id ? weights?.get(model.upstream_model_id) : undefined
    return {
      label: model.model_name ?? '(deleted model)',
      value: model.requests,
      ...(expected === undefined ? {} : { expected }),
    }
  })
}


/** "5-minute buckets" — the resolution the *server* chose, in words. */
export function intervalLabel(seconds: number | undefined): string {
  if (!seconds) return ''
  if (seconds < 3600) return `${seconds / 60}-minute buckets`
  if (seconds < 86_400) return `${seconds / 3600}-hour buckets`
  return `${seconds / 86_400}-day buckets`
}
