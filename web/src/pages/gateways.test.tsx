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
  makeApiKey,
  makeConnector,
  makeGateway,
  makeGatewayProbe,
  makeIssuedKey,
  makeModel,
  makePromptPreview,
  makeRetrievalPreview,
  makeRetrievedChunk,
  makeSeries,
  makeUser,
} from '@/test/factories'
import { bodyOf, jsonResponse as json, pathOf } from '@/test/http'

type ServerOptions = {
  user?: ReturnType<typeof makeUser>
  gateways?: ReturnType<typeof makeGateway>[]
  apiKeys?: ReturnType<typeof makeApiKey>[]
  issued?: ReturnType<typeof makeIssuedKey>
  probe?: ReturnType<typeof makeGatewayProbe>
  counts?: ReturnType<typeof makeSeries>
  connectors?: ReturnType<typeof makeConnector>[]
  retrieval?: ReturnType<typeof makeRetrievalPreview>
  saveError?: { status: number; code: string; message: string; param?: string }
}

/**
 * A scripted server, not a mocked client — the real `ApiClient` is exercised so a test can
 * assert what actually went on the wire. For this screen that is the point twice over:
 * whether `slug` was sent on a PATCH, and whether the minted token ever appears anywhere
 * except the dialog that showed it.
 */
function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  const gateways = options.gateways ?? [makeGateway()]
  const apiKeys = options.apiKeys ?? [makeApiKey()]
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

    if (path.endsWith('/try-retrieval') && method === 'POST') {
      return Promise.resolve(json(options.retrieval ?? makeRetrievalPreview()))
    }
    if (path.endsWith('/prompt-preview') && method === 'POST') {
      return Promise.resolve(
        json(makePromptPreview({ retrieval: options.retrieval ?? makeRetrievalPreview() })),
      )
    }
    if (path.startsWith('/api/v1/connectors')) {
      return Promise.resolve(
        json({ items: options.connectors ?? [makeConnector()], next_cursor: null }),
      )
    }
    if (path.endsWith('/test') && method === 'POST') {
      return Promise.resolve(json(options.probe ?? makeGatewayProbe()))
    }
    if (path.endsWith('/keys') && method === 'POST') {
      return Promise.resolve(json(options.issued ?? makeIssuedKey(), 201))
    }
    if (path.endsWith('/keys') && method === 'GET') {
      return Promise.resolve(json(apiKeys))
    }
    if (path.startsWith('/api/v1/keys/') && method === 'DELETE') {
      return Promise.resolve(json(makeApiKey({ revoked_at: '2026-09-07T12:00:00Z' })))
    }
    if (path === '/api/v1/gateways' && method === 'POST') {
      const failure = options.saveError
      if (failure) return Promise.resolve(json({ error: failure }, failure.status))
      return Promise.resolve(json(makeGateway(bodyOf(init)), 201))
    }
    if (path.startsWith('/api/v1/gateways/') && method === 'PATCH') {
      const failure = options.saveError
      if (failure) return Promise.resolve(json({ error: failure }, failure.status))
      return Promise.resolve(json(makeGateway(bodyOf(init))))
    }
    if (path.startsWith('/api/v1/gateways/') && method === 'DELETE') {
      return Promise.resolve(new Response(null, { status: 204 }))
    }
    if (path.startsWith('/api/v1/gateways') && method === 'GET') {
      if (path.startsWith('/api/v1/gateways/')) {
        const id = path.split('/').pop()
        return Promise.resolve(json(gateways.find((row) => row.id === id) ?? gateways[0]!))
      }
      return Promise.resolve(json({ items: gateways, next_cursor: null }))
    }
    if (path.startsWith('/api/v1/models')) {
      // Two, because a routing chain needs somewhere to route to: with one model in the
      // catalog every failover and A/B assertion below would be about an empty picker.
      return Promise.resolve(
        json({
          items: [makeModel(), makeModel({ id: 'mo2', name: 'acme-mini' })],
          next_cursor: null,
        }),
      )
    }
    // The list's 24-hour column: one grouped series for the whole page.
    if (path.startsWith('/api/v1/metrics/timeseries')) {
      return Promise.resolve(json(options.counts ?? makeSeries([])))
    }

    throw new Error(`unexpected ${method} ${path}`)
  })

  return { client: new ApiClient(impl), requests, gateways, apiKeys }
}

