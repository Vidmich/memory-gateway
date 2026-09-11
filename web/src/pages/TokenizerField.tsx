import type { EffectiveTokenizerResponse, TokenizerSpec } from '@/api/types'
import { Field, Select, TextInput } from '@/components/Form'
import { tokenizerKey } from '@/pages/tokenizers'

/**
 * "Derived unless overridden", as a form control (task 101).
 *
 * The derived value is shown greyed and read-only with its origin, and stays visible when
 * an override is on — the day a derivation is wrong for a new model, the fastest diagnosis
 * is seeing what it *would* have been. The override is a checkbox and two inputs, not a
 * dropdown with a "derived" entry, because clearing the override has to be a distinct act
 * from choosing a value: `null` on the wire is "go back to deriving".
 *
 * Shared by the model editor and the platform embedding panel so the two read identically.
 */
export function TokenizerField({
  namePrefix,
  derived,
  effective,
  value,
  onChange,
  names,
  disabled = false,
}: {
  /** `tokenizer` or `embedding.tokenizer` — the API field, so server errors land here. */
  namePrefix: string
  /** What the table derives for the dialect and model id on screen. */
  derived: TokenizerSpec
  /** What the server says is in effect for the *saved* row, when there is one. */
  effective?: EffectiveTokenizerResponse | null | undefined
  value: TokenizerSpec | null
  onChange: (value: TokenizerSpec | null) => void
  names: readonly string[]
  disabled?: boolean
}) {
  const overriding = value !== null
  //  The server's label wins when it describes the same derivation, because it knows what
  //  the tokenizer *says it is* — a vocabulary that failed to load names itself as the
  //  word fallback, which the table cannot know from here.
  const derivedLabel =
    effective && effective.origin === 'derived' && tokenizerKey(effective.spec) === tokenizerKey(derived)
      ? effective.label
      : `${tokenizerKey(derived)} (derived)`

  const toggle = (on: boolean) => {
    if (!on) {
      onChange(null)
      return
    }
    // Start the override from the derivation, so switching it on changes nothing until
    // somebody picks a different value.
    onChange(derived)
  }

  const setName = (name: string) => {
    if (name === 'approximate') {
      onChange({ name, ratio: value?.ratio ?? derived.ratio ?? 4 })
    } else {
      onChange({ name: name as TokenizerSpec['name'], ratio: null })
    }
  }

  return (
    <div className="mb-4">
      <div className="flex items-baseline justify-between gap-3">
        <span className="block text-sm font-medium text-slate-700">Tokenizer</span>
        <label className="flex items-center gap-2 text-xs text-slate-600">
          <input
            type="checkbox"
            checked={overriding}
            disabled={disabled}
            onChange={(event) => toggle(event.target.checked)}
            className="rounded border-slate-300"
          />
          Override
        </label>
      </div>
      <p
        data-testid={`${namePrefix}-derived`}
        className={`mt-1 font-mono text-sm ${overriding ? 'text-slate-400 line-through' : 'text-slate-600'}`}
      >
        {derivedLabel}
      </p>
      {effective?.degraded ? (
        <p role="status" className="mt-1 text-xs text-amber-700">
          The <span className="font-mono">{effective.spec.name}</span> vocabulary could not be
          loaded in this process, so counts are the word fallback&rsquo;s. Documents cut this
          way say so on their row.
        </p>
      ) : null}

      {overriding ? (
        <div className="mt-2 grid gap-3 sm:grid-cols-2">
          <Field name={`${namePrefix}.name`} label="Encoding">
            {(props) => (
              <Select
                {...props}
                value={value.name}
                disabled={disabled}
                onChange={(event) => setName(event.target.value)}
              >
                {names.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {value.name === 'approximate' ? (
            <Field
              name={`${namePrefix}.ratio`}
              label="Characters per token"
              hint="An estimate, not a vocabulary. The calibration below measures how far off it is; Calibrate replaces it with what the provider's counts imply."
            >
              {(props) => (
                <TextInput
                  {...props}
                  type="number"
                  step="0.1"
                  min={1}
                  max={20}
                  value={value.ratio ?? ''}
                  disabled={disabled}
                  onChange={(event) =>
                    onChange({ name: 'approximate', ratio: Number(event.target.value) })
                  }
                />
              )}
            </Field>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
