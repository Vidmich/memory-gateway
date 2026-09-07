import { describe, expect, it, vi } from 'vitest'

import { ApiClient, ApiError } from '@/api/client'
import { jsonResponse as json, pathOf } from '@/test/http'

/** A fetch stand-in that answers from a script and records what it was asked. */
function stubFetch(handlers: Record<string, () => Response | Promise<Response>>) {
  const calls: string[] = []
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = pathOf(input)
    calls.push(`${init?.method ?? 'GET'} ${path}`)
    const handler = handlers[path]
    if (!handler) throw new Error(`no stub for ${path}`)
    return await handler()
  })
  return { impl: impl as unknown as typeof fetch, calls }
}

/** A fetch that always succeeds, kept typed so `mock.calls` carries the init object. */
function recordingFetch() {
  return vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
    Promise.resolve(json({ ok: true })),
  )
}

const unauthorized = () =>
  json({ error: { code: 'session_expired', message: 'expired', request_id: 'abc' } }, 401)

const session = (token: string) =>
  json({
    access_token: token,
    token_type: 'bearer',
    expires_at: new Date(Date.now() + 900_000).toISOString(),
    expires_in: 900,
    user: {
      id: 'u1',
      email: 'ada@example.com',
      name: 'Ada',
      role: 'org_admin',
      status: 'active',
      last_login_at: null,
      organization: { id: 'o1', name: 'Acme', slug: 'acme' },
    },
  })