function renderAt(client: ApiClient, path: string) {
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

const lastBody = (requests: { method: string; body: Record<string, unknown> }[], method: string) =>
  requests.filter((request) => request.method === method).at(-1)!.body

// ---------------------------------------------------------------------------

describe('the gateways list', () => {
  it('shows the endpoint URL with a copy button', async () => {
    // The single most common action on this screen: it is what goes into `base_url`.
    const { client } = fakeServer()
    renderAt(client, '/gateways')

    expect(
      await screen.findByText('https://localhost:8000/g/acme-support/v1'),
    ).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /copy/i }).length).toBeGreaterThan(0)
  })

  it('shows the last day\'s request count from one grouped query', async () => {
    // One query for the page, not one per row — and a real zero for a gateway with no
    // traffic, which is a different answer from "not measured".
    const { client, requests } = fakeServer({
      counts: makeSeries([
        { start: '2026-09-06T00:00:00Z', series: { 'g1.requests': 41 } },
        { start: '2026-09-07T00:00:00Z', series: { 'g1.requests': 9 } },
      ]),
    })
    renderAt(client, '/gateways')

    expect(await screen.findByText('50')).toBeInTheDocument()
    expect(
      requests.filter((request) => request.path.startsWith('/api/v1/metrics/timeseries')),
    ).toHaveLength(1)
  })

  it('names the primary and counts the rest of a chain', async () => {
    // Naming only the first would make a two-model gateway look like a one-model one,
    // which is the reading that matters: a disabled *secondary* is a failover that will
    // not work, and it is only visible if the count says there is one.
    const { client } = fakeServer({
      gateways: [
        makeGateway({
          routing_mode: 'failover',
          targets: [
            { id: 'mo1', name: 'acme-gpt', dialect: 'openai', enabled: true, organization_id: 'o1', priority: 0, weight: 100 },
            { id: 'mo2', name: 'acme-mini', dialect: 'openai', enabled: false, organization_id: 'o1', priority: 1, weight: 100 },
          ],
        }),
      ],
    })
    renderAt(client, '/gateways')

    const table = await screen.findByRole('table')
    expect(await within(table).findByText('+1')).toBeInTheDocument()
    expect(within(table).getByText('1 of 2 models disabled')).toBeInTheDocument()
  })

  it('shows the target model and the key count', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways')

    expect(await screen.findByText('acme-gpt')).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()
  })

  it('warns when a gateway has no model', async () => {
    const { client } = fakeServer({ gateways: [makeGateway({ targets: [] })] })
    renderAt(client, '/gateways')

    expect(await screen.findByText(/No model — requests will fail/)).toBeInTheDocument()
  })

  it('warns when the target model is switched off', async () => {
    const { client } = fakeServer({
      gateways: [
        makeGateway({
          targets: [
            {
              id: 'mo1',
              name: 'acme-gpt',
              dialect: 'openai',
              enabled: false,
              organization_id: 'o1',
              priority: 0,
              weight: 100,
            },
          ],
        }),
      ],
    })
    renderAt(client, '/gateways')

    expect(await screen.findByText('Model is disabled')).toBeInTheDocument()
  })

  it('hides the create button from a viewer', async () => {
    const { client } = fakeServer({
      user: makeUser({ role: 'org_viewer', capabilities: ['org:read'] }),
    })
    renderAt(client, '/gateways')

    await screen.findByText('Support Bot')
    expect(screen.queryByRole('link', { name: 'New gateway' })).not.toBeInTheDocument()
  })

  it('teaches rather than shrugging when there is nothing yet', async () => {
    const { client } = fakeServer({ gateways: [] })
    renderAt(client, '/gateways')

    expect(await screen.findByText('No gateways yet')).toBeInTheDocument()
    expect(screen.getByText(/OpenAI-compatible endpoint/)).toBeInTheDocument()
  })
})

