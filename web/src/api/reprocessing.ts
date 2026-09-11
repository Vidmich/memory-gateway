/**
 * Queries and mutations for reprocessing (task 104): the runs a connector's stale documents
 * are re-ingested by, the preview a settings form shows before saving, and the dashboard's
 * stale-connector alerts.
 *
 * The connector's own query already carries the run in flight (`connector.reprocessing`),
 * so the header polls through `useConnector` rather than through a second timer; the
 * history list polls only while its newest run is still going, the way the document table
 * polls only while something is unfinished.
 */

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { keys as connectorKeys } from '@/api/connectors'
import type {
  ConnectorUpdateRequest,
  ReprocessingRunResponse,
  ReprocessRequest,
  StaleAlertResponse,
  StalePreviewResponse,
} from '@/api/types'
import { useApiClient } from '@/auth/AuthContext'

const POLL_INTERVAL_MS = 2000

export const keys = {
  all: ['reprocessing'] as const,
  runs: (connectorId: string) => ['reprocessing', 'runs', connectorId] as const,
  run: (runId: string) => ['reprocessing', 'run', runId] as const,
  alerts: ['reprocessing', 'alerts'] as const,
}

export function runInFlight(run: ReprocessingRunResponse | null | undefined): boolean {
  return Boolean(run && run.status === 'running')
}

function useInvalidateReprocessing(): () => Promise<void> {
  const queryClient = useQueryClient()
  return async () => {
    await queryClient.invalidateQueries({ queryKey: keys.all })
    // The run changes the connector's counts and the rows' statuses too.
    await queryClient.invalidateQueries({ queryKey: connectorKeys.all })
  }
}

/** Start a run over the connector's stale documents, or the scope named. */
export function useReprocess(connectorId: string | undefined) {
  const client = useApiClient()
  const invalidate = useInvalidateReprocessing()
  return useMutation({
    mutationFn: (body?: ReprocessRequest) =>
      client.post<ReprocessingRunResponse>(
        `/api/v1/connectors/${connectorId}/reprocess`,
        body ?? { scope: 'stale', formats: [] },
      ),
    onSuccess: invalidate,
  })
}

export function useReprocessingRuns(
  connectorId: string | undefined,
): UseQueryResult<{ items: ReprocessingRunResponse[] }> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.runs(connectorId ?? ''),
    queryFn: () =>
      client.get<{ items: ReprocessingRunResponse[] }>(
        `/api/v1/connectors/${connectorId}/reprocessing-runs`,
      ),
    enabled: Boolean(connectorId),
    refetchInterval: (query) =>
      query.state.data?.items.some(runInFlight) ? POLL_INTERVAL_MS : false,
  })
}

export function useReprocessingRun(
  runId: string | undefined,
): UseQueryResult<ReprocessingRunResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.run(runId ?? ''),
    queryFn: () => client.get<ReprocessingRunResponse>(`/api/v1/reprocessing-runs/${runId}`),
    enabled: Boolean(runId),
    refetchInterval: (query) => (runInFlight(query.state.data) ? POLL_INTERVAL_MS : false),
  })
}

/** **Retry failed**: a new run over exactly the documents this one left failed. */
export function useRetryFailed() {
  const client = useApiClient()
  const invalidate = useInvalidateReprocessing()
  return useMutation({
    mutationFn: (runId: string) =>
      client.post<ReprocessingRunResponse>(`/api/v1/reprocessing-runs/${runId}/retry`, {}),
    onSuccess: invalidate,
  })
}

/**
 * How many documents saving this patch would mark stale, per format. A mutation rather
 * than a query although it writes nothing, because it is asked as the person edits — on
 * the patch they have typed — and not on a schedule.
 */
export function useStalePreview(connectorId: string | undefined) {
  const client = useApiClient()
  return useMutation({
    mutationFn: (body: ConnectorUpdateRequest) =>
      client.post<StalePreviewResponse>(`/api/v1/connectors/${connectorId}/stale-preview`, body),
  })
}

export function useStaleAlerts(): UseQueryResult<{ items: StaleAlertResponse[] }> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.alerts,
    queryFn: () => client.get<{ items: StaleAlertResponse[] }>('/api/v1/reprocessing/alerts'),
  })
}
