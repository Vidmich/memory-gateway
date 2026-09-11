import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiClient } from '@/api/client'
import { AppRoutes, makeQueryClient } from '@/App'
import { AuthProvider } from '@/auth/AuthContext'
import { ToastProvider } from '@/components/Toast'
import {
  makeGateway,
  makeGatewayLimits,
  makeLimitUsage,
  makeMemoryHealth,
  makeSummarizationHealth,
  makeModel,
  makeSummary,
  makeThrottledEndUser,
  makeUser,
} from '@/test/factories'
import { bodyOf, jsonResponse as json, pathOf } from '@/test/http'

type ServerOptions = {
  user?: ReturnType<typeof makeUser>
  gateway?: ReturnType<typeof makeGateway>
  limits?: ReturnType<typeof makeGatewayLimits>
  pressure?: ReturnType<typeof makeGatewayLimits> extends never ? never : unknown[]
  throttled?: ReturnType<typeof makeThrottledEndUser>[]
}

/**
 * A scripted server, not a mocked client — the real `ApiClient` runs, so a test asserts
 * what actually went on the wire.
 *
 * That matters most for the limits body: an empty input and an omitted key are the same
 * thing in a form and opposite things on the wire, and the only honest way to check which
 * one was sent is to read the request.
 */
function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  const gateway = options.gateway ?? makeGateway()
  const requests: { path: string; method: string; body: Record<string, unknown> }[] = []

  const session = {
    access_token: 'token-1',
    token_type: 'bearer',
    expires_at: new Date(Date.now() + 900_000).toISOString(),
    expires_in: 900,
    user,
  }

  const impl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = pathOf(input)
    const method = init?.method ?? 'GET'
    requests.push({ path, method, body: bodyOf<Record<string, unknown>>(init) })

    if (path === '/api/v1/auth/refresh') return Promise.resolve(json(session))
    if (path === '/api/v1/auth/me') return Promise.resolve(json(user))
    // The editor renders the Keys section too, and it expects a bare array.
    if (path.endsWith('/keys')) return Promise.resolve(json([]))
    if (path.endsWith('/limits')) {
      return Promise.resolve(json(options.limits ?? makeGatewayLimits()))
    }
    if (path.startsWith('/api/v1/limits/pressure')) {
      return Promise.resolve(json({ items: options.pressure ?? [] }))
    }
    if (path.startsWith('/api/v1/metrics/throttled')) {
      return Promise.resolve(json({ items: options.throttled ?? [] }))
    }
    if (path.startsWith('/api/v1/metrics/summary')) return Promise.resolve(json(makeSummary()))
    // Task 13's panel shares the monitoring screen; without this its query falls through
    // to the catch-all and the page crashes on a shape that is not a health report.
    if (path.startsWith('/api/v1/distillation/health')) {
      return Promise.resolve(json(makeMemoryHealth()))
    }
    // Task 102's panel, for the same reason.
    if (path.startsWith('/api/v1/summarization/health')) {
      return Promise.resolve(json(makeSummarizationHealth()))
    }
    if (path.startsWith('/api/v1/metrics/timeseries')) {
      return Promise.resolve(json({ interval_seconds: 60, buckets: [] }))
    }
    if (path === `/api/v1/gateways/${gateway.id}`) {
      return Promise.resolve(json(gateway))
    }
    // Task 103's section, for the same reason: it lives under the gateway's path.
    if (path.endsWith('/evaluation-sets')) {
      return Promise.resolve(json({ items: [] }))
    }
    if (path.startsWith('/api/v1/gateways')) {
      return Promise.resolve(json({ items: [gateway], next_cursor: null }))
    }
    if (path.startsWith('/api/v1/models')) {
      return Promise.resolve(json({ items: [makeModel()], next_cursor: null }))
    }
    return Promise.resolve(json({ items: [], next_cursor: null }))
  })

  return { impl, requests, gateway }
}

/**
 * One scope's inputs. Both fieldsets carry the same four labels — which is the point of
 * the section, and also why a bare `findByLabelText` here would match two elements.
 */
async function scope(legend: string) {
  return within(await screen.findByRole('group', { name: legend }))
}

