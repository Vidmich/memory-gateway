import type { ReactNode } from 'react'

import { useAuth } from '@/auth/AuthContext'

/**
 * Placeholder for SPEC §13.1's dashboard.
 *
 * The cards are named for the metrics task 07 will populate, rather than being generic
 * boxes, so that task adds a query per card instead of designing the screen. Each says
 * plainly that it has no data yet — a card showing "0" for a number nobody is measuring
 * is worse than one that admits it.
 */
export function DashboardPage() {
  const { user } = useAuth()

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Dashboard</h1>
        <p className="mt-1 text-sm text-slate-500">
          {user?.organization
            ? `An overview of ${user.organization.name}.`
            : 'An overview of the platform.'}
        </p>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <MetricCard title="Requests (24 h)" detail="Populated in task 07." />
        <MetricCard title="Error rate (24 h)" detail="Populated in task 07." />
        <MetricCard title="Active gateways" detail="Populated in task 06." />
        <MetricCard title="Documents indexed" detail="Populated in task 09." />
        <MetricCard title="Memory facts stored" detail="Populated in task 13." />
        <MetricCard title="Degraded state" detail="Populated in task 07." />
      </div>

      <section className="mt-8 rounded-lg border border-slate-200 bg-white p-6">
        <h2 className="text-sm font-semibold text-slate-900">Next steps</h2>
        <p className="mt-2 max-w-2xl text-sm text-slate-600">
          Signing in works. Configuring models, gateways and keys from this UI arrives in
          tasks 05 and 06; until then, <code className="font-mono text-slate-800">make seed</code>{' '}
          creates a demo gateway from an OpenAI key.
        </p>
      </section>
    </div>
  )
}

function MetricCard({ title, detail, value }: { title: string; detail: ReactNode; value?: ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <h2 className="text-sm font-medium text-slate-600">{title}</h2>
      <div className="mt-2 text-2xl font-semibold text-slate-900">{value ?? '—'}</div>
      <p className="mt-1 text-xs text-slate-400">{detail}</p>
    </div>
  )
}
