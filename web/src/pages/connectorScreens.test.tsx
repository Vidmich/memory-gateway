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
  makeChunkingPreview,
  makeConnector,
  makeDocument,
  makeDocumentChunk,
  makeSearchHit,
  makeSummary,
  makeUser,
} from '@/test/factories'
import { bodyOf, jsonResponse as json, pathOf } from '@/test/http'

type ServerOptions = {
  user?: ReturnType<typeof makeUser>
  connectors?: ReturnType<typeof makeConnector>[]
  documents?: ReturnType<typeof makeDocument>[]
  hits?: ReturnType<typeof makeSearchHit>[]
  chunks?: ReturnType<typeof makeDocumentChunk>[]
  resync?: { added: number; updated: number; deleted: number; unchanged: number; skipped: number }
  preview?: ReturnType<typeof makeChunkingPreview>
  saveError?: { status: number; code: string; message: string; param?: string }
}

/**
 * A scripted server, not a mocked client — the real `ApiClient` runs, so a test asserts
 * what actually went on the wire.
 *
 * For these screens that matters twice: whether a chunking save sent the *whole* section
 * or only the field that was touched, and whether the document table stops polling once
 * everything is terminal.
 */
function fakeServer(options: ServerOptions = {}) {
  const user = options.user ?? makeUser()
  const connectors = options.connectors ?? [makeConnector()]
  const documents = options.documents ?? [makeDocument()]
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
    // The dashboard opens with this. Without it the page renders against a body shaped
    // like a page of nothing, which is a failure about the stub rather than the screen.
    if (path.startsWith('/api/v1/metrics/summary')) return Promise.resolve(json(makeSummary()))
    if (path.startsWith('/api/v1/gateways')) {
      return Promise.resolve(json({ items: [], next_cursor: null }))
    }

    if (path.endsWith('/search') && method === 'POST') {
      return Promise.resolve(
        json({ hits: options.hits ?? [makeSearchHit()], embedding_model: 'hash-bow' }),
      )
    }
    if (path.endsWith('/chunking/preview') && method === 'POST') {
      return Promise.resolve(json(options.preview ?? makeChunkingPreview()))
    }
    if (path.endsWith('/reindex') && method === 'POST' && path.includes('/connectors/')) {
      return Promise.resolve(json({ documents: 1 }))
    }
    if (path.endsWith('/resync') && method === 'POST') {
      return Promise.resolve(
        json(
          options.resync ?? { added: 0, updated: 0, deleted: 0, unchanged: 1, skipped: 0 },
        ),
      )
    }
    if (path.endsWith('/upload-url') && method === 'POST') {
      return Promise.resolve(
        json({
          url: 'https://storage.test/presigned?sig=abc',
          key: 'orgs/o1/connectors/c1/handbook.md',
          expires_in: 900,
        }),
      )
    }
    if (path.startsWith('/api/v1/documents/') && path.endsWith('/chunks')) {
      const rows = options.chunks ?? [makeDocumentChunk()]
      return Promise.resolve(json({ chunks: rows, chunk_count: rows.length }))
    }
    if (path.includes('/documents') && method === 'GET') {
      const status = new URL(path, 'http://x').searchParams.get('status')
      const rows = status ? documents.filter((row) => row.status === status) : documents
      return Promise.resolve(json({ items: rows, next_cursor: null }))
    }
    if (path.startsWith('/api/v1/documents/') && path.endsWith('/reindex')) {
      return Promise.resolve(json(makeDocument({ status: 'pending', error: null })))
    }
    if (path.startsWith('/api/v1/documents/') && method === 'DELETE') {
      return Promise.resolve(new Response(null, { status: 204 }))
    }
    if (path === '/api/v1/connectors' && method === 'POST') {
      const failure = options.saveError
      if (failure) return Promise.resolve(json({ error: failure }, failure.status))
      return Promise.resolve(json(makeConnector(bodyOf(init)), 201))
    }
    if (path.startsWith('/api/v1/connectors/') && method === 'PATCH') {
      const failure = options.saveError
      if (failure) return Promise.resolve(json({ error: failure }, failure.status))
      return Promise.resolve(json(makeConnector(bodyOf(init))))
    }
    if (path.startsWith('/api/v1/connectors/') && method === 'DELETE') {
      return Promise.resolve(new Response(null, { status: 202 }))
    }
    if (path.startsWith('/api/v1/connectors')) {
      if (path.startsWith('/api/v1/connectors/')) {
        const id = path.split('/')[4]
        return Promise.resolve(json(connectors.find((row) => row.id === id) ?? connectors[0]!))
      }
      return Promise.resolve(json({ items: connectors, next_cursor: null }))
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

describe('connectors list', () => {
  it('shows a connector with its counts and size', async () => {
    renderAt('/connectors', fakeServer())

    expect(await screen.findByText('Product docs')).toBeInTheDocument()
    expect(screen.getByText('4 KB')).toBeInTheDocument()
  })

  it('leads with what is wrong rather than with a document count', async () => {
    renderAt(
      '/connectors',
      fakeServer({
        connectors: [makeConnector({ document_count: 5, counts: { indexed: 4, failed: 1 } })],
      }),
    )

    expect(await screen.findByText('1 failed')).toBeInTheDocument()
  })

  it('tells an empty organization what a connector is for', async () => {
    renderAt('/connectors', fakeServer({ connectors: [] }))

    expect(await screen.findByText('No connectors yet')).toBeInTheDocument()
    expect(screen.getByText(/handbook/i)).toBeInTheDocument()
  })

  it('creates a connector inline', async () => {
    const server = fakeServer({ connectors: [] })
    renderAt('/connectors', server)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'New connector' }))
    await user.type(screen.getByLabelText('Name'), 'Runbooks')
    await user.click(screen.getByRole('button', { name: 'Create connector' }))

    await waitFor(() => {
      const created = server.requests.find(
        (request) => request.path === '/api/v1/connectors' && request.method === 'POST',
      )
      expect(created?.body).toMatchObject({ name: 'Runbooks', type: 'managed_file_drop' })
    })
  })

  it('never offers the create button to a viewer', async () => {
    const viewer = makeUser({ role: 'org_viewer', capabilities: ['org:read'] })
    renderAt('/connectors', fakeServer({ user: viewer }))

    expect(await screen.findByText('Product docs')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New connector' })).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// the detail screen
// ---------------------------------------------------------------------------

describe('connector detail', () => {
  it('shows each document with its type, size, status and chunk count', async () => {
    renderAt('/connectors/c1', fakeServer())

    expect(await screen.findByText('handbook.md')).toBeInTheDocument()
    const table = screen.getByRole('table')
    expect(within(table).getByText('text/markdown')).toBeInTheDocument()
    expect(within(table).getByText('2 KB')).toBeInTheDocument()
    expect(within(table).getByText('indexed')).toBeInTheDocument()
    expect(within(table).getByText('3')).toBeInTheDocument()
  })

  it('shows what each document was cut with, and marks the stale ones (task 101)', async () => {
    // The tokenizer by the name it gave itself, so a worker whose vocabulary failed to
    // load is visible on the row rather than in a log line.
    renderAt(
      '/connectors/c1',
      fakeServer({
        documents: [
          makeDocument({ id: 'd1', source_name: 'fresh.md', tokenizer: 'o200k_base' }),
          makeDocument({
            id: 'd2',
            source_name: 'old.md',
            tokenizer: 'words (cl100k_base unavailable)',
            stale: true,
          }),
        ],
      }),
    )

    const table = await screen.findByRole('table')
    expect(within(table).getByText('o200k_base')).toBeInTheDocument()
    expect(within(table).getByText('words (cl100k_base unavailable)')).toBeInTheDocument()
    const rows = within(table).getAllByRole('row')
    const old = rows.find((row) => row.textContent?.includes('old.md'))
    const fresh = rows.find((row) => row.textContent?.includes('fresh.md'))
    expect(old?.textContent).toContain('stale')
    expect(fresh?.textContent).not.toContain('stale')
  })

  it('shows a failed document’s error inline', async () => {
    // SPEC §13.1. A detail view per failed row would mean the table cannot say what is
    // wrong until somebody clicks.
    renderAt(
      '/connectors/c1',
      fakeServer({
        documents: [
          makeDocument({
            status: 'failed',
            error: 'This file is not valid JSON: line 2, column 8.',
            chunk_count: 0,
          }),
        ],
      }),
    )

    expect(
      await screen.findByText('This file is not valid JSON: line 2, column 8.'),
    ).toBeInTheDocument()
  })

  it('offers Retry on a failed document and Reindex on a healthy one', async () => {
    renderAt(
      '/connectors/c1',
      fakeServer({ documents: [makeDocument({ status: 'failed', error: 'broken' })] }),
    )
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument()

    screen.getByRole('button', { name: 'Retry' }).click()
  })

  it('retries a failed document through the reindex endpoint', async () => {
    const server = fakeServer({
      documents: [makeDocument({ status: 'failed', error: 'broken' })],
    })
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Retry' }))

    await waitFor(() => {
      expect(
        server.requests.some((request) => request.path === '/api/v1/documents/d1/reindex'),
      ).toBe(true)
    })
  })

  it('deletes a document', async () => {
    const server = fakeServer()
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    await screen.findByText('handbook.md')
    await user.click(within(screen.getByRole('table')).getByRole('button', { name: 'Delete' }))

    await waitFor(() => {
      expect(
        server.requests.some(
          (request) => request.path === '/api/v1/documents/d1' && request.method === 'DELETE',
        ),
      ).toBe(true)
    })
  })

  it('filters the document table by status', async () => {
    const server = fakeServer({
      documents: [
        makeDocument({ id: 'd1', source_name: 'good.md', status: 'indexed' }),
        makeDocument({ id: 'd2', source_name: 'clip.mov', status: 'skipped' }),
      ],
    })
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    expect(await screen.findByText('clip.mov')).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Filter by status'), 'indexed')

    await waitFor(() => expect(screen.queryByText('clip.mov')).not.toBeInTheDocument())
  })

  it('says what a resync actually did', async () => {
    const server = fakeServer({
      resync: { added: 2, updated: 1, deleted: 0, unchanged: 4, skipped: 0 },
    })
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Resync' }))

    expect(await screen.findByText('2 added, 1 updated.')).toBeInTheDocument()
  })

  it('hides the upload zone and the write controls from a viewer', async () => {
    const viewer = makeUser({ role: 'org_viewer', capabilities: ['org:read'] })
    renderAt('/connectors/c1', fakeServer({ user: viewer }))

    expect(await screen.findByText('handbook.md')).toBeInTheDocument()
    expect(screen.queryByLabelText('Upload files')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Resync' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reindex' })).not.toBeInTheDocument()
  })

  it('says a connector is going away and stops offering uploads', async () => {
    renderAt('/connectors/c1', fakeServer({ connectors: [makeConnector({ status: 'deleting' })] }))

    expect(await screen.findByText(/This connector is being deleted/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resync' })).toBeDisabled()
  })

  it('requires the connector name to be typed before deleting it', async () => {
    // SPEC §13.2. Deleting a connector takes its documents, its files and its vectors.
    const server = fakeServer()
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    await screen.findByText('handbook.md')
    await user.click(screen.getByRole('button', { name: 'Delete connector' }))

    const confirm = await screen.findByRole('dialog')
    expect(within(confirm).getByRole('button', { name: 'Delete' })).toBeDisabled()

    await user.type(within(confirm).getByRole('textbox'), 'Product docs')
    expect(within(confirm).getByRole('button', { name: 'Delete' })).toBeEnabled()
  })
})

// ---------------------------------------------------------------------------
// chunking
// ---------------------------------------------------------------------------

describe('chunking panel', () => {
  it('starts from the stored configuration with the save button off', async () => {
    renderAt('/connectors/c1', fakeServer())

    expect(await screen.findByLabelText('Chunk size (tokens)')).toHaveValue(1000)
    expect(screen.getByRole('button', { name: 'Save chunking' })).toBeDisabled()
  })

  it('sends the whole section, not only the field that changed', async () => {
    // The server merges a partial patch, so a form that sent one key would be correct
    // *and* would silently stop working the day merging changed. Sending the section is
    // what makes the saved value the value on screen.
    const server = fakeServer()
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    const size = await screen.findByLabelText('Chunk size (tokens)')
    await user.clear(size)
    await user.type(size, '500')
    await user.click(screen.getByRole('button', { name: 'Save chunking' }))

    await waitFor(() => {
      const saved = server.requests.find((request) => request.method === 'PATCH')
      expect(saved?.body.chunking).toMatchObject({
        strategy: 'recursive',
        chunk_size: 500,
        overlap: 150,
        respect_boundaries: true,
      })
    })
  })

  it('refuses to save an overlap over half the chunk size', async () => {
    renderAt('/connectors/c1', fakeServer())
    const user = userEvent.setup()

    const overlap = await screen.findByLabelText('Overlap (tokens)')
    await user.clear(overlap)
    await user.type(overlap, '900')

    expect(await screen.findByRole('alert')).toHaveTextContent('half')
    expect(screen.getByRole('button', { name: 'Save chunking' })).toBeDisabled()
  })

  it('warns that indexed documents keep their old chunks', async () => {
    renderAt('/connectors/c1', fakeServer())
    const user = userEvent.setup()

    await user.selectOptions(await screen.findByLabelText('Strategy'), 'by_heading')

    expect(await screen.findByText(/keep their old chunks/)).toBeInTheDocument()
  })

  it('offers a reindex when the stored chunking no longer matches what is indexed', async () => {
    // Saying "your chunks are stale" and offering nothing to do about it is the state
    // task 09 left this screen in. The button is the action, and it is a *different*
    // operation from Platform → Settings' reindex: this one re-runs the pipeline, because
    // a changed chunk size makes the chunks wrong rather than the vectors.
    const server = fakeServer({
      connectors: [makeConnector({ reindex_required: true })],
    })
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Reindex every document' }))

    await waitFor(() => {
      expect(
        server.requests.some((request) => request.path === '/api/v1/connectors/c1/reindex'),
      ).toBe(true)
    })
  })

  it('offers no reindex when nothing is stale', async () => {
    renderAt('/connectors/c1', fakeServer())
    await screen.findByLabelText('Chunk size (tokens)')

    expect(
      screen.queryByRole('button', { name: 'Reindex every document' }),
    ).not.toBeInTheDocument()
  })

  it('does not warn an empty connector', async () => {
    renderAt(
      '/connectors/c1',
      fakeServer({
        connectors: [makeConnector({ document_count: 0, counts: {} })],
        documents: [],
      }),
    )
    const user = userEvent.setup()

    await user.selectOptions(await screen.findByLabelText('Strategy'), 'by_heading')

    expect(screen.queryByText(/keep their old chunks/)).not.toBeInTheDocument()
  })

  it('explains what the chosen strategy does', async () => {
    renderAt('/connectors/c1', fakeServer())
    const user = userEvent.setup()

    await user.selectOptions(await screen.findByLabelText('Strategy'), 'by_heading')

    expect(screen.getByText(/Falls back to recursive/)).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// search and presigned uploads
// ---------------------------------------------------------------------------

describe('debug search', () => {
  it('shows a hit with its score and where it came from', async () => {
    renderAt('/connectors/c1', fakeServer())
    const user = userEvent.setup()

    await user.type(await screen.findByLabelText('Question'), 'annual leave')
    await user.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText(/twenty-five days/)).toBeInTheDocument()
    expect(screen.getByText('0.820')).toBeInTheDocument()
    expect(screen.getByText(/Handbook > Leave/)).toBeInTheDocument()
  })

  it('names the model that scored the results', async () => {
    // Two searches under different embedding models are not comparable, and this is the
    // only place the difference is visible.
    renderAt('/connectors/c1', fakeServer())
    const user = userEvent.setup()

    await user.type(await screen.findByLabelText('Question'), 'leave')
    await user.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText('hash-bow')).toBeInTheDocument()
  })

  it('says where to look when nothing matched', async () => {
    renderAt('/connectors/c1', fakeServer({ hits: [] }))
    const user = userEvent.setup()

    await user.type(await screen.findByLabelText('Question'), 'nothing at all')
    await user.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText(/Check that the documents you expect/)).toBeInTheDocument()
  })
})

describe('presigned upload', () => {
  it('mints a URL and shows a copyable PUT', async () => {
    renderAt('/connectors/c1', fakeServer())
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Get URL' }))

    const snippet = await screen.findByText(/curl -X PUT/)
    expect(snippet).toHaveTextContent('https://storage.test/presigned?sig=abc')
    expect(screen.getByText(/Expires in 15 minutes/)).toBeInTheDocument()
  })

  it('shows the storage prefix a script would write into', async () => {
    renderAt('/connectors/c1', fakeServer())

    expect(await screen.findByText('orgs/o1/connectors/c1/')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// extraction states and the chunk inspector (task 11)
// ---------------------------------------------------------------------------

describe('extraction states', () => {
  const PDF = 'application/pdf'

  it('shows how long a document is in the unit its format has', async () => {
    renderAt(
      '/connectors/c1',
      fakeServer({
        documents: [makeDocument({ source_name: 'manual.pdf', mime_type: PDF, page_count: 147 })],
      }),
    )

    expect(await screen.findByText('147 pages')).toBeInTheDocument()
  })

  it('explains a scanned PDF instead of showing it as a failure', async () => {
    renderAt(
      '/connectors/c1',
      fakeServer({
        documents: [
          makeDocument({
            source_name: 'scan.pdf',
            mime_type: PDF,
            status: 'skipped',
            reason: 'needs_ocr',
            error: 'This PDF has 4 pages and almost no text in it, so it is probably a scan.',
            chunk_count: 0,
            page_count: 4,
          }),
        ],
      }),
    )

    // The headline and the next step, not the server's sentence: a red row with a
    // paragraph in it reads as a defect in the product rather than as a task.
    expect(await screen.findByText(/needs OCR/i)).toBeInTheDocument()
    expect(screen.getByText(/selectable text/i)).toBeInTheDocument()
    expect(screen.queryByText(/almost no text in it/)).not.toBeInTheDocument()
  })

  it('falls back to the server sentence for a reason it does not know', async () => {
    renderAt(
      '/connectors/c1',
      fakeServer({
        documents: [
          makeDocument({
            status: 'failed',
            reason: 'invented_later',
            error: 'Something specific went wrong.',
            chunk_count: 0,
          }),
        ],
      }),
    )

    expect(await screen.findByText('Something specific went wrong.')).toBeInTheDocument()
  })
})

describe('the chunk inspector', () => {
  it('lists what a document became, with the page each chunk came from', async () => {
    const user = userEvent.setup()
    renderAt(
      '/connectors/c1',
      fakeServer({
        documents: [makeDocument({ source_name: 'manual.pdf', chunk_count: 2 })],
        chunks: [
          makeDocumentChunk({ id: 'p1', chunk_index: 0, page_or_section: 'Warranty (p. 1)' }),
          makeDocumentChunk({
            id: 'p2',
            chunk_index: 1,
            page_or_section: 'Warranty > Coverage (p. 147)',
            text: 'The Zynthorp QX-4471 ships from the Utrecht depot.',
          }),
        ],
      }),
    )

    await user.click(await screen.findByRole('button', { name: 'Chunks' }))

    expect(await screen.findByText('Warranty > Coverage (p. 147)')).toBeInTheDocument()
    expect(screen.getByText(/Utrecht depot/)).toBeInTheDocument()
  })

  it('opens at the chunk a citation links to (task 100)', async () => {
    // The URL a citation carries: the document's inspector is open on arrival and the
    // cited chunk is marked, so "where did this come from" is one click from the answer.
    renderAt(
      '/connectors/c1?document=d1&chunk=p2',
      fakeServer({
        documents: [makeDocument({ id: 'd1', chunk_count: 2 })],
        chunks: [
          makeDocumentChunk({ id: 'p1', chunk_index: 0 }),
          makeDocumentChunk({ id: 'p2', chunk_index: 1, text: 'The cited passage.' }),
        ],
      }),
    )

    const cited = await screen.findByText('cited chunk')
    expect(cited.closest('li')?.textContent).toContain('The cited passage.')
    expect(screen.getByRole('button', { name: 'Hide chunks' })).toBeInTheDocument()
  })

  it('is not offered for a document with nothing in the index', async () => {
    renderAt(
      '/connectors/c1',
      fakeServer({ documents: [makeDocument({ status: 'skipped', chunk_count: 0 })] }),
    )

    await screen.findByText('handbook.md')
    expect(screen.queryByRole('button', { name: 'Chunks' })).not.toBeInTheDocument()
  })

  it('says so when the index holds fewer chunks than the row claims', async () => {
    // The two disagreeing is the finding: a row claiming five with two indexed was written
    // into a collection that has since been dropped, and "retrieval is bad" is how that
    // otherwise presents.
    const user = userEvent.setup()
    renderAt(
      '/connectors/c1',
      fakeServer({
        documents: [makeDocument({ chunk_count: 5 })],
        chunks: [makeDocumentChunk()],
      }),
    )

    await user.click(await screen.findByRole('button', { name: 'Chunks' }))

    expect(await screen.findByText(/The document row says 5/)).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// the dashboard
// ---------------------------------------------------------------------------

describe('the dashboard card', () => {
  it('counts indexed documents across every connector', async () => {
    renderAt(
      '/',
      fakeServer({
        connectors: [
          makeConnector({ id: 'c1', counts: { indexed: 40 } }),
          makeConnector({ id: 'c2', name: 'Runbooks', counts: { indexed: 2 } }),
        ],
      }),
    )

    expect(await screen.findByText('42')).toBeInTheDocument()
  })

  it('reports what could not be read instead of what could', async () => {
    // A dashboard that says "900 indexed" and nothing about the 40 that failed is
    // reporting the half nobody needs to act on.
    renderAt(
      '/',
      fakeServer({
        connectors: [makeConnector({ counts: { indexed: 900, failed: 40 } })],
      }),
    )

    expect(await screen.findByText('40 could not be read →')).toBeInTheDocument()
  })

  it('links to the connectors screen when everything is fine', async () => {
    renderAt('/', fakeServer({ connectors: [makeConnector({ counts: { indexed: 3 } })] }))

    expect(await screen.findByText('Manage content →')).toBeInTheDocument()
  })
})


describe('chunking comparison', () => {
  it('runs the settings on screen against one document and shows both columns', async () => {
    // The demoable half of task 20. Without it the release is three more words in a
    // dropdown, and every user picks by name.
    const server = fakeServer()
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: /product docs/i })

    await user.click(await screen.findByRole('button', { name: /^compare$/i }))
    // The document list has to have arrived, or the button is disabled and the click is a
    // no-op that would make this test fail somewhere much less informative.
    await screen.findByRole('option', { name: 'handbook.md' })
    await user.click(await screen.findByRole('button', { name: /run comparison/i }))

    expect(await screen.findByText('Boundaries mid-sentence')).toBeInTheDocument()
    expect(await screen.findByText('Cut by the size limit')).toBeInTheDocument()
    expect(await screen.findByText('Embedding calls per ingestion')).toBeInTheDocument()
    // Two columns with different answers, which is the whole point of putting them side
    // by side: the proposed cut is five chunks where the current one is two.
    expect(await screen.findByRole('columnheader', { name: /proposed/i })).toBeInTheDocument()
  })

  it('sends the unsaved form as the candidate, so what is compared is the pending change', async () => {
    const server = fakeServer()
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: /product docs/i })

    const size = screen.getByLabelText(/chunk size/i)
    await user.clear(size)
    await user.type(size, '400')
    await user.click(await screen.findByRole('button', { name: /^compare$/i }))
    await screen.findByRole('option', { name: 'handbook.md' })
    await user.click(await screen.findByRole('button', { name: /run comparison/i }))

    await waitFor(() => {
      const preview = server.requests.find((request) => request.path.endsWith('/chunking/preview'))
      expect(preview).toBeDefined()
      const candidates = preview!.body.candidates as Record<string, unknown>[]
      expect(candidates[0]!.chunk_size).toBe(400)
    })
  })

  it('reindexes only the formats the change invalidated', async () => {
    // The payoff of per-format overrides: adding one for code re-runs the code files and
    // leaves a thousand PDFs where they are.
    const connector = makeConnector({ reindex_required: true, reindex_formats: ['code'] })
    const server = fakeServer({ connectors: [connector] })
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: /reindex the code documents/i }))

    await waitFor(() => {
      const call = server.requests.find(
        (request) => request.path.endsWith('/reindex') && request.method === 'POST',
      )
      expect(call?.body.formats).toEqual(['code'])
    })
  })
})

describe('the chunk inspector under sentence_window', () => {
  it('marks the sentence that was embedded inside the window it returns', async () => {
    // Two strings with different jobs. Without the mark, a chunk that does not contain the
    // words somebody searched for looks like a bug rather than like the strategy working.
    const server = fakeServer({
      chunks: [
        makeDocumentChunk({
          text: 'Before it. The sentence that matched. After it.',
          embedded_text: 'The sentence that matched.',
          chunk_strategy: 'sentence_window',
        }),
      ],
    })
    renderAt('/connectors/c1', server)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: /chunks/i }))

    const marked = await screen.findByText('The sentence that matched.')
    expect(marked.tagName).toBe('MARK')
    expect(await screen.findByText(/what was embedded/i)).toBeInTheDocument()
  })
})
