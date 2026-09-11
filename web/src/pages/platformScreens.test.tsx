import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiClient } from '@/api/client'
import { AppRoutes, makeQueryClient } from '@/App'
import { AuthProvider } from '@/auth/AuthContext'
import { ToastProvider } from '@/components/Toast'
import { makeReprocessingRun, makeSuperadmin, makeTokenizers } from '@/test/factories'
import { bodyOf, jsonResponse as json, pathOf } from '@/test/http'

/**
 * Platform → Settings and Platform → Maintenance (task 17).
 *
 * The assertions that matter here are about *saying what will happen*, because these two
 * screens sit in front of the only irreversible operations in the product. A form that
 * silently started a four-figure re-embedding run, or a Delete button that acted on a set
 * nobody had looked at, would pass any test that only checked the request body.
 */

const SETTINGS = {
  settings: {
    version: 1,
    embedding: { provider: 'hash', name: 'hash-bow', dimension: 256 },
    distillation: { model_id: null },
    logging: {
      version: 1,
      log_metadata: true,
      log_request_body: true,
      log_assembled_prompt: true,
      log_response_body: true,
      retention_days: 30,
      metadata_retention_days: 365,
      redaction_patterns: [],
      enable_distillation: true,
    },
    retention: { max_body_days: null, max_metadata_days: null },
    limits: {
      defaults: {
        requests_per_minute: null,
        tokens_per_minute: null,
        concurrent_requests: null,
        requests_per_day: null,
      },
      global_model_ceilings: {
        requests_per_minute: null,
        tokens_per_minute: null,
        concurrent_requests: null,
        requests_per_day: null,
      },
    },
    storage: { max_file_bytes: 52428800, quota_bytes: null },
  },
  attribution: [],
  from_environment: ['embedding', 'distillation', 'limits', 'storage'],
  reindex: null,
  pending_embedding: null,
  // Task 101: the hash embedder has no vocabulary, so the chunker's unit is derived as
  // the fallback approximation.
  embedding_tokenizer: {
    spec: { name: 'approximate', ratio: 4 },
    origin: 'derived',
    name: 'approximate:4',
    label: 'approximate:4 (derived)',
    degraded: false,
    approximate: true,
  },
}

const MAINTENANCE = {
  runway: [
    { table: 'request_logs', days_ahead: 30, last_day: '2026-10-14', low: false },
    { table: 'transcripts', days_ahead: 3, last_day: '2026-09-17', low: true },
  ],
  runway_threshold_days: 7,
  last_runs: [
    {
      id: 'run-1',
      job: 'retention',
      status: 'succeeded',
      started_at: '2026-09-14T03:05:00Z',
      finished_at: '2026-09-14T03:06:00Z',
      report: {
        rows_removed: 120,
        bodies_removed: 400,
        bytes_reclaimed: 5_242_880,
        facts_expired: 3,
      },
      error: null,
    },
  ],
  reindex: null,
  recent_reindexes: [],
}

type Options = {
  settings?: typeof SETTINGS
  maintenance?: typeof MAINTENANCE
  /** Task 104: the per-connector runs a reindex spawned. */
  spawned?: ReturnType<typeof makeReprocessingRun>[]
  sweep?: Record<string, unknown>
  estimate?: Record<string, unknown>
}

