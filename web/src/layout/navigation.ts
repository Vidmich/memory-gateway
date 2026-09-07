/**
 * The sidebar's contents.
 *
 * This is the extension point for tasks 05 through 15: add an entry and a route, and
 * touch nothing else in the shell. `roles` decides only what is *drawn* — enforcement is
 * task 04's job, and a hidden link is not a permission check.
 */

export type NavEntry = {
  to: string
  label: string
  /** Omit for "everyone". */
  roles?: readonly string[]
}

export const NAVIGATION: readonly NavEntry[] = [{ to: '/', label: 'Dashboard' }]

export function visibleNavigation(
  entries: readonly NavEntry[],
  role: string | undefined,
): readonly NavEntry[] {
  return entries.filter((entry) => !entry.roles || (role ? entry.roles.includes(role) : false))
}
