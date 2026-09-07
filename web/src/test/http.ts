/** Helpers for stubbing `fetch` in tests. */

/** The path a `fetch` call was made to, whichever of the three input shapes was used. */
export function pathOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.pathname
  return input.url
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** The JSON body a `fetch` call carried, or `{}` if it carried none. */
export function bodyOf<T>(init: RequestInit | undefined): T {
  return typeof init?.body === 'string' ? (JSON.parse(init.body) as T) : ({} as T)
}
