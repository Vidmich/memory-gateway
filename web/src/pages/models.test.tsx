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
  makeCalibration,
  makeEffectiveTokenizer,
  makeGlobalModel,
  makeModel,
  makeProbe,
  makeSuperadmin,
  makeTokenizers,
  makeUser,
} from '@/test/factories'
import { bodyOf, jsonResponse as json, pathOf } from '@/test/http'
import { presetFor } from '@/pages/providerPresets'

type ServerOptions = {
  user?: ReturnType<typeof makeUser>
  models?: ReturnType<typeof makeModel>[]
  probe?: ReturnType<typeof makeProbe>
  /** Error to answer a DELETE with — the referenced-model guard, usually. */
  deleteError?: { status: number; code: string; message: string; details?: unknown }
  /** Error to answer a POST/PATCH with. */
  saveError?: { status: number; code: string; message: string; param?: string }
  /** Task 101: what `GET /models/calibration` answers. */
  calibrations?: ReturnType<typeof makeCalibration>[]
}

/**
 * A scripted server, not a mocked client — the real `ApiClient` is exercised so a test
 * can assert what actually went on the wire, which for this screen is the whole point:
 * whether `credential` was sent at all is the difference between keeping a key and
 * clearing it.
 */
function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  const models = options.models ?? [makeModel(), makeGlobalModel()]
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

    if (path.startsWith('/api/v1/models') && path.endsWith('/test') && method === 'POST') {
      return Promise.resolve(json(options.probe ?? makeProbe()))
    }
    // Task 101. Before the models prefix, which would otherwise answer these as a page.
    if (path === '/api/v1/tokenizers') return Promise.resolve(json(makeTokenizers()))
    if (path === '/api/v1/models/calibration') {
      return Promise.resolve(json(options.calibrations ?? []))
    }
    if (path.endsWith('/calibrate') && method === 'POST') {
      const proposed = options.calibrations?.[0]?.proposed ?? null
      return Promise.resolve(
        json(
          makeModel({
            tokenizer: proposed,
            effective_tokenizer: makeEffectiveTokenizer({
              spec: proposed ?? { name: 'o200k_base', ratio: null },
              origin: 'override',
              name: 'approximate:3.365',
              label: 'approximate:3.365 (override)',
              approximate: true,
            }),
          }),
        ),
      )
    }
    if (path === '/api/v1/models' && method === 'POST') {
      const failure = options.saveError
      if (failure) {
        return Promise.resolve(json({ error: failure }, failure.status))
      }
      return Promise.resolve(json(makeModel(bodyOf(init)), 201))
    }
    if (path.startsWith('/api/v1/models/') && method === 'PATCH') {
      const failure = options.saveError
      if (failure) return Promise.resolve(json({ error: failure }, failure.status))
      return Promise.resolve(json(makeModel(bodyOf(init))))
    }
    if (path.startsWith('/api/v1/models/') && method === 'DELETE') {
      const failure = options.deleteError
      if (failure) return Promise.resolve(json({ error: failure }, failure.status))
      return Promise.resolve(new Response(null, { status: 204 }))
    }
    if (path.startsWith('/api/v1/models') && method === 'GET') {
      if (path.startsWith('/api/v1/models/')) {
        const id = path.split('/').pop()
        const found = models.find((model) => model.id === id) ?? models[0]!
        return Promise.resolve(json(found))
      }
      const scope = new URL(`http://x${path}`).searchParams.get('scope')
      const visible = scope ? models.filter((model) => model.scope === scope) : models
      return Promise.resolve(json({ items: visible, next_cursor: null }))
    }
    if (path.startsWith('/api/v1/organizations')) {
      return Promise.resolve(json({ items: [], next_cursor: null }))
    }

    throw new Error(`unexpected ${method} ${path}`)
  })

  return { client: new ApiClient(impl), requests, models }
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

