import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiClient } from '@/api/client'
import { AppRoutes, makeQueryClient } from '@/App'
import { AuthProvider } from '@/auth/AuthContext'
import { ToastProvider } from '@/components/Toast'
import {
  makeInvitation,
  makeMember,
  makeOrganization,
  makeSuperadmin,
  makeUser,
} from '@/test/factories'
import { jsonResponse as json, bodyOf, pathOf } from '@/test/http'

const ACME = makeOrganization({ id: 'o1', name: 'Acme', slug: 'acme' })
const GLOBEX = makeOrganization({ id: 'o2', name: 'Globex', slug: 'globex', member_count: 1 })

type ServerOptions = {
  /** Who `/auth/me` reports. */
  user?: ReturnType<typeof makeUser>
  members?: ReturnType<typeof makeMember>[]
  invitations?: ReturnType<typeof makeInvitation>[]
  /** Error to answer a member PATCH with, e.g. the last-admin guard. */
  memberPatchError?: { status: number; code: string; message: string }
}

/**
 * A scripted server, not a mocked client.
 *
 * The point is to exercise the real `ApiClient` — headers included — so a test can assert
 * that "open as" actually puts `X-Assume-Organization` on the wire, rather than that a
 * function was called.
 */
function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  const members = options.members ?? [makeMember({ id: 'm1', email: 'member@example.com' })]
  const invitations = options.invitations ?? []
  const requests: { path: string; method: string; headers: Record<string, string> }[] = []

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
    requests.push({
      path,
      method,
      headers: (init?.headers ?? {}) as Record<string, string>,
    })

    if (path === '/api/v1/auth/refresh') return Promise.resolve(json(session))
    if (path === '/api/v1/auth/me') return Promise.resolve(json(user))
    if (path === '/api/v1/auth/logout') return Promise.resolve(new Response(null, { status: 204 }))

    if (path.startsWith('/api/v1/organizations') && path.endsWith('/members')) {
      return Promise.resolve(json({ items: members, next_cursor: null }))
    }
    if (path.startsWith('/api/v1/invitations') && method === 'GET') {
      return Promise.resolve(json({ items: invitations, next_cursor: null }))
    }
    if (path.startsWith('/api/v1/organizations') && method === 'GET') {
      return Promise.resolve(
        path === '/api/v1/organizations'
          ? json({ items: [ACME, GLOBEX], next_cursor: null })
          : json(ACME),
      )
    }
    if (path.includes('/invitations') && method === 'POST') {
      const body = bodyOf<{ email?: string; role?: string }>(init)
      return Promise.resolve(
        json(
          {
            invitation: makeInvitation({ email: body.email ?? '', role: body.role ?? '' }),
            accept_url: 'https://app.example.com/invitations/accept/secret-token',
          },
          201,
        ),
      )
    }
    if (path.startsWith('/api/v1/members/') && method === 'PATCH') {
      const failure = options.memberPatchError
      if (failure) {
        return Promise.resolve(
          json({ error: { code: failure.code, message: failure.message } }, failure.status),
        )
      }
      return Promise.resolve(json(makeMember(bodyOf(init))))
    }
    if (method === 'PATCH') return Promise.resolve(json(ACME))
    if (method === 'DELETE') return Promise.resolve(new Response(null, { status: 204 }))

    throw new Error(`unexpected ${method} ${path}`)
  })

  return { client: new ApiClient(impl), requests }
}