describe('creating a gateway', () => {
  it('suggests a slug from the name and previews the URL', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/new')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Name'), 'Support Bot')

    expect(await screen.findByLabelText('Slug')).toHaveValue('support-bot')
    expect(screen.getByText('…/g/support-bot/v1')).toBeInTheDocument()
  })

  it('stops suggesting once the slug has been typed into', async () => {
    // After that the slug is the user's: it becomes a URL they have to live with.
    const { client } = fakeServer()
    renderAt(client, '/gateways/new')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Slug'), 'acme-support')
    await person.type(await screen.findByLabelText('Name'), 'Something Else')

    expect(screen.getByLabelText('Slug')).toHaveValue('acme-support')
  })

  it('sends the slug on create', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/new')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Name'), 'Support Bot')
    await person.click(screen.getByRole('button', { name: 'Create gateway' }))

    await waitFor(() => expect(lastBody(requests, 'POST').slug).toBe('support-bot'))
  })

  it('puts a server-side field error on the field it names', async () => {
    const { client } = fakeServer({
      saveError: {
        status: 409,
        code: 'conflict',
        message: "The slug 'acme-support' is already in use.",
        param: 'slug',
      },
    })
    renderAt(client, '/gateways/new')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Name'), 'Support Bot')
    await person.click(screen.getByRole('button', { name: 'Create gateway' }))

    expect(await screen.findByText(/already in use/)).toBeInTheDocument()
  })
})