describe('the models list', () => {
  it('starts on our models and asks the server to filter', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/models')

    expect(await screen.findByText('acme-gpt')).toBeInTheDocument()
    expect(requests.some((request) => request.path.includes('scope=org'))).toBe(true)
  })

  it('switches to the global catalog', async () => {
    const { client } = fakeServer()
    renderAt(client, '/models')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Global catalog' }))

    expect(await screen.findByText('shared-gpt-4o')).toBeInTheDocument()
  })

  it('shows a credential hint but never a credential', async () => {
    const { client } = fakeServer()
    renderAt(client, '/models')

    expect(await screen.findByText('sk-...4f2a')).toBeInTheDocument()
  })

  it('marks a model the caller does not own as read-only', async () => {
    const { client } = fakeServer({ models: [makeGlobalModel()] })
    renderAt(client, '/models')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Global catalog' }))

    expect(await screen.findByText('Read-only')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
  })

  it('offers no create button to a viewer', async () => {
    const { client } = fakeServer({
      user: makeUser({ role: 'org_viewer', capabilities: ['org:read'] }),
    })
    renderAt(client, '/models')

    await screen.findByText('acme-gpt')
    expect(screen.queryByRole('link', { name: 'New model' })).not.toBeInTheDocument()
  })

  it('requires the typed name before deleting', async () => {
    const { client } = fakeServer({ models: [makeModel()] })
    renderAt(client, '/models')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('dialog')
    const confirm = within(dialog).getByRole('button', { name: 'Delete' })

    expect(confirm).toBeDisabled()
    await person.type(within(dialog).getByRole('textbox'), 'acme-gpt')
    expect(confirm).toBeEnabled()
  })

  it('shows the referencing gateways when a delete is blocked', async () => {
    const { client } = fakeServer({
      models: [makeModel()],
      deleteError: {
        status: 409,
        code: 'conflict',
        message: "This model is still used by the gateway 'Acme Chat'. Point them at another model.",
        details: { gateways: [{ id: 'g1', slug: 'acme-chat', name: 'Acme Chat' }] },
      },
    })
    renderAt(client, '/models')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('dialog')
    await person.type(within(dialog).getByRole('textbox'), 'acme-gpt')
    await person.click(within(dialog).getByRole('button', { name: 'Delete' }))

    expect(await screen.findByText(/still used by the gateway 'Acme Chat'/)).toBeInTheDocument()
    // The message says what to do; the list says where.
    expect(screen.getByText('/acme-chat')).toBeInTheDocument()
  })
})

