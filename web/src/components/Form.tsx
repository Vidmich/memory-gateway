import { createContext, useContext, useId, type FormEvent, type ReactNode } from 'react'

import { errorsFrom, type FormErrors } from '@/components/formErrors'

/**
 * Form plumbing shared by every configuration screen.
 *
 * It exists for field-level errors from the API: showing them all in one banner at the
 * top makes the user hunt for which input is wrong, so the failure goes into context and
 * each `Field` picks out its own by name.
 */

const FormErrorContext = createContext<FormErrors>({ fieldErrors: {}, formError: null })

export function Form({
  onSubmit,
  error,
  children,
  className = '',
}: {
  onSubmit: () => void | Promise<void>
  /** The rejected value from the mutation, if any. */
  error?: unknown
  children: ReactNode
  className?: string
}) {
  const errors = errorsFrom(error)

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    void onSubmit()
  }

  return (
    <FormErrorContext.Provider value={errors}>
      <form onSubmit={handleSubmit} className={className} noValidate>
        {errors.formError ? (
          <div
            role="alert"
            className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          >
            {errors.formError}
          </div>
        ) : null}
        {children}
      </form>
    </FormErrorContext.Provider>
  )
}

export function Field({
  name,
  label,
  hint,
  children,
}: {
  /** Must match the API field name for server-side errors to land here. */
  name: string
  label: string
  hint?: ReactNode
  children: (props: { id: string; invalid: boolean; describedBy: string | undefined }) => ReactNode
}) {
  const { fieldErrors } = useContext(FormErrorContext)
  const id = useId()
  const message = fieldErrors[name]
  const describedBy = message ? `${id}-error` : hint ? `${id}-hint` : undefined

  return (
    <div className="mb-4">
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {label}
      </label>
      <div className="mt-1">
        {children({ id, invalid: Boolean(message), describedBy })}
      </div>
      {message ? (
        <p id={`${id}-error`} className="mt-1 text-sm text-red-600">
          {message}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="mt-1 text-sm text-slate-500">
          {hint}
        </p>
      ) : null}
    </div>
  )
}

export function TextInput({
  id,
  invalid,
  describedBy,
  ...props
}: {
  id: string
  invalid: boolean
  describedBy: string | undefined
} & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      id={id}
      aria-invalid={invalid || undefined}
      aria-describedby={describedBy}
      className={`w-full rounded-md border px-3 py-2 text-sm focus:outline-none ${
        invalid
          ? 'border-red-400 focus:border-red-500'
          : 'border-slate-300 focus:border-slate-500'
      }`}
      {...props}
    />
  )
}

export function SubmitButton({
  children,
  busy = false,
  className = '',
}: {
  children: ReactNode
  busy?: boolean
  className?: string
}) {
  return (
    <button
      type="submit"
      disabled={busy}
      className={`inline-flex w-full items-center justify-center rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400 ${className}`}
    >
      {busy ? 'Working…' : children}
    </button>
  )
}
