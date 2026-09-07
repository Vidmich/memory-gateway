import { useCallback, useEffect } from 'react'

/**
 * Warn before losing unsaved edits.
 *
 * Two halves, because there are two ways to leave and the browser only lets us intervene
 * in one of them.
 *
 * **Leaving the site** — a reload, a closed tab, a typed URL — goes through
 * `beforeunload`, which is registered here while `dirty` is true. The browser shows its
 * own wording; the string is ignored by every current browser and is set only because
 * some still require the assignment for the prompt to appear at all.
 *
 * **Leaving within the app** cannot be intercepted from a hook with this router setup:
 * `useBlocker` needs a data router (`createBrowserRouter`), and this app mounts
 * `BrowserRouter` with a plain `<Routes>` tree. Rather than pretend otherwise, the hook
 * returns {@link confirmLeave}, which the screen calls on the one link that leaves the
 * editor. That is honest about its coverage: the "Back" link is guarded, and a
 * hand-typed in-app URL is not.
 *
 * Converting the app to a data router to close that gap is a change to routing that
 * belongs with a task that has a reason to touch it, not smuggled into this one.
 */
export function useUnsavedChanges(dirty: boolean): { confirmLeave: () => boolean } {
  useEffect(() => {
    if (!dirty) return

    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      // Required by older browsers to trigger the dialog; the value is never displayed.
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const confirmLeave = useCallback(() => {
    if (!dirty) return true
    return window.confirm('You have unsaved changes. Leave without saving?')
  }, [dirty])

  return { confirmLeave }
}