describe('the model form', () => {
  it('prefills a base URL from a provider preset', async () => {
    const { client } = fakeServer()
    renderAt(client, '/models/new')
    const person = userEvent.setup()

    await person.selectOptions(await screen.findByLabelText('Preset'), 'groq')

    expect(screen.getByLabelText('Base URL')).toHaveValue('https://api.groq.com/openai/v1')
  })

  it('recognises a saved model as the preset it came from', () => {
    expect(presetFor('https://api.openai.com/v1')).toBe('openai')
    expect(presetFor('https://internal.example.com/v1')).toBe('custom')
  })

  it('never loads a stored credential into the field', async () => {
    // There is no endpoint that returns one. The hint is all the evidence there is.
    const { client } = fakeServer()
    renderAt(client, '/models/mo1')

    const field = await screen.findByLabelText('Credential')
    expect(field).toHaveValue('')
    expect(screen.getByText(/A credential is stored \(sk-\.\.\.4f2a\)/)).toBeInTheDocument()
  })

  it('omits the credential on save when the field was left blank', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Description'), 'now described')
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(requests.some((r) => r.method === 'PATCH')).toBe(true))
    expect('credential' in lastBody(requests, 'PATCH')).toBe(false)
  })

  it('sends a null credential when removal is asked for', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.click(await screen.findByLabelText('Remove the stored credential'))
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(requests.some((r) => r.method === 'PATCH')).toBe(true))
    expect(lastBody(requests, 'PATCH').credential).toBeNull()
  })

  it('sends a typed credential as a replacement', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Credential'), 'sk-rotated')
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(requests.some((r) => r.method === 'PATCH')).toBe(true))
    expect(lastBody(requests, 'PATCH').credential).toBe('sk-rotated')
  })

  it('refuses to submit default params that are not JSON', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.clear(await screen.findByLabelText('Default parameters'))
    await person.type(screen.getByLabelText('Default parameters'), 'temperature: 0.2')
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText(/Not valid JSON/)).toBeInTheDocument()
    expect(requests.some((request) => request.method === 'PATCH')).toBe(false)
  })

  it("shows the server's parameter error against the parameters field", async () => {
    // The allowlist lives on the server (`app/services/params.py`); re-implementing it
    // here would produce two answers that drift.
    const { client } = fakeServer({
      saveError: {
        status: 422,
        code: 'validation_error',
        message: "'temprature' is not a generation parameter this gateway sets.",
        param: 'default_params.temprature',
      },
    })
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText(/'temprature' is not a generation parameter/)).toBeInTheDocument()
  })

  it('reports a successful probe with its latency', async () => {
    const { client } = fakeServer()
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Test connection' }))

    expect(await screen.findByText('OK, 340 ms')).toBeInTheDocument()
  })

  it("reports a failure with the upstream's own words", async () => {
    const { client } = fakeServer({
      probe: makeProbe({
        ok: false,
        latency_ms: 87,
        upstream_status: 401,
        error_message: 'invalid_api_key Incorrect API key provided.',
        model_echo: null,
      }),
    })
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Test connection' }))

    expect(await screen.findByText(/invalid_api_key Incorrect API key provided/)).toBeInTheDocument()
    expect(screen.getByText(/Failed — HTTP 401/)).toBeInTheDocument()
  })

  it('probes the saved model when no credential has been typed', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.click(await screen.findByRole('button', { name: 'Test connection' }))

    await waitFor(() =>
      expect(requests.some((r) => r.path === '/api/v1/models/mo1/test')).toBe(true),
    )
    expect(await screen.findByText(/Tested the saved configuration/)).toBeInTheDocument()
  })

  it('probes the values on screen once a credential is typed', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.type(await screen.findByLabelText('Credential'), 'sk-about-to-save')
    await person.click(screen.getByRole('button', { name: 'Test connection' }))

    await waitFor(() => expect(requests.some((r) => r.path === '/api/v1/models/test')).toBe(true))
    expect(lastBody(requests, 'POST').credential).toBe('sk-about-to-save')
  })

  it('offers the availability choice only to a platform administrator', async () => {
    const { client } = fakeServer({ user: makeSuperadmin() })
    renderAt(client, '/models/new')

    expect(await screen.findByLabelText('Availability')).toBeInTheDocument()
  })

  it('does not offer it to an org admin', async () => {
    const { client } = fakeServer()
    renderAt(client, '/models/new')

    await screen.findByLabelText('Base URL')
    expect(screen.queryByLabelText('Availability')).not.toBeInTheDocument()
  })

  it('disables every field on a model the caller does not own', async () => {
    const { client } = fakeServer({ models: [makeGlobalModel({ id: 'mo1' })] })
    renderAt(client, '/models/mo1')

    expect(await screen.findByLabelText('Base URL')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()
  })

  it('offers the anthropic dialect', async () => {
    const { client } = fakeServer()
    renderAt(client, '/models/new')

    const dialect = await screen.findByLabelText('Dialect')
    expect(within(dialect).getByText(/Anthropic/)).toBeInTheDocument()
  })

  it('names the parameters the anthropic dialect cannot carry', async () => {
    // The failure this prevents is silent: the request succeeds and the parameter does
    // nothing. Saying so on the form is cheaper than the support ticket.
    const { client } = fakeServer()
    renderAt(client, '/models/new')
    const person = userEvent.setup()

    await person.selectOptions(await screen.findByLabelText('Dialect'), 'anthropic')

    expect(screen.getByText(/presence_penalty/)).toBeInTheDocument()
    expect(screen.getByText(/are not sent/)).toBeInTheDocument()
  })

  it('says nothing about dropped parameters for an openai-shaped provider', async () => {
    const { client } = fakeServer()
    renderAt(client, '/models/new')

    expect(await screen.findByLabelText('Dialect')).toBeInTheDocument()
    expect(screen.queryByText(/presence_penalty/)).not.toBeInTheDocument()
  })

  it('the anthropic preset sets the dialect along with the url', async () => {
    // A Claude base URL with the openai dialect is a 404 on /chat/completions, and the
    // dropdown that would have prevented it is two fields further down the form.
    const { client, requests } = fakeServer()
    renderAt(client, '/models/new')
    const person = userEvent.setup()

    await person.selectOptions(await screen.findByLabelText('Preset'), 'anthropic')
    await person.type(screen.getByLabelText('Name'), 'claude')
    await person.type(screen.getByLabelText('Credential'), 'sk-ant-test')
    await person.click(screen.getByRole('button', { name: 'Create model' }))

    await waitFor(() => expect(requests.some((r) => r.method === 'POST')).toBe(true))
    const body = lastBody(requests, 'POST')
    expect(body.dialect).toBe('anthropic')
    expect(body.base_url).toBe('https://api.anthropic.com/v1')
    expect(body.auth_type).toBe('api_key_header')
  })

  it('choosing an openai-shaped preset afterwards puts the dialect back', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/models/new')
    const person = userEvent.setup()

    await person.selectOptions(await screen.findByLabelText('Preset'), 'anthropic')
    await person.selectOptions(screen.getByLabelText('Preset'), 'groq')
    await person.type(screen.getByLabelText('Name'), 'llama')
    await person.type(screen.getByLabelText('Credential'), 'gsk-test')
    await person.click(screen.getByRole('button', { name: 'Create model' }))

    await waitFor(() => expect(requests.some((r) => r.method === 'POST')).toBe(true))
    expect(lastBody(requests, 'POST').dialect).toBe('openai')
  })
})

