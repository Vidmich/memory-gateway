import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import {
  RANGES,
  RANGE_LABELS,
  resolveRange,
  useLogs,
  useSeries,
  useSummary,
  type LogFilters,
  type RangeName,
} from '@/api/monitoring'
import { useGateways } from '@/api/gateways'
import type { BucketResponse, RequestLogResponse, SummaryResponse } from '@/api/types'
import { BarList, ChartFrame, LineChart, StackedBars } from '@/components/Charts'
import {
  intervalLabel,
  latencySeries,
  modelSlices,
  retrievalSeries,
  pointsOf,
  statusSeries,
  tokenSeries,
} from '@/components/chartSeries'
import { DataTable, type Column } from '@/components/DataTable'
import { StatusBadge } from '@/components/StatusBadge'
import { MemoryHealthPanel } from '@/pages/MemoryHealthPanel'
import { SummarizationHealthPanel } from '@/pages/SummarizationHealthPanel'
import { ThrottledPanel } from '@/pages/ThrottledPanel'
import { RequestDrawer } from '@/pages/RequestDrawer'

/**
 * SPEC §13.1's Monitoring screen: the §10.1 charts, a filterable request table, and the
 * §10.3 drawer behind a row click.
 *
 * Three things are worth stating.
 *
 * **The window is resolved once, at the top.** Everything below takes the same
 * `{from, to}`, so the charts and the table are describing the same minutes. Letting each
 * query compute its own "last 24 hours" would produce a table whose rows are not in the
 * chart, which reads as data loss.
 *
 * **Live tail pauses when the reader scrolls.** SPEC §13.1 asks for it and the reason is
 * concrete: new rows arrive at the top, so refreshing while somebody is reading row forty
 * moves what they are looking at. Scrolling back to the top resumes it.
 *
 * **Four queries, not one per chart.** The summary carries the cards, the model
 * distribution and the error taxonomy; three series calls fill the three time charts.
 * The summary is the cached one server-side, which is what makes reloading this screen
 * cheap.
 */
