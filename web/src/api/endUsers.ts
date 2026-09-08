/**
 * Queries and mutations for the memory browser.
 *
 * One decision shapes the whole file: **every write invalidates both the fact list and
 * the end-user list**, because a fact count is shown on the list screen and the two
 * disagreeing is the sort of thing a person notices and then stops trusting. It costs one
 * extra request on an action somebody performs by hand.
 *
 * Purge is a `DELETE` that returns a body — how many facts and transcripts it removed —
 * so the toast can say what happened rather than "done".
 */

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { useApiClient } from '@/auth/AuthContext'
import type {
  EndUserPage,
  EndUserResponse,
  MemoryFactCreateRequest,
  MemoryFactPage,
  MemoryFactResponse,
  MemoryFactUpdateRequest,
  MemoryPurgeResponse,
  MemorySearchResponse,
} from '@/api/types'

export const keys = {
  all: ['end-users'] as const,
  list: (search: string, cursor?: string | null) =>
    ['end-users', { search, cursor: cursor ?? null }] as const,
  one: (id: string) => ['end-users', id] as const,
  facts: (id: string, filters: FactFilters) => ['end-users', id, 'memory', filters] as const,
}

/**
 * How the memory browser narrows a list.
 *
 * Applied server-side rather than in the browser, and that is not an optimisation: a page
 * filtered after it arrives is a page that can come back empty while the next one is full,
 * and a screen that says "no constraints" when it means "none in the first fifty" is worse
 * than one with no filter at all.
 */
export type FactFilters = {
  liveOnly?: boolean
  kind?: string | undefined
  minConfidence?: number | undefined
}

function factsPath(id: string | undefined, filters: FactFilters): string {
  const query = new URLSearchParams()
  if (filters.liveOnly) query.set('live_only', 'true')
  if (filters.kind) query.set('kind', filters.kind)
  if (filters.minConfidence) query.set('min_confidence', String(filters.minConfidence))
  const suffix = query.toString()
  return `/api/v1/end-users/${id}/memory${suffix ? `?${suffix}` : ''}`
}

function listPath(search: string, cursor?: string | null): string {
  const query = new URLSearchParams()
  if (search) query.set('search', search)
  if (cursor) query.set('cursor', cursor)
  const suffix = query.toString()
  return suffix ? `/api/v1/end-users?${suffix}` : '/api/v1/end-users'
}

// -- reads -----------------------------------------------------------------

export function useEndUsers(search = '', cursor?: string | null): UseQueryResult<EndUserPage> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.list(search, cursor),
    queryFn: () => client.get<EndUserPage>(listPath(search, cursor)),
  })
}

export function useEndUser(id: string | undefined): UseQueryResult<EndUserResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.one(id ?? 'unknown'),
    queryFn: () => client.get<EndUserResponse>(`/api/v1/end-users/${id}`),
    enabled: Boolean(id),
  })
}

export function useMemoryFacts(
  id: string | undefined,
  filters: FactFilters = {},
): UseQueryResult<MemoryFactPage> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.facts(id ?? 'unknown', filters),
    queryFn: () => client.get<MemoryFactPage>(factsPath(id, filters)),
    enabled: Boolean(id),
  })
}

// -- writes ----------------------------------------------------------------

export function useSearchMemory(id: string | undefined) {
  const client = useApiClient()
  return useMutation({
    mutationFn: (query: string) =>
      client.post<MemorySearchResponse>(`/api/v1/end-users/${id}/memory/search`, { query }),
  })
}

export function useCreateFact(id: string | undefined) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: MemoryFactCreateRequest) =>
      client.post<MemoryFactResponse>(`/api/v1/end-users/${id}/memory`, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.all }),
  })
}

export function useUpdateFact() {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: MemoryFactUpdateRequest }) =>
      client.patch<MemoryFactResponse>(`/api/v1/memory-facts/${id}`, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.all }),
  })
}

export function useDeleteFact() {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => client.delete<void>(`/api/v1/memory-facts/${id}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.all }),
  })
}

export function usePurgeMemory(id: string | undefined) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (includeTranscripts: boolean) =>
      client.delete<MemoryPurgeResponse>(
        `/api/v1/end-users/${id}/memory${includeTranscripts ? '?include_transcripts=true' : ''}`,
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.all }),
  })
}
