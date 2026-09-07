import { useEffect, useRef, useState } from 'react'

/**
 * Used wherever a value exists that nobody should retype: an API key shown once, a
 * gateway URL, a request id from the logs.
 *
 * `navigator.clipboard` is unavailable over plain http on some browsers and can be
 * refused by permission policy, so the fallback keeps the value selectable rather than
 * leaving the user with a button that silently does nothing.
 */
export function CopyButton({
  value,
  label = 'Copy',
  className = '',
}: {
  value: string
  label?: string
  className?: string
}) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current)
    }
  }, [])

  const copy = () => {
    const done = (next: 'copied' | 'failed') => {
      setState(next)
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => setState('idle'), 2000)
    }

    if (!navigator.clipboard?.writeText) {
      done('failed')
      return
    }
    navigator.clipboard.writeText(value).then(
      () => done('copied'),
      () => done('failed'),
    )
  }

  return (
    <button
      type="button"
      onClick={copy}
      className={`inline-flex items-center rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 ${className}`}
      aria-label={`${label} to clipboard`}
    >
      {state === 'copied' ? 'Copied' : state === 'failed' ? 'Select and copy' : label}
    </button>
  )
}
