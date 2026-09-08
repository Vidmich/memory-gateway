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
  makeAuditChange,
  makeAuditEvent,
  makeGateway,
  makeGatewayLimits,
  makeModel,
  makeUser,
} from '@/test/factories'
import { jsonResponse as json, pathOf } from '@/test/http'

type ServerOptions = {
  user?: ReturnType<typeof makeUser>
  events?: ReturnType<typeof makeAuditEvent>[]
  nextCursor?: string | null
}

/**
 * A scripted server, not a mocked client — the real `ApiClient` runs, so a test asserts
 * what actually went on the wire. That matters most for the filters: the screen's whole
 * claim is that it narrows *server-side*, and the only way to check is to read the URL.
 */
function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  const gateway = makeGateway()
  const requests: string[] = []

  const session = {
    access_token: 'token-1',
    token_type: 'bearer',
    expires_at: new Date(Date.now() + 900_000).toISOString(),
    expires_in: 900,
    user,
  }

  const impl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = pathOf(input)
    requests.push(`${init?.method ?? 'GET'} ${path}`)

    if (path === '/api/v1/auth/refresh') return Promise.resolve(json(session))
    if (path === '/api/v1/auth/me') return Promise.resolve(json(user))
    if (path.startsWith('/api/v1/audit-events/export')) {
      return Promise.resolve(
        new Response('id,action\r\n', { status: 200, headers: { 'content-type': 'text/csv' } }),
      )
    }
    if (path.startsWith('/api/v1/audit-events')) {
      return Promise.resolve(
        json({
          items: options.events ?? [makeAuditEvent()],
          next_cursor: options.nextCursor ?? null,
        }),
      )
    }
    if (path.endsWith('/keys')) return Promise.resolve(json([]))
    if (path.endsWith('/limits')) return Promise.resolve(json(makeGatewayLimits()))
    if (path === `/api/v1/gateways/${gateway.id}`) return Promise.resolve(json(gateway))
    if (path.startsWith('/api/v1/models/')) return Promise.resolve(json(makeModel()))
    if (path.startsWith('/api/v1/models')) {
      return Promise.resolve(json({ items: [makeModel()], next_cursor: null }))
    }
    return Promise.resolve(json({ items: [], next_cursor: null }))
  })

  return { impl, requests, gateway }
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
// the screen
// ---------------------------------------------------------------------------

