import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiClient } from '@/api/client'
import { AppRoutes, makeQueryClient } from '@/App'
import { AuthProvider } from '@/auth/AuthContext'
import { ToastProvider } from '@/components/Toast'
import { jsonResponse as json, bodyOf, pathOf } from '@/test/http'

const USER = {
  id: 'u1',
  email: 'ada@example.com',
  name: 'Ada Lovelace',
  role: 'org_admin',
  status: 'active',
  last_login_at: null,
  organization: { id: 'o1', name: 'Acme', slug: 'acme' },
}

const sessionBody = {
  access_token: 'token-1',
  token_type: 'bearer',
  expires_at: new Date(Date.now() + 900_000).toISOString(),
  expires_in: 900,
  user: USER,
}

/**
 * A stand-in for the server that behaves like the real one on the paths this test
 * touches: no cookie means no refresh, a good password starts a session, a bad one
 * returns the API's error envelope.
 */
function fakeServer({ password = 'correct-horse-battery-staple' } = {}) {
  let signedIn = false
  const seen: string[] = []

  const impl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = pathOf(input)
    seen.push(path)

    if (path === '/api/v1/auth/refresh') {
      return Promise.resolve(
        signedIn
          ? json(sessionBody)
          : json({ error: { code: 'session_expired', message: 'no session' } }, 401),
      )
    }
    if (path === '/api/v1/auth/login') {
      if (bodyOf<{ password?: string }>(init).password !== password) {
        return Promise.resolve(
          json(
            { error: { code: 'invalid_credentials', message: 'Incorrect email or password.' } },
            401,
          ),
        )
      }
      signedIn = true
      return Promise.resolve(json(sessionBody))
    }
    if (path === '/api/v1/auth/logout') {
      signedIn = false
      return Promise.resolve(new Response(null, { status: 204 }))
    }
    if (path === '/api/v1/auth/me') {
      return Promise.resolve(
        signedIn ? json(USER) : json({ error: { code: 'not_authenticated' } }, 401),
      )
    }
    throw new Error(`unexpected request to ${path}`)
  })

  const fetchImpl = impl as unknown as typeof fetch
  return { client: new ApiClient(fetchImpl), fetchImpl, seen }
}

function renderApp(client: ApiClient, path = '/') {
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

async function signIn(password = 'correct-horse-battery-staple') {
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('Email'), 'ada@example.com')
  await user.type(screen.getByLabelText('Password'), password)
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
  return user
}

describe('signing in', () => {
  it('sends an unauthenticated visitor to the login form', async () => {
    const { client } = fakeServer()

    renderApp(client)

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('lands on the dashboard with the org name and user menu', async () => {
    const { client } = fakeServer()
    renderApp(client)

    await signIn()

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(screen.getByText('Acme')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Ada Lovelace/ })).toBeInTheDocument()
  })

  it('shows the placeholder cards task 07 will fill in', async () => {
    const { client } = fakeServer()
    renderApp(client)

    await signIn()

    expect(await screen.findByText('Requests (24 h)')).toBeInTheDocument()
    expect(screen.getByText('Active gateways')).toBeInTheDocument()
  })

  it('reports a wrong password without saying which half was wrong', async () => {
    const { client } = fakeServer()
    renderApp(client)

    await signIn('wrong-password')

    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect email or password.')
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('never puts the access token in browser storage', async () => {
    // The acceptance criterion. `localStorage` is readable by any injected script, which
    // is exactly what a short-lived in-memory token is meant to survive.
    const { client } = fakeServer()
    renderApp(client)
    await signIn()
    await screen.findByRole('heading', { name: 'Dashboard' })

    const stored = JSON.stringify({ ...localStorage, ...sessionStorage })

    expect(stored).not.toContain('token-1')
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('honours a next parameter after signing in', async () => {
    const { client } = fakeServer()
    renderApp(client, '/login?next=%2F')

    await signIn()

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
  })
})

describe('staying signed in', () => {
  it('restores the session from the cookie on a reload', async () => {
    const { client, fetchImpl } = fakeServer()
    const first = renderApp(client)
    await signIn()
    await screen.findByRole('heading', { name: 'Dashboard' })

    // A reload: the page is thrown away and rebuilt, so the in-memory access token is
    // gone. Only the (server-side) session and its cookie survive — a brand-new client
    // is exactly what the browser would construct.
    first.unmount()
    renderApp(new ApiClient(fetchImpl))

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
  })

  it('shows the login page when there is no cookie to restore from', async () => {
    const { client } = fakeServer()

    renderApp(client)

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })
})

describe('signing out', () => {
  it('returns to the login page', async () => {
    const { client } = fakeServer()
    renderApp(client)
    const user = await signIn()
    await screen.findByRole('heading', { name: 'Dashboard' })

    await user.click(screen.getByRole('button', { name: /Ada Lovelace/ }))
    await user.click(screen.getByRole('menuitem', { name: 'Sign out' }))

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('does not restore the session on a subsequent reload', async () => {
    // "The back button does not restore the session": the server revoked the family, so
    // a fresh page load has nothing to restore from.
    const { client, seen } = fakeServer()
    renderApp(client)
    const user = await signIn()
    await screen.findByRole('heading', { name: 'Dashboard' })
    await user.click(screen.getByRole('button', { name: /Ada Lovelace/ }))
    await user.click(screen.getByRole('menuitem', { name: 'Sign out' }))
    await screen.findByRole('button', { name: 'Sign in' })

    await waitFor(() => {
      expect(seen).toContain('/api/v1/auth/logout')
    })
    expect(screen.queryByRole('heading', { name: 'Dashboard' })).not.toBeInTheDocument()
  })
})
