import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiClient } from '@/api/client'
import { AppRoutes, makeQueryClient } from '@/App'
import { AuthProvider } from '@/auth/AuthContext'
import { ToastProvider } from '@/components/Toast'
import { makeUser } from '@/test/factories'
import { jsonResponse as json, bodyOf, pathOf } from '@/test/http'

const TOKEN = 'an-invitation-token'
const PREVIEW = {
  email: 'invitee@example.com',
  role: 'org_member',
  organization_name: 'Acme',
  expires_at: new Date(Date.now() + 7 * 86_400_000).toISOString(),
}

function fakeServer({ valid = true, minLength = 12 } = {}) {
  const user = makeUser({ email: PREVIEW.email, name: 'New Person' })
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

    if (path === `/api/v1/invitations/accept/${TOKEN}` && method === 'GET') {
      return Promise.resolve(
        valid
          ? json(PREVIEW)
          : json({ error: { code: 'not_found', message: 'no longer valid' } }, 404),
      )
    }
    if (path === `/api/v1/invitations/accept/${TOKEN}` && method === 'POST') {
      const body = bodyOf<{ password?: string }>(init)
      if ((body.password ?? '').length < minLength) {
        return Promise.resolve(
          json(
            {
              error: {
                code: 'validation_error',
                message: 'Request validation failed',
                details: {
                  errors: [
                    { loc: ['body', 'password'], msg: 'String should have at least 12 characters' },
                  ],
                },
              },
            },
            422,
          ),
        )
      }
      return Promise.resolve(json(session))
    }
    // The session restore that every page mount performs.
    if (path === '/api/v1/auth/refresh') {
      return Promise.resolve(json({ error: { code: 'session_expired' } }, 401))
    }
    if (path === '/api/v1/auth/me') return Promise.resolve(json(user))

    throw new Error(`unexpected ${method} ${path}`)
  })

  return new ApiClient(impl)
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

describe('accepting an invitation', () => {
  it('is reachable without a session', async () => {
    // The whole point: the person has no account yet, so `ProtectedRoute` must not be in
    // front of this page.
    renderAt(fakeServer(), `/invitations/accept/${TOKEN}`)

    expect(await screen.findByRole('heading', { name: 'Join Acme' })).toBeInTheDocument()
    expect(screen.getByText('invitee@example.com')).toBeInTheDocument()
  })

  it('says so before anyone types a password when the link is dead', async () => {
    renderAt(fakeServer({ valid: false }), `/invitations/accept/${TOKEN}`)

    expect(await screen.findByText('This link is not valid')).toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
  })

  it('shows the server’s field error against the password', async () => {
    renderAt(fakeServer(), `/invitations/accept/${TOKEN}`)
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Your name'), 'New Person')
    await person.type(screen.getByLabelText('Password'), 'short')
    await person.click(screen.getByRole('button', { name: 'Create my account' }))

    expect(await screen.findByText(/at least 12 characters/)).toBeInTheDocument()
  })

  it('signs the new member in and lands them on the dashboard', async () => {
    renderAt(fakeServer(), `/invitations/accept/${TOKEN}`)
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Your name'), 'New Person')
    await person.type(screen.getByLabelText('Password'), 'a-perfectly-fine-password')
    await person.click(screen.getByRole('button', { name: 'Create my account' }))

    // No second sign-in: the token they followed is proof enough, and re-entering the
    // password they just chose proves nothing extra.
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Dashboard' })).toBeInTheDocument(),
    )
  })
})
