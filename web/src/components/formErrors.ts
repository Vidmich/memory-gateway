import { ApiError } from '@/api/client'

/**
 * Turning an API failure into something a form can render.
 *
 * The server is the only thing that knows some rules — a slug already taken, a
 * credential the provider rejected — so its errors have to reach the field they are
 * about. Re-implementing those rules in the browser produces two answers that drift.
 */

export type FormErrors = {
  fieldErrors: Record<string, string>
  formError: string | null
}

export function errorsFrom(error: unknown): FormErrors {
  if (error instanceof ApiError) {
    const fieldErrors = error.fieldErrors
    return {
      fieldErrors,
      // A message that is entirely about specific fields would otherwise be shown twice.
      formError: Object.keys(fieldErrors).length > 0 ? null : error.message,
    }
  }
  if (error) return { fieldErrors: {}, formError: 'Something went wrong. Try again.' }
  return { fieldErrors: {}, formError: null }
}
