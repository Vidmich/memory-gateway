/**
 * Who is signed in, and the three operations that change it.
 *
 * On mount the provider attempts one silent refresh. That is what makes a reload keep
 * you signed in: the access token died with the page, but the httpOnly cookie did not.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { ApiError, type ApiClient, type CurrentUser, type SessionResponse, api } from '@/api/client'
import { authReducer, initialAuthState, type AuthState } from '@/auth/authReducer'

/** The organization a platform admin is currently viewing for support. */
export type AssumedOrganization = { id: string; name: string }

/**
 * Where "open as" is remembered across a reload.
 *
 * `sessionStorage`, not `localStorage`: it is scoped to the tab and gone when the tab
 * closes, which matches what the banner promises. It holds no credential — the server
 * re-checks that the caller is a superadmin on every request, so the worst a tampered
 * value can do is show its owner their own data.
 */
const ASSUMED_KEY = 'mg.assumed-organization'

export type AuthContextValue = AuthState & {
  signIn: (credentials: { email: string; password: string; remember: boolean }) => Promise<void>
  signOut: () => Promise<void>
  refreshUser: () => Promise<void>
  /**
   * Adopt a session this app did not create through the login form.
   *
   * Invitation acceptance returns one: the person proved they hold a token only the
   * invited address could have received, so making them type the password again adds
   * nothing. `refreshUser` is not enough — `user-updated` is deliberately ignored while
   * anonymous, so that a late `/auth/me` cannot resurrect a signed-out session.
   */
  adoptSession: (session: SessionResponse) => void
  /** The shared client, so query hooks use the same one the provider was given. */
  client: ApiClient
  assumedOrganization: AssumedOrganization | null
  openAs: (organization: AssumedOrganization) => void
  closeAs: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({
  children,
  client = api,
}: {
  children: ReactNode
  /** Injected in tests; the app uses the shared client. */
  client?: ApiClient
}) {
  const [state, dispatch] = useReducer(authReducer, initialAuthState)
  // Kept in a ref so the effect below does not re-run when the reducer changes identity.
  const clientRef = useRef(client)
  clientRef.current = client

  const [assumedOrganization, setAssumedOrganization] = useState<AssumedOrganization | null>(
    () => readAssumed(),
  )

  // Push it into the client before the first request goes out, so a reload lands back on
  // the same organization rather than briefly showing the platform's view of the page.
  useEffect(() => {
    clientRef.current.setAssumedOrganization(assumedOrganization?.id ?? null)
    writeAssumed(assumedOrganization)
  }, [assumedOrganization])

  useEffect(() => {
    const current = clientRef.current
    current.setSessionEndedHandler(() => {
      dispatch({ type: 'session-ended' })
    })
  }, [])

  useEffect(() => {
    let cancelled = false

    void (async () => {
      const token = await clientRef.current.refresh()
      if (cancelled) return
      if (!token) {
        dispatch({ type: 'restore-finished', user: null })
        return
      }
      try {
        const user = await clientRef.current.get<CurrentUser>('/api/v1/auth/me')
        if (!cancelled) dispatch({ type: 'restore-finished', user })
      } catch {
        if (!cancelled) dispatch({ type: 'restore-finished', user: null })
      }
    })()

    return () => {
      cancelled = true
    }
  }, [])

  const signIn = useCallback<AuthContextValue['signIn']>(async (credentials) => {
    dispatch({ type: 'sign-in-started' })
    try {
      const session = await clientRef.current.post<SessionResponse>(
        '/api/v1/auth/login',
        credentials,
      )
      clientRef.current.setAccessToken(session.access_token)
      dispatch({ type: 'sign-in-succeeded', user: session.user })
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : 'Could not reach the server. Try again.'
      dispatch({ type: 'sign-in-failed', error: message })
      throw error
    }
  }, [])

  const adoptSession = useCallback<AuthContextValue['adoptSession']>((session) => {
    clientRef.current.setAccessToken(session.access_token)
    dispatch({ type: 'sign-in-succeeded', user: session.user })
  }, [])

  const openAs = useCallback<AuthContextValue['openAs']>((organization) => {
    setAssumedOrganization(organization)
  }, [])

  const closeAs = useCallback<AuthContextValue['closeAs']>(() => {
    setAssumedOrganization(null)
  }, [])

  const signOut = useCallback<AuthContextValue['signOut']>(async () => {
    setAssumedOrganization(null)
    try {
      await clientRef.current.post<void>('/api/v1/auth/logout')
    } catch {
      // The server-side session is what matters, and it is either gone or unreachable.
      // Either way the local state should not stay signed in.
    }
    clientRef.current.setAccessToken(null)
    dispatch({ type: 'signed-out' })
  }, [])

  const refreshUser = useCallback<AuthContextValue['refreshUser']>(async () => {
    const user = await clientRef.current.get<CurrentUser>('/api/v1/auth/me')
    dispatch({ type: 'user-updated', user })
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      signIn,
      signOut,
      refreshUser,
      adoptSession,
      client,
      assumedOrganization,
      openAs,
      closeAs,
    }),
    [
      state,
      signIn,
      signOut,
      refreshUser,
      adoptSession,
      client,
      assumedOrganization,
      openAs,
      closeAs,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside <AuthProvider>')
  return value
}

export function useApiClient(): ApiClient {
  return useAuth().client
}

function readAssumed(): AssumedOrganization | null {
  try {
    const raw = sessionStorage.getItem(ASSUMED_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<AssumedOrganization>
    return parsed.id && parsed.name ? { id: parsed.id, name: parsed.name } : null
  } catch {
    // Private mode, cleared storage, or a value someone edited by hand. Showing the
    // platform view is the correct fallback; it is never less restrictive.
    return null
  }
}

function writeAssumed(organization: AssumedOrganization | null): void {
  try {
    if (organization) sessionStorage.setItem(ASSUMED_KEY, JSON.stringify(organization))
    else sessionStorage.removeItem(ASSUMED_KEY)
  } catch {
    // Storage can be unavailable; the banner still works for this page view.
  }
}
