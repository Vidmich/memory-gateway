/**
 * Queries and mutations for gateways and their API keys.
 *
 * One module, like `models.ts`, so the cache keys are written next to each other. Two of
 * them are load-bearing:
 *
 * Creating or revoking a **key** invalidates the *gateway* queries as well as the key
 * list, because the list screen shows a key count. Forgetting that is how a page shows
 * "0 keys" next to a key somebody just made.
 *
 * The key mint is a mutation with **no cached result**. Its response is the only place
 * the plaintext exists, so it is handed to the caller and deliberately never written into
 * the query cache — a cache entry is a copy, and a copy of a secret that cannot be shown
 * twice is a copy too many.
 */

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { useApiClient } from '@/auth/AuthContext'
import type {
  ApiKeyCreateRequest,
  ApiKeyResponse,
  GatewayCreateRequest,
  GatewayPage,
  GatewayResponse,
  GatewayTestRequest,
  GatewayTestResponse,
  GatewayUpdateRequest,
  IssuedApiKeyResponse,
  MemoryPreviewRequest,
  PromptPreviewResponse,
  RetrievalPreviewResponse,
} from '@/api/types'

export const keys = {
  all: ['gateways'] as const,
  list: (cursor?: string | null) => ['gateways', { cursor: cursor ?? null }] as const,
  one: (id: string) => ['gateways', id] as const,
  apiKeys: (gatewayId: string) => ['gateways', gatewayId, 'keys'] as const,
}

function listPath(cursor?: string | null): string {
  return cursor ? `/api/v1/gateways?cursor=${encodeURIComponent(cursor)}` : '/api/v1/gateways'
}

// -- reads -----------------------------------------------------------------

export function useGateways(cursor?: string | null): UseQueryResult<GatewayPage> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.list(cursor),
    queryFn: () => client.get<GatewayPage>(listPath(cursor)),
  })
}

export function useGateway(id: string | undefined): UseQueryResult<GatewayResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.one(id ?? ''),
    queryFn: () => client.get<GatewayResponse>(`/api/v1/gateways/${id}`),
    enabled: Boolean(id),
  })
}

export function useApiKeys(gatewayId: string | undefined): UseQueryResult<ApiKeyResponse[]> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.apiKeys(gatewayId ?? ''),
    queryFn: () => client.get<ApiKeyResponse[]>(`/api/v1/gateways/${gatewayId}/keys`),
    enabled: Boolean(gatewayId),
  })
}

// -- writes ----------------------------------------------------------------

/**
 * Invalidate every gateway query, not the clever subset.
 *
 * A rename changes the list and the row's own entry; a key change changes the list's key
 * count. Working out which is how a stale count survives a save, and these lists are
 * small.
 */
function useInvalidateGateways(): () => Promise<void> {
  const queryClient = useQueryClient()
  return async () => {
    await queryClient.invalidateQueries({ queryKey: keys.all })
  }
}

export function useCreateGateway() {
  const client = useApiClient()
  const invalidate = useInvalidateGateways()
  return useMutation({
    mutationFn: (body: GatewayCreateRequest) =>
      client.post<GatewayResponse>('/api/v1/gateways', body),
    onSuccess: invalidate,
  })
}

export function useUpdateGateway(id: string | undefined) {
  const client = useApiClient()
  const invalidate = useInvalidateGateways()
  return useMutation({
    mutationFn: (body: GatewayUpdateRequest) =>
      client.patch<GatewayResponse>(`/api/v1/gateways/${id}`, body),
    onSuccess: invalidate,
  })
}

export function useDeleteGateway() {
  const client = useApiClient()
  const invalidate = useInvalidateGateways()
  return useMutation({
    mutationFn: (id: string) => client.delete<void>(`/api/v1/gateways/${id}`),
    onSuccess: invalidate,
  })
}

/**
 * Mint a key. The response carries the only copy of the plaintext there will ever be, so
 * the caller shows it once and lets it go; nothing here retains it.
 */
export function useCreateApiKey(gatewayId: string | undefined) {
  const client = useApiClient()
  const invalidate = useInvalidateGateways()
  return useMutation({
    mutationFn: (body: ApiKeyCreateRequest) =>
      client.post<IssuedApiKeyResponse>(`/api/v1/gateways/${gatewayId}/keys`, body),
    onSuccess: invalidate,
  })
}

export function useRevokeApiKey() {
  const client = useApiClient()
  const invalidate = useInvalidateGateways()
  return useMutation({
    mutationFn: (keyId: string) => client.delete<ApiKeyResponse>(`/api/v1/keys/${keyId}`),
    onSuccess: invalidate,
  })
}

/** Send a probe completion through the real proxy path. */
export function useTestGateway(gatewayId: string | undefined) {
  const client = useApiClient()
  return useMutation({
    mutationFn: (body: GatewayTestRequest) =>
      client.post<GatewayTestResponse>(`/api/v1/gateways/${gatewayId}/test`, body),
  })
}

/**
 * Try retrieval: the chunks a question would inject, with their scores.
 *
 * A mutation rather than a query, and the result is deliberately not cached. It is an
 * action somebody takes — type, press, read — not state the screen is showing, and a
 * cached answer from before the last edit to the score floor is worse than no answer.
 */
export function useTryRetrieval(gatewayId: string | undefined) {
  const client = useApiClient()
  return useMutation({
    mutationFn: (body: MemoryPreviewRequest) =>
      client.post<RetrievalPreviewResponse>(
        `/api/v1/gateways/${gatewayId}/try-retrieval`,
        body,
      ),
  })
}

/** The fully assembled prompt for a sample question, layer by layer. */
export function usePromptPreview(gatewayId: string | undefined) {
  const client = useApiClient()
  return useMutation({
    mutationFn: (body: MemoryPreviewRequest) =>
      client.post<PromptPreviewResponse>(
        `/api/v1/gateways/${gatewayId}/prompt-preview`,
        body,
      ),
  })
}
