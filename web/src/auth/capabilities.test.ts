import { describe, expect, it } from 'vitest'

import { CAPABILITIES, can, canAll } from '@/auth/capabilities'
import { makeSuperadmin, makeUser } from '@/test/factories'

describe('capabilities', () => {
  it('reads what the server sent rather than re-deriving the matrix', () => {
    // The point of `/auth/me` returning these: a role added on the server needs no
    // change here, and the two cannot disagree.
    const viewer = makeUser({ role: 'org_viewer', capabilities: ['org:read'] })

    expect(can(viewer, 'org:read')).toBe(true)
    expect(can(viewer, 'org:administer')).toBe(false)
  })

  it('gives nobody anything before the session is restored', () => {
    expect(can(null, 'org:read')).toBe(false)
    expect(can(undefined, 'platform:administer')).toBe(false)
  })

  it('treats a response with no capabilities as no capabilities', () => {
    // The field is optional on the wire, so an older or partial response must read as
    // "cannot", never as "can". Built by omission rather than by passing `undefined`,
    // which `exactOptionalPropertyTypes` correctly refuses.
    const { capabilities: _absent, ...user } = makeUser()

    for (const capability of CAPABILITIES) expect(can(user, capability)).toBe(false)
  })

  it('requires every capability in the list', () => {
    const member = makeUser({ capabilities: ['org:read', 'resources:write'] })

    expect(canAll(member, ['org:read'])).toBe(true)
    expect(canAll(member, ['org:read', 'keys:manage'])).toBe(false)
  })

  it('is trivially true for an empty requirement', () => {
    expect(canAll(makeUser({ capabilities: [] }), [])).toBe(true)
  })

  it('gives a superadmin the platform capability', () => {
    expect(can(makeSuperadmin(), 'platform:administer')).toBe(true)
  })
})