describe('the editor', () => {
  it('shows every section, including the ones later releases fill', async () => {
    // Hidden sections would make the editor look finished and then move everything below
    // them when they arrive.
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    expect(await screen.findByRole('heading', { name: 'Identity' })).toBeInTheDocument()
    for (const name of ['Routing', 'Memory', 'Prompt', 'Logging', 'Limits', 'Keys']) {
      expect(screen.getByRole('heading', { name })).toBeInTheDocument()
    }
    // Limits alone, now that Logging and Memory are built. The count is asserted rather
    // than left implicit so filling one in has to come here and say so.
    expect(screen.getAllByText('Coming soon')).toHaveLength(1)
  })

  it('makes the slug read-only and says why', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    expect(await screen.findByLabelText('Slug')).toHaveAttribute('readonly')
    expect(screen.getByText(/in the URL your clients already use/)).toBeInTheDocument()
  })

  it('never sends the slug on a save', async () => {
    // The API refuses it, and sending one the user cannot have changed would turn every
    // save into a 422.
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Name'), ' updated')
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(lastBody(requests, 'PATCH')).not.toHaveProperty('slug'))
  })

  it('offers a clone as the way to get a different slug', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    expect(
      await screen.findByRole('button', { name: 'Clone with a new slug' }),
    ).toBeInTheDocument()
  })

  it('pre-fills a clone from the original but leaves the slug empty', async () => {
    const { client } = fakeServer({
      gateways: [makeGateway({ system_context: 'Be concise.' })],
    })
    renderAt(client, '/gateways/new?clone=g1')

    await waitFor(() =>
      expect(screen.getByLabelText('Name')).toHaveValue('Support Bot (copy)'),
    )
    expect(screen.getByLabelText('Slug')).toHaveValue('')
    expect(screen.getByLabelText(/System context/)).toHaveValue('Be concise.')
  })

  it('flags unsaved changes', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Name'), '!')

    expect(await screen.findByText('Unsaved changes')).toBeInTheDocument()
  })

  it('says which target answered when the primary did not', async () => {
    // "OK" and "OK, on the second target" would be the same green box otherwise, and
    // only one of them is a gateway somebody needs to go and look at.
    const { client } = fakeServer({
      probe: makeGatewayProbe({
        model_name: 'acme-mini',
        attempts: [
          {
            target_id: 'mo1',
            model_name: 'acme-gpt',
            status: 503,
            error_code: 'upstream_error',
            latency_ms: 120,
            retryable: true,
          },
          {
            target_id: 'mo2',
            model_name: 'acme-mini',
            status: 200,
            error_code: null,
            latency_ms: 300,
            retryable: false,
          },
        ],
      }),
    })
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Send test message' }))

    const result = await screen.findByRole('status')
    expect(within(result).getByText(/answered by acme-mini after 1 failed attempt/)).toBeInTheDocument()
    expect(within(result).getByText('upstream_error')).toBeInTheDocument()
  })

  it('offers all three routing modes', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    const mode = await screen.findByLabelText('Mode')
    const options = within(mode).getAllByRole<HTMLOptionElement>('option')
    expect(options.filter((option) => !option.disabled).map((option) => option.value)).toEqual([
      'single',
      'failover',
      'ab_split',
    ])
  })

  it('describes each mode by what happens when it fails', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.selectOptions(await screen.findByLabelText('Mode'), 'failover')

    expect(await screen.findByText(/moves to the next one/)).toBeInTheDocument()
  })

  it('warns that a stream cannot fail over once output has begun', async () => {
    // Genuinely surprising behaviour, and a release note is not where anybody reads it.
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.selectOptions(await screen.findByLabelText('Mode'), 'failover')

    expect(
      await screen.findByText(/cannot fail over once\s+output has begun/),
    ).toBeInTheDocument()
  })

  it('saves a failover chain in the order the buttons put it in', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.selectOptions(await screen.findByLabelText('Mode'), 'failover')
    await person.selectOptions(await screen.findByLabelText('Target 2'), 'mo2')
    await person.click(screen.getByLabelText('Move target 2 up'))
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    const saved = await waitFor(() => {
      const patch = requests.find((request) => request.method === 'PATCH')
      expect(patch).toBeDefined()
      return patch!
    })
    expect(saved.body.targets).toEqual([
      { model_id: 'mo2', weight: 100 },
      { model_id: 'mo1', weight: 100 },
    ])
  })

  it('sends the chain rather than the single-target shorthand', async () => {
    // Both together are a 422: the server refuses to guess which one meant it.
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Save changes' }))

    const saved = await waitFor(() => {
      const patch = requests.find((request) => request.method === 'PATCH')
      expect(patch).toBeDefined()
      return patch!
    })
    expect(saved.body).not.toHaveProperty('model_id')
    expect(saved.body.targets).toEqual([{ model_id: 'mo1', weight: 100 }])
  })

  it('blocks the save until A/B weights add up to a hundred', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.selectOptions(await screen.findByLabelText('Mode'), 'ab_split')
    await person.selectOptions(await screen.findByLabelText('Target 2'), 'mo2')

    // Two rows at 100 and 0: a legal-looking pair that is not a split.
    const save = screen.getByRole('button', { name: 'Save changes' })
    expect(save).toBeEnabled()

    const weight = screen.getByLabelText('Weight percentage for target 1')
    await person.clear(weight)
    await person.type(weight, '70')

    expect(await screen.findByText(/must add up to 100. These add up to 70/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })

  it('shows the split it will actually produce, not the numbers typed', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.selectOptions(await screen.findByLabelText('Mode'), 'ab_split')
    await person.selectOptions(await screen.findByLabelText('Target 2'), 'mo2')
    const first = screen.getByLabelText('Weight percentage for target 1')
    await person.clear(first)
    await person.type(first, '70')
    const second = screen.getByLabelText('Weight percentage for target 2')
    await person.clear(second)
    await person.type(second, '20')

    // 70 and 20 is not a 70/20 split. It is a 78/22 split the server will refuse, and
    // the bar is where that becomes obvious rather than a surprise after saving.
    expect(await screen.findByText('90 / 100')).toBeInTheDocument()
    expect(screen.getByText(/78% · 22%/)).toBeInTheDocument()
  })

  it('puts a rejected chain on the routing section rather than in a banner', async () => {
    const { client } = fakeServer({
      saveError: {
        status: 422,
        code: 'validation_error',
        message: 'A/B weights are percentages and must add up to 100.',
        param: 'targets',
      },
    })
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Save changes' }))

    expect(
      await screen.findByText('A/B weights are percentages and must add up to 100.'),
    ).toBeInTheDocument()
  })

  it('previews the assembled system message', async () => {
    const { client } = fakeServer({
      gateways: [makeGateway({ system_context: "You are Acme's support assistant." })],
    })
    renderAt(client, '/gateways/g1')

    expect(await screen.findByText('Assembled system message')).toBeInTheDocument()
    expect(screen.getByText(/Test gateway below shows the prompt/)).toBeInTheDocument()
  })

  it('sends locked parameters separately from defaults', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    const locked = await screen.findByLabelText('Locked parameters')
    await person.clear(locked)
    await person.type(locked, '{{"temperature": 0.2}')
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(lastBody(requests, 'PATCH').locked_params).toEqual({ temperature: 0.2 }),
    )
  })

  it('refuses to save parameters that are not JSON', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    const overrides = await screen.findByLabelText('Parameter defaults')
    await person.clear(overrides)
    await person.type(overrides, 'temperature = 0.2')
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText(/Not valid JSON/)).toBeInTheDocument()
    expect(requests.some((request) => request.method === 'PATCH')).toBe(false)
  })

  it('makes deleting require the slug to be typed', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Delete gateway' }))

    const dialog = await screen.findByRole('dialog', { name: 'Delete this gateway?' })
    expect(within(dialog).getByRole('button', { name: 'Delete gateway' })).toBeDisabled()

    await person.type(within(dialog).getByRole('textbox'), 'acme-support')
    expect(within(dialog).getByRole('button', { name: 'Delete gateway' })).toBeEnabled()
  })
})

