import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiClient } from '@/api/client'
import { AppRoutes, makeQueryClient } from '@/App'
import { AuthProvider } from '@/auth/AuthContext'
import { ToastProvider } from '@/components/Toast'
import {
  makeEndUser,
  makeFact,
  makeMemoryHit,
  makeSummary,
  makeUser,
} from '@/test/factories'
import { bodyOf, jsonResponse as json, pathOf } from '@/test/http'

type ServerOptions = {
  user?: ReturnType<typeof makeUser>
  endUsers?: ReturnType<typeof makeEndUser>[]
  facts?: ReturnType<typeof makeFact>[]
  hits?: ReturnType<typeof makeMemoryHit>[]
  purge?: { facts: number; transcripts: number }
  pass?: {
    sessions: number
    inserted: number
    deduped: number
    superseded: number
    evicted: number
    rejected: number
    reason: string | null
  }
}

/**
 * A scripted server, not a mocked client — the real `ApiClient` runs, so a test asserts
 * what actually went on the wire.
 *
 * That matters most for the purge: the difference between erasing somebody's memory and
 * erasing every transcript they ever produced is one query parameter, and the only honest
 * way to check which one the checkbox sends is to read the request.
 */
function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  const endUsers = options.endUsers ?? [makeEndUser()]
  const facts = options.facts ?? [makeFact()]
  const requests: { path: string; method: string; body: Record<string, unknown> }[] = []

  const session = {
    access_token: 'token-1',
    token_type: 'bearer',
    expires_at: new Date(Date.now() + 900_000).toISOString(),
    expires_in: 900,
    user,
  }

  const impl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = pathOf(input)
    const method = init?.method ?? 'GET'
    requests.push({ path, method, body: bodyOf<Record<string, unknown>>(init) })

    if (path === '/api/v1/auth/refresh') return Promise.resolve(json(session))
    if (path === '/api/v1/auth/me') return Promise.resolve(json(user))
    if (path === '/api/v1/auth/logout') return Promise.resolve(new Response(null, { status: 204 }))
    if (path.startsWith('/api/v1/metrics/summary')) return Promise.resolve(json(makeSummary()))

    if (path.endsWith('/distil') && method === 'POST') {
      return Promise.resolve(
        json(
          options.pass ?? {
            sessions: 1,
            inserted: 2,
            deduped: 1,
            superseded: 0,
            evicted: 0,
            rejected: 0,
            reason: null,
          },
        ),
      )
    }
    if (path.includes('/memory/search') && method === 'POST') {
      return Promise.resolve(
        json({ hits: options.hits ?? [makeMemoryHit()], embedding_model: 'hash-bow' }),
      )
    }
    if (path.includes('/memory') && method === 'DELETE') {
      return Promise.resolve(json(options.purge ?? { facts: facts.length, transcripts: 0 }))
    }
    if (path.includes('/memory') && method === 'POST') {
      return Promise.resolve(json(makeFact(bodyOf(init)), 201))
    }
    if (path.includes('/memory') && method === 'GET') {
      // The filters are applied here rather than in the page, because that is where they
      // are applied in production — a page that filtered what it had already fetched would
      // say "no constraints" when it meant "none on this page".
      const query = new URL(path, 'http://x').searchParams
      const kind = query.get('kind')
      const floor = Number(query.get('min_confidence') ?? 0)
      const rows = facts
        .filter((row) => query.get('live_only') !== 'true' || !row.superseded_at)
        .filter((row) => !kind || row.kind === kind)
        .filter((row) => row.confidence >= floor)
      return Promise.resolve(json({ items: rows, next_cursor: null }))
    }
    if (path.startsWith('/api/v1/memory-facts/') && method === 'PATCH') {
      return Promise.resolve(json(makeFact(bodyOf(init))))
    }
    if (path.startsWith('/api/v1/memory-facts/') && method === 'DELETE') {
      return Promise.resolve(new Response(null, { status: 204 }))
    }
    if (path.startsWith('/api/v1/end-users/')) {
      const id = path.split('/')[4]
      return Promise.resolve(json(endUsers.find((row) => row.id === id) ?? endUsers[0]!))
    }
    if (path.startsWith('/api/v1/end-users')) {
      const search = new URL(path, 'http://x').searchParams.get('search') ?? ''
      const rows = search
        ? endUsers.filter((row) => row.external_id.includes(search))
        : endUsers
      return Promise.resolve(json({ items: rows, next_cursor: null }))
    }
    return Promise.resolve(json({ items: [], next_cursor: null }))
  })

  return { impl, requests, user }
}

