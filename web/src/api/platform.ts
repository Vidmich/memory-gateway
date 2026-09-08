/**
 * Queries and mutations for the Platform screens (task 17).
 *
 * Two things here are not the usual shape.
 *
 * **A settings save can start a job.** Changing the embedding model does not write the
 * setting — it starts a reindex, and the setting lands when the aliases swap. So the
 * mutation's response carries a `reindex` run as well as the settings, and saving
 * invalidates the maintenance query too, because the progress bar lives on the other
 * screen.
 *
 * **The maintenance query polls while something is running, and not otherwise.** A
 * reindex is minutes to hours, and a fixed interval would either be too slow to watch or
 * a request every two seconds forever on a screen nobody is looking at.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query'

import { useApiClient } from '@/auth/AuthContext'
import type {
  ErasureReport,
  MaintenanceResponse,
  MaintenanceRun,
  OrganizationDeletionRequest,
  PlatformSettingsPatch,
  PlatformSettingsResponse,
  ReindexEstimate,
  ReindexRun,
  RetentionCeilingsResponse,
  SweepResponse,
} from '@/api/types'

/** How often to re-read while a job is in flight. */
const LIVE_MS = 3_000

export const keys = {
  settings: ['platform', 'settings'] as const,
  maintenance: ['platform', 'maintenance'] as const,
  reindex: (runId: string) => ['platform', 'reindex', runId] as const,
  ceilings: ['platform', 'retention-ceilings'] as const,
}

// -- reads -----------------------------------------------------------------

/**
 * The retention maxima, readable inside an organization.
 *
 * The one part of the platform configuration a tenant is entitled to see, because it is
 * what explains why the number they typed is not the number being honoured. Everything
 * else on the Platform screens — other tenants' quotas, the operator's spending ceilings —
 * is none of their business, and this is a separate endpoint rather than a filtered view
 * of the settings document so that staying that way needs no care.
 */
export function useRetentionCeilings(): UseQueryResult<RetentionCeilingsResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.ceilings,
    // Changed a few times a year by somebody else entirely, so a long stale time and no
    // refetch on focus: this is context for a form, not a live number.
    staleTime: 5 * 60_000,
    queryFn: () => client.get<RetentionCeilingsResponse>('/api/v1/retention-ceilings'),
  })
}

export function usePlatformSettings(): UseQueryResult<PlatformSettingsResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.settings,
    queryFn: () => client.get<PlatformSettingsResponse>('/api/v1/platform/settings'),
    // A reindex in flight changes what this endpoint reports, so it follows the same
    // cadence rather than showing a stale "reindexing" badge after the run has finished.
    refetchInterval: (query) => (query.state.data?.reindex ? LIVE_MS : false),
  })
}

export function useMaintenance(): UseQueryResult<MaintenanceResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.maintenance,
    queryFn: () => client.get<MaintenanceResponse>('/api/v1/platform/maintenance'),
    refetchInterval: (query) => (query.state.data?.reindex ? LIVE_MS : false),
  })
}

// -- writes ----------------------------------------------------------------

function useInvalidatePlatform(): () => Promise<void> {
  const queryClient = useQueryClient()
  return async () => {
    await queryClient.invalidateQueries({ queryKey: ['platform'] })
  }
}

export function useUpdatePlatformSettings() {
  const client = useApiClient()
  const invalidate = useInvalidatePlatform()
  return useMutation({
    mutationFn: (body: PlatformSettingsPatch) =>
      client.patch<PlatformSettingsResponse>('/api/v1/platform/settings', body),
    onSuccess: invalidate,
  })
}

/**
 * Ask what a reindex would cost, without starting one.
 *
 * A mutation rather than a query even though it changes nothing: it is a POST, it is
 * triggered by opening a dialog rather than by rendering a screen, and caching "what
 * would this cost" under a key would mean showing yesterday's number for a corpus that
 * has doubled since.
 */
export function useReindexEstimate() {
  const client = useApiClient()
  return useMutation({
    mutationFn: (organizationId?: string) =>
      client.post<ReindexEstimate>('/api/v1/platform/reindex', {
        dry_run: true,
        organization_id: organizationId ?? null,
      }),
  })
}

export function useStartReindex() {
  const client = useApiClient()
  const invalidate = useInvalidatePlatform()
  return useMutation({
    mutationFn: (organizationId?: string) =>
      client.post<ReindexRun>('/api/v1/platform/reindex', {
        organization_id: organizationId ?? null,
      }),
    onSuccess: invalidate,
  })
}

export function useRunMaintenance(job: 'partitions' | 'retention') {
  const client = useApiClient()
  const invalidate = useInvalidatePlatform()
  return useMutation({
    mutationFn: () => client.post<MaintenanceRun>(`/api/v1/platform/maintenance/${job}`, {}),
    onSuccess: invalidate,
  })
}

/**
 * The orphan sweep. `apply` is the caller's decision and defaults to a report.
 *
 * The default is repeated here as well as on the server, and that is not belt-and-braces
 * for its own sake: a UI that sent `apply` implicitly would make the destructive pass one
 * forgotten argument away, which is exactly the mistake the server's default exists to
 * catch.
 */
export function useSweep() {
  const client = useApiClient()
  const invalidate = useInvalidatePlatform()
  return useMutation({
    mutationFn: (options: { apply?: boolean } = {}) =>
      client.post<SweepResponse>('/api/v1/platform/maintenance/sweep', {
        apply: options.apply ?? false,
      }),
    onSuccess: invalidate,
  })
}

export function useScheduleDeletion(organizationId: string | undefined) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: OrganizationDeletionRequest) =>
      client.post<ErasureReport>(
        `/api/v1/platform/organizations/${organizationId}/deletion`,
        body,
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['organizations'] })
    },
  })
}

export function useCancelDeletion(organizationId: string | undefined) {
  const client = useApiClient()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      client.delete<void>(`/api/v1/platform/organizations/${organizationId}/deletion`),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['organizations'] })
    },
  })
}
