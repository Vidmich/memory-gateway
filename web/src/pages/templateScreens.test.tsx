import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiClient } from '@/api/client'
import { AppRoutes, makeQueryClient } from '@/App'
import { AuthProvider } from '@/auth/AuthContext'
import { ToastProvider } from '@/components/Toast'
import {
  makeGateway,
  makeGatewayLimits,
  makeModel,
  makePromptPreview,
  makeTemplateConfig,
  makeTemplateDefaults,
  makeUser,
} from '@/test/factories'
import { bodyOf, jsonResponse as json, pathOf } from '@/test/http'

type ServerOptions = {
  user?: ReturnType<typeof makeUser>
  gateway?: ReturnType<typeof makeGateway>
  saveError?: { status: number; code: string; message: string; param?: string }
}

/**
 * Gateways → Advanced (task 105), against a scripted server: what the page sends on
 * Preview and on Save is the point, so the real `ApiClient` is exercised and the bodies
 * recorded.
 */
function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  let gateway = options.gateway ?? makeGateway()
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
    if (path === '/api/v1/templates/defaults') return Promise.resolve(json(makeTemplateDefaults()))
    if (path.endsWith('/prompt-preview') && method === 'POST') {
      const body = bodyOf<{ template_config?: Record<string, string> }>(init)
      const heading = body.template_config?.reference_heading ?? '## Reference material'
      return Promise.resolve(
        json(
          makePromptPreview({
            layers: [
              { name: 'documents', label: 'Documents', text: `${heading}\n[1] x`, tokens: 9 },
            ],
            template_fingerprint: 'feedfeedfeedfeed',
            answer_suffix: body.template_config?.answer_suffix ?? '',
          }),
        ),
      )
    }
    if (path.startsWith('/api/v1/gateways/') && method === 'PATCH') {
      const failure = options.saveError
      if (failure) return Promise.resolve(json({ error: failure }, failure.status))
      const body = bodyOf<{ template_config?: Record<string, string> }>(init)
      gateway = makeGateway({
        ...gateway,
        template_config: makeTemplateConfig({
          ...gateway.template_config,
          ...body.template_config,
        }),
        template_fingerprint: 'abcdabcdabcdabcd',
      })
      return Promise.resolve(json(gateway))
    }
    if (path.startsWith('/api/v1/gateways/') && method === 'GET') {
      if (path.endsWith('/limits')) return Promise.resolve(json(makeGatewayLimits()))
      if (path.endsWith('/keys')) return Promise.resolve(json([]))
      return Promise.resolve(json(gateway))
    }
    if (path.startsWith('/api/v1/gateways')) {
      return Promise.resolve(json({ items: [gateway], next_cursor: null }))
    }
    if (path.startsWith('/api/v1/connectors')) {
      return Promise.resolve(json({ items: [], next_cursor: null }))
    }
    if (path.startsWith('/api/v1/models')) {
      return Promise.resolve(json({ items: [makeModel()], next_cursor: null }))
    }
    if (path.startsWith('/api/v1/metrics/timeseries')) {
      return Promise.resolve(json({ interval_seconds: 86400, buckets: [] }))
    }
    throw new Error(`unexpected ${method} ${path}`)
  })

  return { client: new ApiClient(impl), requests }
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

const field = (name: string) => within(screen.getByTestId(`template-${name}`))
const lastBody = (requests: { method: string; body: Record<string, unknown> }[], method: string) =>
  requests.filter((request) => request.method === method).at(-1)!.body

