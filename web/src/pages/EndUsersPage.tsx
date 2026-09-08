import { useState } from 'react'
import { Link } from 'react-router-dom'

import { useEndUsers } from '@/api/endUsers'
import type { EndUserResponse } from '@/api/types'
import { DataTable, type Column } from '@/components/DataTable'
import { displayName, emptyMemoryHint } from '@/pages/endUsers'

/**
 * The memory browser's first screen (SPEC §13.1): who has been asking.
 *
 * These are not users of this product — they are the people on the far side of a
 * customer's own application — and the screen is built around the one question an
 * operator arrives with: *why did the assistant say that to them?*
 *
 * Three decisions.
 *
 * **The search box is the primary control, not the table.** A busy gateway produces
 * thousands of end users and nobody browses them; they arrive holding an id out of a
 * support ticket. So the box is above the table, filters server-side on `external_id`,
 * and the table is what is left after it.
 *
 * **"Facts" is the column that matters, and zero has three meanings.** A person seen once
 * has nothing yet; a person whose traffic is anonymous will never have anything until the
 * integration sends an identity; a person with real traffic and no facts is waiting for
 * distillation. Those need different actions, so the empty case says which it is instead
 * of showing a bare 0.
 *
 * **Anonymous rows are labelled.** A page full of `anon:…` is not a privacy feature
 * working as intended — it is usually a customer who has not wired `X-Gateway-User` — and
 * the screen should say so where somebody will read it.
 */
export function EndUsersPage() {
  const [search, setSearch] = useState('')
  const [cursor, setCursor] = useState<string | null>(null)
  const [previous, setPrevious] = useState<(string | null)[]>([])

  const { data, isLoading } = useEndUsers(search, cursor)
  const rows = data?.items ?? []

  const columns: Column<EndUserResponse>[] = [
    {
      key: 'who',
      header: 'End user',
      sortValue: (row) => displayName(row),
      render: (row) => (
        <div className="min-w-0">
          <Link
            to={`/memory/${row.id}`}
            className="font-medium text-slate-900 hover:underline"
          >
            {displayName(row)}
          </Link>
          {row.label ? (
            <div className="truncate font-mono text-xs text-slate-500">{row.external_id}</div>
          ) : null}
          {row.anonymous ? (
            <div className="text-xs text-amber-700">
              Identified by address, not by your application
            </div>
          ) : null}
        </div>
      ),
    },
    {
      key: 'facts',
      header: 'Facts',
      align: 'right',
      sortValue: (row) => row.fact_count,
      render: (row) => <FactsCell endUser={row} />,
    },
    {
      key: 'requests',
      header: 'Requests',
      align: 'right',
      sortValue: (row) => row.request_count,
      render: (row) => (
        <span className="text-sm text-slate-700">{row.request_count.toLocaleString()}</span>
      ),
    },
    {
      key: 'seen',
      header: 'Last seen',
      sortValue: (row) => row.last_seen_at,
      render: (row) => (
        <span className="text-sm text-slate-500">
          {new Date(row.last_seen_at).toLocaleString()}
        </span>
      ),
    },
  ]

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Memory</h1>
        <p className="mt-1 text-sm text-slate-500">
          The people your gateways have answered, and what the assistant remembers about
          each of them. Open one to read, correct, or erase it.
        </p>
      </header>

      <div className="mb-4">
        <label htmlFor="end-user-search" className="sr-only">
          Search end users
        </label>
        <input
          id="end-user-search"
          value={search}
          placeholder="Search by the id your application sends"
          onChange={(event) => {
            setSearch(event.target.value)
            setCursor(null)
            setPrevious([])
          }}
          className="w-full max-w-md rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
        />
      </div>

      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        caption="End users"
        loading={isLoading}
        emptyTitle={search ? 'Nobody matches that' : 'No end users yet'}
        emptyDescription={
          search
            ? 'Search matches the id your application sends as X-Gateway-User or as the OpenAI “user” field.'
            : 'An end user appears the first time a request identifies one. Send X-Gateway-User (or the OpenAI “user” field) from your application, and the people using it show up here.'
        }
      />

      <div className="mt-4 flex items-center justify-between">
        <button
          type="button"
          disabled={previous.length === 0}
          onClick={() => {
            const history = [...previous]
            setCursor(history.pop() ?? null)
            setPrevious(history)
          }}
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
        >
          Previous
        </button>
        <button
          type="button"
          disabled={!data?.next_cursor}
          onClick={() => {
            setPrevious((history) => [...history, cursor])
            setCursor(data?.next_cursor ?? null)
          }}
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
        >
          Next
        </button>
      </div>
    </div>
  )
}

function FactsCell({ endUser }: { endUser: EndUserResponse }) {
  const hint = emptyMemoryHint(endUser)
  if (!hint) {
    return <span className="text-sm font-medium text-slate-900">{endUser.fact_count}</span>
  }
  return (
    <span className="text-xs text-slate-500" title={hint}>
      none
    </span>
  )
}
