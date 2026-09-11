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
  makeDistillationSettings,
  makeSummarizationHealth,
  makeSummarizationSettings,
  makeMemoryHealth,
  makeModel,
  makeOrganization,
  makeSummary,
  makeUser,
  makeTemplateDefaults,
} from '@/test/factories'
import { bodyOf, jsonResponse as json, pathOf } from '@/test/http'

type ServerOptions = {
  user?: ReturnType<typeof makeUser>
  settings?: ReturnType<typeof makeDistillationSettings>
  health?: ReturnType<typeof makeMemoryHealth>
  /** Task 102: the summarization default beside the distillation model. */
  summarization?: ReturnType<typeof makeSummarizationSettings>
}

/**
 * A scripted server, not a mocked client — the real `ApiClient` runs, so a test asserts
 * what actually went on the wire.
 *
 * That matters most for the model selector: "use the platform default" and "leave the
 * model alone" arrive as the same empty value in a form and mean opposite things on the
 * wire, and the only honest way to check which one was sent is to read the request.
 */
function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  const organization = makeOrganization()
  const requests: { path: string; method: string; body: Record<string, unknown> }[] = []
  let settings = options.settings ?? makeDistillationSettings()
  let summarization = options.summarization ?? makeSummarizationSettings()

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
    if (path.startsWith('/api/v1/distillation/health')) {
      return Promise.resolve(json(options.health ?? makeMemoryHealth()))
    }
    if (path === '/api/v1/distillation' && method === 'PATCH') {
      const body = bodyOf<Record<string, unknown>>(init)
      settings = {
        ...settings,
        config: { ...settings.config, ...body },
      }
      return Promise.resolve(json(settings))
    }
    if (path === '/api/v1/distillation') return Promise.resolve(json(settings))
    if (path === '/api/v1/summarization' && method === 'PATCH') {
      const body = bodyOf<{ model_id: string | null }>(init)
      summarization = {
        ...summarization,
        config: { ...summarization.config, model_id: body.model_id },
        effective_model_source: body.model_id ? 'summarization' : 'platform',
      }
      return Promise.resolve(json(summarization))
    }
    if (path === '/api/v1/summarization') return Promise.resolve(json(summarization))
    if (path.startsWith('/api/v1/summarization/health')) {
      return Promise.resolve(json(makeSummarizationHealth()))
    }
    if (path.startsWith('/api/v1/models')) {
      return Promise.resolve(
        json({ items: [makeModel({ id: 'm-cheap', name: 'cheap-one' })], next_cursor: null }),
      )
    }
    if (path.startsWith('/api/v1/organizations/')) return Promise.resolve(json(organization))
    // Task 105: the template defaults section on the same settings page.
    if (path === '/api/v1/templates/defaults') return Promise.resolve(json(makeTemplateDefaults()))
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
// the settings form
// ---------------------------------------------------------------------------

