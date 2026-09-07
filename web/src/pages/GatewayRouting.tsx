import { useState } from 'react'
import { Link } from 'react-router-dom'

import type { ModelResponse } from '@/api/types'
import { Field, Select } from '@/components/Form'
import {
  MODES,
  TOTAL_WEIGHT,
  chainProblem,
  expectedShare,
  modeInfo,
  moveRow,
  totalWeight,
  type ChainRow,
} from '@/pages/routing'

/**
 * Section 2 of the gateway editor: where completions are sent, and what happens when
 * that fails (SPEC §13.1).
 *
 * Four decisions are worth stating.
 *
 * **Each mode is described by its failure behaviour, not its mechanism.** "Targets are
 * tried in order" says what the code does; "a 400 is returned straight away because the
 * next target would reject it too" says what will happen to somebody's traffic, which is
 * the thing being chosen between.
 *
 * **Order is changed with buttons, and dragging is the shortcut.** Rows are draggable
 * because SPEC §13.1 asks for it, but a drag-only list cannot be operated from a keyboard
 * or read out by a screen reader, and priority order is exactly the kind of setting
 * somebody changes during an incident. The buttons are the interface; the drag is an
 * accelerator on top of it.
 *
 * **The running total is a number and a bar.** A/B weights that do not add up to 100 are
 * refused by the server, so the form says so before the round trip and disables the save —
 * but the bar is the part that matters, because "70 and 20" reads as fine until you see
 * that a tenth of the traffic has nowhere to go.
 *
 * **The streaming caveat is stated next to the mode that has it.** Failover before the
 * first byte and no failover after it is genuinely surprising behaviour, and a release
 * note is not where somebody will read it.
 */
export function RoutingSection({
  mode,
  rows,
  models,
  onMode,
  onRows,
}: {
  mode: string
  rows: ChainRow[]
  models: readonly ModelResponse[]
  onMode: (mode: string) => void
  onRows: (rows: ChainRow[]) => void
}) {
  const [dragging, setDragging] = useState<number | null>(null)
  const info = modeInfo(mode)
  const problem = chainProblem(mode, rows)
  const multi = mode !== 'single'

  const setRow = (index: number, patch: Partial<ChainRow>) =>
    onRows(rows.map((row, at) => (at === index ? { ...row, ...patch } : row)))

  const add = () =>
    onRows([...rows, { modelId: '', weight: mode === 'ab_split' ? 0 : TOTAL_WEIGHT }])

  const remove = (index: number) => onRows(rows.filter((_, at) => at !== index))

  return (
    <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
      <h2 className="text-sm font-semibold text-slate-900">Routing</h2>
      <p className="mb-4 mt-1 text-sm text-slate-500">
        Where completions are sent, and what happens when that fails.
      </p>

      <Field name="routing_mode" label="Mode" hint={info.failure}>
        {(props) => (
          <Select
            {...props}
            value={mode}
            onChange={(event) => {
              const next = event.target.value
              onMode(next)
              // Switching to a mode that needs a second row shouldn't leave the operator
              // staring at a list that cannot be saved and no obvious way to fix it.
              if (next !== 'single' && rows.length < 2) {
                onRows([...rows, { modelId: '', weight: next === 'ab_split' ? 0 : TOTAL_WEIGHT }])
              }
            }}
          >
            {MODES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        )}
      </Field>

      {mode === 'failover' ? (
        <p className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <strong className="font-semibold">Streaming responses cannot fail over once
          output has begun.</strong>{' '}
          Up to the first token a failed target is replaced silently. After it, the status
          line has already been sent, so the stream ends with an error event and the
          request is logged as failed after stream start.
        </p>
      ) : null}

      <Field
        name="targets"
        label={multi ? 'Targets' : 'Model'}
        hint={
          <>
            Your own models and the shared catalog. Add one under{' '}
            <Link to="/models" className="underline">
              Models
            </Link>
            .
          </>
        }
      >
        {(props) => (
          <div className="space-y-2">
            {rows.length === 0 ? (
              <p className="rounded-md border border-dashed border-slate-300 p-3 text-sm text-slate-500">
                No model — requests to this gateway will fail with a message saying so.
              </p>
            ) : null}

            {rows.map((row, index) => (
              <div
                key={index}
                draggable={multi}
                onDragStart={() => setDragging(index)}
                onDragOver={(event) => event.preventDefault()}
                onDrop={() => {
                  if (dragging !== null) onRows(moveRow(rows, dragging, index))
                  setDragging(null)
                }}
                onDragEnd={() => setDragging(null)}
                className={`rounded-md border p-2 ${
                  dragging === index ? 'border-slate-400 bg-slate-50' : 'border-slate-200'
                }`}
              >
                <div className="flex items-center gap-2">
                  {multi ? (
                    <span
                      aria-hidden="true"
                      className="w-5 shrink-0 text-center text-xs font-medium text-slate-400"
                    >
                      {index + 1}
                    </span>
                  ) : null}
                  <Select
                    id={index === 0 ? props.id : `${props.id}-${index}`}
                    invalid={props.invalid}
                    describedBy={props.describedBy}
                    value={row.modelId}
                    aria-label={multi ? `Target ${index + 1}` : 'Model'}
                    onChange={(event) => setRow(index, { modelId: event.target.value })}
                  >
                    <option value="">Choose a model…</option>
                    {models.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.name}
                        {model.organization_id ? '' : ' (shared)'}
                        {model.enabled ? '' : ' — disabled'}
                      </option>
                    ))}
                  </Select>

                  {multi ? (
                    <div className="flex shrink-0 gap-1">
                      <OrderButton
                        label={`Move target ${index + 1} up`}
                        glyph="↑"
                        disabled={index === 0}
                        onClick={() => onRows(moveRow(rows, index, index - 1))}
                      />
                      <OrderButton
                        label={`Move target ${index + 1} down`}
                        glyph="↓"
                        disabled={index === rows.length - 1}
                        onClick={() => onRows(moveRow(rows, index, index + 1))}
                      />
                      <OrderButton
                        label={`Remove target ${index + 1}`}
                        glyph="×"
                        onClick={() => remove(index)}
                      />
                    </div>
                  ) : null}
                </div>

                {mode === 'ab_split' ? (
                  <div className="mt-2 flex items-center gap-3 pl-7">
                    <input
                      type="range"
                      min={0}
                      max={100}
                      step={1}
                      value={row.weight}
                      aria-label={`Weight for target ${index + 1}`}
                      onChange={(event) => setRow(index, { weight: Number(event.target.value) })}
                      className="h-1 flex-1 accent-slate-900"
                    />
                    <input
                      type="number"
                      min={0}
                      max={100}
                      value={row.weight}
                      aria-label={`Weight percentage for target ${index + 1}`}
                      onChange={(event) => setRow(index, { weight: Number(event.target.value) })}
                      className="w-16 rounded-md border border-slate-300 px-2 py-1 text-sm tabular-nums"
                    />
                    <span className="text-xs text-slate-500">%</span>
                  </div>
                ) : null}
              </div>
            ))}

            {multi || rows.length === 0 ? (
              <button
                type="button"
                onClick={add}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                Add a target
              </button>
            ) : null}

            {mode === 'ab_split' && rows.length > 0 ? <SplitPreview rows={rows} /> : null}

            {problem ? (
              <p role="status" className="text-sm text-amber-700">
                {problem}
              </p>
            ) : null}
          </div>
        )}
      </Field>
    </section>
  )
}

