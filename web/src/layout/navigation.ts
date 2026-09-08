/**
 * The sidebar's contents.
 *
 * The extension point for tasks 05 through 15: add an entry and a route, and touch
 * nothing else in the shell.
 *
 * `capabilities` decides only what is *drawn*. A hidden link is not a permission check —
 * the API refuses the request either way — so this list is about not showing people
 * doors they cannot open, not about locking them.
 */

import type { CurrentUser } from '@/api/types'
import { canAll, type Capability } from '@/auth/capabilities'

export type NavEntry = {
  to: string
  label: string
  /** Omit for "everyone signed in". All listed capabilities are required. */
  capabilities?: readonly Capability[]
  /** Grouping heading shown above the entry, when it starts a new group. */
  section?: string
}

export const NAVIGATION: readonly NavEntry[] = [
  { to: '/', label: 'Dashboard' },
  // No capability: every role may read the catalog, and the write controls inside
  // the screen are what `resources:write` gates.
  { to: '/models', label: 'Models' },
  // Same reasoning: the list is readable by every role, and `resources:write` gates the
  // controls inside the screen. Keys have their own gate, inside the editor.
  { to: '/gateways', label: 'Gateways' },
  // Where content comes from. Readable by every role for the same reason as the two
  // above: the write controls inside the screen are what `resources:write` gates.
  { to: '/connectors', label: 'Connectors' },
  // Readable by every role: the person triaging a support ticket should not need the
  // permission to reconfigure production in order to answer it.
  { to: '/monitoring', label: 'Monitoring' },
  // What the assistant remembers about the people on the far side of a customer's app.
  // Same reasoning again: reading it is how a support question gets answered, and the
  // write controls inside the screen are what `resources:write` gates.
  { to: '/memory', label: 'Memory' },
  // Readable by every role, like Monitoring and for the same reason: the question
  // "who turned this off yesterday" is asked by whoever is answering the ticket, and
  // needing write permission to look would be a worse posture, not a better one.
  { to: '/audit', label: 'Audit log' },
  { to: '/settings', label: 'Organization', section: 'Settings' },
  { to: '/settings/members', label: 'Members' },
  {
    to: '/platform/organizations',
    label: 'Organizations',
    section: 'Platform',
    capabilities: ['platform:administer'],
  },
  { to: '/platform/settings', label: 'Settings', capabilities: ['platform:administer'] },
  // Maintenance rather than "Jobs": what an operator comes here for is the state of the
  // data lifecycle — runway, what retention took, whether a reindex is running — and the
  // fact that jobs produce it is an implementation detail of the answer.
  { to: '/platform/maintenance', label: 'Maintenance', capabilities: ['platform:administer'] },
]

export function visibleNavigation(
  entries: readonly NavEntry[],
  user: CurrentUser | null | undefined,
): readonly NavEntry[] {
  return entries.filter((entry) => canAll(user, entry.capabilities ?? []))
}

/**
 * Entries grouped under their section heading, with a heading dropped when everything
 * beneath it was filtered out — an empty "Platform" label is worse than no label.
 */
export function navigationSections(
  entries: readonly NavEntry[],
): readonly { section: string | null; entries: NavEntry[] }[] {
  const groups: { section: string | null; entries: NavEntry[] }[] = []
  for (const entry of entries) {
    const last = groups.at(-1)
    if (!last || entry.section) groups.push({ section: entry.section ?? null, entries: [entry] })
    else last.entries.push(entry)
  }
  return groups.filter((group) => group.entries.length > 0)
}