function fakeServer(options: Options = {}) {
  const user = makeSuperadmin()
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
    if (path === '/api/v1/platform/settings') {
      return Promise.resolve(json(options.settings ?? SETTINGS))
    }
    if (path === '/api/v1/tokenizers') return Promise.resolve(json(makeTokenizers()))
    if (path === '/api/v1/platform/maintenance') {
      return Promise.resolve(json(options.maintenance ?? MAINTENANCE))
    }
    if (path === '/api/v1/platform/maintenance/sweep') {
      return Promise.resolve(
        json(
          options.sweep ?? {
            applied: false,
            organizations: 2,
            deleted: 0,
            groups: [{ store: 'qdrant', kind: 'document_points', count: 2, sample: ['d1', 'd2'] }],
          },
        ),
      )
    }
    if (path.startsWith('/api/v1/platform/reindex/')) {
      return Promise.resolve(
        json({
          ...((options.maintenance ?? MAINTENANCE).reindex as Record<string, unknown> | null),
          reprocessing_runs: options.spawned ?? [],
        }),
      )
    }
    if (path === '/api/v1/platform/reindex') {
      return Promise.resolve(
        json(
          options.estimate ?? {
            collections: ['org_a_docs_v1'],
            organizations: 1,
            points: 1200,
            tokens: 48000,
            from_model: 'hash-bow',
            to_model: 'text-embedding-3-large',
            to_dimension: 3072,
          },
        ),
      )
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
// settings
// ---------------------------------------------------------------------------

describe('Platform → Settings', () => {
  it('says which sections are still running on an environment variable', async () => {
    // "From the environment" and "set to the same value" look identical on screen and are
    // not the same state: the first changes when a pod is redeployed.
    renderAt('/platform/settings', fakeServer())

    expect(await screen.findAllByText('From the environment')).not.toHaveLength(0)
  })

  it('sends an empty ceiling as null rather than as zero', async () => {
    // Blank means "no ceiling", which is a different value from zero. A form that coerced
    // one into the other would silently throttle every gateway on the platform to nothing.
    const server = fakeServer()
    renderAt('/platform/settings', server)

    await userEvent.click(await screen.findByRole('button', { name: 'Save settings' }))

    const patch = server.requests.find((entry) => entry.method === 'PATCH')
    expect(patch).toBeDefined()
    expect((patch!.body.retention as Record<string, unknown>).max_body_days).toBeNull()
  })

  it('warns before lowering a retention ceiling', async () => {
    // Not a typed confirmation: lowering a ceiling is not destructive *now*, it caps
    // gateways and tonight's pass acts on the new number. Saying so is the honest thing.
    renderAt('/platform/settings', fakeServer())

    const field = await screen.findByLabelText('Maximum body retention (days)')
    await userEvent.type(field, '7')

    expect(await screen.findByRole('status')).toHaveTextContent(/cannot be undone/i)
  })
})

describe('the embedding panel', () => {
  it('shows the derived tokenizer and says chunk sizes are estimates (task 101)', async () => {
    renderAt('/platform/settings', fakeServer())

    expect(await screen.findByTestId('embedding.tokenizer-derived')).toHaveTextContent(
      'approximate:4 (derived)',
    )
    expect(screen.getByRole('note')).toHaveTextContent(/chunk sizes are estimates/)
  })

  it('derives a real encoding once the model is one we know', async () => {
    renderAt('/platform/settings', fakeServer())

    await userEvent.selectOptions(await screen.findByLabelText('Provider'), 'openai')
    const model = screen.getByLabelText('Model')
    await userEvent.clear(model)
    await userEvent.type(model, 'text-embedding-3-small')

    expect(screen.getByTestId('embedding.tokenizer-derived')).toHaveTextContent(
      'cl100k_base (derived)',
    )
    // A different unit from the one the documents were cut in: every one of them is stale.
    expect(screen.getByRole('status')).toHaveTextContent(/every indexed document becomes stale/i)
  })

  it('sends an override as part of the embedding section, without a reindex', async () => {
    const server = fakeServer()
    renderAt('/platform/settings', server)

    await userEvent.click(await screen.findByLabelText('Override'))
    const ratio = screen.getByLabelText('Characters per token')
    await userEvent.clear(ratio)
    await userEvent.type(ratio, '3.6')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      const sent = server.requests.find(
        (entry) => entry.path === '/api/v1/platform/settings' && entry.method === 'PATCH',
      )
      expect(sent?.body.embedding).toMatchObject({
        provider: 'hash',
        name: 'hash-bow',
        tokenizer: { name: 'approximate', ratio: 3.6 },
      })
    })
  })

  it('will not start a reindex until the model name is retyped', async () => {
    // The most expensive button in the product. Nobody should reach it by tabbing.
    renderAt('/platform/settings', fakeServer())

    const model = await screen.findByLabelText('Model')
    await userEvent.clear(model)
    await userEvent.type(model, 'text-embedding-3-large')

    expect(await screen.findByRole('button', { name: 'Start reindex' })).toBeDisabled()
  })

  it('fetches the cost from the server rather than guessing it', async () => {
    // The number an operator agrees to has to be the number the run will work through.
    const server = fakeServer()
    renderAt('/platform/settings', server)

    const model = await screen.findByLabelText('Model')
    await userEvent.clear(model)
    await userEvent.type(model, 'text-embedding-3-large')
    await userEvent.click(await screen.findByRole('button', { name: /what this will cost/i }))

    expect(await screen.findByText(/1,200 chunks/)).toBeInTheDocument()
    expect(server.requests.some((entry) => entry.body.dry_run === true)).toBe(true)
  })

  it('shows the old model as current while a run is in flight', async () => {
    // Until the aliases swap, the old model is the one every collection agrees with.
    // Showing the new one would be describing a state that does not exist yet.
    const server = fakeServer({
      settings: {
        ...SETTINGS,
        pending_embedding: { provider: 'openai', name: 'text-embedding-3-large', dimension: 3072 },
        reindex: {
          id: 'r1',
          scope: 'platform',
          status: 'running',
          from_model: 'hash-bow',
          from_dimension: 256,
          to_model: 'text-embedding-3-large',
          to_dimension: 3072,
          estimated_points: 1200,
          estimated_tokens: 48000,
          started_at: '2026-09-14T09:00:00Z',
          finished_at: null,
          error: null,
          targets: [],
          eta_seconds: null,
        },
      } as unknown as typeof SETTINGS,
    })
    renderAt('/platform/settings', server)

    expect(await screen.findByText(/A reindex is running/)).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Save' })).toBeDisabled()
  })
})

