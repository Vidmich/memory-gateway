import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiClient } from '@/api/client'
import { AppRoutes, makeQueryClient } from '@/App'
import { AuthProvider } from '@/auth/AuthContext'
import { ToastProvider } from '@/components/Toast'
import {
  makeAudit,
  makeAuditStatus,
  makeConnector,
  makeDocument,
  makeEvaluationItem,
  makeEvaluationRun,
  makeEvaluationRunDetail,
  makeEvaluationSet,
  makeGateway,
  makeGatewayLimits,
  makeModel,
  makeRetrievalPreview,
  makeStaleAlert,
  makeSummarizationHealth,
  makeSummary,
  makeUser,
} from '@/test/factories'
import { bodyOf, jsonResponse as json, pathOf } from '@/test/http'

/**
 * Task 103's screens, over a scripted server: the connector's Validation section, the
 * gateway's evaluation sets and runs, Try retrieval's link into a set, and the
 * dashboard's degraded state.
 */

type ServerOptions = {
  audits?: ReturnType<typeof makeAuditStatus>
  sets?: ReturnType<typeof makeEvaluationSet>[]
  items?: ReturnType<typeof makeEvaluationItem>[]
  runs?: ReturnType<typeof makeEvaluationRun>[]
  run?: ReturnType<typeof makeEvaluationRunDetail>
  alerts?: {
    connector_id: string
    connector_name: string | null
    kind: 'chunking' | 'embedding'
    audit_id: string
    finding: string
    created_at: string
  }[]
  user?: ReturnType<typeof makeUser>
  /** Task 104: what `GET /reprocessing/alerts` answers. */
  stale?: ReturnType<typeof makeStaleAlert>[]
}

function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  const connector = makeConnector()
  const base = makeGateway({ id: 'g1' })
  const gateway = {
    ...base,
    memory_config: { ...base.memory_config, connector_ids: ['c1'], doc_top_k: 6 },
  }
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
    if (path.startsWith('/api/v1/metrics/summary')) return Promise.resolve(json(makeSummary()))
    if (path.startsWith('/api/v1/metrics/timeseries')) {
      return Promise.resolve(json({ interval_seconds: 60, buckets: [] }))
    }
    if (path.startsWith('/api/v1/summarization/health')) {
      return Promise.resolve(json(makeSummarizationHealth()))
    }
    if (path.startsWith('/api/v1/limits/pressure')) return Promise.resolve(json({ items: [] }))
    if (path === '/api/v1/validation/alerts') {
      return Promise.resolve(json({ items: options.alerts ?? [] }))
    }
    if (path.startsWith('/api/v1/reprocessing/alerts')) {
      return Promise.resolve(json({ items: options.stale ?? [] }))
    }
    // -- audits
    if (path.endsWith('/audits') && method === 'GET') {
      return Promise.resolve(json(options.audits ?? makeAuditStatus()))
    }
    if (path.includes('/audits/') && method === 'POST') {
      const kind = path.split('/').pop() as 'chunking' | 'embedding'
      return Promise.resolve(
        json(
          makeAudit({ id: 'a-new', kind, status: 'running', report: null, severity: null }),
          202,
        ),
      )
    }
    // -- evaluation
    if (path.endsWith('/evaluation-sets') && method === 'GET') {
      return Promise.resolve(json({ items: options.sets ?? [makeEvaluationSet()] }))
    }
    if (path.endsWith('/evaluation-sets') && method === 'POST') {
      return Promise.resolve(json(makeEvaluationSet({ id: 'es-new', ...bodyOf(init) }), 201))
    }
    if (path.endsWith('/try-retrieval') && method === 'POST') {
      return Promise.resolve(json(makeRetrievalPreview()))
    }
    if (
      path.startsWith('/api/v1/evaluation-sets/') &&
      path.endsWith('/items') &&
      method === 'POST'
    ) {
      const body = bodyOf<{ question: string }>(init)
      return Promise.resolve(
        json(
          makeEvaluationItem({
            id: 'ei-new',
            question: body.question,
            source: 'manual',
            verified: true,
          }),
          201,
        ),
      )
    }
    if (
      path.startsWith('/api/v1/evaluation-sets/') &&
      path.endsWith('/runs') &&
      method === 'POST'
    ) {
      return Promise.resolve(json(makeEvaluationRun({ id: 'er-new', status: 'queued' }), 202))
    }
    if (path.startsWith('/api/v1/evaluation-sets/') && path.endsWith('/runs')) {
      return Promise.resolve(json({ items: options.runs ?? [makeEvaluationRun()] }))
    }
    if (path.startsWith('/api/v1/evaluation-sets/') && path.endsWith('/import')) {
      return Promise.resolve(json({ imported: 7, duplicates: 2, skipped: 0, labelled: 5 }))
    }
    if (path.startsWith('/api/v1/evaluation-sets/') && path.endsWith('/generate')) {
      return Promise.resolve(
        json({ generated: 3, failed: 0, tokens_in: 900, tokens_out: 60, model_name: 'cheap' }),
      )
    }
    if (path.startsWith('/api/v1/evaluation-sets/')) {
      const set = (options.sets ?? [makeEvaluationSet()])[0]!
      return Promise.resolve(
        json({ ...set, items: options.items ?? [makeEvaluationItem()], last_run: undefined }),
      )
    }
    if (path.startsWith('/api/v1/evaluation-items/') && method === 'PATCH') {
      return Promise.resolve(json(makeEvaluationItem({ verified: true })))
    }
    if (path.startsWith('/api/v1/evaluation-runs/') && path.includes('/diff/')) {
      return Promise.resolve(
        json({
          before: makeEvaluationRun({ id: 'er0' }),
          after: makeEvaluationRun(),
          metrics: [{ name: 'chunk recall', before: 0.7, after: 0.82, change: 0.12 }],
          config_changes: { doc_min_score: [0.35, 0.2] },
          index_changes: [],
          won: [{ item_id: 'x', question: 'Where is the depot?' }],
          lost: [],
        }),
      )
    }
    if (path.startsWith('/api/v1/evaluation-runs/')) {
      return Promise.resolve(json(options.run ?? makeEvaluationRunDetail()))
    }
    // -- the rest of the editor and the connector page
    if (path.endsWith('/keys')) return Promise.resolve(json([]))
    if (path.endsWith('/limits')) return Promise.resolve(json(makeGatewayLimits()))
    if (path === '/api/v1/tokenizers') return Promise.resolve(json({ items: [] }))
    if (path === `/api/v1/gateways/${gateway.id}`) return Promise.resolve(json(gateway))
    if (path.startsWith('/api/v1/gateways')) {
      return Promise.resolve(json({ items: [gateway], next_cursor: null }))
    }
    if (path.startsWith('/api/v1/models')) {
      return Promise.resolve(json({ items: [makeModel()], next_cursor: null }))
    }
    if (path.includes('/documents') && method === 'GET') {
      return Promise.resolve(json({ items: [makeDocument()], next_cursor: null }))
    }
    if (path.startsWith('/api/v1/connectors/')) return Promise.resolve(json(connector))
    if (path.startsWith('/api/v1/connectors')) {
      return Promise.resolve(json({ items: [connector], next_cursor: null }))
    }
    return Promise.resolve(json({ items: [], next_cursor: null }))
  })
  return { impl, requests }
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
// connectors → validation
// ---------------------------------------------------------------------------

