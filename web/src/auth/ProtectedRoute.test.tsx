import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiClient } from '@/api/client'
import { AuthProvider } from '@/auth/AuthContext'
import { ProtectedRoute } from '@/auth/ProtectedRoute'
import { loginPathFor, safeNext } from '@/auth/redirect'
import { makeUser } from '@/test/factories'
import { jsonResponse as json, pathOf } from '@/test/http'

const USER = makeUser({ name: 'Ada' })

function clientFor(signedIn: boolean): ApiClient {
  const impl = vi.fn((input: RequestInfo | URL) => {
    const path = pathOf(input)
    if (path === '/api/v1/auth/refresh') {
      return Promise.resolve(
        signedIn
          ? json({
              access_token: 'token',
              token_type: 'bearer',
              expires_at: new Date().toISOString(),
              expires_in: 900,
              user: USER,
            })
          : json({ error: { code: 'session_expired', message: 'no' } }, 401),
      )
    }
    if (path === '/api/v1/auth/me') return Promise.resolve(json(USER))
    throw new Error(`unexpected ${path}`)
  })
  return new ApiClient(impl)
}

function ShowLocation() {
  const location = useLocation()
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>
}

function renderAt(path: string, signedIn: boolean) {
  return render(
    <AuthProvider client={clientFor(signedIn)}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/login" element={<ShowLocation />} />
          <Route
            path="/gateways/:id"
            element={
              <ProtectedRoute>
                <div>gateway detail</div>
              </ProtectedRoute>
            }
          />
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <div>dashboard</div>
              </ProtectedRoute>
            }
          />
        </Routes>
      </MemoryRouter>
    </AuthProvider>,
  )
}

describe('loginPathFor', () => {
  it('remembers where the user was headed', () => {
    expect(loginPathFor({ pathname: '/gateways/abc', search: '?tab=keys' })).toBe(
      '/login?next=%2Fgateways%2Fabc%3Ftab%3Dkeys',
    )
  })

  it('adds nothing for the landing page', () => {
    expect(loginPathFor({ pathname: '/', search: '' })).toBe('/login')
  })
})

describe('safeNext', () => {
  it('accepts a same-site path', () => {
    expect(safeNext('/gateways/abc')).toBe('/gateways/abc')
  })

  it.each(['https://evil.example.com', '//evil.example.com', 'javascript:alert(1)', null])(
    'refuses %s',
    (candidate) => {
      // `next` comes from the query string, so it is attacker-controlled: without this
      // the login page is an open redirect.
      expect(safeNext(candidate)).toBe('/')
    },
  )
})

describe('<ProtectedRoute>', () => {
  it('waits rather than flashing the login page while restoring', () => {
    renderAt('/', true)

    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.queryByTestId('location')).not.toBeInTheDocument()
  })

  it('renders the page once the session is restored', async () => {
    renderAt('/', true)

    expect(await screen.findByText('dashboard')).toBeInTheDocument()
  })

  it('redirects to login with the intended path', async () => {
    renderAt('/gateways/abc', false)

    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent(
        '/login?next=%2Fgateways%2Fabc',
      )
    })
  })

  it('redirects without a next param from the landing page', async () => {
    renderAt('/', false)

    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent('/login')
    })
    expect(screen.getByTestId('location')).not.toHaveTextContent('next=')
  })
})
