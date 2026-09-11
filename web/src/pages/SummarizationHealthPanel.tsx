import type { Window } from '@/api/monitoring'
import { useSummarizationHealth } from '@/api/summarization'
import type { SummarizationHealth } from '@/api/types'
import { BarList, ChartFrame, LineChart } from '@/components/Charts'
import type { Point, Series } from '@/components/chartSeries'
import { healthSummary, waitingSummary } from '@/pages/summarization'

/**
 * SPEC §10.1's summarization health (task 102), beside memory health.
 *
 * The chart is documents per day; the two bar lists are what it *cost* — tokens by model,
 * and the connectors spending the most — because this is the first panel on the page that
 * is about a bill rather than about traffic. The window is the page's own: summarization
 * happens when documents arrive, so "the last hour" is a question with an answer here,
 * unlike the memory chart's thirty days.
 *
 * The banner above it is the degraded state: documents parked on a cap are a connector that
 * is silently not indexing, and a number in a table is not how anybody finds that out.
 */
export function SummarizationHealthPanel({
  window,
  connectorId = null,
}: {
  window: Window
  connectorId?: string | null
}) {
  const { data: health, isLoading } = useSummarizationHealth(window, connectorId)

  if (isLoading || !health || !Array.isArray(health.days)) {
    return (
      <ChartFrame title="Summarization" subtitle="Loading…">
        <p className="py-8 text-center text-sm text-slate-400">Loading…</p>
      </ChartFrame>
    )
  }

  const waiting = waitingSummary(health)

  return (
    <div className="lg:col-span-2" data-testid="summarization-panel">
      <ChartFrame
        title="Summarization"
        subtitle={`What summarizing documents did and cost in this window. ${healthSummary(health)}`}
        legend={series(health)}
        empty={health.days.length === 0}
      >
        <LineChart points={points(health)} series={series(health)} />
      </ChartFrame>

      {waiting ? (
        <p
          role="status"
          className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
        >
          {waiting}. They resume after midnight UTC; raise the connector's daily cap to
          index them sooner.
        </p>
      ) : null}

      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <ChartFrame title="Tokens by model" subtitle="In and out, over the window.">
          <BarList
            slices={health.by_model.map((row) => ({
              label: row.model_name,
              value: row.tokens_in + row.tokens_out,
            }))}
          />
        </ChartFrame>
        <ChartFrame title="Connectors by spend" subtitle="Where the tokens went.">
          <BarList
            slices={health.top_connectors.map((row) => ({
              label: row.name ?? row.connector_id,
              value: row.tokens_in + row.tokens_out,
            }))}
          />
        </ChartFrame>
        <dl className="grid grid-cols-2 gap-3 rounded-lg border border-slate-200 bg-white p-4 text-sm">
          <Stat label="Attempts" value={health.runs.toLocaleString()} />
          <Stat label="Failed" value={`${Math.round(health.failure_rate * 100)}%`} />
          <Stat label="Refused by a cap" value={health.capped.toLocaleString()} />
          <Stat label="Waiting on a cap" value={health.waiting_documents.toLocaleString()} />
          <Stat label="Tokens in" value={health.tokens_in.toLocaleString()} />
          <Stat label="Tokens out" value={health.tokens_out.toLocaleString()} />
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

function series(health: SummarizationHealth): Series[] {
  const days = health.days
  return [
    {
      name: 'documents',
      label: 'summarized',
      color: '#2563eb',
      values: days.map((d) => d.documents),
    },
    { name: 'failures', label: 'failed', color: '#dc2626', values: days.map((d) => d.failures) },
    { name: 'capped', label: 'refused by a cap', color: '#d97706', values: days.map((d) => d.capped) },
  ]
}

function points(health: SummarizationHealth): Point[] {
  return health.days.map((day) => ({
    label: new Date(day.day).toLocaleDateString(),
    short: new Date(day.day).toLocaleDateString([], { month: 'short', day: 'numeric' }),
  }))
}
