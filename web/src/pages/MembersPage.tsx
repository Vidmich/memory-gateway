import { useState } from 'react'

import { ApiError } from '@/api/client'
import {
  useCreateInvitation,
  useInvitations,
  useMembers,
  useRemoveMember,
  useResendInvitation,
  useRevokeInvitation,
} from '@/api/directory'
import type { InvitationResponse, MemberResponse } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { CopyButton } from '@/components/CopyButton'
import { DataTable, type Column } from '@/components/DataTable'
import { Field, Form, SubmitButton, TextInput } from '@/components/Form'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'
import { useUpdateMember } from '@/api/directory'

const ROLES = [
  { value: 'org_admin', label: 'Admin', hint: 'Members, keys, and everything below.' },
  { value: 'org_member', label: 'Member', hint: 'Create and edit gateways, models and connectors.' },
  { value: 'org_viewer', label: 'Viewer', hint: 'Read-only, including monitoring.' },
]

/**
 * Settings → Members (SPEC §13.1).
 *
 * Readable by every role — seeing who your colleagues are is not an admin power — with
 * the editing controls rendered only for `org:administer`. The API refuses the same calls
 * either way; hiding them just avoids offering a door that does not open.
 */
export function MembersPage() {
  const { user, assumedOrganization } = useAuth()
  const organizationId = assumedOrganization?.id ?? user?.organization?.id
  const administers = can(user, 'org:administer')

  const { data, isLoading } = useMembers(organizationId)

  if (!organizationId) {
    return (
      <EmptyPlatformState />
    )
  }

  const columns: Column<MemberResponse>[] = [
    {
      key: 'person',
      header: 'Person',
      sortValue: (row) => row.name,
      render: (row) => (
        <div>
          <div className="font-medium text-slate-900">{row.name}</div>
          <div className="text-xs text-slate-500">{row.email}</div>
        </div>
      ),
    },
    {
      key: 'role',
      header: 'Role',
      sortValue: (row) => row.role,
      render: (row) =>
        administers ? (
          <RoleSelect member={row} />
        ) : (
          <span className="text-slate-600">{labelFor(row.role)}</span>
        ),
    },
    {
      key: 'status',
      header: 'Status',
      sortValue: (row) => row.status,
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: 'last_login',
      header: 'Last signed in',
      sortValue: (row) => row.last_login_at ?? '',
      render: (row) =>
        row.last_login_at ? (
          new Date(row.last_login_at).toLocaleDateString()
        ) : (
          <span className="text-slate-400">Never</span>
        ),
    },
    ...(administers
      ? [
          {
            key: 'actions',
            header: <span className="sr-only">Actions</span>,
            align: 'right' as const,
            render: (row: MemberResponse) => <RemoveMember member={row} />,
          },
        ]
      : []),
  ]

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Members</h1>
        <p className="mt-1 text-sm text-slate-500">
          Who can sign in to this organization, and what each of them may do.
        </p>
      </header>

      <DataTable
        rows={data?.items ?? []}
        columns={columns}
        rowKey={(row) => row.id}
        caption="Members"
        loading={isLoading}
        emptyTitle="No members yet"
        emptyDescription="Invite someone to give them access."
      />

      {administers ? <Invitations organizationId={organizationId} /> : null}
    </div>
  )
}

function EmptyPlatformState() {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
      <h3 className="text-sm font-semibold text-slate-900">No organization selected</h3>
      <p className="mx-auto mt-2 max-w-md text-sm text-slate-500">
        Your account belongs to the platform rather than to a customer. Open an organization
        from Platform → Organizations to see its members.
      </p>
    </div>
  )
}

function RoleSelect({ member }: { member: MemberResponse }) {
  const update = useUpdateMember()
  const { notify } = useToast()

  return (
    <select
      value={member.role}
      aria-label={`Role for ${member.email}`}
      disabled={update.isPending}
      onChange={(event) =>
        update.mutate(
          { id: member.id, role: event.target.value },
          {
            onSuccess: () => notify(`${member.name} is now a ${labelFor(event.target.value)}.`),
            onError: (error) =>
              notify(
                // The last-admin guard lands here, and its message is the whole point:
                // "appoint another one first" tells the user what to do next.
                error instanceof ApiError ? error.message : 'Could not change the role.',
                'error',
              ),
          },
        )
      }
      className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm text-slate-700"
    >
      {ROLES.map((role) => (
        <option key={role.value} value={role.value}>
          {role.label}
        </option>
      ))}
    </select>
  )
}

function RemoveMember({ member }: { member: MemberResponse }) {
  const [confirming, setConfirming] = useState(false)
  const remove = useRemoveMember()
  const { notify } = useToast()

  return (
    <>
      <button
        type="button"
        onClick={() => setConfirming(true)}
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50"
      >
        Remove
      </button>
      <ConfirmDialog
        open={confirming}
        title="Remove this member?"
        description={
          <>
            {member.name} loses access immediately. Anything they created stays where it is.
          </>
        }
        resourceName={member.email}
        confirmLabel="Remove"
        busy={remove.isPending}
        onConfirm={() =>
          remove.mutate(member.id, {
            onSuccess: () => {
              notify(`${member.name} removed.`)
              setConfirming(false)
            },
            onError: (error) => {
              notify(error instanceof ApiError ? error.message : 'Could not remove them.', 'error')
              setConfirming(false)
            },
          })
        }
        onCancel={() => setConfirming(false)}
      />
    </>
  )
}