describe('the write-back settings', () => {
  it('shows what will happen, not just what is stored', async () => {
    renderAt('/settings', fakeServer())

    // Awaited, not asserted synchronously after the heading: the loading state renders the
    // same heading, so a `getByText` here would run before the settings had arrived and
    // pass or fail on how busy the machine is.
    expect(await screen.findByText(/No distillation model is configured/)).toBeInTheDocument()
  })

  it('names the platform default rather than showing a blank selector', async () => {
    // An organization that has chosen nothing is not broken. A blank selector with no
    // sentence beside it reads as "distillation is not configured".
    renderAt(
      '/settings',
      fakeServer({
        settings: makeDistillationSettings({
          effective_model_id: 'm-platform',
          effective_model_name: 'gpt-4o-mini',
          using_platform_default: true,
        }),
      }),
    )

    expect(await screen.findByText(/Using the platform default, gpt-4o-mini/)).toBeInTheDocument()
  })

  it('saves the knobs, and always sends the model so it can be cleared', async () => {
    const user = userEvent.setup()
    const server = fakeServer()
    renderAt('/settings', server)

    const debounce = await screen.findByLabelText('Wait before distilling')
    await user.clear(debounce)
    await user.type(debounce, '120')
    await user.click(screen.getByRole('button', { name: 'Save write-back settings' }))

    const saved = server.requests.find((request) => request.method === 'PATCH')
    expect(saved?.body).toMatchObject({ debounce_seconds: 120 })
    expect(Object.keys(saved?.body ?? {})).toContain('model_id')
  })

  it('says what the wait actually means', async () => {
    renderAt('/settings', fakeServer())

    expect(
      await screen.findByText(/A burst of turns is one pass, not one per turn/),
    ).toBeInTheDocument()
  })

  it('warns before today’s budget is gone', async () => {
    // A guard that stops memory silently is worse than no cap.
    renderAt(
      '/settings',
      fakeServer({
        settings: makeDistillationSettings({
          usage: {
            calls_today: 4900,
            daily_call_cap: 5000,
            day_started_at: '2026-09-06T00:00:00Z',
          },
        }),
      }),
    )

    expect(await screen.findByText(/nearly spent/)).toBeInTheDocument()
  })

  it('says what switching it off does, which is not what it sounds like', async () => {
    const user = userEvent.setup()
    renderAt('/settings', fakeServer())

    await user.click(await screen.findByLabelText(/Learn from conversations/))

    expect(screen.getByText(/Facts already stored are still recalled/)).toBeInTheDocument()
  })

  it('is read-only for a role that cannot administer the organization', async () => {
    renderAt(
      '/settings',
      fakeServer({ user: makeUser({ role: 'org_viewer', capabilities: ['org:read'] }) }),
    )

    // The form has to be *loaded* before "there is no save button" means anything — while
    // it is loading there is no save button either.
    expect(await screen.findByLabelText('Distillation model')).toBeDisabled()
    expect(
      screen.queryByRole('button', { name: 'Save write-back settings' }),
    ).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// memory health
// ---------------------------------------------------------------------------

describe('the summarization model default (task 102)', () => {
  it('says which link of the chain answers, and saves a choice with null to clear it', async () => {
    const user = userEvent.setup()
    const server = fakeServer()
    renderAt('/settings', server)

    expect(await screen.findByText(/Using the platform default, acme-gpt/)).toBeInTheDocument()
    const select = screen.getByLabelText('Summarization model')
    await user.selectOptions(select, 'm-cheap')
    await user.click(screen.getByRole('button', { name: 'Save summarization model' }))

    await waitFor(() => {
      const saved = server.requests.find(
        (request) => request.path === '/api/v1/summarization' && request.method === 'PATCH',
      )
      expect(saved?.body).toEqual({ model_id: 'm-cheap' })
    })
    expect(await screen.findByText('Summarizing with acme-gpt.')).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Summarization model'), '')
    await user.click(screen.getByRole('button', { name: 'Save summarization model' }))
    await waitFor(() => {
      const patches = server.requests.filter(
        (request) => request.path === '/api/v1/summarization' && request.method === 'PATCH',
      )
      expect(patches.at(-1)?.body).toEqual({ model_id: null })
    })
  })

  it('names the distillation model when that is what answers', async () => {
    renderAt(
      '/settings',
      fakeServer({
        summarization: makeSummarizationSettings({ effective_model_source: 'distillation' }),
      }),
    )

    expect(await screen.findByText(/Using the distillation model, acme-gpt/)).toBeInTheDocument()
  })
})

describe('the memory-health panel', () => {
  it('says nothing has run rather than drawing a chart of zeroes', async () => {
    renderAt('/monitoring', fakeServer())

    expect(await screen.findByText(/No distillation has run/)).toBeInTheDocument()
  })

  it('names the failure that looks exactly like success', async () => {
    // Passes succeeding and producing nothing new: green jobs, no errors, facts on the
    // screen. Two ratios on a card leave that to be noticed; a sentence says it.
    renderAt(
      '/monitoring',
      fakeServer({
        health: makeMemoryHealth({
          runs: 30,
          candidates: 60,
          deduped: 59,
          written: 1,
          dedupe_rate: 0.98,
          days: [
            {
              day: '2026-09-05T00:00:00Z',
              runs: 30,
              failures: 0,
              written: 1,
              deduped: 59,
              superseded: 0,
            },
          ],
        }),
      }),
    )

    expect(await screen.findByText(/producing nothing new/)).toBeInTheDocument()
    expect(screen.getByText('Memory health')).toBeInTheDocument()
  })

  it('shows the rates and the average beside the chart', async () => {
    renderAt(
      '/monitoring',
      fakeServer({
        health: makeMemoryHealth({
          runs: 10,
          written: 20,
          candidates: 30,
          deduped: 6,
          superseded: 3,
          facts: 20,
          end_users_with_facts: 8,
          dedupe_rate: 0.2,
          supersession_rate: 0.1,
          average_facts_per_end_user: 2.5,
          days: [
            {
              day: '2026-09-05T00:00:00Z',
              runs: 10,
              failures: 0,
              written: 20,
              deduped: 6,
              superseded: 3,
            },
          ],
        }),
      }),
    )

    const stats = await screen.findByText('Facts per person')
    expect(within(stats.parentElement as HTMLElement).getByText('2.5')).toBeInTheDocument()
  })
})