describe('the connector validation section', () => {
  it('shows the last chunking report with its histogram, numbers and findings', async () => {
    renderAt('/connectors/c1', fakeServer())

    const section = await screen.findByTestId('connector-validation')
    await within(section).findByTestId('chunking-report')
    expect(within(section).getByTestId('audit-age')).toHaveTextContent(
      /1,375 points, 1 red and 1 amber/,
    )
    expect(within(section).getByTestId('chunk-histogram')).toBeInTheDocument()
    expect(within(section).getByText('312 chunks under 40 tokens')).toBeInTheDocument()
    expect(within(section).getByText('41 documents are a single chunk')).toBeInTheDocument()
    // The document behind a finding opens Compare with it preselected.
    const link = within(section).getByRole('link', { name: 'CHANGELOG.md' })
    expect(link).toHaveAttribute('href', '/connectors/c1?compare=d2')
  })

  it('switches to the embedding report and prices the drift check before running it', async () => {
    const server = fakeServer()
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('tab', { name: /Embeddings/ }))
    const section = screen.getByTestId('connector-validation')
    expect(within(section).getByTestId('embedding-report')).toHaveTextContent('94%')
    expect(within(section).getByTestId('drift-cost')).toHaveTextContent(
      'Re-embeds 100 of 1,375 chunks now — about 52,000 tokens',
    )

    await user.click(within(section).getByRole('checkbox'))
    await user.click(within(section).getByRole('button', { name: /run again/i }))

    await waitFor(() => {
      const started = server.requests.find(
        (request) =>
          request.path === '/api/v1/connectors/c1/audits/embedding' && request.method === 'POST',
      )
      expect(started?.body).toEqual({ drift_sample: 100 })
    })
  })

  it('runs a chunking audit without a body and says it is running', async () => {
    const server = fakeServer({
      audits: makeAuditStatus({ chunking: null, embedding: null }),
    })
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    const section = await screen.findByTestId('connector-validation')
    await waitFor(() =>
      expect(within(section).getByTestId('audit-age')).toHaveTextContent('Never run.'),
    )
    await user.click(within(section).getByRole('button', { name: 'Run' }))

    await waitFor(() => {
      expect(
        server.requests.some(
          (request) =>
            request.path === '/api/v1/connectors/c1/audits/chunking' && request.method === 'POST',
        ),
      ).toBe(true)
    })
  })
})