function renderAt(client: ApiClient, path: string) {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <AuthProvider client={client}>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <AppRoutes />
          </MemoryRouter>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

// ---------------------------------------------------------------------------

describe('members', () => {
  it('lists the organization the session belongs to', async () => {
    const { client } = fakeServer()
    renderAt(client, '/settings/members')

    expect(await screen.findByText('member@example.com')).toBeInTheDocument()
  })

  it('offers role editing to an admin', async () => {
    const { client } = fakeServer()
    renderAt(client, '/settings/members')

    expect(await screen.findByLabelText('Role for member@example.com')).toBeInTheDocument()
  })

  it('does not offer it to a viewer', async () => {
    // The API refuses the call either way; this is about not showing a door that will
    // not open. `tests/test_directory_api.py` covers the refusal.
    const { client } = fakeServer({
      user: makeUser({ role: 'org_viewer', capabilities: ['org:read'] }),
    })
    renderAt(client, '/settings/members')

    expect(await screen.findByText('member@example.com')).toBeInTheDocument()
    expect(screen.queryByLabelText('Role for member@example.com')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
  })

  it('surfaces the last-admin guard where the user can act on it', async () => {
    const { client } = fakeServer({
      memberPatchError: {
        status: 409,
        code: 'conflict',
        message: "This is the organization's only active administrator. Appoint another one first.",
      },
    })
    renderAt(client, '/settings/members')
    const person = userEvent.setup()

    await person.selectOptions(
      await screen.findByLabelText('Role for member@example.com'),
      'org_viewer',
    )

    expect(await screen.findByText(/only active administrator/)).toBeInTheDocument()
  })

  it('shows an invitation link once, and says so', async () => {
    const { client } = fakeServer()
    renderAt(client, '/settings/members')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Email'), 'new@example.com')
    await person.click(screen.getByRole('button', { name: 'Invite' }))

    expect(await screen.findByText(/not shown again/)).toBeInTheDocument()
    expect(
      screen.getByText('https://app.example.com/invitations/accept/secret-token'),
    ).toBeInTheDocument()
  })

  it('never renders a link for an invitation it merely listed', async () => {
    // Only the hash is stored, so the list endpoint has nothing to show — and the UI must
    // not imply otherwise.
    const { client } = fakeServer({ invitations: [makeInvitation()] })
    renderAt(client, '/settings/members')

    expect(await screen.findByText('invitee@example.com')).toBeInTheDocument()
    expect(screen.queryByText(/invitations\/accept\//)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New link' })).toBeInTheDocument()
  })

  it('requires the typed email before removing someone', async () => {
    const { client } = fakeServer()
    renderAt(client, '/settings/members')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Remove' }))
    const dialog = screen.getByRole('dialog')
    const confirm = within(dialog).getByRole('button', { name: 'Remove' })

    expect(confirm).toBeDisabled()
    await person.type(within(dialog).getByRole('textbox'), 'member@example.com')
    expect(confirm).toBeEnabled()
  })
})

describe('platform organizations', () => {
  it('lists every organization with its counts', async () => {
    const { client } = fakeServer({ user: makeSuperadmin() })
    renderAt(client, '/platform/organizations')

    expect(await screen.findByText('Acme')).toBeInTheDocument()
    expect(screen.getByText('Globex')).toBeInTheDocument()
  })

  it('opening an organization shows a banner that names it', async () => {
    const { client } = fakeServer({ user: makeSuperadmin() })
    renderAt(client, '/platform/organizations')
    const person = userEvent.setup()

    await person.click((await screen.findAllByRole('button', { name: 'Open as' }))[0]!)

    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Acme')
    expect(banner).toHaveTextContent(/recorded/)
  })

  it('and sends the header that narrows the scope server-side', async () => {
    const { client, requests } = fakeServer({ user: makeSuperadmin() })
    renderAt(client, '/platform/organizations')
    const person = userEvent.setup()

    await person.click((await screen.findAllByRole('button', { name: 'Open as' }))[0]!)

    await waitFor(() =>
      expect(
        requests.some((request) => request.headers['x-assume-organization'] === 'o1'),
      ).toBe(true),
    )
  })

  it('leaving the organization removes the banner', async () => {
    const { client } = fakeServer({ user: makeSuperadmin() })
    renderAt(client, '/platform/organizations')
    const person = userEvent.setup()

    await person.click((await screen.findAllByRole('button', { name: 'Open as' }))[0]!)
    await person.click(await screen.findByRole('button', { name: 'Leave organization' }))

    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
  })

  it('requires the typed slug before suspending', async () => {
    const { client } = fakeServer({ user: makeSuperadmin() })
    renderAt(client, '/platform/organizations')
    const person = userEvent.setup()

    await person.click((await screen.findAllByRole('button', { name: 'Suspend' }))[0]!)
    const dialog = screen.getByRole('dialog')

    expect(within(dialog).getByRole('button', { name: 'Suspend' })).toBeDisabled()
    await person.type(within(dialog).getByRole('textbox'), 'acme')
    expect(within(dialog).getByRole('button', { name: 'Suspend' })).toBeEnabled()
  })

  it('is not linked in the sidebar for an org admin', async () => {
    const { client } = fakeServer()
    renderAt(client, '/settings/members')

    await screen.findByText('member@example.com')
    expect(
      within(screen.getByRole('navigation', { name: 'Main' })).queryByText('Organizations'),
    ).not.toBeInTheDocument()
  })
})

describe('organization settings', () => {
  it('lets an admin edit the profile', async () => {
    const { client } = fakeServer()
    renderAt(client, '/settings')

    expect(await screen.findByLabelText('Name')).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeInTheDocument()
  })

  it('shows a viewer the same fields, disabled', async () => {
    const { client } = fakeServer({
      user: makeUser({ role: 'org_viewer', capabilities: ['org:read'] }),
    })
    renderAt(client, '/settings')

    // Disabled rather than hidden: a viewer still needs to read the slug to build a
    // gateway URL.
    expect(await screen.findByLabelText('Name')).toBeDisabled()
    expect(screen.getByLabelText('Slug')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()
  })
})
