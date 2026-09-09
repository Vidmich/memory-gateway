/**
 * Queries and mutations for connectors, their documents, and the debug search.
 *
 * Two decisions here shape how the screen behaves.
 *
 * **The document list polls while anything is still moving.** SPEC §9.5's progression is
 * the demo, and a table that only advances when somebody presses refresh does not show
 * it. `useDocuments` takes the interval from the *data*: rows in a non-terminal state
 * mean poll, everything terminal means stop. So an idle connector costs nothing and a
 * busy one updates itself, without a websocket or a manual timer to leak.
 *
 * **Upload is not a `client.post`.** It needs `FormData` and a progress callback, so it
 * goes through `client.upload`, which is `XMLHttpRequest` — the only browser API that
 * reports upload progress at all. It still lives on the client rather than here, so the
 * access token stays private and the 401 refresh-and-retry is the same one every other
 * call gets.
 */

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import type { UploadProgress } from '@/api/client'
import { useApiClient } from '@/auth/AuthContext'
import type {
  ChunkingPreviewResponse,
  ConnectorCreateRequest,
  ConnectorPage,
  ConnectorResponse,
  ConnectorUpdateRequest,
  DocumentChunksResponse,
  DocumentPage,
  DocumentResponse,
  ResyncResponse,
  SearchRequest,
  SearchResponse,
  UploadResponse,
  UploadUrlResponse,
} from '@/api/types'

export const keys = {
  all: ['connectors'] as const,
  list: (cursor?: string | null) => ['connectors', { cursor: cursor ?? null }] as const,
  one: (id: string) => ['connectors', id] as const,
  documents: (connectorId: string, status?: string | null) =>
    ['connectors', connectorId, 'documents', { status: status ?? null }] as const,
  chunks: (documentId: string) => ['connectors', 'chunks', documentId] as const,
}

/** Statuses from which nothing happens on its own — the same list the server has. */
export const TERMINAL_STATUSES = ['indexed', 'failed', 'skipped'] as const

/**
 * How often the document table refreshes while work is outstanding.
 *
 * Two seconds is the compromise: fast enough that `pending → extracting → indexed` reads
 * as a progression rather than a jump, slow enough that a hundred-document connector is
 * not a request every frame.
 */
export const POLL_INTERVAL_MS = 2000

export function isSettled(documents: readonly DocumentResponse[]): boolean {
  return documents.every((document) =>
    (TERMINAL_STATUSES as readonly string[]).includes(document.status),
  )
}

// -- reads -----------------------------------------------------------------

export function useConnectors(cursor?: string | null): UseQueryResult<ConnectorPage> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.list(cursor),
    queryFn: () =>
      client.get<ConnectorPage>(
        cursor ? `/api/v1/connectors?cursor=${encodeURIComponent(cursor)}` : '/api/v1/connectors',
      ),
  })
}

export function useConnector(id: string | undefined): UseQueryResult<ConnectorResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.one(id ?? ''),
    queryFn: () => client.get<ConnectorResponse>(`/api/v1/connectors/${id}`),
    enabled: Boolean(id),
  })
}

export function useDocuments(
  connectorId: string | undefined,
  status?: string | null,
): UseQueryResult<DocumentPage> {
  const client = useApiClient()
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return useQuery({
    queryKey: keys.documents(connectorId ?? '', status),
    queryFn: () =>
      client.get<DocumentPage>(`/api/v1/connectors/${connectorId}/documents${query}`),
    enabled: Boolean(connectorId),
    // Derived from the data rather than from a flag somebody has to remember to clear.
    // An idle connector polls not at all; a busy one polls until it is not busy.
    refetchInterval: (query_) =>
      query_.state.data && isSettled(query_.state.data.items) ? false : POLL_INTERVAL_MS,
  })
}

/**
 * The chunk inspector: what one document actually became.
 *
 * Fetched only while the panel is open — `enabled` on the id — because it is a per-row
 * detail nobody wants on the table's polling schedule, and a connector with two hundred
 * documents would otherwise fetch two hundred chunk lists to render one.
 */
export function useDocumentChunks(
  documentId: string | undefined,
): UseQueryResult<DocumentChunksResponse> {
  const client = useApiClient()
  return useQuery({
    queryKey: keys.chunks(documentId ?? ''),
    queryFn: () => client.get<DocumentChunksResponse>(`/api/v1/documents/${documentId}/chunks`),
    enabled: Boolean(documentId),
  })
}

// -- writes ----------------------------------------------------------------

/**
 * Invalidate every connector query rather than the clever subset.
 *
 * A document changing status changes its row, its connector's per-status counts, and the
 * total size on the list screen. Working out which is how a stale count survives an
 * upload, and these lists are small.
 */
function useInvalidateConnectors(): () => Promise<void> {
  const queryClient = useQueryClient()
  return async () => {
    await queryClient.invalidateQueries({ queryKey: keys.all })
  }
}

