import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { ApiError } from '@/api/client'
import type { InvitationPreviewResponse, SessionResponse } from '@/api/types'
import { useApiClient, useAuth } from '@/auth/AuthContext'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { Field, Form, SubmitButton, TextInput } from '@/components/Form'

/**
 * The page an invitation link opens.
 *
 * Unauthenticated by definition — the person has no account yet — so the token in the URL
 * is the only credential involved, and nothing here reads the auth state except to adopt
 * the session at the end.
 *
 * The preview is fetched before the form renders, so an expired or already-used link says
 * so immediately rather than after someone has chosen a password.
 */
export function AcceptInvitationPage() {
  const { token = '' } = useParams()
  const navigate = useNavigate()
  const { adoptSession } = useAuth()
  const api = useApiClient()

  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const preview = useQuery({
    queryKey: ['invitation', token],
    queryFn: () => api.get<InvitationPreviewResponse>(`/api/v1/invitations/accept/${token}`),
    retry: false,
  })

  if (preview.isLoading) return <FullPageSpinner label="Checking your invitation…" />

  if (preview.isError) {
    return (
      <Centered>
        <h1 className="text-lg font-semibold text-slate-900">This link is not valid</h1>
        <p className="mt-2 text-sm text-slate-600">
          It may have expired, already been used, or been revoked. Ask whoever invited you for
          a new one.
        </p>
        <Link to="/login" className="mt-6 inline-block text-sm font-medium text-slate-900 underline">
          Go to sign in
        </Link>
      </Centered>
    )
  }

  const invitation = preview.data
  // `isLoading` and `isError` are both false here, so the query resolved — but the type
  // does not know that, and crashing on a shape we did not expect is worse than the
  // message the error branch already renders.
  if (!invitation) return <FullPageSpinner label="Checking your invitation…" />

  const submit = async () => {
    setSubmitting(true)
    setError(null)
    try {
      const session = await api.post<SessionResponse>(`/api/v1/invitations/accept/${token}`, {
        name,
        password,
      })
      adoptSession(session)
      void navigate('/', { replace: true })
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new Error('Could not create your account.'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Centered>
      <h1 className="text-lg font-semibold text-slate-900">
        Join {invitation.organization_name}
      </h1>
      <p className="mt-2 text-sm text-slate-600">
        You were invited as <strong>{invitation.email}</strong>. Choose a password to finish
        setting up your account.
      </p>

      <Form
        onSubmit={() => void submit()}
        error={error}
        className="mt-6 text-left"
      >
        <Field name="name" label="Your name">
          {(props) => (
            <TextInput
              {...props}
              value={name}
              autoFocus
              autoComplete="name"
              onChange={(event) => setName(event.target.value)}
            />
          )}
        </Field>
        <Field name="password" label="Password" hint="At least 12 characters.">
          {(props) => (
            <TextInput
              {...props}
              type="password"
              value={password}
              autoComplete="new-password"
              onChange={(event) => setPassword(event.target.value)}
            />
          )}
        </Field>
        <SubmitButton busy={submitting}>Create my account</SubmitButton>
      </Form>
    </Centered>
  )
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4">
      <div className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-8 text-center">
        {children}
      </div>
    </div>
  )
}
