import { useEffect, useState } from 'react'

import { useUpdateOrganization } from '@/api/directory'
import { useTemplateDefaults } from '@/api/gateways'
import type { OrganizationResponse } from '@/api/types'
import { Form, SubmitButton } from '@/components/Form'
import { useToast } from '@/components/Toast'
import { TemplateEditor } from '@/pages/TemplateEditor'
import {
  ORGANIZATION_TEMPLATE_NAMES,
  organizationTemplateForm,
  organizationTemplateSettings,
  templatesDiffer,
  type TemplateForm,
} from '@/pages/templates'

/**
 * Settings → Organization → Template defaults (task 105).
 *
 * The same editor as Gateways → Advanced, minus the preview (there is no gateway to
 * preview against) and minus the answer prefix and suffix (those are per endpoint by
 * nature). Applied at creation only, and the section says so: a change here does not
 * rewrite a gateway that already exists.
 *
 * What is stored is the *difference* from the platform defaults, under
 * `settings.template_defaults`, so an organization that only wants a German heading does
 * not also freeze the eight strings it never had an opinion about.
 */
export function TemplateDefaultsSettings({
  organization,
  editable,
}: {
  organization: OrganizationResponse
  editable: boolean
}) {
  const { data: defaults } = useTemplateDefaults()
  const update = useUpdateOrganization(organization.id)
  const { notify } = useToast()
  const [form, setForm] = useState<TemplateForm | null>(null)
  const [saved, setSaved] = useState<TemplateForm | null>(null)

  useEffect(() => {
    if (defaults?.defaults && form === null) {
      const loaded = organizationTemplateForm(organization.settings, defaults.defaults)
      setForm(loaded)
      setSaved(loaded)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaults, organization])

  // Nothing to edit against until the platform defaults have arrived — and nothing at
  // all if they never do: this section must not take the rest of the page down.
  if (!defaults?.defaults || !form || !saved) return null
  const dirty = templatesDiffer(form, saved, ORGANIZATION_TEMPLATE_NAMES)

  return (
    <section
      className="mt-8 rounded-lg border border-slate-200 bg-white p-6"
      data-testid="template-defaults"
    >
      <h2 className="text-sm font-semibold text-slate-900">Template defaults</h2>
      <p className="mb-4 mt-1 max-w-2xl text-sm text-slate-600">
        The text a gateway writes around documents and memory, for the whole organization.{' '}
        <strong className="font-medium">New gateways start from these</strong>; a gateway that
        already exists keeps its own, editable under its Advanced page.
      </p>

      <Form
        onSubmit={() => {
          update.mutate(
            {
              settings: organizationTemplateSettings(
                organization.settings,
                form,
                defaults.defaults,
              ),
            },
            {
              onSuccess: () => {
                setSaved(form)
                notify('Saved. New gateways start from these templates.')
              },
            },
          )
        }}
        error={update.error}
      >
        <fieldset disabled={!editable} className="contents">
          <TemplateEditor
            form={form}
            defaults={defaults}
            names={ORGANIZATION_TEMPLATE_NAMES}
            disabled={!editable}
            onChange={(name, value) =>
              setForm((current) => (current ? { ...current, [name]: value } : current))
            }
          />
        </fieldset>
        {editable ? (
          <div className="mt-2 flex items-center gap-3">
            <SubmitButton busy={update.isPending} className="w-auto">
              Save defaults
            </SubmitButton>
            {dirty ? <span className="text-xs text-amber-700">Unsaved changes.</span> : null}
          </div>
        ) : null}
      </Form>
    </section>
  )
}
