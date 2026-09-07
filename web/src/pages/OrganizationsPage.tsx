import { useState } from 'react'

import { ApiError } from '@/api/client'
import { useCreateOrganization, useOrganizations, useUpdateOrganization } from '@/api/directory'
import type { OrganizationResponse } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { DataTable, type Column } from '@/components/DataTable'
import { Field, Form, SubmitButton, TextInput } from '@/components/Form'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'
import { suggestSlug } from '@/pages/slug'

/**
 * Platform → Organizations (SPEC §13.1).
 *
 * Superadmin only, and the sidebar entry is hidden without `platform:administer` — but
 * the reason an org admin cannot reach it is that the API answers 403, not that the link
 * is missing.
 */
export function OrganizationsPage() {
  const [cursor, setCursor] = useState<string | null>(null)
  const [previous, setPrevious] = useState<(string | null)[]>([])
  const [creating, setCreating] = useState(false)

  const { data, isLoading } = useOrganizations(cursor)
  const rows = data?.items ?? []

  const columns: Column<OrganizationResponse>[] = [
    {
      key: 'name',
      header: 'Name',
      sortValue: (row) => row.name,
      render: (row) => (
        <div>
          <div className="font-medium text-slate-900">{row.name}</div>
          <div className="font-mono text-xs text-slate-500">{row.slug}</div>
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      sortValue: (row) => row.status,
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: 'members',
      header: 'Members',
      align: 'right',
      sortValue: (row) => row.member_count,
      render: (row) => row.member_count,
    },
    {
      key: 'gateways',
      header: 'Gateways',
      align: 'right',
      sortValue: (row) => row.gateway_count,
      render: (row) => row.gateway_count,
    },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      align: 'right',
      render: (row) => <OrganizationActions organization={row} />,
    },
  ]

  return (
    <div>
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Organizations</h1>
          <p className="mt-1 text-sm text-slate-500">
            Every tenant on this platform. Creating one gives it no members — invite an
            administrator from its Members page, or open it and invite from there.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="shrink-0 rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800"
        >
          New organization
        </button>
      </header>

      {creating ? <CreateOrganization onDone={() => setCreating(false)} /> : null}

      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        caption="Organizations"
        loading={isLoading}
        emptyTitle="No organizations yet"
        emptyDescription="Create one to give a customer their own isolated workspace, members, gateways and data."
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

function OrganizationActions({ organization }: { organization: OrganizationResponse }) {
  const { openAs } = useAuth()
  const { notify } = useToast()
  const update = useUpdateOrganization(organization.id)
  const [confirming, setConfirming] = useState(false)

  const suspended = organization.status === 'suspended'

  const setStatus = (status: 'active' | 'suspended') => {
    update.mutate(
      { status },
      {
        onSuccess: () => {
          notify(status === 'suspended' ? 'Organization suspended.' : 'Organization restored.')
          setConfirming(false)
        },
        onError: (error) =>
          notify(error instanceof ApiError ? error.message : 'Could not change the status.', 'error'),
      },
    )
  }

  return (
    <div className="flex justify-end gap-2">
      <button
        type="button"
        onClick={() => openAs({ id: organization.id, name: organization.name })}
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
      >
        Open as
      </button>
      {suspended ? (
        <button
          type="button"
          onClick={() => setStatus('active')}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
        >
          Restore
        </button>
      ) : (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50"
        >
          Suspend
        </button>
      )}

      <ConfirmDialog
        open={confirming}
        title="Suspend this organization?"
        description={
          <>
            Its members will be signed out and unable to sign in again, and its gateways stop
            serving traffic. Nothing is deleted, and you can restore it at any time.
          </>
        }
        resourceName={organization.slug}
        confirmLabel="Suspend"
        busy={update.isPending}
        onConfirm={() => setStatus('suspended')}
        onCancel={() => setConfirming(false)}
      />
    </div>
  )
}

function CreateOrganization({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const create = useCreateOrganization()
  const { notify } = useToast()

  const submit = async () => {
    await create.mutateAsync(
      { name, slug },
      {
        onSuccess: (organization) => {
          notify(`Created ${organization.name}.`)
          onDone()
        },
      },
    )
  }

  return (
    <div className="mb-6 rounded-lg border border-slate-200 bg-white p-6">
      <h2 className="mb-4 text-sm font-semibold text-slate-900">New organization</h2>
      <Form
        onSubmit={() => {
          void submit().catch(() => {
            // Rendered inline by `Form` from the rejected mutation.
          })
        }}
        error={create.error}
        className="max-w-lg"
      >
        <Field name="name" label="Name">
          {(props) => (
            <TextInput
              {...props}
              value={name}
              onChange={(event) => {
                setName(event.target.value)
                // Suggest a slug while it is untouched. The field stays editable, because
                // the slug ends up in a public URL and the customer may want their own.
                if (!slug || slug === suggestSlug(name)) setSlug(suggestSlug(event.target.value))
              }}
              autoFocus
            />
          )}
        </Field>
        <Field
          name="slug"
          label="Slug"
          hint="Lower-case letters, digits and hyphens. Appears in gateway URLs."
        >
          {(props) => (
            <TextInput
              {...props}
              value={slug}
              onChange={(event) => setSlug(event.target.value)}
              className="w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-sm"
            />
          )}
        </Field>
        <div className="flex gap-2">
          <SubmitButton busy={create.isPending}>Create</SubmitButton>
          <button
            type="button"
            onClick={onDone}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Cancel
          </button>
        </div>
      </Form>
    </div>
  )
}
