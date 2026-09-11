/**
 * Queries and mutations for document summarization (task 102).
 *
 * The per-connector settings travel on the connector PATCH — `useUpdateConnector` with a
 * `summarization` section — so what is here is the rest: the organization's default model,
 * the ledger's numbers for the Monitoring panel, and the two per-document actions.
 *
 * Both document actions invalidate the connectors tree wholesale. Editing a summary enqueues
 * a re-embed and regenerating one enqueues a model call; the document row, its chunks and
 * the connector's counts all move afterwards, and the table polls anyway. The health query
 * is invalidated too: a regeneration is a ledger row.
 */

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { keys as connectorKeys } from '@/api/connectors'
import type { Window } from '@/api/monitoring'
import type {
  DocumentResponse,
  SummarizationHealth,
  SummarizationSettings,
  SummarizationSettingsRequest,
} from '@/api/types'
import { useApiClient } from '@/auth/AuthContext'

export const keys = {
  all: ['summarization'] as const,
  settings: ['summarization', 'settings'] as const,
  health: (window: Window, connectorId: string | null) =>
    ['summarization', 'health', { window, connectorId }] as const,
}

export function useSummarizationSettings(): UseQueryResult<SummarizationSettings> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.settings,
    queryFn: () => client.get<SummarizationSettings>('/api/v1/summarization'),
  })
}

export function useUpdateSummarizationSettings() {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: SummarizationSettingsRequest) =>
      client.patch<SummarizationSettings>('/api/v1/summarization', body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.all })
      // A connector with no model of its own resolves through this default, and the
      // connector screen shows what it resolved to.
      void queryClient.invalidateQueries({ queryKey: connectorKeys.all })
    },
  })
}

/**
 * The panel's block over the monitoring page's own window, optionally one connector's
 * slice. Summarization happens when documents arrive, so unlike the memory-health chart
 * "the last hour" is a question with an answer here.
 */
export function useSummarizationHealth(
  window: Window,
  connectorId: string | null = null,
): UseQueryResult<SummarizationHealth> {
  const client = useApiClient()
  const params = new URLSearchParams({ from: window.from, to: window.to })
  if (connectorId) params.set('connector_id', connectorId)
  return useQuery({
    queryKey: keys.health(window, connectorId),
    queryFn: () =>
      client.get<SummarizationHealth>(`/api/v1/summarization/health?${params.toString()}`),
  })
}

export function useEditSummary() {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ documentId, summary }: { documentId: string; summary: string }) =>
      client.patch<DocumentResponse>(`/api/v1/documents/${documentId}/summary`, { summary }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: connectorKeys.all }),
  })
}

export function useRegenerateSummary() {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (documentId: string) =>
      client.post<DocumentResponse>(`/api/v1/documents/${documentId}/summarize`, {}),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: connectorKeys.all })
      void queryClient.invalidateQueries({ queryKey: keys.all })
    },
  })
}
