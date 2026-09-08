import { useMemoryHealth } from '@/api/distillation'
import type { MemoryHealth } from '@/api/types'
import { BarList, ChartFrame, LineChart } from '@/components/Charts'
import type { Point, Series } from '@/components/chartSeries'
import { average, healthSummary, healthWarning, percent } from '@/pages/distillation'

/**
 * SPEC §10.1's memory health, plus the two rates that catch a silent failure.
 *
 * The chart is the ordinary part. The banner above it is the point.
 *
 * A dedupe rate near 100% and a supersession rate near zero are the two ways this feature
 * fails while looking healthy: passes succeed, no errors are logged, facts are on the
 * screen, and nothing new is being learned or nothing contradictory is being caught. Two
 * ratios on a card leave that to be noticed; a sentence says it.
 *
 * The window is not the monitoring page's. Distillation is a background pass with a
 * debounce measured in seconds and an effect measured in weeks, so "the last hour" is a
 * window in which the honest answer is almost always "nothing happened" — which reads as
 * broken. Thirty days is the shortest window in which these rates mean anything.
 */
export function MemoryHealthPanel({ days = 30 }: { days?: number }) {
  const { data: health, isLoading } = useMemoryHealth(days)

  if (isLoading || !health) {
    return (
      <ChartFrame title="Memory health" subtitle="Loading…">
        <p className="py-8 text-center text-sm text-slate-400">Loading…</p>
      </ChartFrame>
    )
  }

  const warning = healthWarning(health)

  return (
    <div className="lg:col-span-2">
      <ChartFrame
        title="Memory health"
        subtitle={`What conversation memory learned over the last ${days} days. ${healthSummary(health)}`}
        legend={series(health)}
        empty={health.days.length === 0}
      >
        <LineChart points={points(health)} series={series(health)} />
      </ChartFrame>

      {warning ? (
        <p
          role="status"
          className={`mt-3 rounded-md border p-3 text-sm ${
            warning.level === 'warn'
              ? 'border-amber-200 bg-amber-50 text-amber-900'
              : 'border-slate-200 bg-slate-50 text-slate-600'
          }`}
        >
          {warning.text}
        </p>
      ) : null}

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <ChartFrame
          title="What became of each proposal"
          subtitle="Written, already known, replaced something, or refused."
        >
          <BarList
            slices={[
              { label: 'written', value: health.written },
              { label: 'already known', value: health.deduped },
              { label: 'replaced something', value: health.superseded },
              { label: 'refused', value: health.rejected },
              { label: 'forgotten to stay in bounds', value: health.evicted },
            ]}
          />
        </ChartFrame>
        <dl className="grid grid-cols-2 gap-3 rounded-lg border border-slate-200 bg-white p-4 text-sm">
          <Stat label="Passes" value={health.runs.toLocaleString()} />
          <Stat label="Failed" value={percent(health.failure_rate)} />
          <Stat label="Already known" value={percent(health.dedupe_rate)} />
          <Stat label="Replaced something" value={percent(health.supersession_rate)} />
          <Stat label="People remembered" value={health.end_users_with_facts.toLocaleString()} />
          <Stat label="Facts per person" value={average(health.average_facts_per_end_user)} />
        </dl>
      </div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-lg font-semibold tabular-nums text-slate-900">{value}</dd>
    </div>
  )
}

/** One line per disposition, coloured to match the rest of the monitoring screen. */
function series(health: MemoryHealth): Series[] {
  const days = health.days
  return [
    { name: 'written', label: 'written', color: '#2563eb', values: days.map((d) => d.written) },
    {
      name: 'deduped',
      label: 'already known',
      color: '#64748b',
      values: days.map((d) => d.deduped),
    },
    {
      name: 'superseded',
      label: 'replaced',
      color: '#16a34a',
      values: days.map((d) => d.superseded),
    },
    {
      name: 'failures',
      label: 'failed passes',
      color: '#dc2626',
      values: days.map((d) => d.failures),
    },
  ]
}

/** A day per point. Dates rather than times, because a pass is a daily-scale event. */
function points(health: MemoryHealth): Point[] {
  return health.days.map((day) => ({
    label: new Date(day.day).toLocaleDateString(),
    short: new Date(day.day).toLocaleDateString([], { month: 'short', day: 'numeric' }),
  }))
}
