import type { ReactNode } from 'react'

/**
 * SPEC §13.2: empty states are instructional. A screen that says "No connectors" teaches
 * nothing; one that says what to upload and why is the first half of onboarding.
 */
export function EmptyState({
  title,
  description,
  action,
  icon,
}: {
  title: string
  description: ReactNode
  action?: ReactNode
  icon?: ReactNode
}) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
      {icon ? <div className="mb-3 flex justify-center text-slate-400">{icon}</div> : null}
      <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
      <div className="mx-auto mt-2 max-w-md text-sm text-slate-500">{description}</div>
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  )
}
