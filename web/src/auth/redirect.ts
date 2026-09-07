/**
 * Where the login page sends you, and where it lets you come back to.
 *
 * Pure URL handling, kept out of the component so it can be tested on its own — and
 * because `safeNext` is a security boundary, not a rendering detail.
 */

/** Builds `/login?next=…` so a deep link survives a sign-in. */
export function loginPathFor(location: { pathname: string; search: string }): string {
  const target = `${location.pathname}${location.search}`
  if (target === '/' || target === '') return '/login'
  return `/login?next=${encodeURIComponent(target)}`
}

/**
 * Only same-site paths come back out.
 *
 * `next` arrives in the query string, so it is attacker-controlled: without this the
 * login page is an open redirect that borrows this application's credibility.
 */
export function safeNext(next: string | null): string {
  if (!next) return '/'
  if (!next.startsWith('/') || next.startsWith('//')) return '/'
  return next
}
