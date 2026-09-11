/**
 * Queries and mutations for upstream models.
 *
 * One module, like `directory.ts`, so the cache keys are written next to each other:
 * saving a model has to invalidate both tabs *and* the single-model query the form is
 * reading, and that is only obvious when the keys are in one file.
 *
 * The two test-connection mutations are deliberately separate rather than one call with
 * an optional id. They are different operations — one probes what is stored, the other
 * probes what is on screen — and conflating them is how a form ends up testing the saved
 * configuration and reporting it as if it had tested the edits.
 */

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { useApiClient } from '@/auth/AuthContext'
import type {
  CalibrationResponse,
  ModelCreateRequest,
  ModelPage,
  ModelResponse,
  ModelTestRequest,
  ModelUpdateRequest,
  ProbeResponse,
  TokenizersResponse,
} from '@/api/types'

export type ModelScope = 'global' | 'org'

export const keys = {
  all: ['models'] as const,
  list: (scope: ModelScope | null, cursor?: string | null) =>
    ['models', { scope: scope ?? null, cursor: cursor ?? null }] as const,
  one: (id: string) => ['models', id] as const,
  //  Task 101. Under the models prefix so a save invalidates the drift rows too: a
  //  tokenizer override starts a fresh window, and the old ratio must not linger.
  calibration: ['models', 'calibration'] as const,
  tokenizers: ['tokenizers'] as const,
}

function listPath(scope: ModelScope | null, cursor?: string | null): string {
  const query = new URLSearchParams()
  if (scope) query.set('scope', scope)
  if (cursor) query.set('cursor', cursor)
  const suffix = query.toString()
  return suffix ? `/api/v1/models?${suffix}` : '/api/v1/models'
}

// -- reads -----------------------------------------------------------------

export function useModels(
  scope: ModelScope | null,
  cursor?: string | null,
): UseQueryResult<ModelPage> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.list(scope, cursor),
    queryFn: () => client.get<ModelPage>(listPath(scope, cursor)),
  })
}

export function useModel(id: string | undefined): UseQueryResult<ModelResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.one(id ?? ''),
    queryFn: () => client.get<ModelResponse>(`/api/v1/models/${id}`),
    enabled: Boolean(id),
  })
}

/** The closed registry and the derivation table (task 101) — served, not duplicated. */
export function useTokenizers(): UseQueryResult<TokenizersResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.tokenizers,
    queryFn: () => client.get<TokenizersResponse>('/api/v1/tokenizers'),
    staleTime: Infinity,
  })
}

/** Every visible model's tokenizer drift, in one round trip (task 101). */
export function useCalibrations(enabled = true): UseQueryResult<CalibrationResponse[]> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.calibration,
    queryFn: () => client.get<CalibrationResponse[]>('/api/v1/models/calibration'),
    enabled,
  })
}

// -- writes ----------------------------------------------------------------

/**
 * Invalidate every model query, not the clever subset.
 *
 * A rename changes both tabs and the row's own cache entry; a superadmin's edit to a
 * global model changes what every organization sees. Working out which is how a stale
 * name survives a save, and these lists are small.
 */
function useInvalidateModels(): () => Promise<void> {
  const queryClient = useQueryClient()
  return async () => {
    await queryClient.invalidateQueries({ queryKey: keys.all })
  }
}

export function useCreateModel() {
  const client = useApiClient()
  const invalidate = useInvalidateModels()
  return useMutation({
    mutationFn: (body: ModelCreateRequest) => client.post<ModelResponse>('/api/v1/models', body),
    onSuccess: invalidate,
  })
}

export function useUpdateModel(id: string | undefined) {
  const client = useApiClient()
  const invalidate = useInvalidateModels()
  return useMutation({
    mutationFn: (body: ModelUpdateRequest) =>
      client.patch<ModelResponse>(`/api/v1/models/${id}`, body),
    onSuccess: invalidate,
  })
}

export function useDeleteModel() {
  const client = useApiClient()
  const invalidate = useInvalidateModels()
  return useMutation({
    mutationFn: (id: string) => client.delete<void>(`/api/v1/models/${id}`),
    onSuccess: invalidate,
  })
}

/** Store the ratio the window measured as the model's tokenizer override (task 101). */
export function useCalibrateModel() {
  const client = useApiClient()
  const invalidate = useInvalidateModels()
  return useMutation({
    mutationFn: (id: string) => client.post<ModelResponse>(`/api/v1/models/${id}/calibrate`),
    onSuccess: invalidate,
  })
}

/** Probe a saved model, exactly as stored. */
export function useTestModel() {
  const client = useApiClient()
  return useMutation({
    mutationFn: (id: string) => client.post<ProbeResponse>(`/api/v1/models/${id}/test`),
  })
}

/** Probe what is on screen, before it has been saved. */
export function useTestDraft() {
  const client = useApiClient()
  return useMutation({
    mutationFn: (body: ModelTestRequest) =>
      client.post<ProbeResponse>('/api/v1/models/test', body),
  })
}