describe('keys', () => {
  it('shows the prefix, never a secret', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    expect(await screen.findByText('mg_1a2b3c4d…')).toBeInTheDocument()
  })

  it('says a key has never been used rather than showing a blank', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    expect(await screen.findByText('Never')).toBeInTheDocument()
  })

  it('reveals a new key once, with the warning above the value', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Create key' }))
    const form = await screen.findByRole('dialog', { name: 'Create an API key' })
    await person.type(within(form).getByLabelText('Name'), 'production')
    await person.click(within(form).getByRole('button', { name: 'Create key' }))

    const dialog = await screen.findByRole('alertdialog', { name: 'Copy your API key' })
    expect(within(dialog).getByText('mg_1a2b3c4d_shown-exactly-once')).toBeInTheDocument()
    expect(within(dialog).getByText(/only time this key will be shown/)).toBeInTheDocument()
  })

  it('forgets the secret once the dialog is dismissed', async () => {
    // Nothing caches it: a copy of a secret that cannot be shown twice is a copy too many.
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Create key' }))
    const form = await screen.findByRole('dialog', { name: 'Create an API key' })
    await person.type(within(form).getByLabelText('Name'), 'production')
    await person.click(within(form).getByRole('button', { name: 'Create key' }))
    await person.click(await screen.findByRole('button', { name: 'I have copied it' }))

    await waitFor(() =>
      expect(screen.queryByText('mg_1a2b3c4d_shown-exactly-once')).not.toBeInTheDocument(),
    )
  })

  it('hides key management from a role that may not mint one', async () => {
    // `org_member` configures the endpoint and cannot issue a bearer credential for it.
    const { client } = fakeServer({
      user: makeUser({ role: 'org_member', capabilities: ['org:read', 'resources:write'] }),
    })
    renderAt(client, '/gateways/g1')

    await screen.findByRole('heading', { name: 'Keys' })
    expect(screen.queryByRole('button', { name: 'Create key' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revoke' })).not.toBeInTheDocument()
  })

  it('makes revoking require the key name to be typed', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Revoke' }))

    const dialog = await screen.findByRole('dialog', { name: 'Revoke this key?' })
    expect(within(dialog).getByRole('button', { name: 'Revoke' })).toBeDisabled()

    await person.type(within(dialog).getByRole('textbox'), 'production')
    expect(within(dialog).getByRole('button', { name: 'Revoke' })).toBeEnabled()
  })

  it('shows a revoked key rather than hiding it', async () => {
    const { client } = fakeServer({
      apiKeys: [makeApiKey({ revoked_at: '2026-09-07T12:00:00Z' })],
    })
    renderAt(client, '/gateways/g1')

    expect(await screen.findByText('revoked')).toBeInTheDocument()
  })
})

