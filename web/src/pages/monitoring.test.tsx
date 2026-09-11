import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiClient } from '@/api/client'
import { resolveRange } from '@/api/monitoring'
import { AppRoutes, makeQueryClient } from '@/App'
import { AuthProvider } from '@/auth/AuthContext'
import {
  latencySeries,
  modelSlices,
  retrievalSeries,
  pointsOf,
  seriesFrom,
  statusSeries,
  intervalLabel,
  colorFor,
} from '@/components/chartSeries'
import { ToastProvider } from '@/components/Toast'
import { asCurl, contentOf, countInjected } from '@/pages/requestDetail'
import {
  makeGateway,
  makeRequestDetail,
  makeRequestLog,
  makeSeries,
  makeSummarizationHealth,
  makeSummary,
  makeTemplateUse,
  makeUser,
} from '@/test/factories'
import { jsonResponse as json, pathOf } from '@/test/http'

type ServerOptions = {
  user?: ReturnType<typeof makeUser>
  summary?: ReturnType<typeof makeSummary>
  logs?: ReturnType<typeof makeRequestLog>[]
  detail?: ReturnType<typeof makeRequestDetail>
  nextCursor?: string | null
  /** Task 102: what `GET /summarization/health` answers. */
  summarization?: ReturnType<typeof makeSummarizationHealth>
  /** Task 105: what `GET /logs/templates` answers. */
  templates?: ReturnType<typeof makeTemplateUse>[]
}

/**
 * A scripted server, so the assertions are about what the screen does with real
 * responses. The query strings are recorded because half of what this page is *for* is
 * turning a filter into the right request.
 */
function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  const logs = options.logs ?? [makeRequestLog()]
  const requests: { path: string; method: string }[] = []

  const session = {
    access_token: 'token-1',
    token_type: 'bearer',
    expires_at: new Date(Date.now() + 900_000).toISOString(),
    expires_in: 900,
    user,
  }

  const impl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = pathOf(input)
    requests.push({ path, method: init?.method ?? 'GET' })

    if (path === '/api/v1/auth/refresh') return Promise.resolve(json(session))
    if (path === '/api/v1/auth/me') return Promise.resolve(json(user))
    if (path.startsWith('/api/v1/metrics/summary')) {
      return Promise.resolve(json(options.summary ?? makeSummary()))
    }
    if (path.startsWith('/api/v1/metrics/timeseries')) {
      return Promise.resolve(json(makeSeries()))
    }
    if (path.startsWith('/api/v1/logs/templates')) {
      return Promise.resolve(json({ items: options.templates ?? [] }))
    }
    if (path.startsWith('/api/v1/logs/')) {
      return Promise.resolve(json(options.detail ?? makeRequestDetail()))
    }
    if (path.startsWith('/api/v1/logs')) {
      return Promise.resolve(json({ items: logs, next_cursor: options.nextCursor ?? null }))
    }
    if (path.startsWith('/api/v1/gateways')) {
      return Promise.resolve(json({ items: [makeGateway()], next_cursor: null }))
    }
    if (path.startsWith('/api/v1/summarization/health')) {
      return Promise.resolve(json(options.summarization ?? makeSummarizationHealth()))
    }

    throw new Error(`unexpected ${path}`)
  })

  return { client: new ApiClient(impl), requests }
}

