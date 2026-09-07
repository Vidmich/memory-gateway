import { describe, expect, it } from 'vitest'

import { authReducer, initialAuthState, isSettled, type AuthState } from '@/auth/authReducer'
import type { CurrentUser } from '@/api/client'

const ADA: CurrentUser = {
  id: 'u1',
  email: 'ada@example.com',
  name: 'Ada',
  role: 'org_admin',
  status: 'active',
  last_login_at: null,
  organization: { id: 'o1', name: 'Acme', slug: 'acme' },
}

const authenticated: AuthState = { status: 'authenticated', user: ADA, error: null }

describe('authReducer', () => {
  it('starts out not knowing whether anyone is signed in', () => {
    // The distinction "not asked yet" vs "asked, nobody" is what stops the login page
    // flashing on every reload.
    expect(initialAuthState.status).toBe('restoring')
    expect(isSettled(initialAuthState)).toBe(false)
  })

  it('settles to authenticated when the restore finds a session', () => {
    const state = authReducer(initialAuthState, { type: 'restore-finished', user: ADA })

    expect(state).toEqual({ status: 'authenticated', user: ADA, error: null })
    expect(isSettled(state)).toBe(true)
  })

  it('settles to anonymous when the restore finds none', () => {
    const state = authReducer(initialAuthState, { type: 'restore-finished', user: null })

    expect(state.status).toBe('anonymous')
    expect(state.error).toBeNull()
  })

  it('does not report an error for a plain unauthenticated visit', () => {
    // Someone who has simply never signed in should not be told anything went wrong.
    const state = authReducer(initialAuthState, { type: 'restore-finished', user: null })

    expect(state.error).toBeNull()
  })

  it('shows a busy state while signing in', () => {
    const state = authReducer(
      { status: 'anonymous', user: null, error: 'previous failure' },
      { type: 'sign-in-started' },
    )

    expect(state.status).toBe('signing-in')
    expect(state.error).toBeNull()
  })

  it('records the user on success', () => {
    const state = authReducer(
      { status: 'signing-in', user: null, error: null },
      { type: 'sign-in-succeeded', user: ADA },
    )

    expect(state).toEqual(authenticated)
  })

  it('keeps the failure message for the form to show', () => {
    const state = authReducer(
      { status: 'signing-in', user: null, error: null },
      { type: 'sign-in-failed', error: 'Incorrect email or password.' },
    )

    expect(state).toEqual({
      status: 'anonymous',
      user: null,
      error: 'Incorrect email or password.',
    })
  })

  it('drops the user on sign-out', () => {
    const state = authReducer(authenticated, { type: 'signed-out' })

    expect(state.user).toBeNull()
    expect(state.status).toBe('anonymous')
  })

  it('explains an expired session rather than silently signing out', () => {
    const state = authReducer(authenticated, { type: 'session-ended' })

    expect(state.status).toBe('anonymous')
    expect(state.error).toMatch(/expired/i)
  })

  it('applies a user update while signed in', () => {
    const renamed = { ...ADA, name: 'Ada Lovelace' }

    const state = authReducer(authenticated, { type: 'user-updated', user: renamed })

    expect(state.user).toEqual(renamed)
  })

  it('ignores a user update that lands after signing out', () => {
    // An in-flight `/auth/me` must not resurrect a session the user just ended.
    const signedOut = authReducer(authenticated, { type: 'signed-out' })

    const state = authReducer(signedOut, { type: 'user-updated', user: ADA })

    expect(state).toEqual(signedOut)
  })

  it('is pure', () => {
    const before = { ...authenticated }

    authReducer(authenticated, { type: 'signed-out' })

    expect(authenticated).toEqual(before)
  })
})