describe('testing a gateway', () => {
  it('shows the assembled prompt, the answer and the timings', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Send test message' }))

    const panel = await screen.findByRole('status')
    expect(within(panel).getByText('OK, 412 ms')).toBeInTheDocument()
    expect(within(panel).getByText(/support assistant/)).toBeInTheDocument()
    expect(within(panel).getByText('Hello — how can I help?')).toBeInTheDocument()
    expect(within(panel).getByText(/380 ms upstream/)).toBeInTheDocument()
  })

  it('shows the upstream error verbatim when it failed', async () => {
    // "401 invalid_api_key" is the whole answer; paraphrasing loses the searchable part.
    const { client } = fakeServer({
      probe: makeGatewayProbe({
        ok: false,
        upstream_status: 401,
        error_message: '[upstream:acme-gpt] Incorrect API key provided.',
        content: null,
      }),
    })
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Send test message' }))

    const panel = await screen.findByRole('status')
    expect(within(panel).getByText('Failed — HTTP 401')).toBeInTheDocument()
    expect(within(panel).getByText(/Incorrect API key provided/)).toBeInTheDocument()
  })

  it('names a parameter the gateway overrode', async () => {
    const { client } = fakeServer({
      probe: makeGatewayProbe({ locked_overrides: ['max_tokens'] }),
    })
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Send test message' }))

    expect(await screen.findByText(/Locked by this gateway: max_tokens/)).toBeInTheDocument()
  })
})

