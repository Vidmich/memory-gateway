import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { ApiError } from '@/api/client'
import {
  useCreateFact,
  useDeleteFact,
  useEndUser,
  useMemoryFacts,
  usePurgeMemory,
  useSearchMemory,
  useUpdateFact,
} from '@/api/endUsers'
import type { EndUserResponse, MemoryFactResponse, MemorySearchHit } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { can } from '@/auth/capabilities'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { EmptyState } from '@/components/EmptyState'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { Select } from '@/components/Form'
import { useToast } from '@/components/Toast'
import {
  FACT_KINDS,
  KIND_HINTS,
  activity,
  displayName,
  factState,
  formatConfidence,
  formatScore,
  purgeDescription,
  purgeSummary,
  stateLabel,
  type FactKind,
} from '@/pages/endUsers'

/**
 * One person's memory (SPEC §6.5, §13.1): read it, correct it, or erase it.
 *
 * Four decisions shape the screen.
 *
 * **Retracted facts stay on screen, greyed.** The question this page exists to answer is
 * often "why did it say that last month", and the answer is usually the fact that has
 * since been replaced. A browser that showed only live memory could not answer it at all.
 *
 * **Retracting is the primary action; deleting is the secondary one.** Retracting keeps
 * the row and stops it being used, which is what "that is wrong" almost always means.
 * Deleting destroys the evidence, and the only time that is the right answer is an
 * erasure request — which has its own button, at the bottom, with a typed confirmation.
 *
 * **Search is the same search a request runs.** It is the fastest way to see why a fact
 * that obviously answers the question is not being recalled, and a second implementation
 * would be a screen that agrees with itself and disagrees with the request path.
 *
 * **Purge names what stays.** People expect a purge to remove the *person*; it removes
 * what was learned about them and leaves the record that they were here. Finding that out
 * afterwards is finding it out too late, so the dialog says it before.
 */