// ---------------------------------------------------------------------------
// maintenance
// ---------------------------------------------------------------------------

describe('Platform → Maintenance', () => {
  it('calls out a runway that is below the threshold', async () => {
    // A missing partition is not a slow query, it is an insert that fails — and the thing
    // that fails is request logging, so the first symptom would be silence.
    renderAt('/platform/maintenance', fakeServer())

    expect(await screen.findByText(/3 days ahead — below 7/)).toBeInTheDocument()
    expect(await screen.findByText(/30 days ahead/)).toBeInTheDocument()
  })

  it('summarises what retention took, rather than printing its report', async () => {
    renderAt('/platform/maintenance', fakeServer())

    expect(await screen.findByText(/400 bodies and 120 rows removed/)).toBeInTheDocument()
    expect(await screen.findByText(/5 MB reclaimed/)).toBeInTheDocument()
  })

  it('lists the per-connector runs a running reindex spawned (task 104)', async () => {
    renderAt(
      '/platform/maintenance',
      fakeServer({
        maintenance: {
          ...MAINTENANCE,
          reindex: {
            id: 'r1',
            scope: 'platform',
            status: 'running',
            from_model: 'hash-bow',
            from_dimension: 256,
            to_model: 'text-embedding-3-large',
            to_dimension: 3072,
            estimated_points: 1200,
            estimated_tokens: 48000,
            started_at: '2026-09-14T09:00:00Z',
            finished_at: null,
            error: null,
            targets: [],
            eta_seconds: null,
          },
        } as unknown as typeof MAINTENANCE,
        spawned: [
          makeReprocessingRun({
            id: 'rr1',
            connector_id: 'cn-semantic',
            trigger: 'embedding_model',
            scope: 'all',
            total: 40,
            done: 12,
            failed: 0,
          }),
        ],
      }),
    )

    const list = await screen.findByTestId('spawned-runs')
    expect(list).toHaveTextContent('cn-semantic')
    expect(list).toHaveTextContent('embedding model change · everything · running · 12 / 40')
  })

  it('offers no delete button until a sweep has produced a list', async () => {
    // Report before delete. The destructive pass acts on the set somebody has seen.
    renderAt('/platform/maintenance', fakeServer())
    await screen.findByRole('button', { name: 'Find orphans' })

    expect(screen.queryByRole('button', { name: /Delete the/ })).not.toBeInTheDocument()
  })

  it('names the count in the delete button once orphans have been found', async () => {
    const server = fakeServer()
    renderAt('/platform/maintenance', server)

    await userEvent.click(await screen.findByRole('button', { name: 'Find orphans' }))

    expect(
      await screen.findByRole('button', { name: 'Delete the 2 orphans listed above' }),
    ).toBeInTheDocument()
    const sweep = server.requests.find((entry) => entry.path.endsWith('/sweep'))
    expect(sweep!.body.apply).toBe(false)
  })
})
