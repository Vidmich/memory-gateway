/**
 * What the signed-in user may do, as the server reported it.
 *
 * `/auth/me` returns the resolved capability set, so the matrix lives in one place
 * (`app/services/permissions.py`) and the UI reads it rather than re-deriving it. A role
 * added on the server therefore needs no change here.
 *
 * **This is presentation only.** Hiding a button is a courtesy, not a permission check —
 * the API refuses the same call whether or not the button was rendered, and the tests in
 * `tests/test_directory_api.py` assert exactly that. Never let a check here be the only
 * thing standing between a user and an action.
 */

import type { CurrentUser } from '@/api/types'

export const CAPABILITIES = [
  'org:read',
  'resources:write',
  'keys:manage',
  'org:administer',
  'platform:administer',
] as const

export type Capability = (typeof CAPABILITIES)[number]

export function can(user: CurrentUser | null | undefined, capability: Capability): boolean {
  // Optional on the wire (it has a server-side default), so an older response or a
  // half-built fixture reads as "no capabilities" rather than throwing.
  return Boolean(user?.capabilities?.includes(capability))
}

/** True when every one of `required` is held. An empty list is trivially true. */
export function canAll(
  user: CurrentUser | null | undefined,
  required: readonly Capability[],
): boolean {
  return required.every((capability) => can(user, capability))
}
