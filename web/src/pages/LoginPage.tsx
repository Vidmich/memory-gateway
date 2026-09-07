import { useState } from 'react'
import { Navigate, useLocation, useSearchParams } from 'react-router-dom'

import { useAuth } from '@/auth/AuthContext'
import { safeNext } from '@/auth/redirect'
import { Field, Form, SubmitButton, TextInput } from '@/components/Form'
import { FullPageSpinner } from '@/components/FullPageSpinner'

export function LoginPage() {
  const { status, error, signIn } = useAuth()
  const [searchParams] = useSearchParams()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(false)
  const [failure, setFailure] = useState<unknown>(null)

  const next = safeNext(searchParams.get('next'))

  if (status === 'restoring') return <FullPageSpinner label="Checking your session…" />
  if (status === 'authenticated') return <Navigate to={next} replace />

  const submit = async () => {
    setFailure(null)
    try {
      await signIn({ email, password, remember })
    } catch (caught) {
      setFailure(caught)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <div className="w-full max-w-sm">
        <h1 className="mb-1 text-lg font-semibold text-slate-900">Memory Gateway</h1>
        <p className="mb-6 text-sm text-slate-500">Sign in to manage your gateways.</p>

        <div className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          <Form onSubmit={submit} error={failure ?? (error ? new Error(error) : null)}>
            {/* `error` from the context covers the expired-session case, which arrives
                without anyone having submitted this form. */}
            {!failure && error ? (
              <div
                role="alert"
                className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
              >
                {error}
              </div>
            ) : null}

            <Field name="email" label="Email">
              {({ id, invalid, describedBy }) => (
                <TextInput
                  id={id}
                  invalid={invalid}
                  describedBy={describedBy}
                  type="email"
                  name="email"
                  autoComplete="username"
                  required
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                />
              )}
            </Field>

            <Field name="password" label="Password">
              {({ id, invalid, describedBy }) => (
                <TextInput
                  id={id}
                  invalid={invalid}
                  describedBy={describedBy}
                  type="password"
                  name="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
              )}
            </Field>

            <label className="mb-5 flex items-center gap-2 text-sm text-slate-600">
              <input
                type="checkbox"
                name="remember"
                checked={remember}
                onChange={(event) => setRemember(event.target.checked)}
                className="h-4 w-4 rounded border-slate-300"
              />
              Keep me signed in
            </label>

            <SubmitButton busy={status === 'signing-in'}>Sign in</SubmitButton>
          </Form>
        </div>

        {location.state ? null : null}
      </div>
    </div>
  )
}