/**
 * The split as it will actually happen, against the 100 it has to add up to.
 *
 * Drawn from `expectedShare` rather than from the raw weights: 70 and 20 is not a 70/20
 * split, it is a 78/22 split the server will refuse, and the bar is where that becomes
 * obvious.
 */
function SplitPreview({ rows }: { rows: readonly ChainRow[] }) {
  const total = totalWeight(rows)
  const shares = expectedShare(rows)
  const ok = total === TOTAL_WEIGHT

  return (
    <div className="rounded-md border border-slate-200 bg-slate-50 p-3">
      <div className="flex items-baseline justify-between text-xs">
        <span className="font-medium text-slate-600">Expected split</span>
        <span className={`tabular-nums ${ok ? 'text-slate-500' : 'font-medium text-amber-700'}`}>
          {total} / {TOTAL_WEIGHT}
        </span>
      </div>
      <div className="mt-1.5 flex h-3 overflow-hidden rounded-full bg-slate-200">
        {rows.map((_, index) => (
          <div
            key={index}
            style={{
              width: `${shares[index] ?? 0}%`,
              backgroundColor: SPLIT_COLOURS[index % SPLIT_COLOURS.length],
            }}
          />
        ))}
      </div>
      <p className="mt-1.5 text-xs text-slate-500">
        {rows.map((_, index) => `${Math.round(shares[index] ?? 0)}%`).join(' · ')}
        {' — '}
        an end user with an id sticks to one variant; anonymous requests are split at
        random.
      </p>
    </div>
  )
}

const SPLIT_COLOURS = ['#2563eb', '#16a34a', '#d97706', '#7c3aed', '#0891b2']

function OrderButton({
  label,
  glyph,
  disabled = false,
  onClick,
}: {
  label: string
  glyph: string
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
    >
      {glyph}
    </button>
  )
}
