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
  type ReactNode,
} from 'react'

import { ApiError, type ApiClient, type CurrentUser, type SessionResponse, api } from '@/api/client'
import { authReducer, initialAuthState, type AuthState } from '@/auth/authReducer'

export type AuthContextValue = AuthState & {
  signIn: (credentials: { email: string; password: string; remember: boolean }) => Promise<void>
  signOut: () => Promise<void>
  refreshUser: () => Promise<void>
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

  const signOut = useCallback<AuthContextValue['signOut']>(async () => {
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
    () => ({ ...state, signIn, signOut, refreshUser }),
    [state, signIn, signOut, refreshUser],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside <AuthProvider>')
  return value
}
