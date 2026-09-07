import { useState } from 'react'
import { Link } from 'react-router-dom'

import { useConnectors, useCreateConnector } from '@/api/connectors'
import type { ConnectorResponse } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { DataTable, type Column } from '@/components/DataTable'
import { Field, Form, SubmitButton, TextInput } from '@/components/Form'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'
import { formatBytes, statusSummary } from '@/pages/connectors'

/**
 * Connectors (SPEC §13.1) — where an organization's content comes from.
 *
 * The **status** column is the reason this screen exists rather than being a list of
 * names. A connector is either fine, busy, or has documents that could not be read, and
 * the third is the only one anybody needs to act on — so it is a badge and a sentence
 * rather than a raw count of seven statuses that a reader has to interpret.
 *
 * Creating one is an inline form, not a separate page. A connector has exactly two
 * settings at creation — a name and a description — and a route change to collect them
 * would be a page that exists to hold one input.
 */
export function ConnectorsPage() {
  const { user } = useAuth()
  const writes = can(user, 'resources:write')
  const [cursor, setCursor] = useState<string | null>(null)
  const [previous, setPrevious] = useState<(string | null)[]>([])
  const [creating, setCreating] = useState(false)

  const { data, isLoading } = useConnectors(cursor)
  const rows = data?.items ?? []

  const columns: Column<ConnectorResponse>[] = [
    {
      key: 'name',
      header: 'Name',
      sortValue: (row) => row.name,
      render: (row) => (
        <div>
          <Link
            to={`/connectors/${row.id}`}
            className="font-medium text-slate-900 hover:underline"
          >
            {row.name}
          </Link>
          {row.description ? (
            <div className="text-xs text-slate-500">{row.description}</div>
          ) : null}
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      sortValue: (row) => statusSummary(row).label,
      render: (row) => <StatusCell connector={row} />,
    },
    {
      key: 'documents',
      header: 'Documents',
      align: 'right',
      sortValue: (row) => row.document_count,
      render: (row) => (
        <span className="text-sm text-slate-700">{row.document_count.toLocaleString()}</span>
      ),
    },
    {
      key: 'size',
      header: 'Size',
      align: 'right',
      sortValue: (row) => row.total_bytes,
      render: (row) => <span className="text-sm text-slate-600">{formatBytes(row.total_bytes)}</span>,
    },
    {
      key: 'synced',
      header: 'Last synced',
      sortValue: (row) => row.last_synced_at ?? '',
      render: (row) => (
        <span className="text-sm text-slate-500">
          {row.last_synced_at ? new Date(row.last_synced_at).toLocaleString() : 'Never'}
        </span>
      ),
    },
  ]

  return (
    <div>
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Connectors</h1>
          <p className="mt-1 text-sm text-slate-500">
            Where your content comes from. Upload files into one, and its documents become
            searchable chunks a gateway can draw on.
          </p>
        </div>
        {writes ? (
          <button
            type="button"
            onClick={() => setCreating((open) => !open)}
            className="shrink-0 rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800"
          >
            {creating ? 'Cancel' : 'New connector'}
          </button>
        ) : null}
      </header>

      {creating ? <NewConnector onDone={() => setCreating(false)} /> : null}

      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        caption="Connectors"
        loading={isLoading}
        emptyTitle="No connectors yet"
        emptyDescription="A connector is a folder of your content. Create one, drop in your handbook, your API docs, your runbooks — anything text — and a gateway can answer from them."
        emptyAction={
          writes ? (
            <button
              type="button"
              onClick={() => setCreating(true)}
              className="inline-flex rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white"
            >
              New connector
            </button>
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
 * The badge answers "is anything wrong"; the sentence under it answers "what should I
 * do". A count per status would make the reader do that arithmetic themselves, on every
 * row, every time.
 */
function StatusCell({ connector }: { connector: ConnectorResponse }) {
  const summary = statusSummary(connector)
  return (
    <div>
      <StatusBadge status={summary.label} tone={summary.tone} />
      <div className="mt-1 text-xs text-slate-500">{summary.detail}</div>
    </div>
  )
}

function NewConnector({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const create = useCreateConnector()
  const { notify } = useToast()

  const submit = async () => {
    const created = await create.mutateAsync({
      name,
      description: description.trim() || null,
      // Sent explicitly, though the server defaults it: `openapi-typescript` treats a
      // property with a default as always present, which is right for a response and
      // wrong for a request. Naming the one type this build has is cheaper than a
      // generator flag, and it is what task 09 actually creates.
      type: 'managed_file_drop',
      chunking: {},
    })
    notify(`Created ${created.name}.`)
    onDone()
  }

  return (
    <section className="mb-6 rounded-lg border border-slate-200 bg-white p-4">
      <Form onSubmit={submit} error={create.error}>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field name="name" label="Name">
            {({ id, invalid, describedBy }) => (
              <TextInput
                id={id}
                value={name}
                onChange={(event) => setName(event.target.value)}
                invalid={invalid}
                describedBy={describedBy}
                autoFocus
              />
            )}
          </Field>
          <Field name="description" label="Description" hint="Optional.">
            {({ id, invalid, describedBy }) => (
              <TextInput
                id={id}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                invalid={invalid}
                describedBy={describedBy}
              />
            )}
          </Field>
        </div>
        <SubmitButton busy={create.isPending} disabled={!name.trim()}>
          Create connector
        </SubmitButton>
      </Form>
    </section>
  )
}