function renderAt(route: string, server: ReturnType<typeof fakeServer>) {
  const client = new ApiClient(server.impl)
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <AuthProvider client={client}>
        <ToastProvider>
          <MemoryRouter initialEntries={[route]}>
            <AppRoutes />
          </MemoryRouter>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

// ---------------------------------------------------------------------------
// the editor's Limits section
// ---------------------------------------------------------------------------

describe('the Limits section', () => {
  it('says what an empty field means, rather than leaving it blank', async () => {
    // Every other number on this screen has a default; these have an absence, and a blank
    // box with no explanation reads as "not filled in yet".
    renderAt('/gateways/g1', fakeServer())

    const gateway = await scope('This gateway')
    expect(gateway.getByLabelText('Requests per minute')).toHaveAttribute(
      'placeholder',
      'Unlimited',
    )
  })

  it('sends null for a field left empty, so a limit can be cleared', async () => {
    const user = userEvent.setup()
    const server = fakeServer()
    renderAt('/gateways/g1', server)

    const gateway = await scope('This gateway')
    await user.type(gateway.getByLabelText('Requests per minute'), '60')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    const saved = server.requests.find((request) => request.method === 'PATCH')
    expect(saved?.body.limits).toMatchObject({
      requests_per_minute: 60,
      tokens_per_minute: null,
      per_end_user: { requests_per_minute: null },
    })
  })

  it('refuses to save a limit that is not a whole number', async () => {
    const user = userEvent.setup()
    const server = fakeServer()
    renderAt('/gateways/g1', server)

    const gateway = await scope('This gateway')
    await user.type(gateway.getByLabelText('Requests per minute'), '1.5')

    expect(await screen.findByText(/must be a whole number/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })

  it('shows what has been spent, from the live counters', async () => {
    renderAt(
      '/gateways/g1',
      fakeServer({
        limits: makeGatewayLimits({
          usage: [makeLimitUsage({ value: 10, remaining: 6, reset_seconds: 37 })],
        }),
      }),
    )

    expect(await screen.findByText('4 of 10 used · resets in 37s')).toBeInTheDocument()
  })

  it('explains the platform ceiling next to the field it lowered', async () => {
    // Otherwise the form looks like it discarded the save.
    renderAt(
      '/gateways/g1',
      fakeServer({
        limits: makeGatewayLimits({
          global_models: true,
          capped: ['requests_per_minute'],
          ceilings: {
            requests_per_minute: 60,
            tokens_per_minute: null,
            requests_per_day: null,
            concurrent_requests: null,
          },
        }),
      }),
    )

    expect(await screen.findByText(/the platform enforces 60/)).toBeInTheDocument()
  })

  it('says the per-end-user block is not a share of the gateway budget', async () => {
    // "Per user" next to "per gateway" invites exactly that reading, and it is wrong:
    // both are checked and either one refuses.
    renderAt('/gateways/g1', fakeServer())

    expect(await screen.findByText(/Not a share of the numbers above/)).toBeInTheDocument()
  })

  it('no longer renders a coming-soon placeholder anywhere', async () => {
    renderAt('/gateways/g1', fakeServer())

    await screen.findByRole('heading', { name: 'Limits' })
    expect(screen.queryByText('Coming soon')).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// the dashboard's warning card
// ---------------------------------------------------------------------------

describe('the near-limit card', () => {
  it('is absent when nothing is under pressure', async () => {
    // A card that says "nothing is near a limit" every day for a year is a card nobody
    // reads on the day it changes.
    renderAt('/', fakeServer())

    await screen.findByRole('heading', { name: 'Dashboard' })
    expect(screen.queryByText(/Close to a rate limit/)).not.toBeInTheDocument()
  })

  it('names the gateway and how close it is', async () => {
    renderAt(
      '/',
      fakeServer({
        pressure: [
          {
            gateway_id: 'g1',
            slug: 'acme-support',
            name: 'Support Bot',
            worst: makeLimitUsage({ value: 10, remaining: 1, utilization: 0.9 }),
          },
        ],
      }),
    )

    expect(
      await screen.findByText('Support Bot is at 90% of its requests per minute limit.'),
    ).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// monitoring
// ---------------------------------------------------------------------------

describe('the throttled-callers panel', () => {
  it('names who was refused most', async () => {
    renderAt(
      '/monitoring',
      fakeServer({ throttled: [makeThrottledEndUser({ external_id: 'noisy-bot', rejections: 12 })] }),
    )

    expect(await screen.findByText('noisy-bot')).toBeInTheDocument()
  })

  it('says nothing was throttled rather than drawing an empty chart', async () => {
    renderAt('/monitoring', fakeServer())

    expect(await screen.findByText(/Nothing was rate-limited/)).toBeInTheDocument()
  })
})