// ---------------------------------------------------------------------------
// gateways → validation
// ---------------------------------------------------------------------------

describe('the gateway evaluation section', () => {
  it('lists the sets and opens one with its questions and runs', async () => {
    renderAt('/gateways/g1', fakeServer())
    const user = userEvent.setup()

    const section = await screen.findByTestId('evaluation-section')
    expect(within(section).getByText('52 questions · 3 verified · 2 negative')).toBeInTheDocument()
    await user.click(within(section).getByRole('button', { name: /Support questions/ }))

    const panel = await within(section).findByTestId('evaluation-set')
    expect(within(panel).getByText('How do refunds work?')).toBeInTheDocument()
    expect(within(panel).getByText('from a citation')).toBeInTheDocument()
    expect(within(panel).getByTestId('runs-table')).toHaveTextContent(
      'recall@6 0.82 · precision@6 0.41 · MRR 0.77',
    )
    expect(within(panel).getByTestId('run-cost')).toHaveTextContent('52 embedding calls')
  })

  it('verifies an item, runs the set with the unsaved form, and opens the run', async () => {
    const server = fakeServer()
    renderAt('/gateways/g1', server)
    const user = userEvent.setup()

    const section = await screen.findByTestId('evaluation-section')
    await user.click(within(section).getByRole('button', { name: /Support questions/ }))
    const panel = await within(section).findByTestId('evaluation-set')

    await user.click(
      within(panel).getByRole('checkbox', { name: 'Verified: How do refunds work?' }),
    )
    await waitFor(() => {
      const patch = server.requests.find(
        (request) => request.path === '/api/v1/evaluation-items/ei1' && request.method === 'PATCH',
      )
      expect(patch?.body).toEqual({ verified: true })
    })

    await user.click(within(panel).getByRole('button', { name: 'Run' }))
    await waitFor(() => {
      const started = server.requests.find(
        (request) =>
          request.path === '/api/v1/evaluation-sets/es1/runs' && request.method === 'POST',
      )
      expect(started?.body.memory_config).toMatchObject({ doc_top_k: 6 })
    })

    await user.click(
      within(panel).getByRole('button', {
        name: new Date('2026-09-06T12:00:00Z').toLocaleString(),
      }),
    )
    const detail = await within(panel).findByTestId('run-detail')
    expect(within(detail).getByTestId('run-metrics')).toHaveTextContent(
      'Chunk recall0.820.781.001.00',
    )
    expect(detail).toHaveTextContent(/49 items are unverified/)
    await user.click(within(detail).getByRole('button', { name: /How do refunds work\?/ }))
    expect(detail).toHaveTextContent('first relevant at 2')
    expect(detail).toHaveTextContent('[2] handbook.md · p. 12 · 0.70 · relevant')
  })

  it('imports from the log and generates with a model, saying what each did', async () => {
    const server = fakeServer()
    renderAt('/gateways/g1', server)
    const user = userEvent.setup()

    const section = await screen.findByTestId('evaluation-section')
    await user.click(within(section).getByRole('button', { name: /Support questions/ }))
    const panel = await within(section).findByTestId('evaluation-set')

    await user.click(within(panel).getByRole('button', { name: 'Import' }))
    expect(
      await screen.findByText(/Imported 7 questions \(5 with citations, 2 already here\)/),
    ).toBeInTheDocument()
    const imported = server.requests.find((request) => request.path.endsWith('/import'))
    expect(imported?.body).toMatchObject({ limit: 200, uncited: null })
    expect(typeof imported?.body.from).toBe('string')

    await user.click(within(panel).getByRole('button', { name: 'Generate' }))
    expect(
      await screen.findByText(/Wrote 3 questions with cheap \(960 tokens\)/),
    ).toBeInTheDocument()
    const generated = server.requests.find((request) => request.path.endsWith('/generate'))
    expect(generated?.body).toEqual({ count: 10 })
  })

  it('adds a question labelled from Try retrieval inside the set', async () => {
    const server = fakeServer()
    renderAt('/gateways/g1', server)
    const user = userEvent.setup()

    const section = await screen.findByTestId('evaluation-section')
    await user.click(within(section).getByRole('button', { name: /Support questions/ }))
    const panel = await within(section).findByTestId('evaluation-set')
    await user.click(within(panel).getByRole('button', { name: 'Add a question' }))
    const form = within(panel).getByTestId('add-item')
    await user.type(within(form).getByLabelText('Question'), 'how do refunds work')
    await user.click(within(form).getByRole('button', { name: 'Find chunks' }))
    await user.click(
      await within(form).findByRole('checkbox', { name: 'Relevant: handbook.md [1]' }),
    )
    await user.click(within(form).getByRole('button', { name: 'Add with 1 chunk' }))

    await waitFor(() => {
      const added = server.requests.find(
        (request) =>
          request.path === '/api/v1/evaluation-sets/es1/items' && request.method === 'POST',
      )
      expect(added?.body).toEqual({
        question: 'how do refunds work',
        relevant: [{ chunk_id: 'ch1', document_id: 'd1' }],
        verified: true,
      })
    })
  })

  it('diffs two runs and names what changed', async () => {
    const server = fakeServer({
      runs: [
        makeEvaluationRun(),
        makeEvaluationRun({ id: 'er0', created_at: '2026-09-05T12:00:00Z' }),
      ],
    })
    renderAt('/gateways/g1', server)
    const user = userEvent.setup()

    const section = await screen.findByTestId('evaluation-section')
    await user.click(within(section).getByRole('button', { name: /Support questions/ }))
    const panel = await within(section).findByTestId('evaluation-set')
    await user.click(
      within(panel).getByRole('button', {
        name: new Date('2026-09-06T12:00:00Z').toLocaleString(),
      }),
    )
    await user.click(
      within(panel).getByRole('radio', {
        name: `Diff against the run of ${new Date('2026-09-05T12:00:00Z').toLocaleString()}`,
      }),
    )

    const diff = await within(panel).findByTestId('run-diff')
    expect(diff).toHaveTextContent('doc_min_score: 0.35 → 0.2')
    expect(diff).toHaveTextContent('+0.12')
    expect(diff).toHaveTextContent('Where is the depot?')
    expect(
      server.requests.some((request) => request.path === '/api/v1/evaluation-runs/er1/diff/er0'),
    ).toBe(true)
  })
})