describe('the Audit log screen', () => {
  it('reads each row as a sentence rather than as a schema', async () => {
    renderAt('/audit', fakeServer())

    expect(
      await screen.findByText('ada@example.com updated gateway acme-support'),
    ).toBeInTheDocument()
  })

  it('keeps the diff behind a click, and shows before and after when opened', async () => {
    const user = userEvent.setup()
    renderAt('/audit', fakeServer())

    const row = await screen.findByRole('button', { name: /updated gateway/ })
    expect(screen.queryByText('Be brief and kind.')).not.toBeInTheDocument()

    await user.click(row)

    expect(await screen.findByText('Be brief.')).toBeInTheDocument()
    expect(screen.getByText('Be brief and kind.')).toBeInTheDocument()
  })

  it('shows a rotated credential as having changed and not as what it became', async () => {
    // The demo, on screen: `credential: "***" → "***"`.
    const user = userEvent.setup()
    renderAt(
      '/audit',
      fakeServer({
        events: [
          makeAuditEvent({
            action: 'model.update',
            target_type: 'upstream_model',
            target: 'acme-gpt',
            changes: [makeAuditChange({ path: 'credential', before: '***', after: '***' })],
          }),
        ],
      }),
    )

    await user.click(await screen.findByRole('button', { name: /updated model/ }))

    // Both cells read `***`, and the note beside them says why.
    expect(screen.getAllByText('***')).toHaveLength(2)
    expect(screen.getByText('(value never recorded)')).toBeInTheDocument()
  })

  it('marks support access so a customer can see it without reading every row', async () => {
    renderAt(
      '/audit',
      fakeServer({
        events: [
          makeAuditEvent({
            action: 'organization.assume',
            actor_type: 'superadmin_impersonation',
            actor: 'root@vendor.example',
            target_type: 'organization',
            target: null,
            changes: [],
          }),
        ],
      }),
    )

    expect(await screen.findByText('Support access')).toBeInTheDocument()
  })

  it('marks a background job as automated rather than as a person', async () => {
    const user = userEvent.setup()
    renderAt(
      '/audit',
      fakeServer({
        events: [
          makeAuditEvent({
            action: 'connector.purge',
            actor_type: 'system',
            actor: 'delete-connector',
            actor_user_id: null,
            target_type: 'connector',
            target: 'Handbook',
          }),
        ],
      }),
    )

    expect(await screen.findByText('Automated')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /purged connector/ }))
    expect(screen.getByText('delete-connector (automated)')).toBeInTheDocument()
  })

  it('narrows on the server, not in the browser', async () => {
    // A page filtered after it arrives can come back empty while the next one is full.
    const user = userEvent.setup()
    const server = fakeServer()
    renderAt('/audit', server)

    await screen.findByText(/updated gateway/)
    await user.selectOptions(screen.getByLabelText('Kind'), 'upstream_model')

    await vi.waitFor(() => {
      expect(server.requests.some((request) => request.includes('target_type=upstream_model'))).toBe(
        true,
      )
    })
  })

  it('says which kind of empty it is', async () => {
    renderAt('/audit', fakeServer({ events: [] }))

    expect(await screen.findByText(/Every change made from here on/)).toBeInTheDocument()
  })

  it('exports through the client rather than through a link', async () => {
    // A plain `<a href>` carries no Authorization header, and the token lives in memory.
    const user = userEvent.setup()
    const server = fakeServer()
    URL.createObjectURL = vi.fn(() => 'blob:audit')
    URL.revokeObjectURL = vi.fn()
    renderAt('/audit', server)

    await screen.findByText(/updated gateway/)
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))

    await vi.waitFor(() => {
      expect(server.requests.some((request) => request.includes('/audit-events/export'))).toBe(true)
    })
  })
})

// ---------------------------------------------------------------------------
// the per-object panel
// ---------------------------------------------------------------------------

describe('an object history panel', () => {
  it('asks for nothing until it is opened', async () => {
    const user = userEvent.setup()
    const server = fakeServer()
    renderAt('/gateways/g1', server)

    const toggle = await screen.findByRole('button', { name: /History/ })
    expect(server.requests.some((request) => request.includes('audit-events'))).toBe(false)

    await user.click(toggle)

    await vi.waitFor(() => {
      expect(
        server.requests.some((request) => request.includes('target_type=gateway&target_id=g1')),
      ).toBe(true)
    })
  })

  it('shows this object’s changes and nothing else', async () => {
    const user = userEvent.setup()
    renderAt('/gateways/g1', fakeServer())

    await user.click(await screen.findByRole('button', { name: /History/ }))

    const panel = await screen.findByText('ada@example.com updated gateway acme-support')
    expect(panel).toBeInTheDocument()
  })

  it('says so plainly when a gateway has never changed', async () => {
    const user = userEvent.setup()
    renderAt('/gateways/g1', fakeServer({ events: [] }))

    await user.click(await screen.findByRole('button', { name: /History/ }))

    expect(
      await screen.findByText(/Nothing has changed on this gateway/),
    ).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// navigation
// ---------------------------------------------------------------------------

describe('the sidebar', () => {
  it('offers the audit log to every role, like monitoring', async () => {
    // The question "who turned this off yesterday" is asked by whoever is answering the
    // ticket; needing write permission to look would be a worse posture, not a better one.
    renderAt('/audit', fakeServer({ user: makeUser({ role: 'org_viewer' }) }))

    const nav = await screen.findByRole('navigation', { name: 'Main' })
    expect(within(nav).getByRole('link', { name: 'Audit log' })).toBeInTheDocument()
  })
})