describe('the logging section', () => {
  it('says plainly that body capture stores end-user content', async () => {
    // SPEC §10.2's data-handling note, on the form rather than in the documentation. An
    // organization that has not thought about it should be told here, not in a
    // subject-access request.
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    expect(await screen.findByText(/stores end-user content/i)).toBeInTheDocument()
  })

  it('shows the effective retention as a sentence, not just a number', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    expect(await screen.findByText(/kept for/i)).toBeInTheDocument()
    expect(screen.getByText(/30 days/)).toBeInTheDocument()
  })

  it('changes what it says when every body toggle is off', async () => {
    const person = userEvent.setup()
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    await screen.findByRole('heading', { name: 'Logging' })
    for (const label of [
      /the client's request/i,
      /assembled prompt sent upstream/i,
      /^the response/i,
    ]) {
      await person.click(screen.getByLabelText(label))
    }

    expect(screen.getByText(/Bodies are not stored/)).toBeInTheDocument()
  })

  it('will not let metadata logging be switched off', async () => {
    // It is what the monitoring charts are made of, and the schema forces it true.
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    const metadata = await screen.findByLabelText(/Metadata/)
    expect(metadata).toBeChecked()
    expect(metadata).toBeDisabled()
  })

  it('sends the logging section as a partial object', async () => {
    // Deep-merged server-side, so this form cannot wipe the Memory or Limits sections
    // that later tasks add beside it.
    const person = userEvent.setup()
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1')

    await screen.findByRole('heading', { name: 'Logging' })
    await person.click(screen.getByLabelText(/^the response/i))
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      const body = lastBody(requests, 'PATCH')
      expect(body.logging_config).toMatchObject({ log_response_body: false })
    })
  })

  it('sends redaction patterns one per line', async () => {
    // A textarea rather than JSON: every backslash in a regular expression would have to
    // be doubled in a JSON string, which is how `\\d` silently becomes `d`.
    const person = userEvent.setup()
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1')

    const patterns = await screen.findByLabelText('Redaction patterns')
    await person.clear(patterns)
    await person.type(patterns, 'first{enter}   {enter}second')
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      const body = lastBody(requests, 'PATCH') as { logging_config: { redaction_patterns: string[] } }
      expect(body.logging_config.redaction_patterns).toEqual(['first', 'second'])
    })
  })

  it('warns before saving a combination the server will refuse', async () => {
    const person = userEvent.setup()
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    await screen.findByRole('heading', { name: 'Logging' })
    await person.click(screen.getByLabelText(/the client's request/i))

    expect(screen.getByText(/Distillation reads logged request bodies/)).toBeInTheDocument()
  })

  it('shows a redaction error from the server on the field it belongs to', async () => {
    const person = userEvent.setup()
    const { client } = fakeServer({
      saveError: {
        status: 422,
        code: 'validation_error',
        message: "redaction_patterns: '(a+)+' repeats a group that itself repeats",
        // Dotted, as the server sends it. The section makes the message unambiguous; the
        // leaf is what this form has an input for.
        param: 'logging_config.redaction_patterns',
      },
    })
    renderAt(client, '/gateways/g1')

    await screen.findByRole('heading', { name: 'Logging' })
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText(/repeats a group that itself repeats/)).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// memory (task 10)
// ---------------------------------------------------------------------------

describe('the memory section', () => {
  it('offers this organization’s connectors with what is in them', async () => {
    const { client } = fakeServer({
      connectors: [makeConnector({ name: 'Product docs', counts: { indexed: 4 } })],
    })
    renderAt(client, '/gateways/g1')

    expect(await screen.findByLabelText(/Product docs/)).toBeInTheDocument()
    expect(screen.getByText('4 documents indexed')).toBeInTheDocument()
  })

  it('says plainly that a gateway with no connectors retrieves nothing', async () => {
    // The default state, and the answer to "why does it not use my documents".
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    expect(await screen.findByText(/retrieves nothing/)).toBeInTheDocument()
  })

  it('saves the attached connectors and the retrieval knobs', async () => {
    const { client, requests } = fakeServer({
      connectors: [makeConnector({ id: 'cn1', name: 'Product docs' })],
    })
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(await screen.findByLabelText(/Product docs/))
    await person.clear(screen.getByLabelText('Minimum score'))
    await person.type(screen.getByLabelText('Minimum score'), '0.5')
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(requests.some((r) => r.method === 'PATCH')).toBe(true))
    const body = lastBody(requests, 'PATCH').memory_config as Record<string, unknown>
    expect(body.connector_ids).toEqual(['cn1'])
    expect(body.doc_min_score).toBe(0.5)
  })

  it('saves both halves of memory together, and still sends a partial', async () => {
    // The blob is deep-merged server-side, so a key this form omits is a key it cannot
    // wipe. That is what lets a later task add a field to the same blob without this
    // section resetting it on every save.
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.clear(await screen.findByLabelText('Chunks to retrieve'))
    await person.type(screen.getByLabelText('Chunks to retrieve'), '8')
    await person.clear(screen.getByLabelText('Facts to recall'))
    await person.type(screen.getByLabelText('Facts to recall'), '4')
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(requests.some((r) => r.method === 'PATCH')).toBe(true))
    const body = lastBody(requests, 'PATCH').memory_config as Record<string, unknown>
    expect(body.doc_top_k).toBe(8)
    expect(body.memory_top_k).toBe(4)
    expect(body).not.toHaveProperty('dedupe_threshold')
  })

  it('warns that conversation memory needs an identity the caller has to send', async () => {
    // The silent failure it prevents: memory on, every request anonymous, nothing ever
    // stored, and no error anywhere to explain it.
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    expect(await screen.findByText(/Send X-Gateway-User/)).toBeInTheDocument()
  })

  it('hides the conversation-memory knobs when it is switched off', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()
    await screen.findByLabelText('Facts to recall')

    await person.click(screen.getByLabelText(/Remember the person asking/))

    expect(screen.queryByLabelText('Facts to recall')).not.toBeInTheDocument()
  })

  it('refuses to save a score the server would reject, before the round trip', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.clear(await screen.findByLabelText('Minimum score'))
    await person.type(screen.getByLabelText('Minimum score'), '5')

    expect(screen.getByText(/cosine similarity/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
    expect(requests.filter((request) => request.method === 'PATCH')).toHaveLength(0)
  })

  it('hides the turn count until the strategy uses it', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    expect(screen.queryByLabelText('Turns to include')).not.toBeInTheDocument()
    await person.selectOptions(
      await screen.findByLabelText('What to search for'),
      'last_n_turns',
    )

    expect(screen.getByLabelText('Turns to include')).toBeInTheDocument()
  })

  it('says what the failure policy actually does, as a consequence', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.selectOptions(
      await screen.findByLabelText('If retrieval fails'),
      'fail_closed',
    )

    expect(screen.getByText(/refused with a 503/)).toBeInTheDocument()
  })
})

