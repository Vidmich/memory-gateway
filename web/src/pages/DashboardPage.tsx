import { useMemo, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { useConnectors } from '@/api/connectors'
import { useGateways } from '@/api/gateways'
import { useLimitPressure } from '@/api/limits'
import { resolveRange, useSummary } from '@/api/monitoring'
import { useSummarizationHealth } from '@/api/summarization'
import { useAuditAlerts } from '@/api/validation'
import { useAuth } from '@/auth/AuthContext'
import { pressureSummary } from '@/pages/limits'
import { waitingSummary } from '@/pages/summarization'

/**
 * SPEC §13.1's dashboard: the org's last 24 hours in six numbers.
 *
 * The traffic cards all come from **one** summary query — the same cached one the
 * monitoring screen opens with, so landing here and clicking through costs one request
 * rather than two. Every card links to the screen that explains it, because a number with
 * no way to drill into it is a number people learn to ignore.
 *
 * Cards for subsystems that do not exist yet still say so rather than showing a zero. A
 * "0" under a subsystem that has not been built is indistinguishable from one that is
 * silently failing, which is exactly the wrong thing for a dashboard to be ambiguous
 * about.
 *
 * *Documents indexed* now has a real number behind it, and it swaps its subtitle for the
 * failure count when there is one. A dashboard that reports 900 indexed and says nothing
 * about the 40 that could not be read is reporting the half nobody needs to act on.
 *
 * The **near-limit card** (SPEC §13.1's "any degraded state") is the same idea one step
 * earlier. A gateway that is being throttled shows up on the error chart as a wall of
 * 429s *after* its customers have started seeing them; this appears at 80%, while there
 * is still time to raise the limit or find the loop. It renders only when something is
 * actually under pressure, because a card that says "nothing is near a limit" every day
 * for a year is a card nobody reads on the day it changes.
 */
export function DashboardPage() {
  const { user } = useAuth()
  const window = useMemo(() => resolveRange('24h'), [])
  const summary = useSummary(window)
  const gateways = useGateways()
  const connectors = useConnectors()
  const pressure = useLimitPressure()
  // Task 102's degraded state: a connector silently not indexing because its
  // summarization cap is spent is exactly what this list is for.
  const summarization = useSummarizationHealth(window)
  const waiting = waitingSummary(summarization.data)
  // Task 103's degraded state: a connector whose last audit raised a red finding is an
  // index that ranks and ranks wrong, and nothing on the traffic charts says so.
  const alerts = useAuditAlerts()
  const redFindings = Array.isArray(alerts.data?.items) ? alerts.data.items : []

  const enabled = (gateways.data?.items ?? []).filter((gateway) => gateway.enabled).length
  const indexed = (connectors.data?.items ?? []).reduce(
    (total, connector) => total + (connector.counts.indexed ?? 0),
    0,
  )
  // Documents that could not be read. Shown *instead of* the count when there are any,
  // because the number a dashboard exists to surface is the one somebody has to act on.
  const unreadable = (connectors.data?.items ?? []).reduce(
    (total, connector) => total + (connector.counts.failed ?? 0),
    0,
  )
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
        <MetricCard
          title="Documents indexed"
          value={connectors.data ? indexed.toLocaleString() : undefined}
          loading={connectors.isLoading}
          detail={
            unreadable > 0 ? (
              <Link to="/connectors" className="text-amber-700">
                {unreadable} could not be read →
              </Link>
            ) : (
              <Link to="/connectors">Manage content →</Link>
            )
          }
        />
        <MetricCard title="Memory facts stored" detail="Arrives with distillation." />
      </div>

      {(pressure.data?.items ?? []).length > 0 ? (
        <section className="mt-8 rounded-lg border border-amber-200 bg-amber-50 p-5">
          <h2 className="text-sm font-semibold text-amber-900">Close to a rate limit</h2>
          <p className="mt-1 text-sm text-amber-900">
            These endpoints are above 80% of one of their caps. Past 100% the gateway
            answers 429 and the request never reaches a model.
          </p>
          <ul className="mt-3 space-y-1 text-sm">
            {(pressure.data?.items ?? []).map((item) => (
              <li key={item.gateway_id}>
                <Link
                  to={`/gateways/${item.gateway_id}`}
                  className="font-medium text-amber-900 underline"
                >
                  {pressureSummary(item.name, item.worst)}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {waiting ? (
        <section className="mt-8 rounded-lg border border-amber-200 bg-amber-50 p-5">
          <h2 className="text-sm font-semibold text-amber-900">Waiting on a summarization cap</h2>
          <p className="mt-1 text-sm text-amber-900">
            {waiting}. They are parked, not failed, and resume after midnight UTC.
          </p>
          <ul className="mt-3 space-y-1 text-sm">
            {(summarization.data?.waiting ?? []).map((item) => (
              <li key={item.connector_id}>
                <Link
                  to={`/connectors/${item.connector_id}`}
                  className="font-medium text-amber-900 underline"
                >
                  {item.name ?? item.connector_id}: {item.documents.toLocaleString()} waiting
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {redFindings.length > 0 ? (
        <section
          className="mt-8 rounded-lg border border-red-200 bg-red-50 p-5"
          data-testid="audit-alerts"
        >
          <h2 className="text-sm font-semibold text-red-900">An index audit found a problem</h2>
          <p className="mt-1 text-sm text-red-900">
            These connectors&apos; last chunking or embedding audit raised a red finding.
            Retrieval keeps working; it is working on the wrong chunks.
          </p>
          <ul className="mt-3 space-y-1 text-sm">
            {redFindings.map((alert) => (
              <li key={`${alert.connector_id}-${alert.kind}`}>
                <Link
                  to={`/connectors/${alert.connector_id}`}
                  className="font-medium text-red-900 underline"
                >
                  {alert.connector_name ?? alert.connector_id}: {alert.finding} ({alert.kind})
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

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
