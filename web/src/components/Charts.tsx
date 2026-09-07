/**
 * The monitoring charts, drawn as SVG by hand.
 *
 * No charting library, and that is a decision rather than an omission. Recharts and its
 * relatives are 100–200 KB of JavaScript for what these five charts actually need: map
 * numbers onto a path, draw some rectangles, put a label under them. The app's whole
 * bundle is around 300 KB today. A dependency that large also has to be kept current for
 * the life of the product, and the first thing anyone would do with it is fight its
 * defaults into looking like the rest of the UI.
 *
 * What it costs is real and worth naming: no tooltips that follow the cursor, no zoom,
 * no animation. Each chart shows its own numbers as text, which turns out to be the
 * thing people read anyway.
 *
 * Everything here is a pure function of its props. Nothing fetches, nothing holds state
 * that outlives a render, and the SVG carries `<title>` and `role="img"` so a screen
 * reader gets a sentence rather than a shape.
 */

import { useId, type ReactNode } from 'react'

import type { Point, Series } from '@/components/chartSeries'

function niceMax(values: number[]): number {
  const highest = Math.max(0, ...values)
  if (highest === 0) return 1
  const magnitude = 10 ** Math.floor(Math.log10(highest))
  return Math.ceil(highest / magnitude) * magnitude
}

// ---------------------------------------------------------------------------

export function ChartFrame({
  title,
  subtitle,
  children,
  legend,
  empty,
}: {
  title: string
  subtitle?: string
  children: ReactNode
  legend?: readonly Series[]
  empty?: boolean
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4">
      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
          {subtitle ? <p className="text-xs text-slate-500">{subtitle}</p> : null}
        </div>
        {legend && legend.length > 0 ? (
          <ul className="flex flex-wrap gap-3 text-xs text-slate-600">
            {legend.map((series) => (
              <li key={series.name} className="flex items-center gap-1.5">
                <span
                  aria-hidden="true"
                  className="inline-block h-2 w-2 rounded-full"
                  style={{ backgroundColor: series.color }}
                />
                {series.label}
              </li>
            ))}
          </ul>
        ) : null}
      </header>
      {empty ? (
        <p className="py-10 text-center text-sm text-slate-400">
          No requests in this window.
        </p>
      ) : (
        children
      )}
    </section>
  )
}

/**
 * A line per series over the same buckets.
 *
 * A `null` in `values` breaks the line rather than being drawn as zero — a bucket with
 * no streamed request has no first-token time, and joining across it would draw a
 * latency that never happened.
 */
export function LineChart({
  points,
  series,
  height = 160,
  format = (value: number) => String(Math.round(value)),
}: {
  points: readonly Point[]
  series: readonly Series[]
  height?: number
  format?: (value: number) => string
}) {
  const width = 640
  const padding = { top: 8, right: 8, bottom: 20, left: 40 }
  const plotWidth = width - padding.left - padding.right
  const plotHeight = height - padding.top - padding.bottom
  const max = niceMax(series.flatMap((line) => line.values.filter((v): v is number => v !== null)))
  const step = points.length > 1 ? plotWidth / (points.length - 1) : 0

  const x = (index: number) => padding.left + index * step
  const y = (value: number) => padding.top + plotHeight - (value / max) * plotHeight

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-40 w-full"
      role="img"
      aria-label={series.map((line) => line.label).join(', ')}
      preserveAspectRatio="none"
    >
      {[0, 0.5, 1].map((fraction) => (
        <g key={fraction}>
          <line
            x1={padding.left}
            x2={width - padding.right}
            y1={y(max * fraction)}
            y2={y(max * fraction)}
            stroke="#e2e8f0"
            strokeWidth={1}
          />
          <text x={0} y={y(max * fraction) + 3} className="fill-slate-400 text-[9px]">
            {format(max * fraction)}
          </text>
        </g>
      ))}

      {series.map((line) => (
        <g key={line.name}>
          {segments(line.values).map((segment, index) => (
            <polyline
              key={index}
              fill="none"
              stroke={line.color}
              strokeWidth={1.75}
              strokeLinejoin="round"
              points={segment.map(({ index: at, value }) => `${x(at)},${y(value)}`).join(' ')}
            />
          ))}
        </g>
      ))}

      <AxisEnds points={points} width={width} height={height} left={padding.left} right={padding.right} />
    </svg>
  )
}

/**
 * The first and last bucket's labels, at the two ends of the axis.
 *
 * Only two, and only at the ends: intermediate labels on a 288-bucket chart overlap into
 * illegibility, and the interval is written under the title in words.
 */
function AxisEnds({
  points,
  width,
  height,
  left,
  right,
}: {
  points: readonly Point[]
  width: number
  height: number
  left: number
  right: number
}) {
  const first = points[0]
  const last = points[points.length - 1]
  if (!first || !last) return null
  return (
    <>
      <text x={left} y={height - 6} className="fill-slate-400 text-[9px]">
        {first.short}
      </text>
      <text x={width - right} y={height - 6} textAnchor="end" className="fill-slate-400 text-[9px]">
        {last.short}
      </text>
    </>
  )
}

/** Contiguous runs of non-null values, so a gap stays a gap. */
function segments(values: readonly (number | null)[]) {
  const runs: { index: number; value: number }[][] = []
  let current: { index: number; value: number }[] = []
  values.forEach((value, index) => {
    if (value === null) {
      if (current.length) runs.push(current)
      current = []
    } else {
      current.push({ index, value })
    }
  })
  if (current.length) runs.push(current)
  // A single point has no line to draw, so it is emitted twice — a one-pixel dash is
  // visible and an empty polyline is not.
  return runs.map((run) => (run.length === 1 && run[0] ? [run[0], run[0]] : run))
}

