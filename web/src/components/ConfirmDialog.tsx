import { useEffect, useId, useRef, useState } from 'react'

/**
 * SPEC §13.2: every destructive action requires typing the resource's name.
 *
 * The point is not ceremony. A plain "are you sure" is answered reflexively; typing
 * `production-gateway` cannot be done by muscle memory when the dialog you expected was
 * about `staging-gateway`. The comparison is exact — normalising case or whitespace
 * would quietly give back the reflex.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  resourceName,
  confirmLabel = 'Delete',
  onConfirm,
  onCancel,
  busy = false,
}: {
  open: boolean
  title: string
  description: React.ReactNode
  /** What the user has to type. Usually the name shown in the list they clicked from. */
  resourceName: string
  confirmLabel?: string
  onConfirm: () => void
  onCancel: () => void
  busy?: boolean
}) {
  const [typed, setTyped] = useState('')
  const inputId = useId()
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (open) {
      setTyped('')
      inputRef.current?.focus()
    }
  }, [open])

  if (!open) return null

  const matches = typed === resourceName

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onKeyDown={(event) => {
        if (event.key === 'Escape') onCancel()
      }}
    >
      <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
        <h2 className="text-base font-semibold text-slate-900">{title}</h2>
        <div className="mt-2 text-sm text-slate-600">{description}</div>

        <label htmlFor={inputId} className="mt-5 block text-sm font-medium text-slate-700">
          Type <span className="font-mono text-slate-900">{resourceName}</span> to confirm
        </label>
        <input
          id={inputId}
          ref={inputRef}
          value={typed}
          autoComplete="off"
          onChange={(event) => setTyped(event.target.value)}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-sm focus:border-slate-500 focus:outline-none"
        />

        <div className="mt-6 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={!matches || busy}
            className="rounded-md bg-red-600 px-3 py-2 text-sm font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
