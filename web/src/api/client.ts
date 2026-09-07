/**
 * The one place the browser talks to the API.
 *
 * Two decisions matter here.
 *
 * **The access token lives in memory, never in `localStorage`.** Anything in
 * `localStorage` is readable by any script that gets injected into the page, which is
 * precisely the attack a short-lived token is supposed to survive. It is lost on reload;
 * the httpOnly refresh cookie is what brings the session back.
 *
 * **Refreshes are single-flight.** When an access token expires, every in-flight request
 * fails at once. Refreshing per request would fire N refreshes, and since the server
 * rotates the token and revokes the family on reuse, the second one would sign the user
 * out. So concurrent 401s queue behind one shared refresh and then retry.
 */

import type { CurrentUser, SessionResponse } from '@/api/types'

export type { CurrentUser, SessionResponse }

export type ErrorBody = {
  error?: {
    code?: string
    message?: string
    request_id?: string
    details?: unknown
  }
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: unknown,
    readonly requestId?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** Field-level messages, keyed by field name, for `Form` to render inline. */
  get fieldErrors(): Record<string, string> {
    const details = this.details as { errors?: { loc?: string[]; msg?: string }[] } | undefined
    const errors = details?.errors
    if (!Array.isArray(errors)) return {}

    const byField: Record<string, string> = {}
    for (const item of errors) {
      // FastAPI reports `["body", "email"]`; the field is the last segment.
      const field = item.loc?.at(-1)
      if (field && item.msg && !(field in byField)) byField[field] = item.msg
    }
    return byField
  }
}

export type RequestOptions = {
  body?: unknown
  signal?: AbortSignal
  /** Internal: false on the retry, so a second 401 cannot loop. */
  allowRefresh?: boolean
}

/** Endpoints that must never trigger a refresh — refreshing them is what they are. */
const NO_REFRESH = ['/api/v1/auth/login', '/api/v1/auth/refresh', '/api/v1/auth/logout']

export class ApiClient {
  private accessToken: string | null = null
  private inFlightRefresh: Promise<string | null> | null = null

  /** Counts completed refresh calls. The concurrency test asserts on it. */
  refreshCount = 0

  constructor(
    private readonly fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis),
    /** Called when a refresh fails, i.e. the session is really over. */
    private onSessionEnded: () => void = () => {},
  ) {}

  setSessionEndedHandler(handler: () => void): void {
    this.onSessionEnded = handler
  }

  setAccessToken(token: string | null): void {
    this.accessToken = token
  }

  hasAccessToken(): boolean {
    return this.accessToken !== null
  }

  async request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
    const { body, signal, allowRefresh = true } = options

    const headers: Record<string, string> = { accept: 'application/json' }
    if (body !== undefined) headers['content-type'] = 'application/json'
    if (this.accessToken) headers.authorization = `Bearer ${this.accessToken}`

    const response = await this.fetchImpl(path, {
      method,
      headers,
      // The refresh cookie is httpOnly and same-origin; without this it is never sent.
      credentials: 'same-origin',
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      ...(signal ? { signal } : {}),
    })

    if (response.status === 401 && allowRefresh && !NO_REFRESH.includes(path)) {
      const token = await this.refresh()
      if (token === null) {
        this.onSessionEnded()
        throw await toError(response)
      }
      return this.request<T>(method, path, { ...options, allowRefresh: false })
    }

    if (!response.ok) throw await toError(response)
    if (response.status === 204) return undefined as T
    return (await response.json()) as T
  }

  get<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>('GET', path, options)
  }

  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>('POST', path, { ...options, ...(body === undefined ? {} : { body }) })
  }

  /**
   * Exchange the refresh cookie for a new access token.
   *
   * Every concurrent caller gets the same promise, so the server sees one rotation.
   */
  refresh(): Promise<string | null> {
    this.inFlightRefresh ??= this.performRefresh().finally(() => {
      this.inFlightRefresh = null
    })
    return this.inFlightRefresh
  }

  private async performRefresh(): Promise<string | null> {
    try {
      const session = await this.request<SessionResponse>('POST', '/api/v1/auth/refresh', {
        allowRefresh: false,
      })
      this.refreshCount += 1
      this.setAccessToken(session.access_token)
      return session.access_token
    } catch {
      this.refreshCount += 1
      this.setAccessToken(null)
      return null
    }
  }
}

async function toError(response: Response): Promise<ApiError> {
  let body: ErrorBody = {}
  try {
    body = (await response.json()) as ErrorBody
  } catch {
    // A proxy error page, or an empty body. The status is still worth reporting.
  }
  const error = body.error
  return new ApiError(
    response.status,
    error?.code ?? `http_${response.status}`,
    error?.message ?? response.statusText ?? 'Request failed',
    error?.details,
    error?.request_id,
  )
}

export const api = new ApiClient()
