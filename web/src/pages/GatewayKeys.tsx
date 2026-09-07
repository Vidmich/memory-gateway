import { useState } from 'react'

import { ApiError } from '@/api/client'
import { useApiKeys, useCreateApiKey, useRevokeApiKey } from '@/api/gateways'
import type { ApiKeyResponse, IssuedApiKeyResponse } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { CopyButton } from '@/components/CopyButton'
import { DataTable, type Column } from '@/components/DataTable'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'

/**
 * The Keys section of the gateway editor.
 *
 * Its whole job is one moment: the secret exists exactly once, in the response to the
 * create call, and after this component forgets it nothing can recover it. So the reveal
 * dialog is deliberately awkward to dismiss by accident — it says plainly that this is
 * the only time, and the acknowledgement is a button press rather than a click anywhere.
 *
 * Revoking asks for the key's *name* to be typed. It is the same ceremony every
 * destructive action in this UI uses (SPEC §13.2), and it earns its keep here: revoking
 * the wrong key takes a production integration down instantly and cannot be undone.
 */
export function GatewayKeys({ gatewayId }: { gatewayId: string }) {
  const { user } = useAuth()
  const manages = can(user, 'keys:manage')

  const { data: apiKeys, isLoading } = useApiKeys(gatewayId)
  const [creating, setCreating] = useState(false)
  const [issued, setIssued] = useState<IssuedApiKeyResponse | null>(null)

  const columns: Column<ApiKeyResponse>[] = [
    {
      key: 'name',
      header: 'Name',
      sortValue: (row) => row.name,
      render: (row) => (
        <div>
          <div className="font-medium text-slate-900">{row.name}</div>
          <div className="font-mono text-xs text-slate-500">{row.prefix}…</div>
        </div>
      ),
    },
    {
      key: 'created',
      header: 'Created',
      sortValue: (row) => row.created_at,
      render: (row) => <DateCell value={row.created_at} />,
    },
    {
      key: 'last_used',
      header: 'Last used',
      sortValue: (row) => row.last_used_at ?? '',
      render: (row) =>
        row.last_used_at ? (
          <DateCell value={row.last_used_at} />
        ) : (
          <span className="text-xs text-slate-400">Never</span>
        ),
    },
    {
      key: 'status',
      header: 'Status',
      render: (row) => <KeyStatus apiKey={row} />,
    },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      align: 'right',
      render: (row) =>
        manages && !row.revoked_at ? <RevokeButton apiKey={row} /> : null,
    },
  ]

  return (
    <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
      <header className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">Keys</h2>
          <p className="mt-1 text-sm text-slate-500">
            Sent as <code className="text-xs">Authorization: Bearer …</code>. A key works only
            on this gateway.
          </p>
        </div>
        {manages ? (
          <button
            type="button"
            onClick={() => setCreating(true)}
            className="shrink-0 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Create key
          </button>
        ) : null}
      </header>

      <DataTable
        rows={apiKeys ?? []}
        columns={columns}
        rowKey={(row) => row.id}
        caption="API keys"
        loading={isLoading}
        emptyTitle="No keys yet"
        emptyDescription="Nothing can call this endpoint until it has one. The secret is shown once, when you create it."
      />

      {creating ? (
        <CreateKeyDialog
          gatewayId={gatewayId}
          onCancel={() => setCreating(false)}
          onCreated={(result) => {
            setCreating(false)
            setIssued(result)
          }}
        />
      ) : null}

      {issued ? <RevealDialog issued={issued} onDismiss={() => setIssued(null)} /> : null}
    </section>
  )
}

// ---------------------------------------------------------------------------

