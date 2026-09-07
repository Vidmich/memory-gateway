import { describe, expect, it } from 'vitest'

import { NAVIGATION, navigationSections, visibleNavigation } from '@/layout/navigation'
import { makeSuperadmin, makeUser } from '@/test/factories'

const labels = (user: Parameters<typeof visibleNavigation>[1]) =>
  visibleNavigation(NAVIGATION, user).map((entry) => entry.label)

describe('navigation', () => {
  it('shows the platform area only to a platform administrator', () => {
    expect(labels(makeSuperadmin())).toContain('Organizations')
    expect(labels(makeUser({ role: 'org_admin' }))).not.toContain('Organizations')
  })

  it('shows the org screens to every role', () => {
    for (const capabilities of [['org:read'], ['org:read', 'resources:write']]) {
      expect(labels(makeUser({ capabilities }))).toEqual(
        expect.arrayContaining(['Dashboard', 'Organization', 'Members']),
      )
    }
  })

  it('shows Models to every role, including a viewer', () => {
    // Reading the catalog is `org:read`; the write controls inside the screen are what
    // `resources:write` gates, and the API is what refuses.
    const viewer = makeUser({ role: 'org_viewer', capabilities: ['org:read'] })

    expect(labels(viewer)).toContain('Models')
  })

  it('shows nothing that needs a capability before the user is known', () => {
    expect(labels(null)).not.toContain('Organizations')
  })

  it('drops a section heading when everything under it was filtered out', () => {
    // An empty "Platform" label is worse than no label.
    const sections = navigationSections(visibleNavigation(NAVIGATION, makeUser()))

    expect(sections.map((group) => group.section)).not.toContain('Platform')
  })

  it('keeps the section heading when its entries survive', () => {
    const sections = navigationSections(visibleNavigation(NAVIGATION, makeSuperadmin()))

    expect(sections.map((group) => group.section)).toContain('Platform')
  })

  it('groups entries under the heading that introduced them', () => {
    const sections = navigationSections(visibleNavigation(NAVIGATION, makeUser()))
    const settings = sections.find((group) => group.section === 'Settings')

    expect(settings?.entries.map((entry) => entry.label)).toEqual(['Organization', 'Members'])
  })

  it('is a hint, not a permission check', () => {
    // A hidden link is a courtesy. `tests/test_directory_api.py` is what asserts the API
    // refuses the request, and this test exists so nobody deletes that one thinking this
    // covers it.
    const viewer = makeUser({ role: 'org_viewer', capabilities: ['org:read'] })

    expect(labels(viewer)).toContain('Members')
  })
})