export function useCreateConnector() {
  const client = useApiClient()
  const invalidate = useInvalidateConnectors()
  return useMutation({
    mutationFn: (body: ConnectorCreateRequest) =>
      client.post<ConnectorResponse>('/api/v1/connectors', body),
    onSuccess: invalidate,
  })
}

export function useUpdateConnector(id: string | undefined) {
  const client = useApiClient()
  const invalidate = useInvalidateConnectors()
  return useMutation({
    mutationFn: (body: ConnectorUpdateRequest) =>
      client.patch<ConnectorResponse>(`/api/v1/connectors/${id}`, body),
    onSuccess: invalidate,
  })
}

export function useDeleteConnector() {
  const client = useApiClient()
  const invalidate = useInvalidateConnectors()
  return useMutation({
    mutationFn: (id: string) => client.delete<void>(`/api/v1/connectors/${id}`),
    onSuccess: invalidate,
  })
}

/**
 * Re-run ingestion for a connector's documents, optionally narrowed to some formats.
 *
 * Not `POST /platform/reindex`, despite the shared word. That one re-embeds chunks that are
 * still correct under a new model; this one exists because a changed `chunk_size` makes the
 * chunks themselves wrong, and only running the pipeline again fixes that.
 *
 * `formats` is what makes a per-format override affordable: adding one for code re-runs the
 * code files and leaves a thousand PDFs indexed. Omitted means everything, which is the
 * right answer when the connector's own settings moved.
 */
export function useReindexConnector(connectorId: string | undefined) {
  const client = useApiClient()
  const invalidate = useInvalidateConnectors()
  return useMutation({
    mutationFn: (formats?: string[]) =>
      client.post<{ documents: number }>(`/api/v1/connectors/${connectorId}/reindex`, {
        formats: formats && formats.length > 0 ? formats : null,
      }),
    onSuccess: invalidate,
  })
}

/**
 * **Compare**: run candidate chunkings over one document and return what each produces.
 *
 * A mutation rather than a query even though it writes nothing, and deliberately: it costs
 * money at the embedding provider on every call, so it must run when somebody presses a
 * button and never because a component re-rendered.
 */
export function usePreviewChunking(connectorId: string | undefined) {
  const client = useApiClient()
  return useMutation({
    mutationFn: (body: {
      document_id: string
      candidates?: Record<string, unknown>[]
      query?: string | null
    }) =>
      client.post<ChunkingPreviewResponse>(
        `/api/v1/connectors/${connectorId}/chunking/preview`,
        body,
      ),
  })
}

export function useResync(connectorId: string | undefined) {
  const client = useApiClient()
  const invalidate = useInvalidateConnectors()
  return useMutation({
    mutationFn: () => client.post<ResyncResponse>(`/api/v1/connectors/${connectorId}/resync`, {}),
    onSuccess: invalidate,
  })
}

export function useReindexDocument() {
  const client = useApiClient()
  const invalidate = useInvalidateConnectors()
  return useMutation({
    mutationFn: (documentId: string) =>
      client.post<DocumentResponse>(`/api/v1/documents/${documentId}/reindex`, {}),
    onSuccess: invalidate,
  })
}

export function useDeleteDocument() {
  const client = useApiClient()
  const invalidate = useInvalidateConnectors()
  return useMutation({
    mutationFn: (documentId: string) => client.delete<void>(`/api/v1/documents/${documentId}`),
    onSuccess: invalidate,
  })
}

export function useUploadUrl(connectorId: string | undefined) {
  const client = useApiClient()
  return useMutation({
    mutationFn: (filename: string) =>
      client.post<UploadUrlResponse>(`/api/v1/connectors/${connectorId}/upload-url`, { filename }),
  })
}

export function useSearch(connectorId: string | undefined) {
  const client = useApiClient()
  return useMutation({
    mutationFn: (body: SearchRequest) =>
      client.post<SearchResponse>(`/api/v1/connectors/${connectorId}/search`, body),
  })
}

// -- upload ----------------------------------------------------------------

/**
 * Multipart upload, with per-batch progress.
 *
 * `webkitRelativePath` is preferred over `name` when a *folder* was dropped, so the
 * folder keeps its shape in the index — `docs/api/auth.md` in a citation rather than a
 * flat pile of `auth.md`.
 */
export function formDataFor(files: readonly File[]): FormData {
  const body = new FormData()
  for (const file of files) {
    const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath
    body.append('files', file, relative || file.name)
  }
  return body
}

export function useUpload(connectorId: string | undefined) {
  const client = useApiClient()
  const invalidate = useInvalidateConnectors()
  return useMutation({
    mutationFn: (input: { files: readonly File[]; onProgress?: (p: UploadProgress) => void }) =>
      client.upload<UploadResponse>(
        `/api/v1/connectors/${connectorId}/upload`,
        formDataFor(input.files),
        input.onProgress ? { onProgress: input.onProgress } : {},
      ),
    onSuccess: invalidate,
  })
}