function renderAt(route: string, server: ReturnType<typeof fakeServer>) {
  const client = new ApiClient(server.impl)
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <AuthProvider client={client}>
        <ToastProvider>
          <MemoryRouter initialEntries={[route]}>
            <AppRoutes />
          </MemoryRouter>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

// ---------------------------------------------------------------------------
// the list
// ---------------------------------------------------------------------------

describe('the end-user list', () => {
  it('shows who has been asking, with their traffic', async () => {
    renderAt('/memory', fakeServer())

    expect(await screen.findByRole('link', { name: 'alice' })).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
  })

  it('labels an identity the gateway derived rather than one the application sent', async () => {
    renderAt(
      '/memory',
      fakeServer({
        endUsers: [
          makeEndUser({ external_id: 'anon:0123456789abcdef', anonymous: true }),
        ],
      }),
    )

    expect(await screen.findByText(/Identified by address/)).toBeInTheDocument()
  })

  it('searches server-side rather than filtering what is already loaded', async () => {
    const user = userEvent.setup()
    const server = fakeServer({
      endUsers: [makeEndUser({ external_id: 'alice' }), makeEndUser({ id: 'eu2', external_id: 'bob' })],
    })
    renderAt('/memory', server)
    await screen.findByRole('link', { name: 'alice' })

    await user.type(screen.getByLabelText('Search end users'), 'bob')

    await screen.findByRole('link', { name: 'bob' })
    expect(
      server.requests.some((request) => request.path.includes('search=bob')),
    ).toBe(true)
  })

  it('tells an empty organization what makes an end user appear', async () => {
    renderAt('/memory', fakeServer({ endUsers: [] }))

    expect(await screen.findByText('No end users yet')).toBeInTheDocument()
    expect(screen.getByText(/X-Gateway-User/)).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// one person's memory
// ---------------------------------------------------------------------------

describe('the memory browser', () => {
  it('lists what the assistant knows', async () => {
    renderAt('/memory/eu1', fakeServer())

    expect(await screen.findByText(/GDPR-compliant answers/)).toBeInTheDocument()
    // Scoped to the list: "constraint" is also one of the kinds the add-a-fact form
    // offers, and a bare query would match the option instead of the badge.
    const list = within(screen.getByLabelText('Memory facts'))
    expect(list.getByText('constraint')).toBeInTheDocument()
    expect(list.getByText('100% confidence')).toBeInTheDocument()
  })

  it('keeps a retracted fact on screen and says it is no longer used', async () => {
    // "Why did it say that last month" is answered by the fact that has since been
    // replaced; a browser showing only live memory could not answer it at all.
    renderAt(
      '/memory/eu1',
      fakeServer({ facts: [makeFact({ superseded_at: '2026-09-01T00:00:00Z' })] }),
    )

    expect(await screen.findByText(/no longer used in prompts/)).toBeInTheDocument()
  })

  it('can hide the retracted ones', async () => {
    const user = userEvent.setup()
    renderAt(
      '/memory/eu1',
      fakeServer({
        facts: [
          makeFact({ id: 'f1', text: 'Still true.' }),
          makeFact({ id: 'f2', text: 'Retracted.', superseded_at: '2026-09-01T00:00:00Z' }),
        ],
      }),
    )
    await screen.findByText('Retracted.')

    await user.click(screen.getByLabelText(/Hide retracted/))

    expect(await screen.findByText('Still true.')).toBeInTheDocument()
    expect(screen.queryByText('Retracted.')).not.toBeInTheDocument()
  })

  it('adds a fact with full confidence, because a person typed it', async () => {
    const user = userEvent.setup()
    const server = fakeServer()
    renderAt('/memory/eu1', server)
    await screen.findByLabelText('Fact')

    await user.type(screen.getByLabelText('Fact'), 'Prefers Python.')
    await user.click(screen.getByRole('button', { name: 'Add fact' }))

    await screen.findByText('Fact added.')
    const posted = server.requests.find(
      (request) => request.method === 'POST' && request.path.endsWith('/memory'),
    )
    expect(posted?.body).toMatchObject({ text: 'Prefers Python.', confidence: 1 })
  })

  it('retracts rather than deleting when somebody says a fact is wrong', async () => {
    const user = userEvent.setup()
    const server = fakeServer()
    renderAt('/memory/eu1', server)
    await screen.findByText(/GDPR-compliant answers/)

    await user.click(screen.getByRole('button', { name: 'Retract' }))

    await screen.findByText('Fact retracted.')
    const patched = server.requests.find((request) => request.method === 'PATCH')
    expect(patched?.body).toEqual({ superseded: true })
  })

  it('offers to restore a fact it has retracted', async () => {
    renderAt(
      '/memory/eu1',
      fakeServer({ facts: [makeFact({ superseded_at: '2026-09-01T00:00:00Z' })] }),
    )

    expect(await screen.findByRole('button', { name: 'Restore' })).toBeInTheDocument()
  })

  it('searches this person’s memory the way a request would', async () => {
    const user = userEvent.setup()
    renderAt('/memory/eu1', fakeServer())
    await screen.findByLabelText('Question')

    await user.type(screen.getByLabelText('Question'), 'storing emails')
    await user.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText('0.71')).toBeInTheDocument()
  })

  it('explains an empty search rather than leaving a blank space', async () => {
    const user = userEvent.setup()
    renderAt('/memory/eu1', fakeServer({ hits: [] }))
    await screen.findByLabelText('Question')

    await user.type(screen.getByLabelText('Question'), 'anything')
    await user.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText(/always included/)).toBeInTheDocument()
  })

  it('warns on the detail screen when the identity was derived from an address', async () => {
    renderAt(
      '/memory/eu1',
      fakeServer({
        endUsers: [makeEndUser({ external_id: 'anon:0123456789abcdef', anonymous: true })],
      }),
    )

    expect(await screen.findByText(/merges everyone behind one network/)).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// erasure
// ---------------------------------------------------------------------------

describe('erasing a memory', () => {
  it('requires the id to be typed, per SPEC §13.2', async () => {
    const user = userEvent.setup()
    renderAt('/memory/eu1', fakeServer())
    await screen.findByRole('button', { name: 'Erase memory' })

    await user.click(screen.getByRole('button', { name: 'Erase memory' }))

    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Erase' })).toBeDisabled()
  })

  it('says what goes and what stays before it happens', async () => {
    const user = userEvent.setup()
    renderAt('/memory/eu1', fakeServer({ endUsers: [makeEndUser({ fact_count: 3 })] }))
    await screen.findByRole('button', { name: 'Erase memory' })

    await user.click(screen.getByRole('button', { name: 'Erase memory' }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/3 facts/)).toBeInTheDocument()
    expect(within(dialog).getByText(/request history stays/)).toBeInTheDocument()
  })

  it('leaves transcripts alone unless the box is ticked', async () => {
    const user = userEvent.setup()
    const server = fakeServer()
    renderAt('/memory/eu1', server)
    await screen.findByRole('button', { name: 'Erase memory' })

    await user.click(screen.getByRole('button', { name: 'Erase memory' }))
    await user.type(screen.getByRole('textbox', { name: /to confirm/ }), 'alice')
    await user.click(screen.getByRole('button', { name: 'Erase' }))

    await screen.findByText(/Removed 1 fact\./)
    const purge = server.requests.find((request) => request.method === 'DELETE')
    expect(purge?.path).not.toContain('include_transcripts')
  })

  it('sends the flag when the box is ticked, and says what it removed', async () => {
    const user = userEvent.setup()
    const server = fakeServer({ purge: { facts: 2, transcripts: 9 } })
    renderAt('/memory/eu1', server)
    await screen.findByRole('button', { name: 'Erase memory' })

    await user.click(screen.getByLabelText(/Also delete every stored request/))
    await user.click(screen.getByRole('button', { name: 'Erase memory' }))
    await user.type(screen.getByRole('textbox', { name: /to confirm/ }), 'alice')
    await user.click(screen.getByRole('button', { name: 'Erase' }))

    expect(await screen.findByText('Removed 2 facts and 9 transcripts.')).toBeInTheDocument()
    const purge = server.requests.find((request) => request.method === 'DELETE')
    expect(purge?.path).toContain('include_transcripts=true')
  })
})

// ---------------------------------------------------------------------------
// capabilities
// ---------------------------------------------------------------------------

describe('a viewer', () => {
  const viewer = makeUser({ role: 'org_viewer', capabilities: ['org:read'] })

  it('can read a memory', async () => {
    renderAt('/memory/eu1', fakeServer({ user: viewer }))

    expect(await screen.findByText(/GDPR-compliant answers/)).toBeInTheDocument()
  })

  it('is not offered the controls that would change it', async () => {
    renderAt('/memory/eu1', fakeServer({ user: viewer }))
    await screen.findByText(/GDPR-compliant answers/)

    expect(screen.queryByRole('button', { name: 'Add fact' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retract' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Erase memory' })).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// task 13: provenance, history, filters, and running a pass by hand
// ---------------------------------------------------------------------------

describe('a distilled memory', () => {
  it('links a fact to the conversation it was learned from', async () => {
    // "Where did it learn that" is the question a surprising fact produces, and the answer
    // has to be a request rather than a shrug.
    renderAt('/memory/eu1', fakeServer({ facts: [makeFact({ source_log_id: 'log-9' })] }))

    const link = await screen.findByRole('link', { name: /Learned from a conversation/ })
    expect(link).toHaveAttribute('href', '/monitoring?request=log-9')
  })

  it('says a hand-written fact was written by hand', async () => {
    renderAt('/memory/eu1', fakeServer())

    expect(await screen.findByText('Added by hand')).toBeInTheDocument()
  })

  it('folds a replaced fact under the one that replaced it, collapsed', async () => {
    const user = userEvent.setup()
    renderAt(
      '/memory/eu1',
      fakeServer({
        facts: [
          makeFact({ id: 'new', text: 'Works in Go.' }),
          makeFact({
            id: 'old',
            text: 'Works in Rust.',
            superseded_at: '2026-09-01T00:00:00Z',
            superseded_by_id: 'new',
          }),
        ],
      }),
    )

    expect(await screen.findByText('Works in Go.')).toBeInTheDocument()
    expect(screen.queryByText('Works in Rust.')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Show what this replaced/ }))

    expect(screen.getByText('Works in Rust.')).toBeInTheDocument()
  })

  it('narrows the list by kind, server-side', async () => {
    const user = userEvent.setup()
    const server = fakeServer({
      facts: [
        makeFact({ id: 'a', kind: 'constraint', text: 'Needs GDPR-compliant answers.' }),
        makeFact({ id: 'b', kind: 'preference', text: 'Prefers terse answers.' }),
      ],
    })
    renderAt('/memory/eu1', server)

    expect(await screen.findByText('Prefers terse answers.')).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Filter by kind'), 'constraint')

    expect(await screen.findByText('Needs GDPR-compliant answers.')).toBeInTheDocument()
    expect(screen.queryByText('Prefers terse answers.')).not.toBeInTheDocument()
    expect(server.requests.some((request) => request.path.includes('kind=constraint'))).toBe(true)
  })

  it('narrows the list by confidence, so a guess can be told from a statement', async () => {
    const user = userEvent.setup()
    const server = fakeServer({
      facts: [
        makeFact({ id: 'a', confidence: 1, text: 'Works in the EU.' }),
        makeFact({ id: 'b', confidence: 0.4, text: 'Might be a manager.' }),
      ],
    })
    renderAt('/memory/eu1', server)

    expect(await screen.findByText('Might be a manager.')).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Filter by confidence'), '0.8')

    expect(await screen.findByText('Works in the EU.')).toBeInTheDocument()
    expect(screen.queryByText('Might be a manager.')).not.toBeInTheDocument()
  })

  it('says the filters are why the list is empty, not that nothing is stored', async () => {
    const user = userEvent.setup()
    renderAt('/memory/eu1', fakeServer({ facts: [makeFact({ kind: 'preference' })] }))

    await screen.findByRole('list', { name: 'Memory facts' })
    await user.selectOptions(screen.getByLabelText('Filter by kind'), 'goal')

    expect(await screen.findByText(/Nothing matches those filters/)).toBeInTheDocument()
  })
})

describe('distil now', () => {
  it('runs a pass and reports what it did', async () => {
    // The feature's worst failure is invisible latency: somebody waits, refreshes, and
    // cannot tell whether the debounce has not fired or the extractor found nothing.
    const user = userEvent.setup()
    const server = fakeServer()
    renderAt('/memory/eu1', server)

    await user.click(await screen.findByRole('button', { name: 'Distil now' }))

    expect(await screen.findByText(/2 new, 1 already known/)).toBeInTheDocument()
    expect(server.requests.some((request) => request.path.endsWith('/distil'))).toBe(true)
  })

  it('distinguishes "nothing new to read" from "nothing was learned"', async () => {
    const user = userEvent.setup()
    renderAt(
      '/memory/eu1',
      fakeServer({
        pass: {
          sessions: 0,
          inserted: 0,
          deduped: 0,
          superseded: 0,
          evicted: 0,
          rejected: 0,
          reason: 'nothing_pending',
        },
      }),
    )

    await user.click(await screen.findByRole('button', { name: 'Distil now' }))

    expect(await screen.findByText(/already been distilled/)).toBeInTheDocument()
  })

  it('is not offered to a role that cannot write memory', async () => {
    renderAt(
      '/memory/eu1',
      fakeServer({ user: makeUser({ role: 'org_viewer', capabilities: ['org:read'] }) }),
    )

    await screen.findByRole('list', { name: 'Memory facts' })
    expect(screen.queryByRole('button', { name: 'Distil now' })).not.toBeInTheDocument()
  })
})
