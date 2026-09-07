/**
 * Fixtures shaped by the *generated* server types.
 *
 * They are here rather than inline in each test because the types come from the API
 * schema: when the server adds a required field, one file fails to compile instead of
 * five, and the fix is made once. That is the whole point of generating the client.
 */

import type {
  ApiKeyResponse,
  BucketResponse,
  ConnectorResponse,
  CurrentUser,
  DocumentResponse,
  GatewayResponse,
  GatewayTestResponse,
  InvitationResponse,
  IssuedApiKeyResponse,
  MemberResponse,
  ModelResponse,
  OrganizationResponse,
  ProbeResponse,
  RequestDetailResponse,
  RequestLogResponse,
  SearchHit,
  SeriesResponse,
  SummaryResponse,
} from '@/api/types'

const NOW = '2026-09-06T12:00:00Z'

export function makeUser(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    id: 'u1',
    email: 'ada@example.com',
    name: 'Ada Lovelace',
    role: 'org_admin',
    status: 'active',
    last_login_at: null,
    organization: { id: 'o1', name: 'Acme', slug: 'acme', status: 'active' },
    capabilities: ['keys:manage', 'org:administer', 'org:read', 'resources:write'],
    ...overrides,
  }
}

export function makeSuperadmin(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return makeUser({
    id: 'root',
    email: 'root@example.com',
    name: 'Root',
    role: 'superadmin',
    organization: null,
    capabilities: [
      'keys:manage',
      'org:administer',
      'org:read',
      'platform:administer',
      'resources:write',
    ],
    ...overrides,
  })
}

export function makeOrganization(
  overrides: Partial<OrganizationResponse> = {},
): OrganizationResponse {
  return {
    id: 'o1',
    name: 'Acme',
    slug: 'acme',
    status: 'active',
    settings: {},
    created_at: NOW,
    member_count: 3,
    gateway_count: 1,
    ...overrides,
  }
}

export function makeMember(overrides: Partial<MemberResponse> = {}): MemberResponse {
  return {
    id: 'm1',
    email: 'member@example.com',
    name: 'Member',
    role: 'org_member',
    status: 'active',
    last_login_at: null,
    created_at: NOW,
    ...overrides,
  }
}

export function makeInvitation(overrides: Partial<InvitationResponse> = {}): InvitationResponse {
  return {
    id: 'i1',
    organization_id: 'o1',
    email: 'invitee@example.com',
    role: 'org_member',
    status: 'pending',
    expires_at: '2026-09-13T12:00:00Z',
    accepted_at: null,
    created_at: NOW,
    ...overrides,
  }
}

export function makeModel(overrides: Partial<ModelResponse> = {}): ModelResponse {
  return {
    id: 'mo1',
    organization_id: 'o1',
    scope: 'org',
    name: 'acme-gpt',
    description: null,
    base_url: 'https://api.openai.com/v1',
    dialect: 'openai',
    upstream_model_id: 'gpt-4o-mini',
    auth_type: 'bearer',
    credential: { configured: true, hint: 'sk-...4f2a' },
    extra_headers: {},
    system_context: null,
    default_params: {},
    timeout_seconds: 60,
    enabled: true,
    editable: true,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  }
}

/** A model from the operator's shared catalog, as an org user sees it: no hint, no
 * headers, and not editable. */
export function makeGlobalModel(overrides: Partial<ModelResponse> = {}): ModelResponse {
  return makeModel({
    id: 'mo-global',
    organization_id: null,
    scope: 'global',
    name: 'shared-gpt-4o',
    credential: { configured: true, hint: null },
    editable: false,
    ...overrides,
  })
}

export function makeProbe(overrides: Partial<ProbeResponse> = {}): ProbeResponse {
  return {
    ok: true,
    latency_ms: 340,
    upstream_status: 200,
    error_message: null,
    model_echo: 'gpt-4o-mini',
    ...overrides,
  }
}

