/**
 * Gate for every signed-in route.
 *
 * Nothing renders while the silent refresh is still in flight — otherwise a reload
 * flashes the login page at someone who is, in fact, signed in.
 */

import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'

import { useAuth } from '@/auth/AuthContext'
import { loginPathFor } from '@/auth/redirect'
import { FullPageSpinner } from '@/components/FullPageSpinner'

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { status } = useAuth()
  const location = useLocation()

  if (status === 'restoring') return <FullPageSpinner label="Restoring your session…" />
  if (status !== 'authenticated') {
    return <Navigate to={loginPathFor(location)} replace />
  }
  return <>{children}</>
}