// ---------------------------------------------------------------------------
// invitations
// ---------------------------------------------------------------------------

function Invitations({ organizationId }: { organizationId: string }) {
  const { data, isLoading } = useInvitations()
  const [link, setLink] = useState<string | null>(null)

  const columns: Column<InvitationResponse>[] = [
    { key: 'email', header: 'Email', sortValue: (row) => row.email, render: (row) => row.email },
    {
      key: 'role',
      header: 'Role',
      sortValue: (row) => row.role,
      render: (row) => labelFor(row.role),
    },
    {
      key: 'status',
      header: 'Status',
      sortValue: (row) => row.status,
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: 'expires',
      header: 'Expires',
      sortValue: (row) => row.expires_at,
      render: (row) => new Date(row.expires_at).toLocaleDateString(),
    },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      align: 'right',
      render: (row) => <InvitationActions invitation={row} onLink={setLink} />,
    },
  ]

  return (
    <section className="mt-10">
      <h2 className="text-sm font-semibold text-slate-900">Invitations</h2>
      <p className="mb-4 mt-1 text-sm text-slate-500">
        There is no email delivery yet, so copy the link and send it however you already talk
        to the person. A link works once and expires after seven days.
      </p>

      <InviteForm organizationId={organizationId} onLink={setLink} />

      {link ? <IssuedLink url={link} onDismiss={() => setLink(null)} /> : null}

      <DataTable
        rows={data?.items ?? []}
        columns={columns}
        rowKey={(row) => row.id}
        caption="Pending invitations"
        loading={isLoading}
        emptyTitle="No invitations"
        emptyDescription="Invite someone above and their link will appear here until it is used."
      />
    </section>
  )
}

function InviteForm({
  organizationId,
  onLink,
}: {
  organizationId: string
  onLink: (url: string) => void
}) {
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('org_member')
  const invite = useCreateInvitation(organizationId)
  const { notify } = useToast()

  return (
    <Form
      onSubmit={() => {
        invite.mutate(
          { email, role },
          {
            onSuccess: (issued) => {
              onLink(issued.accept_url)
              setEmail('')
              notify('Invitation created. Copy the link below — it is shown once.')
            },
          },
        )
      }}
      error={invite.error}
      className="mb-4 flex flex-wrap items-start gap-3 rounded-lg border border-slate-200 bg-white p-4"
    >
      <div className="min-w-[16rem] flex-1">
        <Field name="email" label="Email">
          {(props) => (
            <TextInput
              {...props}
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="person@example.com"
            />
          )}
        </Field>
      </div>
      <div>
        <Field name="role" label="Role">
          {(props) => (
            <select
              id={props.id}
              aria-describedby={props.describedBy}
              value={role}
              onChange={(event) => setRole(event.target.value)}
              className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700"
            >
              {ROLES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          )}
        </Field>
      </div>
      <div className="pt-6">
        <SubmitButton busy={invite.isPending} className="w-auto">
          Invite
        </SubmitButton>
      </div>
    </Form>
  )
}

function IssuedLink({ url, onDismiss }: { url: string; onDismiss: () => void }) {
  return (
    <div className="mb-4 rounded-md border border-emerald-200 bg-emerald-50 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-emerald-900">
            Copy this link now — it is not shown again.
          </p>
          <p className="mt-1 break-all font-mono text-xs text-emerald-800">{url}</p>
        </div>
        <div className="flex shrink-0 gap-2">
          <CopyButton value={url} />
          <button
            type="button"
            onClick={onDismiss}
            className="rounded-md border border-emerald-300 bg-white px-2 py-1 text-xs font-medium text-emerald-800"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  )
}

function InvitationActions({
  invitation,
  onLink,
}: {
  invitation: InvitationResponse
  onLink: (url: string) => void
}) {
  const resend = useResendInvitation()
  const revoke = useRevokeInvitation()
  const { notify } = useToast()
  const [confirming, setConfirming] = useState(false)

  return (
    <div className="flex justify-end gap-2">
      {invitation.status === 'accepted' ? null : (
        <button
          type="button"
          disabled={resend.isPending}
          onClick={() =>
            resend.mutate(invitation.id, {
              onSuccess: (issued) => {
                onLink(issued.accept_url)
                notify('A new link was created. The previous one no longer works.')
              },
            })
          }
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
        >
          New link
        </button>
      )}
      <button
        type="button"
        onClick={() => setConfirming(true)}
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50"
      >
        Revoke
      </button>
      <ConfirmDialog
        open={confirming}
        title="Revoke this invitation?"
        description="The link stops working immediately. You can invite the same address again afterwards."
        resourceName={invitation.email}
        confirmLabel="Revoke"
        busy={revoke.isPending}
        onConfirm={() =>
          revoke.mutate(invitation.id, {
            onSuccess: () => {
              notify('Invitation revoked.')
              setConfirming(false)
            },
          })
        }
        onCancel={() => setConfirming(false)}
      />
    </div>
  )
}

function labelFor(role: string): string {
  return ROLES.find((option) => option.value === role)?.label ?? role
}
