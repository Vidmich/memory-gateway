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

/**
 * Transient feedback: "Gateway saved", "Key revoked".
 *
 * Errors that need a decision do not belong here — a toast that disappears after four
 * seconds is the wrong place for anything the user has to act on. Those go inline, next
 * to the thing that failed, via `Form`.
 */

export type ToastTone = 'success' | 'error' | 'info'
export type Toast = { id: string; tone: ToastTone; message: string }

type Action = { type: 'push'; toast: Toast } | { type: 'dismiss'; id: string }

function reducer(state: Toast[], action: Action): Toast[] {
  switch (action.type) {
    case 'push':
      return [...state, action.toast]
    case 'dismiss':
      return state.filter((toast) => toast.id !== action.id)
    default:
      return state
  }
}

type ToastContextValue = {
  toasts: Toast[]
  notify: (message: string, tone?: ToastTone) => void
  dismiss: (id: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

const VISIBLE_MS = 4000

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, dispatch] = useReducer(reducer, [])
  //  Every pending auto-dismiss, so unmounting cancels them. Without this a toast raised
  //  in the last second of a page's life dispatches into a tree that is no longer there —
  //  harmless in a browser, and in a test runner an unhandled `window is not defined`
  //  attributed to whichever file happened to be running when the timer fired.
  const timers = useRef<ReturnType<typeof setTimeout>[]>([])

  useEffect(
    () => () => {
      timers.current.forEach(clearTimeout)
      timers.current = []
    },
    [],
  )

  const dismiss = useCallback((id: string) => dispatch({ type: 'dismiss', id }), [])

  const notify = useCallback(
    (message: string, tone: ToastTone = 'success') => {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
      dispatch({ type: 'push', toast: { id, tone, message } })
      timers.current.push(setTimeout(() => dispatch({ type: 'dismiss', id }), VISIBLE_MS))
    },
    [],
  )

  const value = useMemo(() => ({ toasts, notify, dismiss }), [toasts, notify, dismiss])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <ToastHost />
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext)
  if (!value) throw new Error('useToast must be used inside <ToastProvider>')
  return value
}

const TONES: Record<ToastTone, string> = {
  success: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  error: 'border-red-200 bg-red-50 text-red-800',
  info: 'border-slate-200 bg-white text-slate-800',
}

export function ToastHost() {
  const { toasts, dismiss } = useToast()

  return (
    // `polite`, not `assertive`: these are confirmations, and interrupting a screen
    // reader mid-sentence to say "Saved" is worse than waiting a moment.
    <div
      aria-live="polite"
      className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2"
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`pointer-events-auto flex items-start gap-2 rounded-md border px-3 py-2 text-sm shadow-sm ${TONES[toast.tone]}`}
        >
          <span className="flex-1">{toast.message}</span>
          <button
            type="button"
            onClick={() => dismiss(toast.id)}
            className="text-slate-400 hover:text-slate-600"
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
