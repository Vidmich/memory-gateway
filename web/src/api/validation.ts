/**
 * Queries and mutations for validation (task 103): index audits per connector, and
 * evaluation sets and runs per gateway.
 *
 * Two things poll. An audit that is `running` and a run that is `queued` or `running` are
 * both jobs on the worker, and the screen that started one should watch it finish rather
 * than ask the person to refresh; the interval comes off the moment nothing is in flight,
 * the way the document table's does.
 *
 * Mutations invalidate by gateway or by set rather than the whole tree: an item added to
 * one set moves that set's counts and nothing else.
 */

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import type {
  AuditAlertResponse,
  AuditRequest,
  AuditResponse,
  AuditStatusResponse,
  EvaluationItemPatch,
  EvaluationItemRequest,
  EvaluationItemResponse,
  EvaluationRunResponse,
  EvaluationRunSummaryResponse,
  EvaluationSetDetailResponse,
  EvaluationSetPatch,
  EvaluationSetRequest,
  EvaluationSetResponse,
  GenerateRequest,
  GenerateResponse,
  ImportRequest,
  ImportResponse,
  RunDiffResponse,
  RunRequest,
} from '@/api/types'
import { useApiClient } from '@/auth/AuthContext'

const POLL_INTERVAL_MS = 3000

export const keys = {
  all: ['validation'] as const,
  audits: (connectorId: string) => ['validation', 'audits', connectorId] as const,
  alerts: ['validation', 'alerts'] as const,
  sets: (gatewayId: string) => ['validation', 'sets', gatewayId] as const,
  set: (setId: string) => ['validation', 'set', setId] as const,
  runs: (setId: string) => ['validation', 'runs', setId] as const,
  run: (runId: string) => ['validation', 'run', runId] as const,
  diff: (runId: string, against: string) => ['validation', 'diff', runId, against] as const,
}

// -- audits ------------------------------------------------------------------

export function auditsInFlight(status: AuditStatusResponse | undefined): boolean {
  return Boolean(
    status && [status.chunking, status.embedding].some((audit) => audit?.status === 'running'),
  )
}

export function useAudits(connectorId: string | undefined): UseQueryResult<AuditStatusResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.audits(connectorId ?? ''),
    queryFn: () => client.get<AuditStatusResponse>(`/api/v1/connectors/${connectorId}/audits`),
    enabled: Boolean(connectorId),
    refetchInterval: (query) => (auditsInFlight(query.state.data) ? POLL_INTERVAL_MS : false),
  })
}

export function useStartAudit(connectorId: string) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ kind, body }: { kind: 'chunking' | 'embedding'; body?: AuditRequest }) =>
      client.post<AuditResponse>(`/api/v1/connectors/${connectorId}/audits/${kind}`, body ?? {}),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: keys.audits(connectorId),
      })
      void queryClient.invalidateQueries({ queryKey: keys.alerts })
    },
  })
}

export function useAuditAlerts(): UseQueryResult<{
  items: AuditAlertResponse[]
}> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.alerts,
    queryFn: () => client.get<{ items: AuditAlertResponse[] }>('/api/v1/validation/alerts'),
  })
}

// -- evaluation sets ------------------------------------------------------

export function useEvaluationSets(
  gatewayId: string | undefined,
): UseQueryResult<{ items: EvaluationSetResponse[] }> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.sets(gatewayId ?? ''),
    queryFn: () =>
      client.get<{ items: EvaluationSetResponse[] }>(
        `/api/v1/gateways/${gatewayId}/evaluation-sets`,
      ),
    enabled: Boolean(gatewayId),
  })
}

export function useEvaluationSet(
  setId: string | null,
): UseQueryResult<EvaluationSetDetailResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.set(setId ?? ''),
    queryFn: () => client.get<EvaluationSetDetailResponse>(`/api/v1/evaluation-sets/${setId}`),
    enabled: Boolean(setId),
  })
}

export function useCreateEvaluationSet(gatewayId: string) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: EvaluationSetRequest) =>
      client.post<EvaluationSetResponse>(`/api/v1/gateways/${gatewayId}/evaluation-sets`, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.sets(gatewayId) }),
  })
}

