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
  makeGateway,
  makeGatewayProbe,
  makeIssuedKey,
  makeModel,
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
      return Promise.resolve(json({ items: [makeModel()], next_cursor: null }))
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
    // Memory and Limits, now that Logging is built. The count is asserted rather than
    // left implicit so filling one in has to come here and say so.
    expect(screen.getAllByText('Coming soon')).toHaveLength(2)
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

  it('offers only the routing mode this build serves', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')

    const mode = await screen.findByLabelText('Mode')
    const options = within(mode).getAllByRole<HTMLOptionElement>('option')
    expect(options.filter((option) => !option.disabled).map((option) => option.value)).toEqual([
      'single',
    ])
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
