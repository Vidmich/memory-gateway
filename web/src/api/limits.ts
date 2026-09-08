/**
 * Queries for SPEC §11's limits and how much of them is spent.
 *
 * Reads only. The limits themselves are written through the gateway editor's own save, so
 * there is no mutation here — and after that save the utilisation bars are stale, which is
 * why `useUpdateGateway` invalidates this key rather than this file inventing a second
 * write path for the same fields.
 *
 * The usage figures are live counters, not aggregates, so they are refetched on focus and
 * kept for only a few seconds. A bar that says "9 of 10 used" from a minute ago is worse
 * than no bar: the whole reason to draw it is to answer "am I about to be throttled".
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { useApiClient } from '@/auth/AuthContext'
import type { Window } from '@/api/monitoring'
import type { GatewayLimits, LimitPressureList, ThrottledEndUsers } from '@/api/types'

/** Long enough to survive a re-render, short enough that a bar is never stale on screen. */
const FRESH_MS = 5_000

export const keys = {
  all: ['limits'] as const,
  gateway: (gatewayId: string) => ['limits', 'gateway', gatewayId] as const,
  pressure: ['limits', 'pressure'] as const,
  throttled: (query: string) => ['limits', 'throttled', query] as const,
}

export function useGatewayLimits(gatewayId: string | undefined): UseQueryResult<GatewayLimits> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.gateway(gatewayId ?? 'none'),
    enabled: gatewayId !== undefined,
    staleTime: FRESH_MS,
    queryFn: () => client.get<GatewayLimits>(`/api/v1/gateways/${gatewayId}/limits`),
  })
}

export function useLimitPressure(): UseQueryResult<LimitPressureList> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.pressure,
    staleTime: FRESH_MS,
    queryFn: () => client.get<LimitPressureList>('/api/v1/limits/pressure'),
  })
}

/**
 * Who was refused most in one window.
 *
 * Takes the monitoring screen's own window and gateway filter, and nothing else: the
 * endpoint understands those three parameters, and passing a status-class filter to a
 * question that is *about* one status class would be noise on the wire and a second cache
 * key for an identical answer.
 */
export function useThrottledEndUsers(
  window: Window,
  gatewayId: string | null | undefined,
): UseQueryResult<ThrottledEndUsers> {
  const client = useApiClient()
  const search = new URLSearchParams({ from: window.from, to: window.to })
  if (gatewayId) search.set('gateway_id', gatewayId)
  const query = search.toString()
  return useQuery({
    queryKey: keys.throttled(query),
    queryFn: () => client.get<ThrottledEndUsers>(`/api/v1/metrics/throttled?${query}`),
  })
}