describe('try retrieval', () => {
  it('offers to add the question and its ticked chunks to an evaluation set', async () => {
    const server = fakeServer()
    renderAt('/gateways/g1', server)
    const user = userEvent.setup()

    await user.type(await screen.findByLabelText('Question'), 'how do refunds work')
    await user.click(screen.getByRole('button', { name: 'Try retrieval' }))
    const box = await screen.findByTestId('add-to-evaluation-set')
    await user.click(within(box).getByRole('checkbox', { name: 'Relevant: [1] handbook.md' }))
    await user.click(within(box).getByRole('button', { name: 'Add to evaluation set' }))

    await waitFor(() => {
      const added = server.requests.find(
        (request) =>
          request.path === '/api/v1/evaluation-sets/es1/items' && request.method === 'POST',
      )
      expect(added?.body).toEqual({
        question: 'how do refunds work',
        relevant: [{ chunk_id: 'ch1', document_id: 'd1' }],
        verified: true,
      })
    })
    expect(await within(box).findByRole('status')).toHaveTextContent('Added.')
  })
})

describe('the dashboard', () => {
  it('lists a connector whose last audit raised a red finding', async () => {
    renderAt(
      '/',
      fakeServer({
        alerts: [
          {
            connector_id: 'c1',
            connector_name: 'Product docs',
            kind: 'embedding',
            audit_id: 'a2',
            finding: 'a re-embedded sample agrees with the index at 0.61',
            created_at: '2026-09-06T12:00:00Z',
          },
        ],
      }),
    )

    const card = await screen.findByTestId('audit-alerts')
    expect(card).toHaveTextContent(
      'Product docs: a re-embedded sample agrees with the index at 0.61 (embedding)',
    )
    expect(within(card).getByRole('link')).toHaveAttribute('href', '/connectors/c1')
  })

  it('lists connectors whose documents have been stale for over a day, with the age (task 104)', async () => {
    renderAt('/', fakeServer({ stale: [makeStaleAlert()] }))

    const card = await screen.findByTestId('stale-alerts')
    expect(card).toHaveTextContent('Product docs: 1,184 documents stale for 29 h')
    expect(within(card).getByRole('link')).toHaveAttribute('href', '/connectors/c1')
  })

  it('shows no stale card when nothing has been stale for a day', async () => {
    renderAt('/', fakeServer())
    await screen.findByText(/Requests/)

    expect(screen.queryByTestId('stale-alerts')).not.toBeInTheDocument()
  })
})