/** Stacked bars — the request rate with its status breakdown. */
export function StackedBars({
  points,
  series,
  height = 160,
}: {
  points: readonly Point[]
  series: readonly Series[]
  height?: number
}) {
  const width = 640
  const padding = { top: 8, right: 8, bottom: 20, left: 40 }
  const plotWidth = width - padding.left - padding.right
  const plotHeight = height - padding.top - padding.bottom
  const totals = points.map((_, index) =>
    series.reduce((sum, line) => sum + (line.values[index] ?? 0), 0),
  )
  const max = niceMax(totals)
  const barWidth = points.length ? Math.max(1, (plotWidth / points.length) * 0.8) : 0
  const slot = points.length ? plotWidth / points.length : 0

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-40 w-full"
      role="img"
      aria-label={`Requests per bucket, split by ${series.map((s) => s.label).join(', ')}`}
      preserveAspectRatio="none"
    >
      {[0, 0.5, 1].map((fraction) => (
        <g key={fraction}>
          <line
            x1={padding.left}
            x2={width - padding.right}
            y1={padding.top + plotHeight - fraction * plotHeight}
            y2={padding.top + plotHeight - fraction * plotHeight}
            stroke="#e2e8f0"
          />
          <text
            x={0}
            y={padding.top + plotHeight - fraction * plotHeight + 3}
            className="fill-slate-400 text-[9px]"
          >
            {Math.round(max * fraction)}
          </text>
        </g>
      ))}

      {points.map((point, index) => {
        let offset = 0
        return (
          <g key={point.label}>
            {series.map((line) => {
              const value = line.values[index] ?? 0
              if (value <= 0) return null
              const barHeight = (value / max) * plotHeight
              offset += barHeight
              return (
                <rect
                  key={line.name}
                  x={padding.left + index * slot + (slot - barWidth) / 2}
                  y={padding.top + plotHeight - offset}
                  width={barWidth}
                  height={barHeight}
                  fill={line.color}
                />
              )
            })}
          </g>
        )
      })}

      <AxisEnds points={points} width={width} height={height} left={padding.left} right={padding.right} />
    </svg>
  )
}

export type Slice = { label: string; value: number }

/**
 * A ranked horizontal bar list — traffic per model, and the error taxonomy.
 *
 * A bar list rather than a pie: the questions are "which is biggest" and "how much
 * bigger", and both are read off lengths far more reliably than off angles. It also
 * carries the number as text, which a pie cannot.
 */
export function BarList({ slices, tone = '#2563eb' }: { slices: readonly Slice[]; tone?: string }) {
  const labelId = useId()
  const max = Math.max(1, ...slices.map((slice) => slice.value))
  const total = slices.reduce((sum, slice) => sum + slice.value, 0)

  if (slices.length === 0) {
    return <p className="py-8 text-center text-sm text-slate-400">Nothing to show.</p>
  }

  return (
    <ul className="space-y-2" aria-describedby={labelId}>
      <span id={labelId} className="sr-only">
        {total} requests in total
      </span>
      {slices.map((slice) => (
        <li key={slice.label}>
          <div className="flex items-baseline justify-between text-xs">
            <span className="truncate font-medium text-slate-700">{slice.label}</span>
            <span className="tabular-nums text-slate-500">
              {slice.value.toLocaleString()}
              {total > 0 ? ` · ${Math.round((slice.value / total) * 100)}%` : ''}
            </span>
          </div>
          <div className="mt-1 h-2 overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full"
              style={{ width: `${(slice.value / max) * 100}%`, backgroundColor: tone }}
            />
          </div>
        </li>
      ))}
    </ul>
  )
}

/**
 * The §10.3 timing waterfall: phases laid out on one horizontal axis.
 *
 * Phases that were not measured are absent, not zero. A retrieval bar of width zero
 * would claim retrieval happened instantly; task 10 is what makes it appear at all.
 */
export function Waterfall({
  total,
  phases,
}: {
  total: number
  phases: readonly { label: string; value: number | null; color: string }[]
}) {
  const measured = phases.filter(
    (phase): phase is { label: string; value: number; color: string } => phase.value !== null,
  )
  const accounted = measured.reduce((sum, phase) => sum + phase.value, 0)
  // Whatever is left is the gateway's own work: routing, assembly, the parameter merge.
  const overhead = Math.max(0, total - accounted)
  const rows = [...measured, { label: 'Gateway overhead', value: overhead, color: '#94a3b8' }]
  const scale = Math.max(1, total)

  let offset = 0
  return (
    <div className="space-y-2">
      {rows.map((phase) => {
        const left = (offset / scale) * 100
        const width = (phase.value / scale) * 100
        offset += phase.value
        return (
          <div key={phase.label} className="flex items-center gap-3 text-xs">
            <span className="w-32 shrink-0 text-slate-600">{phase.label}</span>
            <div className="relative h-3 flex-1 rounded bg-slate-100">
              <div
                className="absolute h-3 rounded"
                style={{
                  left: `${left}%`,
                  width: `${Math.max(width, 0.5)}%`,
                  backgroundColor: phase.color,
                }}
              />
            </div>
            <span className="w-16 shrink-0 text-right tabular-nums text-slate-500">
              {phase.value} ms
            </span>
          </div>
        )
      })}
      <div className="flex items-center gap-3 border-t border-slate-100 pt-2 text-xs font-medium">
        <span className="w-32 shrink-0 text-slate-700">Total</span>
        <div className="flex-1" />
        <span className="w-16 shrink-0 text-right tabular-nums text-slate-700">{total} ms</span>
      </div>
    </div>
  )
}
