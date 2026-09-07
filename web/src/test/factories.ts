/**
 * Fixtures shaped by the *generated* server types.
 *
 * They are here rather than inline in each test because the types come from the API
 * schema: when the server adds a required field, one file fails to compile instead of
 * five, and the fix is made once. That is the whole point of generating the client.
 */

import type {
  CurrentUser,
  InvitationResponse,
  MemberResponse,
  OrganizationResponse,
} from '@/api/types'

const NOW = '2026-09-06T12:00:00Z'

export function makeUser(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    id: 'u1',
    email: 'ada@example.com',
    name: 'Ada Lovelace',
    role: 'org_admin',
    status: 'active',
    last_login_at: null,
    organization: { id: 'o1', name: 'Acme', slug: 'acme', status: 'active' },
    capabilities: ['keys:manage', 'org:administer', 'org:read', 'resources:write'],
    ...overrides,
  }
}

export function makeSuperadmin(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return makeUser({
    id: 'root',
    email: 'root@example.com',
    name: 'Root',
    role: 'superadmin',
    organization: null,
    capabilities: [
      'keys:manage',
      'org:administer',
      'org:read',
      'platform:administer',
      'resources:write',
    ],
    ...overrides,
  })
}

export function makeOrganization(
  overrides: Partial<OrganizationResponse> = {},
): OrganizationResponse {
  return {
    id: 'o1',
    name: 'Acme',
    slug: 'acme',
    status: 'active',
    settings: {},
    created_at: NOW,
    member_count: 3,
    gateway_count: 1,
    ...overrides,
  }
}

export function makeMember(overrides: Partial<MemberResponse> = {}): MemberResponse {
  return {
    id: 'm1',
    email: 'member@example.com',
    name: 'Member',
    role: 'org_member',
    status: 'active',
    last_login_at: null,
    created_at: NOW,
    ...overrides,
  }
}

export function makeInvitation(overrides: Partial<InvitationResponse> = {}): InvitationResponse {
  return {
    id: 'i1',
    organization_id: 'o1',
    email: 'invitee@example.com',
    role: 'org_member',
    status: 'pending',
    expires_at: '2026-09-13T12:00:00Z',
    accepted_at: null,
    created_at: NOW,
    ...overrides,
  }
}
