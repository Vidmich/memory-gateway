/**
 * Authentication state, as a pure reducer.
 *
 * Split out from the provider so the transitions can be tested without React, a router,
 * or a network. The states are deliberately explicit: "we have not asked yet" and "we
 * asked and there is no session" look identical to a boolean, and conflating them makes
 * the app flash the login page on every reload before the silent refresh lands.
 */

import type { CurrentUser } from '@/api/client'

export type AuthStatus = 'restoring' | 'anonymous' | 'signing-in' | 'authenticated'

export type AuthState = {
  status: AuthStatus
  user: CurrentUser | null
  error: string | null
}

export type AuthAction =
  | { type: 'restore-finished'; user: CurrentUser | null }
  | { type: 'sign-in-started' }
  | { type: 'sign-in-succeeded'; user: CurrentUser }
  | { type: 'sign-in-failed'; error: string }
  | { type: 'user-updated'; user: CurrentUser }
  | { type: 'signed-out' }
  | { type: 'session-ended' }

export const initialAuthState: AuthState = {
  status: 'restoring',
  user: null,
  error: null,
}

export function authReducer(state: AuthState, action: AuthAction): AuthState {
  switch (action.type) {
    case 'restore-finished':
      return action.user
        ? { status: 'authenticated', user: action.user, error: null }
        : { status: 'anonymous', user: null, error: null }

    case 'sign-in-started':
      return { ...state, status: 'signing-in', error: null }

    case 'sign-in-succeeded':
      return { status: 'authenticated', user: action.user, error: null }

    case 'sign-in-failed':
      return { status: 'anonymous', user: null, error: action.error }

    case 'user-updated':
      // Ignored unless signed in: a stale `/auth/me` landing after a sign-out must not
      // resurrect the session.
      return state.status === 'authenticated' ? { ...state, user: action.user } : state

    case 'signed-out':
      return { status: 'anonymous', user: null, error: null }

    case 'session-ended':
      return {
        status: 'anonymous',
        user: null,
        error: 'Your session has expired. Please sign in again.',
      }

    default:
      return state
  }
}

/** True while the app does not yet know whether anyone is signed in. */
export function isSettled(state: AuthState): boolean {
  return state.status !== 'restoring'
}