export function useUpdateEvaluationSet(gatewayId: string, setId: string) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: EvaluationSetPatch) =>
      client.patch<EvaluationSetDetailResponse>(`/api/v1/evaluation-sets/${setId}`, body),
    onSuccess: () => invalidateSet(queryClient, gatewayId, setId),
  })
}

export function useDeleteEvaluationSet(gatewayId: string) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (setId: string) => client.delete<void>(`/api/v1/evaluation-sets/${setId}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.sets(gatewayId) }),
  })
}

// -- items -------------------------------------------------------------------

export function useAddEvaluationItem(gatewayId: string | undefined, setId: string) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: EvaluationItemRequest) =>
      client.post<EvaluationItemResponse>(`/api/v1/evaluation-sets/${setId}/items`, body),
    onSuccess: () => invalidateSet(queryClient, gatewayId, setId),
  })
}

export function useUpdateEvaluationItem(gatewayId: string, setId: string) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ itemId, body }: { itemId: string; body: EvaluationItemPatch }) =>
      client.patch<EvaluationItemResponse>(`/api/v1/evaluation-items/${itemId}`, body),
    onSuccess: () => invalidateSet(queryClient, gatewayId, setId),
  })
}

export function useDeleteEvaluationItem(gatewayId: string, setId: string) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (itemId: string) => client.delete<void>(`/api/v1/evaluation-items/${itemId}`),
    onSuccess: () => invalidateSet(queryClient, gatewayId, setId),
  })
}

export function useImportEvaluationItems(gatewayId: string, setId: string) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: ImportRequest) =>
      client.post<ImportResponse>(`/api/v1/evaluation-sets/${setId}/import`, body),
    onSuccess: () => invalidateSet(queryClient, gatewayId, setId),
  })
}

export function useGenerateEvaluationItems(gatewayId: string, setId: string) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: GenerateRequest) =>
      client.post<GenerateResponse>(`/api/v1/evaluation-sets/${setId}/generate`, body),
    onSuccess: () => invalidateSet(queryClient, gatewayId, setId),
  })
}

// -- runs --------------------------------------------------------------------

export function runInFlight(run: { status: string } | null | undefined): boolean {
  return run?.status === 'queued' || run?.status === 'running'
}

export function useEvaluationRuns(
  setId: string | null,
): UseQueryResult<{ items: EvaluationRunSummaryResponse[] }> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.runs(setId ?? ''),
    queryFn: () =>
      client.get<{ items: EvaluationRunSummaryResponse[] }>(
        `/api/v1/evaluation-sets/${setId}/runs`,
      ),
    enabled: Boolean(setId),
    refetchInterval: (query) =>
      query.state.data?.items.some(runInFlight) ? POLL_INTERVAL_MS : false,
  })
}

export function useEvaluationRun(runId: string | null): UseQueryResult<EvaluationRunResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.run(runId ?? ''),
    queryFn: () => client.get<EvaluationRunResponse>(`/api/v1/evaluation-runs/${runId}`),
    enabled: Boolean(runId),
    refetchInterval: (query) => (runInFlight(query.state.data) ? POLL_INTERVAL_MS : false),
  })
}

export function useStartEvaluationRun(gatewayId: string, setId: string) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: RunRequest) =>
      client.post<EvaluationRunSummaryResponse>(`/api/v1/evaluation-sets/${setId}/runs`, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.runs(setId) })
      void queryClient.invalidateQueries({ queryKey: keys.sets(gatewayId) })
    },
  })
}

export function useRunDiff(
  runId: string | null,
  against: string | null,
): UseQueryResult<RunDiffResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.diff(runId ?? '', against ?? ''),
    queryFn: () => client.get<RunDiffResponse>(`/api/v1/evaluation-runs/${runId}/diff/${against}`),
    enabled: Boolean(runId && against && runId !== against),
  })
}

function invalidateSet(
  queryClient: ReturnType<typeof useQueryClient>,
  gatewayId: string | undefined,
  setId: string,
): void {
  void queryClient.invalidateQueries({ queryKey: keys.set(setId) })
  if (gatewayId) void queryClient.invalidateQueries({ queryKey: keys.sets(gatewayId) })
}