describe('ApiClient', () => {
  it('sends no Authorization header before signing in', async () => {
    const impl = recordingFetch()
    const client = new ApiClient(impl)

    await client.get('/api/v1/things')

    const init = impl.mock.calls[0]?.[1]
    expect((init?.headers as Record<string, string>).authorization).toBeUndefined()
  })

  it('attaches the access token once it has one', async () => {
    const impl = recordingFetch()
    const client = new ApiClient(impl)
    client.setAccessToken('token-1')

    await client.get('/api/v1/things')

    const init = impl.mock.calls[0]?.[1]
    expect((init?.headers as Record<string, string>).authorization).toBe('Bearer token-1')
  })

  it('sends cookies, or the refresh cookie would never arrive', async () => {
    const impl = recordingFetch()
    const client = new ApiClient(impl)

    await client.get('/api/v1/things')

    const init = impl.mock.calls[0]?.[1]
    expect(init?.credentials).toBe('same-origin')
  })

  it('surfaces the error envelope as an ApiError', async () => {
    const { impl } = stubFetch({
      '/api/v1/things': () =>
        json({ error: { code: 'not_found', message: 'No such thing', request_id: 'r1' } }, 404),
    })
    const client = new ApiClient(impl)

    await expect(client.get('/api/v1/things')).rejects.toMatchObject({
      status: 404,
      code: 'not_found',
      message: 'No such thing',
      requestId: 'r1',
    })
  })

  it('still reports a failure when the body is not our envelope', async () => {
    const { impl } = stubFetch({
      '/api/v1/things': () => new Response('<html>502</html>', { status: 502 }),
    })

    await expect(new ApiClient(impl).get('/api/v1/things')).rejects.toBeInstanceOf(ApiError)
  })

  it('returns nothing for a 204', async () => {
    const { impl } = stubFetch({
      '/api/v1/auth/logout': () => new Response(null, { status: 204 }),
    })

    await expect(new ApiClient(impl).post('/api/v1/auth/logout')).resolves.toBeUndefined()
  })

  describe('expiry', () => {
    it('refreshes and retries the original request', async () => {
      let expired = true
      const { impl, calls } = stubFetch({
        '/api/v1/things': () => (expired ? unauthorized() : json({ ok: true })),
        '/api/v1/auth/refresh': () => {
          expired = false
          return session('token-2')
        },
      })
      const client = new ApiClient(impl)
      client.setAccessToken('stale')

      await expect(client.get('/api/v1/things')).resolves.toEqual({ ok: true })

      expect(calls).toEqual([
        'GET /api/v1/things',
        'POST /api/v1/auth/refresh',
        'GET /api/v1/things',
      ])
    })

    it('triggers exactly one refresh for concurrent requests', async () => {
      // The acceptance criterion. Two 401s at the same moment must not both rotate the
      // refresh token — the second rotation would look like reuse and end the session.
      let expired = true
      let refreshes = 0
      let releaseRefresh = () => {}
      const refreshStarted = new Promise<void>((resolve) => {
        releaseRefresh = resolve
      })

      const { impl } = stubFetch({
        '/api/v1/things': () => (expired ? unauthorized() : json({ what: 'things' })),
        '/api/v1/other': () => (expired ? unauthorized() : json({ what: 'other' })),
        '/api/v1/auth/refresh': async () => {
          refreshes += 1
          releaseRefresh()
          // Hold the refresh open long enough that both callers are certainly waiting.
          await new Promise((resolve) => setTimeout(resolve, 10))
          expired = false
          return session('token-2')
        },
      })
      const client = new ApiClient(impl)
      client.setAccessToken('stale')

      const both = Promise.all([client.get('/api/v1/things'), client.get('/api/v1/other')])
      await refreshStarted
      const [first, second] = await both

      expect(refreshes).toBe(1)
      expect(client.refreshCount).toBe(1)
      expect(first).toEqual({ what: 'things' })
      expect(second).toEqual({ what: 'other' })
    })

    it('reports the session as ended when the refresh fails', async () => {
      const onSessionEnded = vi.fn()
      const { impl } = stubFetch({
        '/api/v1/things': unauthorized,
        '/api/v1/auth/refresh': unauthorized,
      })
      const client = new ApiClient(impl, onSessionEnded)
      client.setAccessToken('stale')

      await expect(client.get('/api/v1/things')).rejects.toBeInstanceOf(ApiError)

      expect(onSessionEnded).toHaveBeenCalledTimes(1)
      expect(client.hasAccessToken()).toBe(false)
    })

    it('does not retry forever when the retry is also unauthorized', async () => {
      let refreshes = 0
      const { impl, calls } = stubFetch({
        '/api/v1/things': unauthorized,
        '/api/v1/auth/refresh': () => {
          refreshes += 1
          return session(`token-${refreshes}`)
        },
      })
      const client = new ApiClient(impl)
      client.setAccessToken('stale')

      await expect(client.get('/api/v1/things')).rejects.toBeInstanceOf(ApiError)

      expect(refreshes).toBe(1)
      expect(calls.filter((call) => call.endsWith('/api/v1/things'))).toHaveLength(2)
    })

    it('never refreshes in response to a failed login', async () => {
      // Otherwise a wrong password fires a pointless refresh, and on a live session it
      // would rotate the token behind the user's back.
      const { impl, calls } = stubFetch({
        '/api/v1/auth/login': () =>
          json({ error: { code: 'invalid_credentials', message: 'nope' } }, 401),
      })
      const client = new ApiClient(impl)

      await expect(
        client.post('/api/v1/auth/login', { email: 'a@b.c', password: 'x' }),
      ).rejects.toBeInstanceOf(ApiError)

      expect(calls).toEqual(['POST /api/v1/auth/login'])
    })

    it('allows a later refresh after an earlier one finished', async () => {
      let refreshes = 0
      const { impl } = stubFetch({
        '/api/v1/auth/refresh': () => {
          refreshes += 1
          return session(`token-${refreshes}`)
        },
      })
      const client = new ApiClient(impl)

      await client.refresh()
      await client.refresh()

      expect(refreshes).toBe(2)
    })
  })

  describe('field errors', () => {
    it('extracts one message per field from a validation response', () => {
      const error = new ApiError(422, 'validation_error', 'Request validation failed', {
        errors: [
          { loc: ['body', 'email'], msg: 'not a valid email address' },
          { loc: ['body', 'password'], msg: 'too short' },
        ],
      })

      expect(error.fieldErrors).toEqual({
        email: 'not a valid email address',
        password: 'too short',
      })
    })

    it('is empty when the error carries no details', () => {
      expect(new ApiError(500, 'internal_error', 'boom').fieldErrors).toEqual({})
    })
  })
})