afterEach(() => {
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------

describe('Gateways → Advanced', () => {
  it('lists the nine templates with their defaults, chips only where placeholders apply', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1/advanced')

    expect(await screen.findByRole('heading', { name: 'Advanced' })).toBeInTheDocument()
    expect(screen.getByLabelText('Reference heading')).toHaveValue('## Reference material')
    expect(screen.getByLabelText('Excerpt')).toHaveValue(
      '[{handle}] source: {source_name}{section}\n{text}',
    )
    expect(screen.getByLabelText('Answer suffix')).toHaveValue('')
    // Chips for the excerpt, none for a plain heading.
    expect(
      field('excerpt').getByRole('button', { name: 'Insert {source_name} into Excerpt' }),
    ).toBeInTheDocument()
    expect(field('reference_heading').queryByRole('button', { name: /Insert/ })).toBeNull()
    // Nothing differs from the defaults, so no default line and no reset anywhere.
    expect(screen.queryByRole('button', { name: /^Reset/ })).toBeNull()
    expect(screen.getByTestId('template-fingerprint')).toHaveTextContent('d0d0d0d0')
  })

  it('shows the default greyed once a value differs, and Reset restores it', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1/advanced')
    const person = userEvent.setup()

    const heading = await screen.findByLabelText('Reference heading')
    await person.clear(heading)
    await person.type(heading, '## Referenzmaterial')

    expect(screen.getByTestId('default-reference_heading')).toHaveTextContent(
      'Default: ## Reference material',
    )
    await person.click(screen.getByRole('button', { name: 'Reset Reference heading' }))
    expect(heading).toHaveValue('## Reference material')
    expect(screen.queryByTestId('default-reference_heading')).toBeNull()
  })

  it('inserts a placeholder chip at the caret', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1/advanced')
    const person = userEvent.setup()

    const fact = await screen.findByLabelText('Fact')
    await person.clear(fact)
    await person.type(fact, '• ')
    await person.click(field('fact').getByRole('button', { name: 'Insert {text} into Fact' }))

    expect(fact).toHaveValue('• {text}')
  })

  it('warns inline, without blocking, about an empty instruction and a nameless excerpt', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1/advanced')
    const person = userEvent.setup()

    expect(await screen.findByLabelText('Excerpt')).toBeInTheDocument()
    expect(screen.queryAllByRole('note')).toHaveLength(0)

    await person.clear(screen.getByLabelText('Reference instruction'))
    await person.clear(screen.getByLabelText('Excerpt'))
    await person.type(screen.getByLabelText('Excerpt'), '[[{{handle}] {{text}')

    const notes = screen.getAllByRole('note')
    expect(notes).toHaveLength(2)
    expect(notes[0]).toHaveTextContent(/grounded assistant/)
    expect(notes[1]).toHaveTextContent(/cannot name the document/)
    expect(screen.getByRole('button', { name: 'Save templates' })).toBeEnabled()
  })

  it('previews with the unsaved templates as a patch and saves nothing', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1/advanced')
    const person = userEvent.setup()

    const heading = await screen.findByLabelText('Reference heading')
    await person.clear(heading)
    await person.type(heading, '## Referenzmaterial')
    await person.type(screen.getByLabelText('Question'), 'Wie beantrage ich eine Erstattung?')
    await person.click(screen.getByRole('button', { name: 'Preview' }))

    const preview = screen.getByTestId('template-preview')
    await within(preview).findByText(/## Referenzmaterial/)
    expect(lastBody(requests, 'POST')).toEqual({
      query: 'Wie beantrage ich eine Erstattung?',
      template_config: { reference_heading: '## Referenzmaterial' },
    })
    expect(requests.filter((request) => request.method === 'PATCH')).toHaveLength(0)
    expect(within(preview).getByText(/Using the templates below/)).toBeInTheDocument()
    expect(screen.getByTestId('answer-wrapping')).toHaveTextContent('feedfeed')
  })

  it('saves only the fields that changed and shows the new fingerprint', async () => {
    const { client, requests } = fakeServer()
    renderAt(client, '/gateways/g1/advanced')
    const person = userEvent.setup()

    const suffix = await screen.findByLabelText('Answer suffix')
    await person.type(suffix, '_Generated from internal documents._')
    await person.click(screen.getByRole('button', { name: 'Save templates' }))

    await waitFor(() =>
      expect(lastBody(requests, 'PATCH')).toEqual({
        template_config: { answer_suffix: '_Generated from internal documents._' },
      }),
    )
    expect(
      await screen.findByText(/Saved\. The next request uses the new wording/),
    ).toBeInTheDocument()
    expect(screen.getByTestId('template-fingerprint')).toHaveTextContent('abcdabcd')
    expect(screen.getByText('Nothing to save.')).toBeInTheDocument()
  })

  it("renders the server's validation message under the field it names", async () => {
    const { client } = fakeServer({
      saveError: {
        status: 422,
        code: 'validation_error',
        message:
          'excerpt: the excerpt template must contain [{handle}]; without it the model cannot cite and citations cannot resolve',
        param: 'template_config.excerpt',
      },
    })
    renderAt(client, '/gateways/g1/advanced')
    const person = userEvent.setup()

    const excerpt = await screen.findByLabelText('Excerpt')
    await person.clear(excerpt)
    await person.type(excerpt, '{{source_name}: {{text}')
    await person.click(screen.getByRole('button', { name: 'Save templates' }))

    expect(await field('excerpt').findByText(/must contain \[\{handle\}\]/)).toBeInTheDocument()
    expect(excerpt).toHaveAttribute('aria-invalid', 'true')
  })

  it('guards leaving with unsaved edits', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1/advanced')
    const person = userEvent.setup()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)

    const heading = await screen.findByLabelText('Memory heading')
    await person.type(heading, ' (edited)')
    await person.click(screen.getByRole('button', { name: /← Support Bot/ }))

    expect(confirm).toHaveBeenCalledWith('You have unsaved changes. Leave without saving?')
    expect(screen.getByRole('heading', { name: 'Advanced' })).toBeInTheDocument()
  })

  it('is reached from the Prompt section of the editor', async () => {
    const { client } = fakeServer()
    renderAt(client, '/gateways/g1')
    const person = userEvent.setup()

    await person.click(
      await screen.findByRole('button', {
        name: 'Advanced: the text the gateway writes around documents, memory and answers →',
      }),
    )

    expect(await screen.findByRole('heading', { name: 'Advanced' })).toBeInTheDocument()
  })

  it('shows a viewer the templates, disabled, with no save', async () => {
    const { client } = fakeServer({
      user: makeUser({ role: 'org_viewer', capabilities: ['org:read'] }),
    })
    renderAt(client, '/gateways/g1/advanced')

    expect(await screen.findByLabelText('Reference heading')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save templates' })).toBeNull()
    expect(screen.getByText(/can view these templates but not change them/)).toBeInTheDocument()
  })
})
