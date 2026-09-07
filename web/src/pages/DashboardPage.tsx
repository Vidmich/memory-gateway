import { useMemo, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { useGateways } from '@/api/gateways'
import { resolveRange, useSummary } from '@/api/monitoring'
import { useAuth } from '@/auth/AuthContext'

/**
 * SPEC §13.1's dashboard: the org's last 24 hours in six numbers.
 *
 * The traffic cards all come from **one** summary query — the same cached one the
 * monitoring screen opens with, so landing here and clicking through costs one request
 * rather than two. Every card links to the screen that explains it, because a number with
 * no way to drill into it is a number people learn to ignore.
 *
 * Cards for subsystems that do not exist yet still say so rather than showing a zero. A
 * "0" under *Documents indexed* is indistinguishable from an ingestion pipeline that is
 * silently failing, which is exactly the wrong thing for a dashboard to be ambiguous
 * about.
 */
export function DashboardPage() {
  const { user } = useAuth()
  const window = useMemo(() => resolveRange('24h'), [])
  const summary = useSummary(window)
  const gateways = useGateways()

  const enabled = (gateways.data?.items ?? []).filter((gateway) => gateway.enabled).length
  const errorRate = summary.data ? summary.data.error_rate : null

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Dashboard</h1>
        <p className="mt-1 text-sm text-slate-500">
          {user?.organization
            ? `The last 24 hours at ${user.organization.name}.`
            : 'An overview of the platform.'}
        </p>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <MetricCard
          title="Requests (24 h)"
          value={summary.data ? summary.data.requests.toLocaleString() : undefined}
          loading={summary.isLoading}
          detail={<Link to="/monitoring">See them all →</Link>}
        />
        <MetricCard
          title="Error rate (24 h)"
          value={errorRate === null ? undefined : `${(errorRate * 100).toFixed(1)}%`}
          loading={summary.isLoading}
          tone={errorRate !== null && errorRate > 0.05 ? 'warn' : undefined}
          detail={
            summary.data ? (
              <Link to="/monitoring?status=5xx">
                {summary.data.errors.toLocaleString()} failed →
              </Link>
            ) : (
              'No requests yet.'
            )
          }
        />
        <MetricCard
          title="p95 latency (24 h)"
          value={summary.data?.total.p95 === null ? '—' : `${summary.data?.total.p95} ms`}
          loading={summary.isLoading}
          detail="The slow tail, end to end."
        />
        <MetricCard
          title="Active gateways"
          value={gateways.data ? String(enabled) : undefined}
          loading={gateways.isLoading}
          detail={<Link to="/gateways">Manage endpoints →</Link>}
        />
        <MetricCard title="Documents indexed" detail="Arrives with connectors." />
        <MetricCard title="Memory facts stored" detail="Arrives with distillation." />
      </div>

      {summary.data && summary.data.requests === 0 ? (
        <section className="mt-8 rounded-lg border border-slate-200 bg-white p-6">
          <h2 className="text-sm font-semibold text-slate-900">Nothing has called your gateways yet</h2>
          <p className="mt-2 max-w-2xl text-sm text-slate-600">
            Create a gateway, mint a key, and point an OpenAI client at the endpoint URL.
            Every request from then on appears under{' '}
            <Link to="/monitoring" className="text-slate-900 underline">
              Monitoring
            </Link>
            , with the prompt that was actually sent.
          </p>
        </section>
      ) : null}
    </div>
  )
}

function MetricCard({
  title,
  detail,
  value,
  loading = false,
  tone,
}: {
  title: string
  detail: ReactNode
  value?: ReactNode
  loading?: boolean
  tone?: 'warn' | undefined
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <h2 className="text-sm font-medium text-slate-600">{title}</h2>
      <div
        className={`mt-2 text-2xl font-semibold ${
          tone === 'warn' ? 'text-amber-700' : 'text-slate-900'
        }`}
      >
        {loading ? '…' : (value ?? '—')}
      </div>
      <div className="mt-1 text-xs text-slate-400">{detail}</div>
    </div>
  )
}