export function MonitoringPage() {
  const [range, setRange] = useState<RangeName>('24h')
  const [filters, setFilters] = useState<LogFilters>({})
  const [cursor, setCursor] = useState<string | null>(null)
  const [previous, setPrevious] = useState<(string | null)[]>([])
  const [tail, setTail] = useState(true)
  // Seeded from the URL so a link can open one request. Task 13's memory browser points
  // at the conversation a fact was learned from, and a breadcrumb that only worked by
  // clicking through the table would not be a breadcrumb.
  const [searchParams, setSearchParams] = useSearchParams()
  const [selected, setSelected] = useState<string | null>(searchParams.get('request'))

  // Recomputed only when the range changes, and rounded to the minute inside
  // `resolveRange` — otherwise every render is a new query key.
  const window = useMemo(() => resolveRange(range), [range])
  const atTop = useAtTop()

  const { data: gateways } = useGateways()
  // Only when the view is narrowed to one gateway: "traffic by model" across a whole
  // organization has no single set of weights to compare against, and overlaying one
  // gateway's would be a number that means nothing.
  const focused = (gateways?.items ?? []).find((gateway) => gateway.id === filters.gateway_id)
  const summary = useSummary(window, filters)
  const requests = useSeries(window, filters, 'requests', 'status_class')
  const latency = useSeries(window, filters, 'latency')
  const tokens = useSeries(window, filters, 'tokens')
  const retrieval = useSeries(window, filters, 'retrieval')
  const logs = useLogs(window, filters, { cursor, tail: tail && atTop })

  const setFilter = (name: keyof LogFilters, value: string | boolean | null) => {
    setFilters((current) => ({ ...current, [name]: value || null }))
    setCursor(null)
    setPrevious([])
  }

  return (
    <div>
      <header className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Monitoring</h1>
          <p className="mt-1 text-sm text-slate-500">
            Every request through your gateways, and what it cost.
          </p>
        </div>
        <div className="flex gap-1 rounded-md border border-slate-300 bg-white p-0.5">
          {RANGES.map((name) => (
            <button
              key={name}
              type="button"
              aria-pressed={range === name}
              onClick={() => {
                setRange(name)
                setCursor(null)
                setPrevious([])
              }}
              className={`rounded px-2.5 py-1 text-xs font-medium ${
                range === name ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'
              }`}
            >
              {RANGE_LABELS[name]}
            </button>
          ))}
        </div>
      </header>

      <Cards summary={summary.data} loading={summary.isLoading} />

      <div className="mt-6 flex flex-wrap gap-3">
        <label className="text-sm">
          <span className="mr-2 text-slate-600">Gateway</span>
          <select
            value={filters.gateway_id ?? ''}
            onChange={(event) => setFilter('gateway_id', event.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="">All gateways</option>
            {(gateways?.items ?? []).map((gateway) => (
              <option key={gateway.id} value={gateway.id}>
                {gateway.name}
              </option>
            ))}
          </select>
        </label>

        <label className="text-sm">
          <span className="mr-2 text-slate-600">Status</span>
          <select
            value={filters.status_class ?? ''}
            onChange={(event) => setFilter('status_class', event.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="">Any status</option>
            <option value="2xx">2xx success</option>
            <option value="4xx">4xx client error</option>
            <option value="5xx">5xx server error</option>
          </select>
        </label>

        <label className="text-sm">
          <span className="mr-2 text-slate-600">Slower than</span>
          <input
            type="number"
            min={0}
            step={100}
            placeholder="ms"
            value={filters.min_latency_ms ?? ''}
            onChange={(event) => setFilter('min_latency_ms', event.target.value)}
            className="w-24 rounded-md border border-slate-300 px-2 py-1 text-sm"
          />
        </label>

        <label className="flex-1 text-sm">
          <span className="mr-2 text-slate-600">Error contains</span>
          <input
            type="search"
            placeholder="upstream_timeout"
            value={filters.search ?? ''}
            onChange={(event) => setFilter('search', event.target.value)}
            className="w-56 rounded-md border border-slate-300 px-2 py-1 text-sm"
          />
        </label>

        {/* Task 100. The query an operator runs when a corpus is suspected of being
            irrelevant: requests that were given documents and whose answer used none. */}
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={filters.uncited === true}
            onChange={(event) => setFilter('uncited', event.target.checked ? true : null)}
            className="h-4 w-4 rounded border-slate-300"
          />
          <span className="text-slate-600">Nothing cited</span>
        </label>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div className="lg:col-span-2">
          <ChartFrame
            title="Requests"
            subtitle={intervalLabel(requests.data?.interval_seconds)}
            legend={statusSeries(requests.data?.buckets ?? [])}
            empty={isEmpty(requests.data?.buckets)}
          >
            <StackedBars
              points={pointsOf(requests.data?.buckets ?? [])}
              series={statusSeries(requests.data?.buckets ?? [])}
            />
          </ChartFrame>
        </div>

        <ChartFrame
          title="Latency"
          subtitle="Total, and the first-token time inside it."
          legend={latencySeries(latency.data?.buckets ?? [])}
          empty={isEmpty(latency.data?.buckets)}
        >
          <LineChart
            points={pointsOf(latency.data?.buckets ?? [])}
            series={latencySeries(latency.data?.buckets ?? [])}
            format={(value) => `${Math.round(value)}ms`}
          />
        </ChartFrame>

        <ChartFrame
          title="Tokens"
          subtitle="Prompt, completion, and what memory injected."
          legend={tokenSeries(tokens.data?.buckets ?? [])}
          empty={isEmpty(tokens.data?.buckets)}
        >
          <LineChart
            points={pointsOf(tokens.data?.buckets ?? [])}
            series={tokenSeries(tokens.data?.buckets ?? [])}
          />
        </ChartFrame>

        <ChartFrame
          title="Retrieval"
          subtitle="How often a request that searched the knowledge base came back with nothing."
          legend={retrievalSeries(retrieval.data?.buckets ?? [])}
          empty={isEmpty(retrieval.data?.buckets)}
        >
          <LineChart
            points={pointsOf(retrieval.data?.buckets ?? [])}
            series={retrievalSeries(retrieval.data?.buckets ?? [])}
            format={(value) => `${Math.round(value * 100)}%`}
          />
        </ChartFrame>

        <ChartFrame
          title="Traffic by model"
          subtitle={
            focused?.routing_mode === 'ab_split'
              ? 'The mark on each bar is the weight this gateway is configured for.'
              : 'Which upstream served the requests.'
          }
        >
          <BarList slices={modelSlices(summary.data, focused)} />
        </ChartFrame>

        <ChartFrame title="Errors" subtitle="Grouped by what actually went wrong.">
          <BarList
            tone="#dc2626"
            slices={(summary.data?.error_groups ?? []).map((group) => ({
              label: group.error_code,
              value: group.requests,
            }))}
          />
        </ChartFrame>

        {/* SPEC §11. `rate_limited` is already a bar on the chart above — throttling is a
            gateway error like any other — but that bar says how much, and the only useful
            next question is whose. */}
        <ThrottledPanel
          window={window}
          gatewayId={filters.gateway_id}
          rateLimited={rateLimitedCount(summary.data)}
        />
      </div>

      <div className="mt-4">
        <MemoryHealthPanel />
      </div>

      {/* Task 102. The page's own window, not the memory chart's thirty days: summaries
          are written when documents arrive, so a short window has an answer here. */}
      <div className="mt-4">
        <SummarizationHealthPanel window={window} />
      </div>

      <section className="mt-8">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-900">Requests</h2>
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              checked={tail}
              onChange={(event) => setTail(event.target.checked)}
              className="rounded border-slate-300"
            />
            Live tail
            {tail && !atTop ? (
              <span className="text-xs text-amber-700">paused while you scroll</span>
            ) : null}
          </label>
        </div>

        <DataTable
          rows={logs.data?.items ?? []}
          columns={COLUMNS}
          rowKey={(row) => row.id}
          loading={logs.isLoading}
          caption="Requests through this organization's gateways"
          emptyTitle="No requests in this window"
          emptyDescription="Send a completion through one of your gateways, or widen the time range."
          onRowClick={(row) => setSelected(row.id)}
          onNextPage={
            logs.data?.next_cursor
              ? () => {
                  setPrevious((stack) => [...stack, cursor])
                  setCursor(logs.data?.next_cursor ?? null)
                }
              : null
          }
          onPreviousPage={
            previous.length > 0
              ? () => {
                  setCursor(previous[previous.length - 1] ?? null)
                  setPrevious((stack) => stack.slice(0, -1))
                }
              : null
          }
        />
      </section>

      {selected ? (
        <RequestDrawer
          logId={selected}
          gateways={gateways?.items ?? []}
          onClose={() => {
            setSelected(null)
            // Take the id out of the URL too, or reloading reopens what was just closed.
            if (searchParams.has('request')) {
              searchParams.delete('request')
              setSearchParams(searchParams, { replace: true })
            }
          }}
        />
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------

const COLUMNS: readonly Column<RequestLogResponse>[] = [
  {
    key: 'created_at',
    header: 'When',
    render: (row) => (
      <span className="whitespace-nowrap text-slate-600">
        {new Date(row.created_at).toLocaleTimeString()}
      </span>
    ),
  },
  {
    key: 'status',
    header: 'Status',
    render: (row) => (
      <StatusBadge
        status={String(row.status_code)}
        tone={row.status_code < 300 ? 'ok' : row.status_code < 500 ? 'warn' : 'error'}
      />
    ),
  },
  { key: 'model', header: 'Model', render: (row) => row.model_name ?? '(deleted)' },
  {
    key: 'latency',
    header: 'Total',
    align: 'right',
    render: (row) => <span className="tabular-nums">{row.latency_total_ms} ms</span>,
  },
  {
    key: 'ttft',
    header: 'First token',
    align: 'right',
    // An em dash, not "0 ms": a non-streamed request has no first token, and a zero here
    // would be read as an instant one.
    render: (row) => (
      <span className="tabular-nums text-slate-500">
        {row.latency_ttft_ms === null ? '—' : `${row.latency_ttft_ms} ms`}
      </span>
    ),
  },
  {
    key: 'tokens',
    header: 'Tokens',
    align: 'right',
    render: (row) => (
      <span className="tabular-nums text-slate-500">
        {row.prompt_tokens === null && row.completion_tokens === null
          ? '—'
          : `${row.prompt_tokens ?? 0} / ${row.completion_tokens ?? 0}`}
      </span>
    ),
  },
  {
    key: 'error',
    header: 'Error',
    render: (row) =>
      row.error_code ? (
        <code className="text-xs text-red-700">{row.error_code}</code>
      ) : (
        <span className="text-slate-300">—</span>
      ),
  },
]

function Cards({
  summary,
  loading,
}: {
  summary: SummaryResponse | undefined
  loading: boolean
}) {
  const cards: [string, string, string][] = [
    ['Requests', summary ? summary.requests.toLocaleString() : '—', 'in this window'],
    [
      'Error rate',
      summary ? `${(summary.error_rate * 100).toFixed(1)}%` : '—',
      summary ? `${summary.errors.toLocaleString()} failed` : '',
    ],
    ['p50 latency', formatMs(summary?.total.p50), 'half of requests are faster'],
    ['p95 latency', formatMs(summary?.total.p95), 'the slow tail'],
    [
      'Tokens',
      summary
        ? (summary.prompt_tokens + summary.completion_tokens).toLocaleString()
        : '—',
      'prompt plus completion',
    ],
    ['p95 first token', formatMs(summary?.ttft.p95), 'streamed requests only'],
    //  The quality signal, and the reason it is a card rather than only a line: a gateway
    //  retrieving nothing most of the time looks perfectly healthy on every other number
    //  here, and is answering from nowhere. The denominator is requests that actually
    //  searched, so a gateway with no connectors reads "—" rather than a misleading 0%.
    [
      'Retrieved nothing',
      summary?.retrieval_attempts
        ? `${(summary.empty_retrieval_rate * 100).toFixed(0)}%`
        : '—',
      summary?.retrieval_attempts
        ? `${summary.retrieval_empty.toLocaleString()} of ${summary.retrieval_attempts.toLocaleString()} searches`
        : 'no requests used memory',
    ],
    //  Task 100's number, beside the memory tokens it explains: a gateway paying for
    //  context on every request and citing none of it is the cheapest optimisation in the
    //  product. A proxy for relevance and a biased one — models under-cite — so the card
    //  says what it counts rather than passing judgement.
    [
      'Cited nothing',
      summary?.injected_requests ? `${(summary.uncited_rate * 100).toFixed(0)}%` : '—',
      summary?.injected_requests
        ? `${summary.uncited_requests.toLocaleString()} of ${summary.injected_requests.toLocaleString()} answers given documents · ${summary.memory_tokens.toLocaleString()} memory tokens`
        : 'no answers were given documents',
    ],
  ]

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {cards.map(([title, value, detail]) => (
        <div key={title} className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="text-sm font-medium text-slate-600">{title}</h2>
          <div className="mt-1 text-2xl font-semibold text-slate-900">
            {loading ? '…' : value}
          </div>
          <p className="mt-0.5 text-xs text-slate-400">{detail}</p>
        </div>
      ))}
    </div>
  )
}

/** ``null`` and ``0`` are different answers, and only one of them is a number. */
/** Rate-limit rejections in this window, from the error taxonomy already on the page.
 *  Reused rather than re-queried: it decides which of the throttled panel's two empty
 *  states applies, and one number does not deserve a round trip. */
function rateLimitedCount(summary: SummaryResponse | undefined): number {
  return (summary?.error_groups ?? []).find((group) => group.error_code === 'rate_limited')
    ?.requests ?? 0
}

function formatMs(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value} ms`
}

function isEmpty(buckets: readonly BucketResponse[] | undefined): boolean {
  return !buckets || buckets.length === 0
}

/**
 * Whether the page is scrolled to the top.
 *
 * This is the live-tail pause. New rows arrive at the top of the table, so refreshing
 * while somebody is reading further down moves what they are looking at — which is worse
 * than a table that is five seconds stale.
 */
function useAtTop(): boolean {
  const [atTop, setAtTop] = useState(true)

  useEffect(() => {
    const onScroll = () => setAtTop(globalThis.scrollY <= 8)
    globalThis.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => globalThis.removeEventListener('scroll', onScroll)
  }, [])

  return atTop
}