export function EndUserDetailPage() {
  const { endUserId } = useParams<{ endUserId: string }>()
  const { user } = useAuth()
  const writes = can(user, 'resources:write')
  const [liveOnly, setLiveOnly] = useState(false)

  const { data: endUser, isLoading, isError } = useEndUser(endUserId)
  const { data: page } = useMemoryFacts(endUserId, liveOnly)

  if (isLoading) return <FullPageSpinner label="Loading memory…" />
  if (isError || !endUser) {
    return (
      <EmptyState
        title="No such end user"
        description="It may have been removed, or it belongs to another organization."
        action={
          <Link to="/memory" className="text-sm font-medium text-slate-700 underline">
            Back to memory
          </Link>
        }
      />
    )
  }

  const facts = page?.items ?? []

  return (
    <div>
      <header className="mb-6">
        <Link to="/memory" className="text-sm text-slate-500 hover:underline">
          ← Memory
        </Link>
        <h1 className="mt-1 text-xl font-semibold text-slate-900">{displayName(endUser)}</h1>
        <p className="mt-1 text-sm text-slate-500">{activity(endUser)}</p>
        {endUser.anonymous ? (
          <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            This identity was derived from an API key and an address, not supplied by your
            application. It merges everyone behind one network and splits one person across
            two, so anything remembered here is approximate. Send{' '}
            <code className="font-mono">X-Gateway-User</code> to keep memory about a real
            person.
          </p>
        ) : (
          <p className="mt-1 font-mono text-xs text-slate-500">{endUser.external_id}</p>
        )}
      </header>

      <MemorySearch endUserId={endUser.id} />

      {writes ? <AddFact endUserId={endUser.id} /> : null}

      <section className="mb-8 rounded-lg border border-slate-200 bg-white p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-900">
            What the assistant knows ({endUser.fact_count} in use)
          </h2>
          <label className="flex items-center gap-2 text-xs text-slate-600">
            <input
              type="checkbox"
              checked={liveOnly}
              onChange={(event) => setLiveOnly(event.target.checked)}
              className="rounded border-slate-300"
            />
            Hide retracted and expired
          </label>
        </div>

        {facts.length === 0 ? (
          <p className="rounded-md border border-slate-200 bg-slate-50 p-4 text-sm text-slate-500">
            Nothing stored for this person yet. Add a fact above, and the next request that
            identifies them will carry it.
          </p>
        ) : (
          <ul aria-label="Memory facts" className="space-y-2">
            {facts.map((fact) => (
              <FactRow key={fact.id} fact={fact} writes={writes} />
            ))}
          </ul>
        )}
      </section>

      {writes ? <PurgePanel endUser={endUser} /> : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// one fact
// ---------------------------------------------------------------------------

function FactRow({ fact, writes }: { fact: MemoryFactResponse; writes: boolean }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(fact.text)
  const update = useUpdateFact()
  const remove = useDeleteFact()
  const { notify } = useToast()

  const state = factState(fact)
  const note = stateLabel(state)

  return (
    <li
      className={`rounded-md border p-3 ${
        state === 'live' ? 'border-slate-200 bg-white' : 'border-slate-200 bg-slate-50'
      }`}
    >
      {editing ? (
        <div>
          <label htmlFor={`fact-${fact.id}`} className="sr-only">
            Fact text
          </label>
          <textarea
            id={`fact-${fact.id}`}
            value={text}
            rows={2}
            onChange={(event) => setText(event.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
          />
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={() => {
                void update.mutateAsync({ id: fact.id, body: { text } }).then(() => {
                  setEditing(false)
                  notify('Fact updated.')
                })
              }}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800"
            >
              Save
            </button>
            <button
              type="button"
              onClick={() => {
                setText(fact.text)
                setEditing(false)
              }}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <p
          className={`text-sm ${state === 'live' ? 'text-slate-800' : 'text-slate-500 line-through'}`}
        >
          {fact.text}
        </p>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        <span className="rounded bg-slate-100 px-1.5 py-0.5 font-medium text-slate-700">
          {fact.kind}
        </span>
        <span className="tabular-nums">{formatConfidence(fact.confidence)} confidence</span>
        <span>last seen {new Date(fact.last_seen_at).toLocaleDateString()}</span>
        {note ? <span className="font-medium text-amber-700">{note}</span> : null}
        {writes && !editing ? (
          <span className="ml-auto flex gap-3">
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="font-medium text-slate-600 hover:underline"
            >
              Edit
            </button>
            <button
              type="button"
              onClick={() => {
                const superseded = state !== 'superseded'
                void update
                  .mutateAsync({ id: fact.id, body: { superseded } })
                  .then(() =>
                    notify(superseded ? 'Fact retracted.' : 'Fact back in use.'),
                  )
              }}
              className="font-medium text-slate-600 hover:underline"
            >
              {state === 'superseded' ? 'Restore' : 'Retract'}
            </button>
            <button
              type="button"
              onClick={() => {
                void remove.mutateAsync(fact.id).then(() => notify('Fact deleted.'))
              }}
              className="font-medium text-red-600 hover:underline"
            >
              Delete
            </button>
          </span>
        ) : null}
      </div>
    </li>
  )
}

// ---------------------------------------------------------------------------
// adding
// ---------------------------------------------------------------------------

function AddFact({ endUserId }: { endUserId: string }) {
  const [text, setText] = useState('')
  const [kind, setKind] = useState<FactKind>('fact')
  const [error, setError] = useState<string | null>(null)
  const create = useCreateFact(endUserId)
  const { notify } = useToast()

  const submit = () => {
    setError(null)
    void create
      // Full confidence: a person typed it, and nothing the model infers should
      // outrank that.
      .mutateAsync({ text: text.trim(), kind, confidence: 1.0 })
      .then(() => {
        setText('')
        notify('Fact added.')
      })
      .catch((caught: unknown) => {
        setError(caught instanceof ApiError ? caught.message : 'That could not be saved.')
      })
  }

  return (
    <section className="mb-6 rounded-lg border border-slate-200 bg-white p-5">
      <h2 className="text-sm font-semibold text-slate-900">Add a fact</h2>
      <p className="mb-3 mt-1 text-sm text-slate-500">
        One sentence, in the third person. It goes into every prompt for this person that
        it is relevant to — and, if it is confident and recent, into every prompt at all.
      </p>

      <label htmlFor="new-fact" className="sr-only">
        Fact
      </label>
      <input
        id="new-fact"
        value={text}
        placeholder="Works in the EU and needs GDPR-compliant answers."
        onChange={(event) => setText(event.target.value)}
        className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
      />

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <div className="w-48">
          <label htmlFor="new-fact-kind" className="mb-1 block text-xs font-medium text-slate-700">
            Kind
          </label>
          <Select
            id="new-fact-kind"
            invalid={false}
            describedBy={undefined}
            value={kind}
            onChange={(event) => setKind(event.target.value as FactKind)}
          >
            {FACT_KINDS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </Select>
        </div>
        <button
          type="button"
          disabled={text.trim().length === 0 || create.isPending}
          onClick={submit}
          className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {create.isPending ? 'Saving…' : 'Add fact'}
        </button>
        <p className="flex-1 text-xs text-slate-500">{KIND_HINTS[kind]}</p>
      </div>

      {error ? (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {error}
        </p>
      ) : null}
    </section>
  )
}

// ---------------------------------------------------------------------------
// search
// ---------------------------------------------------------------------------

function MemorySearch({ endUserId }: { endUserId: string }) {
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<MemorySearchHit[] | null>(null)
  const search = useSearchMemory(endUserId)

  return (
    <section className="mb-6 rounded-lg border border-slate-200 bg-slate-50 p-4">
      <h2 className="text-sm font-semibold text-slate-900">Search this memory</h2>
      <p className="mb-3 mt-0.5 text-xs text-slate-500">
        The same search a request runs. If a fact that obviously answers a question does
        not appear here, it will not appear in the prompt either.
      </p>
      <div className="flex gap-2">
        <label htmlFor="memory-search" className="sr-only">
          Question
        </label>
        <input
          id="memory-search"
          value={query}
          placeholder="How should I store customer emails?"
          onChange={(event) => setQuery(event.target.value)}
          className="min-w-0 flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
        />
        <button
          type="button"
          disabled={query.trim().length === 0 || search.isPending}
          onClick={() => {
            void search.mutateAsync(query.trim()).then((result) => setHits(result.hits))
          }}
          className="shrink-0 rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {search.isPending ? 'Searching…' : 'Search'}
        </button>
      </div>

      {hits === null ? null : hits.length === 0 ? (
        <p className="mt-3 text-sm text-slate-600">
          Nothing matched. A question can still be answered from a fact that is always
          included — recent, confident facts go into every prompt whatever the question.
        </p>
      ) : (
        <ol className="mt-3 space-y-1">
          {hits.map((hit) => (
            <li
              key={hit.fact.id}
              className="flex items-baseline gap-2 rounded-md border border-slate-200 bg-white p-2 text-xs"
            >
              <span className="rounded bg-slate-900 px-1.5 py-0.5 font-mono text-white tabular-nums">
                {formatScore(hit.score)}
              </span>
              <span className="text-slate-700">{hit.fact.text}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// erasure
// ---------------------------------------------------------------------------

function PurgePanel({ endUser }: { endUser: EndUserResponse }) {
  const [open, setOpen] = useState(false)
  const [includeTranscripts, setIncludeTranscripts] = useState(false)
  const purge = usePurgeMemory(endUser.id)
  const { notify } = useToast()

  return (
    <section className="rounded-lg border border-red-200 bg-red-50 p-5">
      <h2 className="text-sm font-semibold text-red-900">Erase this person&apos;s memory</h2>
      <p className="mt-1 text-sm text-red-800">
        Removes every fact and its vector. Their request history stays, so past traffic
        still shows who it belonged to.
      </p>
      <label className="mt-3 flex items-start gap-2 text-sm text-red-800">
        <input
          type="checkbox"
          checked={includeTranscripts}
          onChange={(event) => setIncludeTranscripts(event.target.checked)}
          className="mt-0.5 rounded border-red-300"
        />
        <span>
          Also delete every stored request and response body from their conversations.
          The metadata rows stay, so the traffic charts do not change.
        </span>
      </label>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-4 rounded-md bg-red-600 px-3 py-2 text-sm font-medium text-white hover:bg-red-500"
      >
        Erase memory
      </button>

      <ConfirmDialog
        open={open}
        title="Erase this memory?"
        description={purgeDescription(endUser, includeTranscripts)}
        resourceName={endUser.external_id}
        confirmLabel="Erase"
        busy={purge.isPending}
        onCancel={() => setOpen(false)}
        onConfirm={() => {
          void purge.mutateAsync(includeTranscripts).then((result) => {
            setOpen(false)
            notify(purgeSummary(result))
          })
        }}
      />
    </section>
  )
}