describe('try retrieval', () => {
  it('shows the chunks that would be injected, with their scores', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Question'), 'how do refunds work')
    await person.click(screen.getByRole('button', { name: 'Try retrieval' }))

    expect(await screen.findByText('0.71')).toBeInTheDocument()
    expect(screen.getByText(/handbook/)).toBeInTheDocument()
    expect(screen.getByText(/1 chunk would be injected/)).toBeInTheDocument()
  })

  it('sends the unsaved settings, so tuning does not change the live endpoint', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.clear(await screen.findByLabelText('Minimum score'))
    await person.type(screen.getByLabelText('Minimum score'), '0.6')
    await person.type(screen.getByLabelText('Question'), 'refunds')
    await person.click(screen.getByRole('button', { name: 'Try retrieval' }))

    await waitFor(() =>
      expect(requests.some((r) => r.path.endsWith('/try-retrieval'))).toBe(true),
    )
    const body = requests.find((r) => r.path.endsWith('/try-retrieval'))!.body
    expect((body.memory_config as Record<string, unknown>).doc_min_score).toBe(0.6)
    // And nothing was saved.
    expect(requests.filter((request) => request.method === 'PATCH')).toHaveLength(0)
  })

  it('marks a chunk that would not survive the token budget', async () => {
    // The interesting failure: the right passage was found and fell off the end.
    const { client } = fakeServer({
      retrieval: makeRetrievalPreview({
        chunks: [makeRetrievedChunk(), makeRetrievedChunk({ id: 'ch2', injected: false })],
      }),
    })
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Question'), 'refunds')
    await person.click(screen.getByRole('button', { name: 'Try retrieval' }))

    expect(await screen.findByText('over budget')).toBeInTheDocument()
    expect(screen.getByText(/1 dropped/)).toBeInTheDocument()
  })

  it('says which kind of empty an empty result is', async () => {
    const { client } = fakeServer({
      retrieval: makeRetrievalPreview({ outcome: 'empty', chunks: [], injected_tokens: 0 }),
    })
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Question'), 'refunds')
    await person.click(screen.getByRole('button', { name: 'Try retrieval' }))

    expect(await screen.findByText(/score floor is too high/)).toBeInTheDocument()
  })

  it('shows a retrieval failure as a diagnostic rather than an outage', async () => {
    const { client } = fakeServer({
      retrieval: makeRetrievalPreview({
        outcome: 'timeout',
        chunks: [],
        error: 'The knowledge base did not answer within 800 ms.',
      }),
    })
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Question'), 'refunds')
    await person.click(screen.getByRole('button', { name: 'Try retrieval' }))

    expect(await screen.findByText(/did not answer within 800 ms/)).toBeInTheDocument()
  })

  it('draws the assembled prompt layer by layer with a token count each', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Question'), 'refunds')
    await person.click(screen.getByRole('button', { name: 'Show the whole prompt' }))

    expect(await screen.findByText('Documents')).toBeInTheDocument()
    expect(screen.getByText('61 tokens')).toBeInTheDocument()
  })

  it('cannot be run before the gateway has been saved once', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/new')

    expect(await screen.findByText('Save the gateway first.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try retrieval' })).toBeDisabled()
  })
})