function CreateKeyDialog({
  gatewayId,
  onCancel,
  onCreated,
}: {
  gatewayId: string
  onCancel: () => void
  onCreated: (issued: IssuedApiKeyResponse) => void
}) {
  const [name, setName] = useState('')
  const [expires, setExpires] = useState('')
  const create = useCreateApiKey(gatewayId)
  const { notify } = useToast()

  const submit = () => {
    create.mutate(
      {
        name,
        // A date input gives a day, not an instant; midnight UTC is the least surprising
        // reading of "expires on the 4th".
        expires_at: expires ? new Date(`${expires}T00:00:00Z`).toISOString() : null,
      },
      {
        onSuccess: onCreated,
        onError: (error) =>
          notify(error instanceof ApiError ? error.message : 'Could not create the key.', 'error'),
      },
    )
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Create an API key"
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
    >
      <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
        <h3 className="text-base font-semibold text-slate-900">Create an API key</h3>

        <label htmlFor="key-name" className="mt-4 block text-sm font-medium text-slate-700">
          Name
        </label>
        <input
          id="key-name"
          value={name}
          autoFocus
          onChange={(event) => setName(event.target.value)}
          placeholder="production"
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
        <p className="mt-1 text-xs text-slate-500">
          How you tell keys apart later, when one has to be revoked.
        </p>

        <label htmlFor="key-expiry" className="mt-4 block text-sm font-medium text-slate-700">
          Expires <span className="font-normal text-slate-500">(optional)</span>
        </label>
        <input
          id="key-expiry"
          type="date"
          value={expires}
          onChange={(event) => setExpires(event.target.value)}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
        <p className="mt-1 text-xs text-slate-500">
          A key that expires is one fewer credential to remember to revoke.
        </p>

        <div className="mt-6 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={!name.trim() || create.isPending}
            className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:bg-slate-300"
          >
            {create.isPending ? 'Working…' : 'Create key'}
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * The one screen in this application that displays a live credential.
 *
 * The warning is above the value, not below it: by the time somebody reads a caption
 * under a code block they have usually already closed the dialog.
 */
function RevealDialog({
  issued,
  onDismiss,
}: {
  issued: IssuedApiKeyResponse
  onDismiss: () => void
}) {
  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-label="Copy your API key"
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
    >
      <div className="w-full max-w-lg rounded-lg bg-white p-6 shadow-xl">
        <h3 className="text-base font-semibold text-slate-900">Copy your API key</h3>
        <p className="mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          This is the only time this key will be shown. Only a hash of it is stored, so it
          cannot be recovered — if you lose it, revoke this key and create another.
        </p>

        <div className="mt-4 flex items-center gap-2 rounded-md border border-slate-200 bg-slate-50 p-3">
          <code className="min-w-0 flex-1 break-all font-mono text-xs text-slate-800">
            {issued.token}
          </code>
          <CopyButton value={issued.token} label="Copy key" />
        </div>

        <p className="mt-4 text-sm text-slate-600">
          Use it as a bearer token against this gateway&apos;s endpoint URL — any
          OpenAI-compatible client will work unchanged.
        </p>

        <div className="mt-6 flex justify-end">
          <button
            type="button"
            onClick={onDismiss}
            className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white"
          >
            I have copied it
          </button>
        </div>
      </div>
    </div>
  )
}

function RevokeButton({ apiKey }: { apiKey: ApiKeyResponse }) {
  const [confirming, setConfirming] = useState(false)
  const revoke = useRevokeApiKey()
  const { notify } = useToast()

  return (
    <>
      <button
        type="button"
        onClick={() => setConfirming(true)}
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50"
      >
        Revoke
      </button>
      <ConfirmDialog
        open={confirming}
        title="Revoke this key?"
        description={
          <>
            Any application still using it stops working on its very next request. The key
            stays listed as revoked so past requests remain traceable.
          </>
        }
        resourceName={apiKey.name}
        confirmLabel="Revoke"
        busy={revoke.isPending}
        onConfirm={() =>
          revoke.mutate(apiKey.id, {
            onSuccess: () => {
              notify(`${apiKey.name} revoked.`)
              setConfirming(false)
            },
            onError: (error) =>
              notify(
                error instanceof ApiError ? error.message : 'Could not revoke the key.',
                'error',
              ),
          })
        }
        onCancel={() => setConfirming(false)}
      />
    </>
  )
}

function KeyStatus({ apiKey }: { apiKey: ApiKeyResponse }) {
  if (apiKey.revoked_at) return <StatusBadge status="revoked" />
  if (apiKey.expires_at && new Date(apiKey.expires_at) <= new Date()) {
    return <StatusBadge status="expired" tone="error" />
  }
  if (apiKey.expires_at) {
    return (
      <span className="text-xs text-slate-600">
        Expires {new Date(apiKey.expires_at).toLocaleDateString()}
      </span>
    )
  }
  return <StatusBadge status="active" />
}

function DateCell({ value }: { value: string }) {
  return (
    <time dateTime={value} className="text-xs text-slate-600">
      {new Date(value).toLocaleString()}
    </time>
  )
}