export function makeGateway(overrides: Partial<GatewayResponse> = {}): GatewayResponse {
  return {
    id: 'g1',
    organization_id: 'o1',
    slug: 'acme-support',
    name: 'Support Bot',
    description: null,
    enabled: true,
    routing_mode: 'single',
    endpoint_url: 'https://localhost:8000/g/acme-support/v1',
    targets: [
      {
        id: 'mo1',
        name: 'acme-gpt',
        dialect: 'openai',
        enabled: true,
        organization_id: 'o1',
        priority: 0,
        weight: 100,
      },
    ],
    system_context: null,
    param_overrides: {},
    locked_params: {},
    // The server always answers with the blob filled in, never `{}` — so the fixture
    // does too, or a component reading a default would pass here and break in the app.
    memory_config: {
      version: 1,
      connector_ids: [],
      doc_top_k: 6,
      doc_min_score: 0.35,
      doc_max_tokens: 2000,
      memory_enabled: true,
      memory_top_k: 8,
      memory_max_tokens: 600,
      query_strategy: 'last_user_message',
      on_retrieval_error: 'fail_open',
    },
    logging_config: {
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
    limits: {
      version: 1,
      requests_per_minute: null,
      tokens_per_minute: null,
      concurrent_requests: null,
      requests_per_day: null,
    },
    key_count: 1,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  }
}

export function makeApiKey(overrides: Partial<ApiKeyResponse> = {}): ApiKeyResponse {
  return {
    id: 'k1',
    gateway_id: 'g1',
    name: 'production',
    prefix: 'mg_1a2b3c4d',
    created_at: NOW,
    last_used_at: null,
    revoked_at: null,
    expires_at: null,
    ...overrides,
  }
}

/** The one response with a live secret on it. Used to prove it is shown once and then
 * never appears again. */
export function makeIssuedKey(
  overrides: Partial<IssuedApiKeyResponse> = {},
): IssuedApiKeyResponse {
  return {
    key: makeApiKey(),
    token: 'mg_1a2b3c4d_shown-exactly-once',
    ...overrides,
  }
}

export function makeGatewayProbe(
  overrides: Partial<GatewayTestResponse> = {},
): GatewayTestResponse {
  return {
    ok: true,
    total_ms: 412,
    upstream_ms: 380,
    assembled_prompt: [
      { role: 'system', content: "You are Acme's support assistant. Be concise." },
      { role: 'user', content: 'Hello!' },
    ],
    model_name: 'acme-gpt',
    content: 'Hello — how can I help?',
    upstream_status: null,
    error_message: null,
    locked_overrides: [],
    ...overrides,
  }
}


// ---------------------------------------------------------------------------
// monitoring
// ---------------------------------------------------------------------------

export function makeSummary(overrides: Partial<SummaryResponse> = {}): SummaryResponse {
  return {
    requests: 120,
    errors: 6,
    error_rate: 0.05,
    status_classes: { '2xx': 114, '5xx': 6 },
    total: { p50: 220, p95: 900, p99: 1400 },
    // Null rather than zero, because most gateways are not streaming — and a fixture that
    // said zero would let a component render "0 ms first token" and still pass.
    ttft: { p50: null, p95: null, p99: null },
    retrieval: { p50: null, p95: null, p99: null },
    prompt_tokens: 4200,
    completion_tokens: 1800,
    memory_tokens: 0,
    models: [{ upstream_model_id: 'mo1', model_name: 'acme-gpt', requests: 120 }],
    error_groups: [{ error_code: 'upstream_timeout', requests: 6 }],
    ...overrides,
  }
}

export function makeSeries(
  buckets: BucketResponse[] = defaultBuckets(),
  overrides: Partial<SeriesResponse> = {},
): SeriesResponse {
  return { interval_seconds: 300, buckets, ...overrides }
}

function defaultBuckets(): BucketResponse[] {
  return [
    { start: '2026-09-06T11:00:00Z', series: { '2xx.requests': 40, '5xx.requests': 2 } },
    { start: '2026-09-06T11:05:00Z', series: { '2xx.requests': 74, '5xx.requests': 4 } },
  ]
}

export function makeRequestLog(
  overrides: Partial<RequestLogResponse> = {},
): RequestLogResponse {
  return {
    id: 'l1',
    created_at: NOW,
    gateway_id: 'g1',
    api_key_id: 'k1',
    end_user_id: null,
    session_id: null,
    upstream_model_id: 'mo1',
    model_name: 'acme-gpt',
    status_code: 200,
    error_code: null,
    error_message: null,
    streamed: false,
    latency_total_ms: 240,
    latency_retrieval_ms: null,
    latency_ttft_ms: null,
    latency_upstream_ms: 210,
    prompt_tokens: 12,
    completion_tokens: 8,
    memory_tokens: null,
    request_id: 'req-1',
    response_truncated: false,
    failed_after_stream_start: false,
    bodies_omitted: null,
    ...overrides,
  }
}

export function makeRequestDetail(
  overrides: Partial<RequestDetailResponse> = {},
): RequestDetailResponse {
  return {
    log: makeRequestLog(),
    transcript: {
      request_body: [{ role: 'user', content: 'what is the answer' }],
      assembled_prompt: [
        { role: 'system', content: 'Be concise.' },
        { role: 'user', content: 'what is the answer' },
      ],
      response_body: 'the answer',
      distilled_at: null,
    },
    retrieved_chunk_ids: [],
    retrieved_fact_ids: [],
    failover_attempts: [],
    ...overrides,
  }
}

export function makeConnector(overrides: Partial<ConnectorResponse> = {}): ConnectorResponse {
  return {
    id: 'c1',
    name: 'Product docs',
    description: 'Everything customer-facing.',
    type: 'managed_file_drop',
    status: 'ready',
    error: null,
    storage_prefix: 'orgs/o1/connectors/c1/',
    chunking: {
      version: 1,
      strategy: 'recursive',
      chunk_size: 1000,
      overlap: 150,
      respect_boundaries: true,
    },
    document_count: 2,
    counts: { indexed: 2 },
    total_bytes: 4096,
    reindex_required: false,
    last_synced_at: NOW,
    created_at: NOW,
    ...overrides,
  }
}

export function makeDocument(overrides: Partial<DocumentResponse> = {}): DocumentResponse {
  return {
    id: 'd1',
    connector_id: 'c1',
    source_name: 'handbook.md',
    source_uri: 'orgs/o1/connectors/c1/handbook.md',
    mime_type: 'text/markdown',
    size_bytes: 2048,
    status: 'indexed',
    error: null,
    chunk_count: 3,
    embedding_model: 'text-embedding-3-small',
    content_hash: 'a'.repeat(64),
    indexed_at: NOW,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  }
}

export function makeSearchHit(overrides: Partial<SearchHit> = {}): SearchHit {
  return {
    id: 'p1',
    score: 0.82,
    text: 'Everyone gets twenty-five days of annual leave.',
    source_name: 'handbook.md',
    source_uri: 'orgs/o1/connectors/c1/handbook.md',
    page_or_section: 'Handbook > Leave',
    chunk_index: 0,
    document_id: 'd1',
    ...overrides,
  }
}
