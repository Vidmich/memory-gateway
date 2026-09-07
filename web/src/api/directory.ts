/**
 * Queries and mutations for organizations, members and invitations.
 *
 * One module rather than a hook file per screen, because the cache keys have to agree
 * across screens: inviting someone on the Members page has to invalidate the invitation
 * list *and* the organization list's member count, and that is only obvious when the keys
 * are written next to each other.
 *
 * Every list is cursor-paginated (SPEC §12.2), so the cursor is part of the key. Paging
 * forward therefore fetches rather than reusing a stale page — which is the correct
 * behaviour for a list whose contents change.
 */

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { useApiClient } from '@/auth/AuthContext'
import type {
  InvitationPage,
  IssuedInvitationResponse,
  MemberPage,
  MemberUpdateRequest,
  OrganizationPage,
  OrganizationResponse,
  OrganizationUpdateRequest,
} from '@/api/types'

export const keys = {
  organizations: (cursor?: string | null) => ['organizations', { cursor: cursor ?? null }] as const,
  organization: (id: string) => ['organizations', id] as const,
  members: (organizationId: string, cursor?: string | null) =>
    ['organizations', organizationId, 'members', { cursor: cursor ?? null }] as const,
  invitations: (cursor?: string | null) => ['invitations', { cursor: cursor ?? null }] as const,
}

function withCursor(path: string, cursor?: string | null): string {
  return cursor ? `${path}${path.includes('?') ? '&' : '?'}cursor=${encodeURIComponent(cursor)}` : path
}

// -- reads -----------------------------------------------------------------

export function useOrganizations(cursor?: string | null): UseQueryResult<OrganizationPage> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.organizations(cursor),
    queryFn: () => client.get<OrganizationPage>(withCursor('/api/v1/organizations', cursor)),
  })
}

export function useOrganization(id: string | undefined): UseQueryResult<OrganizationResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.organization(id ?? ''),
    queryFn: () => client.get<OrganizationResponse>(`/api/v1/organizations/${id}`),
    enabled: Boolean(id),
  })
}

export function useMembers(
  organizationId: string | undefined,
  cursor?: string | null,
): UseQueryResult<MemberPage> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.members(organizationId ?? '', cursor),
    queryFn: () =>
      client.get<MemberPage>(
        withCursor(`/api/v1/organizations/${organizationId}/members`, cursor),
      ),
    enabled: Boolean(organizationId),
  })
}

export function useInvitations(cursor?: string | null): UseQueryResult<InvitationPage> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.invitations(cursor),
    queryFn: () => client.get<InvitationPage>(withCursor('/api/v1/invitations', cursor)),
  })
}

// -- writes ----------------------------------------------------------------

/**
 * Invalidate broadly on purpose.
 *
 * Removing a member changes the member list, the organization's member count on the
 * platform screen, and — if they had a pending invitation — the invitation list. Working
 * out which of those a given mutation touched is how a stale count survives a deploy;
 * refetching a handful of small lists does not.
 */
function useInvalidateDirectory(): () => Promise<void> {
  const queryClient = useQueryClient()
  return async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['organizations'] }),
      queryClient.invalidateQueries({ queryKey: ['invitations'] }),
    ])
  }
}

export function useCreateOrganization() {
  const client = useApiClient()
  const invalidate = useInvalidateDirectory()
  return useMutation({
    mutationFn: (body: { name: string; slug: string }) =>
      client.post<OrganizationResponse>('/api/v1/organizations', body),
    onSuccess: invalidate,
  })
}

export function useUpdateOrganization(organizationId: string | undefined) {
  const client = useApiClient()
  const invalidate = useInvalidateDirectory()
  return useMutation({
    mutationFn: (body: OrganizationUpdateRequest) =>
      client.patch<OrganizationResponse>(`/api/v1/organizations/${organizationId}`, body),
    onSuccess: invalidate,
  })
}

export function useUpdateMember() {
  const client = useApiClient()
  const invalidate = useInvalidateDirectory()
  return useMutation({
    mutationFn: ({ id, ...body }: MemberUpdateRequest & { id: string }) =>
      client.patch<unknown>(`/api/v1/members/${id}`, body),
    onSuccess: invalidate,
  })
}

export function useRemoveMember() {
  const client = useApiClient()
  const invalidate = useInvalidateDirectory()
  return useMutation({
    mutationFn: (id: string) => client.delete<void>(`/api/v1/members/${id}`),
    onSuccess: invalidate,
  })
}

export function useCreateInvitation(organizationId: string | undefined) {
  const client = useApiClient()
  const invalidate = useInvalidateDirectory()
  return useMutation({
    mutationFn: (body: { email: string; role: string }) =>
      client.post<IssuedInvitationResponse>(
        `/api/v1/organizations/${organizationId}/invitations`,
        body,
      ),
    onSuccess: invalidate,
  })
}

export function useResendInvitation() {
  const client = useApiClient()
  const invalidate = useInvalidateDirectory()
  return useMutation({
    mutationFn: (id: string) =>
      client.post<IssuedInvitationResponse>(`/api/v1/invitations/${id}/resend`),
    onSuccess: invalidate,
  })
}

export function useRevokeInvitation() {
  const client = useApiClient()
  const invalidate = useInvalidateDirectory()
  return useMutation({
    mutationFn: (id: string) => client.delete<void>(`/api/v1/invitations/${id}`),
    onSuccess: invalidate,
  })
}