function renderAt(client: ApiClient, path = '/monitoring') {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <AuthProvider client={client}>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <AppRoutes />
          </MemoryRouter>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

const queries = (requests: { path: string }[], prefix: string) =>
  requests.filter((request) => request.path.startsWith(prefix)).map((request) => request.path)

// ---------------------------------------------------------------------------
// the window
// ---------------------------------------------------------------------------

describe('the time range', () => {
  it('is rounded to the minute so it can be a stable query key', () => {
    // Without the rounding, every render is a new `from` and React Query refetches
    // forever — which looks like a server problem and is not one.
    const at = new Date('2026-09-07T12:34:56.789Z')

    const window = resolveRange('1h', at)

    expect(window.to).toBe('2026-09-07T12:34:00.000Z')
    expect(window.from).toBe('2026-09-07T11:34:00.000Z')
  })

  it('is the same value when resolved twice in the same minute', () => {
    const first = resolveRange('24h', new Date('2026-09-07T12:34:01Z'))
    const second = resolveRange('24h', new Date('2026-09-07T12:34:59Z'))

    expect(first).toEqual(second)
  })

  it('spans the range it names', () => {
    const at = new Date('2026-09-07T12:00:00Z')

    const window = resolveRange('30d', at)

    const days = (Date.parse(window.to) - Date.parse(window.from)) / 86_400_000
    expect(days).toBe(30)
  })
})

// ---------------------------------------------------------------------------
// the screen
// ---------------------------------------------------------------------------

describe('the monitoring screen', () => {
  it('shows the traffic summary', async () => {
    const { client } = fakeServer()
    renderAt(client)

    expect(await screen.findByText('120')).toBeInTheDocument()
    expect(screen.getByText('5.0%')).toBeInTheDocument()
    expect(screen.getByText('220 ms')).toBeInTheDocument()
  })

  it('shows an em dash where nothing was measured, not a zero', async () => {
    // A "0 ms" first-token time claims something that never happened. Every gateway in
    // this fixture is non-streaming, so the honest answer is that there is no number.
    const { client } = fakeServer()
    renderAt(client)

    await screen.findByText('120')
    const card = screen.getByText('p95 first token').closest('div')!
    expect(within(card).getByText('—')).toBeInTheDocument()
  })

  it('lists requests with their status and latency', async () => {
    const { client } = fakeServer({
      logs: [makeRequestLog({ status_code: 504, error_code: 'upstream_timeout' })],
    })
    renderAt(client)

    expect(await screen.findByText('504')).toBeInTheDocument()
    const table = screen.getByRole('table')
    // Scoped to the table: `upstream_timeout` is also a bar in the error taxonomy above
    // it, which is the chart working rather than a duplicate.
    expect(within(table).getByText('upstream_timeout')).toBeInTheDocument()
    expect(within(table).getByText('240 ms')).toBeInTheDocument()
  })

  it('sends the filters it is showing', async () => {
    const person = userEvent.setup()
    const { client, requests } = fakeServer()
    renderAt(client)

    await screen.findByText('120')
    await person.selectOptions(screen.getByLabelText('Status'), '5xx')

    await waitFor(() => {
      expect(
        queries(requests, '/api/v1/logs').some((path) => path.includes('status_class=5xx')),
      ).toBe(true)
    })
  })

  it('sends the nothing-cited filter as a boolean (task 100)', async () => {
    const person = userEvent.setup()
    const { client, requests } = fakeServer()
    renderAt(client)

    await screen.findByText('120')
    await person.click(screen.getByLabelText('Nothing cited'))

    await waitFor(() => {
      expect(queries(requests, '/api/v1/logs').some((path) => path.includes('uncited=true'))).toBe(
        true,
      )
    })
  })

  it('offers a Template filter only once two wordings have been seen (task 105)', async () => {
    const { client } = fakeServer({ templates: [makeTemplateUse()] })
    renderAt(client)

    await screen.findByText('120')
    expect(screen.queryByTestId('template-filter')).not.toBeInTheDocument()
  })

  it('sends the chosen template fingerprint, listed with first-seen dates (task 105)', async () => {
    const person = userEvent.setup()
    const { client, requests } = fakeServer({
      templates: [
        makeTemplateUse({ fingerprint: 'aaaaaaaaaaaaaaaa', first_seen: '2026-09-02T09:30:00Z' }),
        makeTemplateUse({ fingerprint: 'bbbbbbbbbbbbbbbb', requests: 3 }),
      ],
    })
    renderAt(client)

    const filter = await screen.findByTestId('template-filter')
    const options = within(filter).getAllByRole('option')
    expect(options.map((option) => option.textContent)).toEqual([
      'Any wording',
      expect.stringMatching(/^aaaaaaaa · first seen .* · 12 requests$/),
      expect.stringMatching(/^bbbbbbbb · first seen .* · 3 requests$/),
    ])
    await person.selectOptions(filter, 'bbbbbbbbbbbbbbbb')

    await waitFor(() => {
      expect(
        queries(requests, '/api/v1/logs?').some((path) =>
          path.includes('template_fingerprint=bbbbbbbbbbbbbbbb'),
        ),
      ).toBe(true)
    })
  })

  it('asks for a different window when the range changes', async () => {
    const person = userEvent.setup()
    const { client, requests } = fakeServer()
    renderAt(client)

    await screen.findByText('120')
    const before = queries(requests, '/api/v1/metrics/summary').length
    await person.click(screen.getByRole('button', { name: 'Last hour' }))

    await waitFor(() => {
      expect(queries(requests, '/api/v1/metrics/summary').length).toBeGreaterThan(before)
    })
  })

  it('says which bucket width the server chose', async () => {
    // The client asked for a range; the server decided the resolution. Labelling the
    // chart from the request rather than the response would be a chart that lies.
    const { client } = fakeServer()
    renderAt(client)

    expect(await screen.findByText('5-minute buckets')).toBeInTheDocument()
  })

  it('can turn live tail off', async () => {
    const person = userEvent.setup()
    const { client } = fakeServer()
    renderAt(client)

    await screen.findByText('120')
    const toggle = screen.getByLabelText(/live tail/i)
    expect(toggle).toBeChecked()

    await person.click(toggle)

    expect(toggle).not.toBeChecked()
  })

  it('offers a next page only when the server said there is one', async () => {
    const { client } = fakeServer({ nextCursor: 'cursor-2' })
    renderAt(client)

    await screen.findByText('120')
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()
  })

  it('explains an empty window rather than showing an empty box', async () => {
    const { client } = fakeServer({ logs: [], summary: makeSummary({ requests: 0, errors: 0 }) })
    renderAt(client)

    expect(await screen.findByText('No requests in this window')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// the drawer
// ---------------------------------------------------------------------------

describe('the summarization panel (task 102)', () => {
  it('shows what summarizing cost in the window, by model and by connector', async () => {
    const { client, requests } = fakeServer()
    renderAt(client)

    const panel = await screen.findByTestId('summarization-panel')
    expect(panel).toHaveTextContent('12 summarized, 1 failed — 25,800 tokens.')
    expect(within(panel).getByText('cheap-summarizer')).toBeInTheDocument()
    expect(within(panel).getByText('Product docs')).toBeInTheDocument()
    // The page's own window, not a window of its own.
    const window = resolveRange('24h')
    await waitFor(() => {
      const [call] = queries(requests, '/api/v1/summarization/health')
      expect(call).toContain(`from=${encodeURIComponent(window.from)}`)
    })
  })

  it('says how many documents are parked on a cap', async () => {
    const { client } = fakeServer({
      summarization: makeSummarizationHealth({
        waiting_documents: 7,
        waiting: [{ connector_id: 'c1', name: 'Product docs', documents: 7 }],
      }),
    })
    renderAt(client)

    const panel = await screen.findByTestId('summarization-panel')
    expect(within(panel).getByRole('status')).toHaveTextContent(
      /7 documents waiting on the summarization cap across 1 connector/,
    )
  })
})

describe('the request drawer', () => {
  /** Click the row, not the model's bar in the traffic chart above it. */
  const openRow = async (person: ReturnType<typeof userEvent.setup>) => {
    const table = await screen.findByRole('table')
    // `find`, not `get`: the table renders its header before the first page arrives.
    await person.click(await within(table).findByText('acme-gpt'))
    return screen.findByRole('dialog', { name: 'Request details' })
  }

  const openDrawer = async () => {
    const person = userEvent.setup()
    const { client } = fakeServer()
    renderAt(client)
    return { person, dialog: await openRow(person) }
  }

  it('opens on a row click and shows the transcript', async () => {
    const { dialog } = await openDrawer()

    expect(within(dialog).getByText('the answer')).toBeInTheDocument()
    expect(within(dialog).getAllByText('what is the answer').length).toBeGreaterThan(0)
  })

  it('shows the template fingerprint beside the model name (task 105)', async () => {
    const { dialog } = await openDrawer()

    expect(within(dialog).getByTestId('drawer-template-fingerprint')).toHaveTextContent('d0d0d0d0')
    expect(within(dialog).getByText('Templates')).toBeInTheDocument()
    expect(within(dialog).getByText('d0d0d0d0d0d0d0d0')).toBeInTheDocument()
  })

  it('marks what the gateway added to the prompt', async () => {
    // The single most common question this drawer answers: what did the provider see
    // that the caller did not send?
    const { dialog } = await openDrawer()

    expect(within(dialog).getByText('added by the gateway')).toBeInTheDocument()
    expect(within(dialog).getByText('Be concise.')).toBeInTheDocument()
  })

  it('shows a timing waterfall that adds up to the total', async () => {
    const { dialog } = await openDrawer()

    expect(within(dialog).getByText('Gateway overhead')).toBeInTheDocument()
    expect(within(dialog).getAllByText('240 ms').length).toBeGreaterThan(0)
  })

  it('closes on Escape', async () => {
    const { person, dialog } = await openDrawer()
    expect(dialog).toBeInTheDocument()

    await person.keyboard('{Escape}')

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Request details' })).not.toBeInTheDocument()
    })
  })

  it('says why a body is missing when the queue dropped it', async () => {
    const person = userEvent.setup()
    const { client } = fakeServer({
      detail: makeRequestDetail({
        log: makeRequestLog({ bodies_omitted: 'queue_pressure' }),
        transcript: null,
      }),
    })
    renderAt(client)
    const dialog = await openRow(person)

    expect(within(dialog).getAllByText(/log queue was saturated/).length).toBeGreaterThan(0)
  })

  it('says the toggle is off when nothing dropped the body', async () => {
    // "Not captured" and "the queue was full" need different fixes, so they read
    // differently. An empty panel would say neither.
    const person = userEvent.setup()
    const { client } = fakeServer({
      detail: makeRequestDetail({ transcript: null }),
    })
    renderAt(client)
    const dialog = await openRow(person)

    expect(within(dialog).getAllByText(/switched off for this gateway/).length).toBeGreaterThan(0)
  })
})

// ---------------------------------------------------------------------------
// the pure helpers
// ---------------------------------------------------------------------------

describe('the routing timeline', () => {
  const withAttempts = () =>
    makeRequestDetail({
      log: makeRequestLog({ model_name: 'acme-spare' }),
      failover_attempts: [
        {
          target_id: 'mo1',
          model_name: 'acme-gpt',
          status: 503,
          error_code: 'upstream_error',
          latency_ms: 412,
          retryable: true,
        },
        {
          target_id: 'mo2',
          model_name: 'acme-spare',
          status: 200,
          error_code: null,
          latency_ms: 640,
          retryable: false,
        },
      ],
    })

  const open = async (detail: ReturnType<typeof makeRequestDetail>) => {
    const person = userEvent.setup()
    const { client } = fakeServer({ detail })
    renderAt(client)
    const table = await screen.findByRole('table')
    await person.click(await within(table).findByText('acme-gpt'))
    return screen.findByRole('dialog', { name: 'Request details' })
  }

  it('draws every attempt when more than one target was involved', async () => {
    const dialog = await open(withAttempts())

    expect(within(dialog).getByText('503')).toBeInTheDocument()
    expect(within(dialog).getByText('upstream_error')).toBeInTheDocument()
    expect(within(dialog).getByText('412 ms')).toBeInTheDocument()
    expect(within(dialog).getByText('640 ms')).toBeInTheDocument()
  })

  it('says so plainly when one target answered', async () => {
    // An empty list is information, not a gap: it means nothing had to be retried.
    const dialog = await open(makeRequestDetail())

    expect(within(dialog).getByText(/served on the first attempt/)).toBeInTheDocument()
  })

  it('explains a stream that could not be failed over', async () => {
    const dialog = await open(
      makeRequestDetail({
        log: makeRequestLog({
          status_code: 200,
          error_code: 'stream_failed',
          failed_after_stream_start: true,
        }),
      }),
    )

    expect(
      within(dialog).getByText(/failed after the first chunk had reached the client/),
    ).toBeInTheDocument()
  })

  it('names the parameters the dialect could not carry', async () => {
    // The request succeeded, so this row is the only place that says a parameter the
    // caller set was never sent (SPEC 8.3).
    const dialog = await open(
      makeRequestDetail({
        log: makeRequestLog({ model_name: 'claude', dropped_params: ['presence_penalty', 'seed'] }),
      }),
    )

    expect(within(dialog).getByText('presence_penalty, seed')).toBeInTheDocument()
    expect(within(dialog).getByText(/were not sent/)).toBeInTheDocument()
  })

  it('says nothing when everything the caller asked for went out', async () => {
    const dialog = await open(makeRequestDetail())

    expect(within(dialog).queryByText(/were not sent/)).not.toBeInTheDocument()
  })

  describe('the memory panel', () => {
    const injected = {
      id: 'ch1',
      score: 0.71,
      document_id: 'd1',
      source_name: 'handbook.md',
      page_or_section: 'p. 12',
      chunk_index: 0,
      injected: true,
    }
    const dropped = {
      ...injected,
      id: 'ch2',
      score: 0.44,
      source_name: 'pricing.md',
      injected: false,
      dropped: 'doc_max_tokens',
    }

    it('shows each retrieved chunk with its score and source', async () => {
      const dialog = await open(
        makeRequestDetail({
          log: makeRequestLog({ latency_retrieval_ms: 18, memory_tokens: 140 }),
          retrieved_chunk_ids: [injected],
        }),
      )

      expect(within(dialog).getByText('0.71')).toBeInTheDocument()
      expect(within(dialog).getByText(/handbook\.md/)).toBeInTheDocument()
      expect(within(dialog).getByText(/1 of 1 chunk injected/)).toBeInTheDocument()
      expect(within(dialog).getByText(/140 tokens/)).toBeInTheDocument()
    })

    it('marks the chunks the answer cited, and counts the handles that named nothing', async () => {
      // Task 100. The record joins two lists: what went in, and which of those the
      // answer's handles pointed at. A handle that pointed at nothing is a count, not a
      // row — there is no chunk to draw.
      const second = { ...injected, id: 'ch3', score: 0.6, source_name: 'pricing.md' }
      const dialog = await open(
        makeRequestDetail({
          log: makeRequestLog({ latency_retrieval_ms: 18, citations_unresolved: 1 }),
          retrieved_chunk_ids: [injected, second],
          cited_chunk_ids: ['ch3'],
        }),
      )

      expect(within(dialog).getByText(/1 of 2 cited by the answer/)).toBeInTheDocument()
      expect(within(dialog).getByText(/1 handle pointed at nothing/)).toBeInTheDocument()
      const rows = within(dialog).getAllByRole('listitem')
      const cited = rows.find((row) => row.textContent?.includes('pricing.md'))
      const uncited = rows.find((row) => row.textContent?.includes('handbook.md'))
      expect(cited?.textContent).toContain('cited')
      expect(cited?.textContent).toContain('[2]')
      expect(uncited?.textContent).not.toContain('cited')
      expect(uncited?.textContent).toContain('[1]')
    })

    it('says why a chunk was dropped, in words rather than a field name', async () => {
      // "over the token budget" and "no room in the context window" need different
      // actions, so the reason is rendered rather than a generic "dropped".
      const dialog = await open(
        makeRequestDetail({
          log: makeRequestLog({ latency_retrieval_ms: 18 }),
          retrieved_chunk_ids: [injected, dropped],
        }),
      )

      expect(within(dialog).getByText(/over this gateway/)).toBeInTheDocument()
    })

    it('distinguishes finding nothing from never having looked', async () => {
      const searched = await open(
        makeRequestDetail({
          log: makeRequestLog({ latency_retrieval_ms: 22 }),
          retrieved_chunk_ids: [],
        }),
      )
      expect(within(searched).getByText(/found nothing above/)).toBeInTheDocument()
    })

    it('says so when retrieval never ran at all', async () => {
      const dialog = await open(
        makeRequestDetail({ log: makeRequestLog({ latency_retrieval_ms: null }) }),
      )

      expect(within(dialog).getByText(/did not run for this request/)).toBeInTheDocument()
    })

    it('renders a record written before the injected flag existed', async () => {
      // The column is jsonb and its shape has moved once already. A row from an older
      // build must render as much as it can rather than blank the panel.
      const dialog = await open(
        makeRequestDetail({
          log: makeRequestLog({ latency_retrieval_ms: 9 }),
          retrieved_chunk_ids: [{ id: 'old', score: 0.5 }],
        }),
      )

      expect(within(dialog).getByText('0.50')).toBeInTheDocument()
      expect(within(dialog).getByText('(unknown document)')).toBeInTheDocument()
    })
  })

  describe('the recalled facts', () => {
    const fact = {
      id: 'f1',
      text: 'Works in the EU and needs GDPR-compliant answers.',
      kind: 'constraint',
      score: 0.62,
      confidence: 1,
      always: false,
      injected: true,
    }

    it('shows what the gateway knew about the person asking', async () => {
      const dialog = await open(
        makeRequestDetail({
          log: makeRequestLog({ latency_retrieval_ms: 18, end_user_id: 'eu1' }),
          retrieved_fact_ids: [fact],
        }),
      )

      expect(within(dialog).getByText(/GDPR-compliant answers/)).toBeInTheDocument()
      expect(within(dialog).getByText('0.62')).toBeInTheDocument()
      expect(
        within(dialog).getByText(/1 of 1 fact about this end user injected/),
      ).toBeInTheDocument()
    })

    it('marks a fact that was included regardless of the question', async () => {
      // "Why is this in my prompt" has two answers, and they are not the same answer.
      const dialog = await open(
        makeRequestDetail({
          log: makeRequestLog({ latency_retrieval_ms: 18, end_user_id: 'eu1' }),
          retrieved_fact_ids: [{ ...fact, always: true, score: 0 }],
        }),
      )

      expect(within(dialog).getByText('always included')).toBeInTheDocument()
    })

    it('says why a fact was dropped, and names the budget that dropped it', async () => {
      const dialog = await open(
        makeRequestDetail({
          log: makeRequestLog({ latency_retrieval_ms: 18, end_user_id: 'eu1' }),
          retrieved_fact_ids: [{ ...fact, injected: false, dropped: 'memory_max_tokens' }],
        }),
      )

      expect(within(dialog).getByText(/memory budget/)).toBeInTheDocument()
    })

    it('links to the memory when there is nothing stored yet', async () => {
      const dialog = await open(
        makeRequestDetail({
          log: makeRequestLog({ latency_retrieval_ms: 18, end_user_id: 'eu1' }),
          retrieved_fact_ids: [],
        }),
      )

      expect(within(dialog).getByText(/Nothing is stored about this end user/)).toBeInTheDocument()
      expect(within(dialog).getByRole('link', { name: 'Open their memory' })).toBeInTheDocument()
    })

    it('names the header when the request identified nobody', async () => {
      const dialog = await open(
        makeRequestDetail({
          log: makeRequestLog({ latency_retrieval_ms: 18, end_user_id: null }),
          retrieved_fact_ids: [],
        }),
      )

      expect(within(dialog).getByText(/identified no end user/)).toBeInTheDocument()
    })
  })
})

describe('the A/B overlay', () => {
  it('marks the configured weight against the traffic each model actually took', () => {
    const gateway = makeGateway({
      routing_mode: 'ab_split',
      targets: [
        {
          id: 'mo1',
          name: 'a',
          dialect: 'openai',
          enabled: true,
          organization_id: 'o1',
          priority: 0,
          weight: 70,
        },
        {
          id: 'mo2',
          name: 'b',
          dialect: 'openai',
          enabled: true,
          organization_id: 'o1',
          priority: 1,
          weight: 30,
        },
      ],
    })
    const summary = makeSummary({
      models: [
        { upstream_model_id: 'mo1', model_name: 'a', requests: 68 },
        { upstream_model_id: 'mo2', model_name: 'b', requests: 32 },
      ],
    })

    expect(modelSlices(summary, gateway)).toEqual([
      { label: 'a', value: 68, expected: 70 },
      { label: 'b', value: 32, expected: 30 },
    ])
  })

  it('leaves a failover chain unmarked', () => {
    // A healthy chain sends everything to its primary, so a mark at its weight would
    // read as drift when nothing is wrong.
    const gateway = makeGateway({
      routing_mode: 'failover',
      targets: [
        {
          id: 'mo1',
          name: 'a',
          dialect: 'openai',
          enabled: true,
          organization_id: 'o1',
          priority: 0,
          weight: 100,
        },
      ],
    })

    expect(modelSlices(makeSummary(), gateway)).toEqual([{ label: 'acme-gpt', value: 120 }])
  })

  it('leaves the all-gateways view unmarked', () => {
    expect(modelSlices(makeSummary(), undefined)).toEqual([{ label: 'acme-gpt', value: 120 }])
  })
})

describe('the assembled-prompt diff', () => {
  it('counts the messages the gateway prepended', () => {
    const injected = countInjected([{ role: 'system' }, { role: 'user' }], [{ role: 'user' }])

    expect(injected).toBe(1)
  })

  it('claims nothing was injected when the original was not stored', () => {
    // Without the original there is nothing to diff against, and guessing would mark a
    // real user message as something the gateway added.
    expect(countInjected([{ role: 'system' }, { role: 'user' }], null)).toBe(0)
  })

  it('renders a multi-part message rather than [object Object]', () => {
    const text = contentOf({ content: [{ type: 'text', text: 'hello' }] })

    expect(text).toContain('hello')
  })
})

describe('copy as curl', () => {
  it('reproduces the request against the gateway, with a placeholder key', () => {
    const detail = makeRequestDetail()

    const command = asCurl(detail.log, detail, 'https://gw.example.com/g/acme-support/v1')

    expect(command).toContain('https://gw.example.com/g/acme-support/v1/chat/completions')
    expect(command).toContain('$GATEWAY_API_KEY')
    expect(command).toContain('what is the answer')
  })

  it('carries the stream flag when the request streamed', () => {
    const detail = makeRequestDetail({ log: makeRequestLog({ streamed: true }) })

    expect(asCurl(detail.log, detail, undefined)).toContain('"stream":true')
  })
})

describe('chart series', () => {
  it('breaks a line where a bucket has no value rather than dropping it to zero', () => {
    const buckets = [
      { start: '2026-09-06T11:00:00Z', series: { total_p95: 100, ttft_p95: 20 } },
      { start: '2026-09-06T11:05:00Z', series: { total_p95: 120 } },
    ]

    const [, ttft] = latencySeries(buckets)

    expect(ttft?.values).toEqual([20, null])
  })

  it('drops a series no bucket carries at all', () => {
    const buckets = [{ start: '2026-09-06T11:00:00Z', series: { total_p95: 100 } }]

    expect(latencySeries(buckets).map((line) => line.name)).toEqual(['total_p95'])
  })

  it('gives every status class its own colour, and keeps 5xx red', () => {
    expect(colorFor('5xx.requests', 0)).toBe('#dc2626')
    expect(colorFor('2xx.requests', 3)).toBe('#16a34a')
    expect(colorFor('5xx.requests', 7)).toBe('#dc2626')
  })

  it('labels the status series without the metric suffix', () => {
    const series = statusSeries([
      { start: '2026-09-06T11:00:00Z', series: { '2xx.requests': 3, '5xx.requests': 1 } },
    ])

    expect(series.map((line) => line.label)).toEqual(['2xx', '5xx'])
  })

  it('names the interval in words', () => {
    expect(intervalLabel(300)).toBe('5-minute buckets')
    expect(intervalLabel(3600)).toBe('1-hour buckets')
    expect(intervalLabel(undefined)).toBe('')
  })

  it('makes one point per bucket', () => {
    expect(pointsOf(makeSeries().buckets)).toHaveLength(2)
  })

  it('handles a window with no buckets at all', () => {
    expect(seriesFrom([], [{ name: 'requests', label: 'Requests' }])).toEqual([])
  })

  it('draws the empty-retrieval rate as its own line', () => {
    // Separate from the latency chart on purpose: a fraction between 0 and 1 sharing an
    // axis with milliseconds is a line flat against the bottom of the frame.
    const series = retrievalSeries([
      { start: '2026-09-06T11:00:00Z', series: { attempts: 9, empty: 3, empty_rate: 0.333 } },
    ])

    expect(series.map((line) => line.name)).toEqual(['empty_rate'])
    expect(series[0]!.values).toEqual([0.333])
  })

  it('draws no line at all for a window where nothing searched', () => {
    // A flat line at zero would read as "this gateway always finds what it needs".
    const series = retrievalSeries([
      { start: '2026-09-06T11:00:00Z', series: { attempts: 0, empty: 0 } },
    ])

    expect(series).toEqual([])
  })
})
