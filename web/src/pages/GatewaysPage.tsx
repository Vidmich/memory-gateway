import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { useGateways } from '@/api/gateways'
import { resolveRange, useRequestCounts } from '@/api/monitoring'
import type { GatewayResponse } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { CopyButton } from '@/components/CopyButton'
import { DataTable, type Column } from '@/components/DataTable'
import { StatusBadge } from '@/components/StatusBadge'

/**
 * Gateways (SPEC §13.1) — the published endpoints.
 *
 * The endpoint URL is a first-class column with a copy button, not a detail buried in the
 * editor, because copying it is the single most common thing anyone does on this screen:
 * it is what goes into a customer's `base_url`.
 *
 * The 24-hour request count comes from one grouped metrics query for the whole page, not
 * one per row. A gateway with no traffic shows a real zero; a gateway whose count has not
 * loaded yet shows an em dash, because "not measured" and "measured as none" are
 * different answers and only one of them is a reason to worry.
 */
export function GatewaysPage() {
  const { user } = useAuth()
  const [cursor, setCursor] = useState<string | null>(null)
  const [previous, setPrevious] = useState<(string | null)[]>([])

  const writes = can(user, 'resources:write')
  const { data, isLoading } = useGateways(cursor)
  const rows = data?.items ?? []
  const window = useMemo(() => resolveRange('24h'), [])
  const counts = useRequestCounts(window)
  const countsLoaded = Object.keys(counts).length > 0 || rows.length === 0

  const columns: Column<GatewayResponse>[] = [
    {
      key: 'name',
      header: 'Name',
      sortValue: (row) => row.name,
      render: (row) => (
        <div>
          <Link to={`/gateways/${row.id}`} className="font-medium text-slate-900 hover:underline">
            {row.name}
          </Link>
          <div className="font-mono text-xs text-slate-500">/{row.slug}</div>
        </div>
      ),
    },
    {
      key: 'endpoint',
      header: 'Endpoint',
      render: (row) => (
        <div className="flex items-center gap-2">
          <code className="truncate text-xs text-slate-600">{row.endpoint_url}</code>
          <CopyButton value={row.endpoint_url} label="Copy" />
        </div>
      ),
    },
    {
      key: 'model',
      header: 'Model',
      sortValue: (row) => row.targets[0]?.name ?? '',
      render: (row) => <TargetCell gateway={row} />,
    },
    {
      key: 'mode',
      header: 'Mode',
      sortValue: (row) => row.routing_mode,
      render: (row) => <span className="font-mono text-xs">{row.routing_mode}</span>,
    },
    {
      key: 'keys',
      header: 'Keys',
      align: 'right',
      sortValue: (row) => row.key_count,
      render: (row) => <span className="text-sm text-slate-700">{row.key_count}</span>,
    },
    {
      key: 'requests',
      header: '24 h',
      align: 'right',
      sortValue: (row) => counts[row.id] ?? -1,
      render: (row) =>
        countsLoaded ? (
          <Link
            to={`/monitoring?gateway=${row.id}`}
            className="text-sm text-slate-700 hover:underline"
          >
            {(counts[row.id] ?? 0).toLocaleString()}
          </Link>
        ) : (
          <span className="text-sm text-slate-400">—</span>
        ),
    },
    {
      key: 'status',
      header: 'Status',
      sortValue: (row) => String(row.enabled),
      render: (row) => <StatusBadge status={row.enabled ? 'enabled' : 'disabled'} />,
    },
  ]

  return (
    <div>
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Gateways</h1>
          <p className="mt-1 text-sm text-slate-500">
            Your published API endpoints. Each has its own URL, its own keys, and its own
            prompt.
          </p>
        </div>
        {writes ? (
          <Link
            to="/gateways/new"
            className="shrink-0 rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800"
          >
            New gateway
          </Link>
        ) : null}
      </header>

      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        caption="Gateways"
        loading={isLoading}
        emptyTitle="No gateways yet"
        emptyDescription="A gateway is the URL your application calls. Create one, point it at a model, add a key, and you have an OpenAI-compatible endpoint."
        emptyAction={
          writes ? (
            <Link
              to="/gateways/new"
              className="inline-flex rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white"
            >
              New gateway
            </Link>
          ) : null
        }
        onNextPage={
          data?.next_cursor
            ? () => {
                setPrevious((stack) => [...stack, cursor])
                setCursor(data.next_cursor ?? null)
              }
            : null
        }
        onPreviousPage={
          previous.length > 0
            ? () => {
                setCursor(previous.at(-1) ?? null)
                setPrevious((stack) => stack.slice(0, -1))
              }
            : null
        }
      />
    </div>
  )
}

/**
 * A gateway with no model cannot serve, and says so here rather than at the first 503.
 * A gateway pointing at a *disabled* model is the same outage with a different fix, so
 * the two are different messages.
 *
 * A chain names its first target and counts the rest. Listing all of them would make the
 * column the widest thing on the screen for a detail the editor is one click away from;
 * naming only the first would make a two-model gateway look like a one-model gateway,
 * which is the reading that matters — a disabled *secondary* is a failover that will not
 * work, and it is only visible if the count says there is one.
 */
function TargetCell({ gateway }: { gateway: GatewayResponse }) {
  const target = gateway.targets[0]
  if (!target) {
    return <span className="text-xs text-amber-700">No model — requests will fail</span>
  }
  const off = gateway.targets.filter((entry) => !entry.enabled)
  const rest = gateway.targets.length - 1

  return (
    <div>
      <div className="text-sm text-slate-700">
        {target.name}
        {rest > 0 ? <span className="text-slate-400"> +{rest}</span> : null}
      </div>
      {off.length > 0 ? (
        <div className="text-xs text-amber-700">
          {off.length === gateway.targets.length
            ? off.length === 1
              ? 'Model is disabled'
              : 'Every model is disabled'
            : `${off.length} of ${gateway.targets.length} models disabled`}
        </div>
      ) : null}
    </div>
  )
}
