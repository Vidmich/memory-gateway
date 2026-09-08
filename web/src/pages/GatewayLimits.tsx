import { useGatewayLimits } from '@/api/limits'
import type { GatewayLimits, LimitUsage } from '@/api/types'
import { Field, TextInput } from '@/components/Form'
import {
  LIMIT_HINTS,
  LIMIT_LABELS,
  LIMIT_NAMES,
  barTone,
  ceilingNote,
  ceilingSummary,
  limitProblems,
  usageHeadline,
  usageSummary,
  utilization,
  type LimitName,
  type LimitsForm,
  type QuotaForm,
} from '@/pages/limits'

/**
 * Section 6 of the gateway editor: SPEC §11's caps, with what is being spent against them.
 *
 * Four decisions worth stating.
 *
 * **Empty means unlimited, and the placeholder says so.** Every other number on this
 * screen has a default; these have an *absence*, and a blank box with no explanation reads
 * as "not filled in yet" rather than as a deliberate policy.
 *
 * **The bars are live counters, not a chart.** They come from the same Redis buckets a
 * request is checked against, refetched every few seconds, so a bar at 100% and a 429 in a
 * client's log are the same fact. That is why they are rendered from the server's `usage`
 * rather than computed from the form: what is being shown is enforcement, not intent.
 *
 * **The per-end-user block is a second set of the same four fields, not a subdivision.**
 * Both are checked and either refuses, so a per-person cap on an otherwise unlimited
 * gateway is a sensible configuration — and the copy says so, because "per user" next to
 * "per gateway" invites the reading that one is a share of the other.
 *
 * **A ceiling is explained next to the input it changed.** An organization types 5000, the
 * platform enforces 600 because this gateway routes to a global catalog model, and without
 * that sentence the form looks like it discarded the save.
 */
export function LimitsSection({
  gatewayId,
  form,
  onChange,
}: {
  /** Undefined while the gateway is being created: there is nothing to measure yet, and
   *  the bars say so rather than rendering zeroes. */
  gatewayId: string | undefined
  form: LimitsForm
  onChange: (form: LimitsForm) => void
}) {
  const { data: limits } = useGatewayLimits(gatewayId)
  const problems = limitProblems(form)
  const ceiling = ceilingSummary(limits)
  const usage = new Map((limits?.usage ?? []).map((row) => [row.limit, row]))

  const set = (scope: 'gateway' | 'perEndUser', name: LimitName, value: string) =>
    onChange({ ...form, [scope]: { ...form[scope], [name]: value } })

  return (
    <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
      <h2 className="text-sm font-semibold text-slate-900">Limits</h2>
      <p className="mb-4 mt-1 text-sm text-slate-500">
        How much traffic this endpoint will accept. Leave a field empty for no limit —
        which is the default, and means requests are bounded only by what your upstream
        provider allows.
      </p>

      {ceiling ? (
        <p className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          {ceiling}
        </p>
      ) : null}

      {problems.length > 0 ? (
        <ul className="mb-4 space-y-1 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : null}

      <Quota
        legend="This gateway"
        caption="Everything through this endpoint, from every caller together."
        scope="gateway"
        form={form.gateway}
        limits={limits}
        usage={usage}
        set={set}
      />

      <Quota
        legend="Per end user"
        caption="The same four caps applied to one X-Gateway-User at a time. Not a share of the numbers above — both are checked, and either one refuses."
        scope="perEndUser"
        form={form.perEndUser}
        limits={undefined}
        usage={new Map()}
        set={set}
      />

      <p className="mt-4 text-xs text-slate-500">
        Refused requests get a 429 in the OpenAI error shape with <code>Retry-After</code>,
        so an SDK&rsquo;s own retry handles them. Every response carries{' '}
        <code>X-RateLimit-Remaining</code> for the tightest limit, so a client can pace
        itself before it is refused.
      </p>
    </section>
  )
}

function Quota({
  legend,
  caption,
  scope,
  form,
  limits,
  usage,
  set,
}: {
  legend: string
  caption: string
  scope: 'gateway' | 'perEndUser'
  form: QuotaForm
  /** Only the gateway scope has bars — see the note in `app.services.limits_service`. */
  limits: GatewayLimits | undefined
  usage: Map<string, LimitUsage>
  set: (scope: 'gateway' | 'perEndUser', name: LimitName, value: string) => void
}) {
  return (
    <fieldset className="mb-6">
      <legend className="mb-1 text-sm font-medium text-slate-700">{legend}</legend>
      <p className="mb-3 text-xs text-slate-500">{caption}</p>

      {scope === 'gateway' ? (
        <p className="mb-3 text-xs text-slate-500">{usageHeadline(limits)}</p>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2">
        {LIMIT_NAMES.map((name) => {
          const note = ceilingNote(limits, name)
          const bar = usage.get(name)
          return (
            <div key={name}>
              <Field
                name={`limits.${scope}.${name}`}
                label={LIMIT_LABELS[name]}
                hint={note ?? LIMIT_HINTS[name]}
              >
                {(props) => (
                  <TextInput
                    {...props}
                    inputMode="numeric"
                    placeholder="Unlimited"
                    value={form[name]}
                    onChange={(event) => set(scope, name, event.target.value)}
                  />
                )}
              </Field>
              {bar ? <Bar usage={bar} /> : null}
            </div>
          )
        })}
      </div>
    </fieldset>
  )
}

const TONES: Record<string, string> = {
  ok: 'bg-emerald-500',
  warn: 'bg-amber-500',
  full: 'bg-red-500',
}

function Bar({ usage }: { usage: LimitUsage }) {
  const tone = barTone(usage)
  return (
    <div className="mt-1">
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-slate-200"
        role="progressbar"
        aria-label={`${LIMIT_LABELS[usage.limit as LimitName]} used`}
        aria-valuenow={Math.round(utilization(usage) * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={`h-full ${TONES[tone]}`}
          style={{ width: `${Math.round(utilization(usage) * 100)}%` }}
        />
      </div>
      <p className="mt-1 text-xs text-slate-500">{usageSummary(usage)}</p>
    </div>
  )
}
