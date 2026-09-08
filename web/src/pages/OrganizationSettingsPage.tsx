import { useEffect, useState } from 'react'

import { useOrganization, useUpdateOrganization } from '@/api/directory'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { Field, Form, SubmitButton, TextInput } from '@/components/Form'
import { StatusBadge } from '@/components/StatusBadge'
import { useToast } from '@/components/Toast'
import { DistillationSettings } from '@/pages/DistillationSettings'
import { useRetentionCeilings } from '@/api/platform'

/**
 * Settings → Organization (SPEC §13.1).
 *
 * Every role can read it; the inputs are disabled without `org:administer`. Disabled
 * rather than hidden, because a viewer looking for the slug to paste into a gateway URL
 * still needs to see it.
 */
export function OrganizationSettingsPage() {
  const { user, assumedOrganization, refreshUser } = useAuth()
  const organizationId = assumedOrganization?.id ?? user?.organization?.id
  const editable = can(user, 'org:administer')

  const { data: organization, isLoading } = useOrganization(organizationId)
  const update = useUpdateOrganization(organizationId)
  const { notify } = useToast()

  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')

  // Seeded from the server rather than kept in sync with it: an edit in another tab
  // should not overwrite what this user is currently typing.
  useEffect(() => {
    if (!organization) return
    setName(organization.name)
    setSlug(organization.slug)
  }, [organization])

  if (!organizationId) {
    return (
      <div className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
        <h3 className="text-sm font-semibold text-slate-900">No organization selected</h3>
        <p className="mx-auto mt-2 max-w-md text-sm text-slate-500">
          Your account belongs to the platform. Open an organization from Platform →
          Organizations to see its settings.
        </p>
      </div>
    )
  }

  if (isLoading || !organization) return <FullPageSpinner label="Loading the organization…" />

  return (
    <div className="max-w-2xl">
      <header className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Organization</h1>
        <p className="mt-1 flex items-center gap-2 text-sm text-slate-500">
          <span>Profile and defaults for this organization.</span>
          <StatusBadge status={organization.status} />
        </p>
      </header>

      <div className="rounded-lg border border-slate-200 bg-white p-6">
        <Form
          onSubmit={() => {
            update.mutate(
              { name, slug },
              {
                onSuccess: () => {
                  notify('Saved.')
                  // The organization name is in the header and the user menu, so the
                  // cached `/auth/me` has to catch up or the change looks like it failed.
                  void refreshUser()
                },
              },
            )
          }}
          error={update.error}
        >
          <Field name="name" label="Name">
            {(props) => (
              <TextInput
                {...props}
                value={name}
                disabled={!editable}
                onChange={(event) => setName(event.target.value)}
              />
            )}
          </Field>

          <Field
            name="slug"
            label="Slug"
            hint="Appears in gateway URLs. Changing it breaks links that clients already use."
          >
            {(props) => (
              <TextInput
                {...props}
                value={slug}
                disabled={!editable}
                onChange={(event) => setSlug(event.target.value)}
              />
            )}
          </Field>

          {editable ? (
            <SubmitButton busy={update.isPending} className="w-auto">
              Save changes
            </SubmitButton>
          ) : (
            <p className="text-sm text-slate-500">
              Your role can view these settings but not change them.
            </p>
          )}
        </Form>
      </div>

      <DistillationSettings />

      <RetentionCeilings />

      <section className="mt-8 rounded-lg border border-slate-200 bg-white p-6">
        <h2 className="text-sm font-semibold text-slate-900">Stored defaults</h2>
        <p className="mt-2 max-w-xl text-sm text-slate-600">
          Everything this organization has set, as it is stored. The write-back section
          above edits the <code className="font-mono">distillation</code> key; logging
          defaults live on each gateway.
        </p>
        <pre className="mt-3 overflow-x-auto rounded-md bg-slate-50 p-3 font-mono text-xs text-slate-600">
          {JSON.stringify(organization.settings, null, 2)}
        </pre>
      </section>
    </div>
  )
}

/**
 * What the platform allows, and therefore why a gateway's retention may not be the number
 * that was typed into it.
 *
 * Rendered only when a ceiling is actually set. A panel that permanently said "no ceiling"
 * would be a sentence every operator reads once and never again, taking up the space where
 * the exception belongs.
 */
function RetentionCeilings() {
  const { data } = useRetentionCeilings()
  const bodies = data?.max_body_days ?? null
  const metadata = data?.max_metadata_days ?? null
  if (bodies === null && metadata === null) return null

  return (
    <section className="mt-8 rounded-lg border border-slate-200 bg-white p-6">
      <h2 className="text-sm font-semibold text-slate-900">Retention ceilings</h2>
      <p className="mt-2 max-w-xl text-sm text-slate-600">
        This platform caps how long data may be kept. A gateway configured for longer is
        lowered to the ceiling, and the nightly retention pass enforces the same number.
      </p>
      <ul className="mt-3 space-y-1 text-sm text-slate-700">
        {bodies === null ? null : (
          <li>
            Request and response bodies: at most{' '}
            <strong>{bodies} day{bodies === 1 ? '' : 's'}</strong>.
          </li>
        )}
        {metadata === null ? null : (
          <li>
            Metadata rows: at most{' '}
            <strong>{metadata} day{metadata === 1 ? '' : 's'}</strong>.
          </li>
        )}
      </ul>
    </section>
  )
}
