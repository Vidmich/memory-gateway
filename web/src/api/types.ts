/**
 * The server's types, not a hand-written copy of them.
 *
 * `schema.d.ts` is generated from the FastAPI schema by `make openapi`, and CI fails if
 * the checked-in copy has drifted. Aliasing the pieces the app uses here is what makes
 * that check bite: rename a field on the server and this file stops compiling, instead
 * of the UI quietly rendering `undefined`.
 */

import type { components } from '@/api/schema'

export type SessionResponse = components['schemas']['SessionResponse']
export type CurrentUser = components['schemas']['UserSummary']
export type OrganizationSummary = components['schemas']['OrganizationSummary']
export type LoginRequest = components['schemas']['LoginRequest']
export type PasswordChangeRequest = components['schemas']['PasswordChangeRequest']
