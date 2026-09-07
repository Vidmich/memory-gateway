import { useState } from 'react'
import { Link } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { useDeleteModel, useModels, type ModelScope } from '@/api/models'
import type { ModelResponse } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { DataTable, type Column } from '@/components/DataTable'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'

const TABS: { scope: ModelScope; label: string; blurb: string }[] = [
  {
    scope: 'org',
    label: 'Our models',
    blurb: 'Providers this organization configured. You own the credentials.',
  },
  {
    scope: 'global',
    label: 'Global catalog',
    blurb:
      'Shared models the platform operator maintains. Point a gateway at one without ' +
      'holding a provider key of your own.',
  },
]

/**
 * Models (SPEC §13.1).
 *
 * Two tabs over **one** endpoint — `?scope=` is a filter, not a second list shape — so a
 * model cannot appear in one view and be missing from the other, and paging works
 * identically on both.
 *
 * Whether a row can be edited comes from the server (`editable`), not from comparing the
 * signed-in user's organization here. The API answers 404 for a model the caller does not
 * own, and a UI that decided otherwise would offer a form that cannot save.
 */
export function ModelsPage() {
  const { user } = useAuth()
  const [scope, setScope] = useState<ModelScope>('org')
  const [cursor, setCursor] = useState<string | null>(null)
  const [previous, setPrevious] = useState<(string | null)[]>([])

  const writes = can(user, 'resources:write')
  const { data, isLoading } = useModels(scope, cursor)
  const rows = data?.items ?? []
  const active = TABS.find((tab) => tab.scope === scope) ?? TABS[0]!

  const goTo = (next: ModelScope) => {
    setScope(next)
    setCursor(null)
    setPrevious([])
  }

  const columns: Column<ModelResponse>[] = [
    {
      key: 'name',
      header: 'Name',
      sortValue: (row) => row.name,
      render: (row) => (
        <div>
          <div className="font-medium text-slate-900">{row.name}</div>
          <div className="font-mono text-xs text-slate-500">{row.upstream_model_id}</div>
        </div>
      ),
    },
    {
      key: 'dialect',
      header: 'Dialect',
      sortValue: (row) => row.dialect,
      render: (row) => <span className="font-mono text-xs">{row.dialect}</span>,
    },
    {
      key: 'endpoint',
      header: 'Endpoint',
      sortValue: (row) => row.base_url,
      render: (row) => (
        <span className="font-mono text-xs text-slate-500">{hostOf(row.base_url)}</span>
      ),
    },
    {
      key: 'credential',
      header: 'Credential',
      render: (row) => <Credential model={row} />,
    },
    {
      key: 'status',
      header: 'Status',
      sortValue: (row) => String(row.enabled),
      render: (row) => <StatusBadge status={row.enabled ? 'enabled' : 'disabled'} />,
    },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      align: 'right',
      render: (row) => <RowActions model={row} canWrite={writes} />,
    },
  ]

  return (
    <div>
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Models</h1>
          <p className="mt-1 text-sm text-slate-500">
            Where completions are actually sent. A gateway points at one of these.
          </p>
        </div>
        {writes ? (
          <Link
            to="/models/new"
            className="shrink-0 rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800"
          >
            New model
          </Link>
        ) : null}
      </header>

      <div className="mb-4 border-b border-slate-200">
        <nav className="-mb-px flex gap-6" aria-label="Model scope">
          {TABS.map((tab) => (
            <button
              key={tab.scope}
              type="button"
              onClick={() => goTo(tab.scope)}
              aria-current={tab.scope === scope ? 'page' : undefined}
              className={`border-b-2 px-1 pb-2 text-sm font-medium ${
                tab.scope === scope
                  ? 'border-slate-900 text-slate-900'
                  : 'border-transparent text-slate-500 hover:text-slate-700'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      <p className="mb-4 text-sm text-slate-500">{active.blurb}</p>

      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        caption={active.label}
        loading={isLoading}
        emptyTitle={scope === 'org' ? 'No models yet' : 'The catalog is empty'}
        emptyDescription={
          scope === 'org'
            ? 'Add a provider endpoint and an API key, press Test connection, and point a gateway at it.'
            : 'Your platform operator has not shared any models. Add one of your own under "Our models".'
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
 * SPEC §5.4: `{configured, hint}` and nothing else, ever. The hint is absent on a global
 * model unless you own it, which is why "Configured" without one is a normal state here
 * rather than a sign something is missing.
 */
function Credential({ model }: { model: ModelResponse }) {
  if (!model.credential.configured) {
    return <span className="text-xs text-slate-400">None</span>
  }
  return (
    <span className="font-mono text-xs text-slate-600">{model.credential.hint ?? 'Configured'}</span>
  )
}

function RowActions({ model, canWrite }: { model: ModelResponse; canWrite: boolean }) {
  const [confirming, setConfirming] = useState(false)
  const [blocked, setBlocked] = useState<BlockedDelete | null>(null)
  const remove = useDeleteModel()
  const { notify } = useToast()

  if (!model.editable) {
    return <span className="text-xs text-slate-400">Read-only</span>
  }

  return (
    <div className="flex items-center justify-end gap-2">
      <Link
        to={`/models/${model.id}`}
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
      >
        {canWrite ? 'Edit' : 'View'}
      </Link>
      {canWrite ? (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50"
        >
          Delete
        </button>
      ) : null}

      <ConfirmDialog
        open={confirming}
        title="Delete this model?"
        description={
          <>
            Any gateway pointing at it stops working. If one does, this is refused and says
            which — disable the model instead to take it out of service without losing the
            configuration.
          </>
        }
        resourceName={model.name}
        confirmLabel="Delete"
        busy={remove.isPending}
        onConfirm={() =>
          remove.mutate(model.id, {
            onSuccess: () => {
              notify(`${model.name} deleted.`)
              setConfirming(false)
            },
            onError: (error) => {
              // The 409 names the gateways in its message and lists them in `details`.
              // Both are used: the sentence says what to do, the list says where.
              setBlocked(
                error instanceof ApiError
                  ? { message: error.message, gateways: gatewaysIn(error.details) }
                  : { message: 'Could not delete it.', gateways: [] },
              )
              setConfirming(false)
            },
          })
        }
        onCancel={() => setConfirming(false)}
      />

      {blocked ? <BlockedDeleteDialog blocked={blocked} onDismiss={() => setBlocked(null)} /> : null}
    </div>
  )
}

type GatewayReference = { id: string; slug: string; name: string }
type BlockedDelete = { message: string; gateways: GatewayReference[] }

/**
 * The 409, rendered as the next step rather than as a failure.
 *
 * Each gateway is listed by name and slug. They become links in task 06, when there is a
 * gateway screen to link to; a link to a route that does not exist yet would be worse
 * than the text.
 */
function BlockedDeleteDialog({
  blocked,
  onDismiss,
}: {
  blocked: BlockedDelete
  onDismiss: () => void
}) {
  return (
    <div
      role="alertdialog"
      aria-label="Still in use"
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
    >
      <div className="w-full max-w-md rounded-lg bg-white p-6 text-left shadow-xl">
        <h2 className="text-base font-semibold text-slate-900">Still in use</h2>
        <p className="mt-2 text-sm text-slate-600">{blocked.message}</p>
        {blocked.gateways.length > 0 ? (
          <ul className="mt-3 space-y-1">
            {blocked.gateways.map((gateway) => (
              <li key={gateway.id} className="text-sm text-slate-700">
                {gateway.name} <span className="font-mono text-xs text-slate-500">/{gateway.slug}</span>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="mt-5 flex justify-end">
          <button
            type="button"
            onClick={onDismiss}
            className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  )
}

/** The `details.gateways` the API attaches to the 409, defensively unpacked. */
function gatewaysIn(details: unknown): GatewayReference[] {
  const listed = (details as { gateways?: unknown } | undefined)?.gateways
  if (!Array.isArray(listed)) return []
  return listed.filter(
    (entry): entry is GatewayReference =>
      typeof entry === 'object' && entry !== null && 'id' in entry && 'slug' in entry,
  )
}

/** Just the host, so the column stays readable next to a long Azure deployment URL. */
function hostOf(baseUrl: string): string {
  try {
    return new URL(baseUrl).host
  } catch {
    return baseUrl
  }
}