describe('the tokenizer (task 101)', () => {
  it('shows the derived tokenizer greyed, with its origin', async () => {
    const { client } = fakeServer({ models: [makeModel()] })
    renderAt(client, '/models/mo1')

    expect(await screen.findByTestId('tokenizer-derived')).toHaveTextContent(
      'o200k_base (derived)',
    )
    // No override, so the save body says so and the model stays on derivation.
    expect(screen.getByLabelText('Override')).not.toBeChecked()
  })

  it('re-derives as the model id is typed', async () => {
    const { client } = fakeServer()
    renderAt(client, '/models/new')

    const modelId = await screen.findByLabelText(/model id/i)
    await userEvent.clear(modelId)
    await userEvent.type(modelId, 'gpt-4-turbo')
    expect(screen.getByTestId('tokenizer-derived')).toHaveTextContent('cl100k_base (derived)')

    await userEvent.clear(modelId)
    await userEvent.type(modelId, 'llama-3.3-70b')
    expect(screen.getByTestId('tokenizer-derived')).toHaveTextContent('approximate:4 (derived)')
  })

  it('sends an override, and null to clear it', async () => {
    const { client, requests } = fakeServer({ models: [makeModel()] })
    renderAt(client, '/models/mo1')

    await userEvent.click(await screen.findByLabelText('Override'))
    await userEvent.selectOptions(screen.getByLabelText('Encoding'), 'approximate')
    const ratio = screen.getByLabelText('Characters per token')
    await userEvent.clear(ratio)
    await userEvent.type(ratio, '3.6')
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      const patch = requests.find((entry) => entry.method === 'PATCH')
      expect(patch?.body.tokenizer).toEqual({ name: 'approximate', ratio: 3.6 })
    })

    await userEvent.click(screen.getByLabelText('Override'))
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => {
      const patches = requests.filter((entry) => entry.method === 'PATCH')
      expect(patches.at(-1)?.body.tokenizer).toBeNull()
    })
  })

  it('shows our count against the provider\'s and calibrates on request', async () => {
    const approximate = makeModel({
      tokenizer: { name: 'approximate', ratio: 3.5 },
      effective_tokenizer: makeEffectiveTokenizer({
        spec: { name: 'approximate', ratio: 3.5 },
        origin: 'override',
        name: 'approximate:3.5',
        label: 'approximate:3.5 (override)',
        approximate: true,
      }),
    })
    const { client, requests } = fakeServer({
      models: [approximate],
      calibrations: [
        makeCalibration({
          ratio: 1.04,
          samples: 3120,
          proposed: { name: 'approximate', ratio: 3.365 },
        }),
      ],
    })
    renderAt(client, '/models/mo1')

    const panel = await screen.findByTestId('calibration')
    expect(panel).toHaveTextContent('×1.04 over 3,120 requests')
    await userEvent.click(within(panel).getByRole('button', { name: /calibrate to 3\.365/i }))

    await waitFor(() => {
      expect(
        requests.some((entry) => entry.path === '/api/v1/models/mo1/calibrate' && entry.method === 'POST'),
      ).toBe(true)
    })
  })

  it('warns when the drift is past the line, and offers no button for a fixed vocabulary', async () => {
    const { client } = fakeServer({
      models: [makeModel()],
      calibrations: [makeCalibration({ ratio: 1.2, samples: 40, warns: true, proposed: null })],
    })
    renderAt(client, '/models/mo1')

    const panel = await screen.findByTestId('calibration')
    expect(within(panel).getByRole('status')).toHaveTextContent(/more than 15% off/i)
    expect(within(panel).queryByRole('button')).not.toBeInTheDocument()
    expect(panel).toHaveTextContent(/fixed vocabulary cannot be calibrated/i)
  })

  it('says when nothing has been measured yet', async () => {
    const { client } = fakeServer({ models: [makeModel()], calibrations: [] })
    renderAt(client, '/models/mo1')

    expect(await screen.findByTestId('calibration')).toHaveTextContent(/no requests measured yet/i)
  })
})

describe('the context window', () => {
  it('is sent as null when left blank, which means unknown rather than unlimited', async () => {
    // A model with no declared window skips the gateway's overflow guard entirely. The
    // alternative — a platform default — would start withholding memory from requests a
    // provider would have served.
    const { client, requests } = fakeServer()
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.clear(await screen.findByLabelText(/Context window/))
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(requests.some((r) => r.method === 'PATCH')).toBe(true))
    expect(lastBody(requests, 'PATCH').context_window).toBeNull()
  })

  it('is sent as a number when set', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/models/mo1')
    const person = userEvent.setup()

    await person.clear(await screen.findByLabelText(/Context window/))
    await person.type(screen.getByLabelText(/Context window/), '8192')
    await person.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(requests.some((r) => r.method === 'PATCH')).toBe(true))
    expect(lastBody(requests, 'PATCH').context_window).toBe(8192)
  })

  it('says what setting it actually does', async () => {
    const { client } = fakeServer()
    renderAt(client, '/models/mo1')

    expect(await screen.findByText(/would overflow it/)).toBeInTheDocument()
  })
})
