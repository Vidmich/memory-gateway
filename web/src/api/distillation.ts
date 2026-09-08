/**
 * Queries and mutations for memory write-back (SPEC §6.4, §10.1).
 *
 * Two decisions shape the file.
 *
 * **A settings save invalidates the health query as well as its own.** Turning distillation
 * off, or lowering the daily cap, changes what the chart below the form is about — and a
 * form that saved while the chart went on describing the old configuration is a screen
 * that quietly disagrees with itself.
 *
 * **"Distil now" invalidates the whole memory browser.** It writes facts, supersedes facts
 * and evicts facts, so the fact list, the fact counts on the end-user list and the health
 * numbers are all potentially stale afterwards. It costs one extra round trip on an action
 * somebody performs by hand, and it is the difference between a button that visibly works
 * and one that requires a refresh to believe.
 */

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { useApiClient } from '@/auth/AuthContext'
import { keys as endUserKeys } from '@/api/endUsers'
import type {
  DistillationSettings,
  DistillationSettingsRequest,
  ManualPassResponse,
  MemoryHealth,
} from '@/api/types'

export const keys = {
  all: ['distillation'] as const,
  settings: ['distillation', 'settings'] as const,
  health: (days: number) => ['distillation', 'health', { days }] as const,
}

export function useDistillationSettings(): UseQueryResult<DistillationSettings> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.settings,
    queryFn: () => client.get<DistillationSettings>('/api/v1/distillation'),
  })
}

export function useMemoryHealth(days = 30): UseQueryResult<MemoryHealth> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.health(days),
    queryFn: () => client.get<MemoryHealth>(`/api/v1/distillation/health?days=${days}`),
  })
}

export function useUpdateDistillation() {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: DistillationSettingsRequest) =>
      client.patch<DistillationSettings>('/api/v1/distillation', body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.all }),
  })
}

export function useDistilNow(endUserId: string | undefined) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      client.post<ManualPassResponse>(`/api/v1/end-users/${endUserId}/distil`, {}),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: endUserKeys.all })
      void queryClient.invalidateQueries({ queryKey: keys.all })
    },
  })
}
