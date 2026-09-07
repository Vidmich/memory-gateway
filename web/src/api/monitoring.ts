/**
 * Queries for the monitoring screen.
 *
 * Three decisions live here rather than in the components.
 *
 * **The time range is a name, not a pair of dates.** `1h`, `24h`, `7d`, `30d` — resolved
 * to an actual window at query time. If the window were computed in a component's render,
 * every re-render would produce a new `from` a few milliseconds later, which is a new
 * query key, which is a refetch on every keystroke elsewhere on the page. `resolveRange`
 * rounds to the minute so the key is stable for a minute at a time.
 *
 * **Live tail is `refetchInterval`, and it is switchable.** The server does not push;
 * five seconds is the poll SPEC §13.1 asks for, and the caller turns it off when the
 * reader has scrolled — a table that jumps while somebody is reading it is worse than a
 * stale one.
 *
 * **The detail query is keyed by id alone.** It needs no window: the id carries its own
 * timestamp, so the server works out which day to look in.
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { useApiClient } from '@/auth/AuthContext'
import type {
  RequestDetailResponse,
  RequestLogPage,
  SeriesResponse,
  SummaryResponse,
} from '@/api/types'

export const RANGES = ['1h', '24h', '7d', '30d'] as const
export type RangeName = (typeof RANGES)[number]

export const RANGE_LABELS: Record<RangeName, string> = {
  '1h': 'Last hour',
  '24h': 'Last 24 hours',
  '7d': 'Last 7 days',
  '30d': 'Last 30 days',
}

const RANGE_MINUTES: Record<RangeName, number> = {
  '1h': 60,
  '24h': 60 * 24,
  '7d': 60 * 24 * 7,
  '30d': 60 * 24 * 30,
}

/** How often the request table re-reads while live tail is on (SPEC §13.1). */
export const TAIL_INTERVAL_MS = 5_000

export type LogFilters = {
  gateway_id?: string | null
  upstream_model_id?: string | null
  status_class?: string | null
  end_user_id?: string | null
  session_id?: string | null
  min_latency_ms?: number | null
  search?: string | null
  streamed?: boolean | null
}

export type Window = { from: string; to: string }

/**
 * A range name as an actual window, rounded down to the minute.
 *
 * The rounding is what makes this usable as a query key: without it every render
 * produces a different `from` and React Query refetches forever.
 */
export function resolveRange(range: RangeName, now: Date = new Date()): Window {
  const to = new Date(Math.floor(now.getTime() / 60_000) * 60_000)
  const from = new Date(to.getTime() - RANGE_MINUTES[range] * 60_000)
  return { from: from.toISOString(), to: to.toISOString() }
}

function params(window: Window, filters: LogFilters, extra: Record<string, string> = {}): string {
  const search = new URLSearchParams({ from: window.from, to: window.to, ...extra })
  for (const [name, value] of Object.entries(filters)) {
    if (value !== null && value !== undefined && value !== '') search.set(name, String(value))
  }
  return search.toString()
}

export const keys = {
  all: ['monitoring'] as const,
  summary: (window: Window, filters: LogFilters) =>
    ['monitoring', 'summary', window, filters] as const,
  series: (window: Window, filters: LogFilters, metric: string, groupBy: string) =>
    ['monitoring', 'series', window, filters, metric, groupBy] as const,
  logs: (window: Window, filters: LogFilters, cursor: string | null) =>
    ['monitoring', 'logs', window, filters, cursor] as const,
  log: (id: string) => ['monitoring', 'log', id] as const,
}

export function useSummary(
  window: Window,
  filters: LogFilters = {},
): UseQueryResult<SummaryResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.summary(window, filters),
    queryFn: () => client.get<SummaryResponse>(`/api/v1/metrics/summary?${params(window, filters)}`),
  })
}

export function useSeries(
  window: Window,
  filters: LogFilters,
  metric: 'requests' | 'latency' | 'tokens',
  groupBy: 'none' | 'status_class' | 'model' | 'gateway' = 'none',
  interval?: number,
): UseQueryResult<SeriesResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: [...keys.series(window, filters, metric, groupBy), interval ?? null],
    queryFn: () =>
      client.get<SeriesResponse>(
        `/api/v1/metrics/timeseries?${params(window, filters, {
          metric,
          group_by: groupBy,
          ...(interval ? { interval: String(interval) } : {}),
        })}`,
      ),
  })
}

export function useLogs(
  window: Window,
  filters: LogFilters,
  options: { cursor?: string | null; tail?: boolean } = {},
): UseQueryResult<RequestLogPage> {
  const client = useApiClient()
  const cursor = options.cursor ?? null
  return useQuery({
    queryKey: keys.logs(window, filters, cursor),
    queryFn: () =>
      client.get<RequestLogPage>(
        `/api/v1/logs?${params(window, filters, cursor ? { cursor } : {})}`,
      ),
    // Only the first page tails. Polling a page the reader has paged *past* would
    // reorder rows underneath them for no benefit.
    refetchInterval: options.tail && !cursor ? TAIL_INTERVAL_MS : false,
  })
}

/**
 * Requests per gateway over a window, keyed by gateway id.
 *
 * One grouped series call rather than a summary per gateway: the list screen shows a
 * count per row, and a query per row is how a five-row screen becomes five round trips.
 * A day-wide bucket means one or two points, which are summed here — the window rarely
 * lines up with a day boundary, and the server anchors buckets to the epoch.
 */
export function useRequestCounts(window: Window): Record<string, number> {
  const series = useSeries(window, {}, 'requests', 'gateway', 86_400)
  const counts: Record<string, number> = {}
  for (const bucket of series.data?.buckets ?? []) {
    for (const [name, value] of Object.entries(bucket.series)) {
      const id = name.replace('.requests', '')
      counts[id] = (counts[id] ?? 0) + value
    }
  }
  return counts
}

export function useRequestDetail(id: string | undefined): UseQueryResult<RequestDetailResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.log(id ?? ''),
    queryFn: () => client.get<RequestDetailResponse>(`/api/v1/logs/${id}`),
    enabled: Boolean(id),
    // A completed request never changes, so there is nothing to refetch for.
    staleTime: Infinity,
  })
}
