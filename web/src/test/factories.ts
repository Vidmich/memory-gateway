/**
 * Fixtures shaped by the *generated* server types.
 *
 * They are here rather than inline in each test because the types come from the API
 * schema: when the server adds a required field, one file fails to compile instead of
 * five, and the fix is made once. That is the whole point of generating the client.
 */

import type {
  AuditChange,
  AuditEvent,
  CalibrationResponse,
  EffectiveTokenizerResponse,
  TokenizersResponse,
  PromptPreviewResponse,
  RetrievalPreviewResponse,
  RetrievedChunkResponse,
  ApiKeyResponse,
  BucketResponse,
  ChunkingConfig,
  ChunkingPreviewResponse,
  ConnectorResponse,
  CurrentUser,
  DocumentChunk,
  DocumentResponse,
  EndUserResponse,
  GatewayLimits,
  GatewayResponse,
  GatewayTestResponse,
  LimitQuota,
  LimitUsage,
  InvitationResponse,
  IssuedApiKeyResponse,
  MemberResponse,
  MemoryFactResponse,
  MemorySearchHit,
  DistillationSettings,
  MemoryHealth,
  ModelResponse,
  OrganizationResponse,
  ProbeResponse,
  RequestDetailResponse,
  RequestLogResponse,
  SearchHit,
  SeriesResponse,
  SummaryResponse,
  ThrottledEndUser,
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
    // Null, which the server reads as *unknown* rather than unlimited — so the assembler's
    // overflow guard is off by default here, as it is for a model nobody has told us about.
    context_window: null,
    default_params: {},
    timeout_seconds: 60,
    // Task 101: no override, so what is in effect is what the table derives for gpt-4o.
    tokenizer: null,
    effective_tokenizer: makeEffectiveTokenizer(),
    enabled: true,
    editable: true,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  }
}

export function makeEffectiveTokenizer(
  overrides: Partial<EffectiveTokenizerResponse> = {},
): EffectiveTokenizerResponse {
  return {
    spec: { name: 'o200k_base', ratio: null },
    origin: 'derived',
    name: 'o200k_base',
    label: 'o200k_base (derived)',
    degraded: false,
    approximate: false,
    ...overrides,
  }
}

export function makeTokenizers(overrides: Partial<TokenizersResponse> = {}): TokenizersResponse {
  return {
    names: ['cl100k_base', 'o200k_base', 'p50k_base', 'approximate', 'words'],
    derivations: [
      { dialect: 'openai', prefix: 'gpt-4o', spec: { name: 'o200k_base', ratio: null } },
      { dialect: 'openai', prefix: 'gpt-4', spec: { name: 'cl100k_base', ratio: null } },
      { dialect: 'openai', prefix: 'text-embedding-3', spec: { name: 'cl100k_base', ratio: null } },
      { dialect: null, prefix: 'claude', spec: { name: 'approximate', ratio: 3.5 } },
      { dialect: 'anthropic', prefix: '', spec: { name: 'approximate', ratio: 3.5 } },
    ],
    fallback: { name: 'approximate', ratio: 4 },
    min_ratio: 1,
    max_ratio: 20,
    drift_warning: 0.15,
    ...overrides,
  }
}

export function makeCalibration(
  overrides: Partial<CalibrationResponse> = {},
): CalibrationResponse {
  return {
    model_id: 'mo1',
    tokenizer: makeEffectiveTokenizer(),
    estimated: 3000,
    reported: 3120,
    samples: 3120,
    ratio: 1.04,
    warns: false,
    proposed: null,
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
      memory_min_score: 0.3,
      allow_anonymous_memory: false,
      query_strategy: 'last_user_message',
      query_n_turns: 3,
      retrieval_timeout_ms: 800,
      on_retrieval_error: 'fail_open',
      citations: 'off',
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
      per_end_user: {
        requests_per_minute: null,
        tokens_per_minute: null,
        concurrent_requests: null,
        requests_per_day: null,
      },
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
    // Zero attempts, not zero rate: a gateway with no connectors has not retrieved
    // nothing, it has not retrieved — and the rate is undefined over an empty denominator.
    retrieval_attempts: 0,
    retrieval_empty: 0,
    empty_retrieval_rate: 0,
    // Same reasoning for task 100's rate: no answer was given documents, so there is no
    // denominator and the card reads "—".
    injected_requests: 0,
    uncited_requests: 0,
    uncited_rate: 0,
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
    dropped_params: [],
    cited_chunks: 0,
    citations_unresolved: 0,
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
    cited_chunk_ids: [],
    failover_attempts: [],
    ...overrides,
  }
}

/** SPEC §9.3's defaults, spelled once. Every format resolves to this until an override
 * says otherwise, which is what the connector screen shows. */
const CHUNKING: ChunkingConfig = {
  version: 1,
  strategy: 'recursive',
  chunk_size: 1000,
  overlap: 150,
  respect_boundaries: true,
  breakpoint_percentile: 85,
  min_chunk_size: 200,
  window_sentences: 2,
  overrides: {},
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
    chunking: CHUNKING,
    effective_chunking: {
      pdf: CHUNKING,
      docx: CHUNKING,
      pptx: CHUNKING,
      xlsx: CHUNKING,
      markdown: CHUNKING,
      html: CHUNKING,
      csv: CHUNKING,
      json: CHUNKING,
      text: CHUNKING,
      code: CHUNKING,
      other: CHUNKING,
    },
    document_count: 2,
    counts: { indexed: 2 },
    total_bytes: 4096,
    reindex_required: false,
    reindex_formats: [],
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
    reason: null,
    chunk_count: 3,
    page_count: null,
    embedding_model: 'text-embedding-3-small',
    chunk_strategy: 'recursive',
    tokenizer: 'cl100k_base',
    stale: false,
    content_hash: 'a'.repeat(64),
    indexed_at: NOW,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  }
}

export function makeDocumentChunk(overrides: Partial<DocumentChunk> = {}): DocumentChunk {
  return {
    id: 'p1',
    chunk_index: 0,
    page_or_section: 'Leave',
    token_count: 42,
    text: 'Everyone gets twenty-five days of annual leave.',
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


// ---------------------------------------------------------------------------
// memory previews (task 10)
// ---------------------------------------------------------------------------

export function makeRetrievedChunk(
  overrides: Partial<RetrievedChunkResponse> = {},
): RetrievedChunkResponse {
  return {
    id: 'ch1',
    score: 0.71,
    text: 'Refunds are issued within 14 days of purchase.',
    source_name: 'handbook.md',
    page_or_section: 'p. 12',
    document_id: 'd1',
    connector_id: 'cn1',
    chunk_index: 0,
    tokens: 24,
    injected: true,
    handle: 1,
    ...overrides,
  }
}

export function makeRetrievalPreview(
  overrides: Partial<RetrievalPreviewResponse> = {},
): RetrievalPreviewResponse {
  return {
    query: 'how do refunds work',
    outcome: 'hit',
    latency_ms: 14,
    error: null,
    chunks: [makeRetrievedChunk()],
    injected_tokens: 64,
    doc_max_tokens: 2000,
    tokenizer: 'o200k_base',
    ...overrides,
  }
}

export function makePromptPreview(
  overrides: Partial<PromptPreviewResponse> = {},
): PromptPreviewResponse {
  return {
    layers: [
      { name: 'model.system_context', label: 'Model', text: '', tokens: 0 },
      { name: 'gateway.system_context', label: 'Gateway', text: 'Be brief.', tokens: 3 },
      {
        name: 'documents',
        label: 'Documents',
        text: '## Reference material\n[1] source: handbook.md',
        tokens: 61,
      },
      { name: 'memory', label: 'Memory', text: '', tokens: 0 },
      { name: 'client.system', label: 'Client', text: '', tokens: 0 },
    ],
    system_message: 'Be brief.\n\n## Reference material\n[1] source: handbook.md',
    total_tokens: 64,
    context_window: 8192,
    model_name: 'acme-gpt',
    overflowed: false,
    retrieval: makeRetrievalPreview(),
    citations: {
      mode: 'off',
      sample_answer: 'According to [1], the answer is …',
      metadata: [
        {
          handle: 1,
          chunk_id: 'ch1',
          document_id: 'd1',
          document_name: 'handbook.md',
          connector_id: 'cn1',
          section: 'p. 12',
          chunk_strategy: null,
          matched_text: null,
          url: 'http://localhost:5173/connectors/cn1?document=d1&chunk=ch1',
        },
      ],
      footer: '\n\nSources:\n[1] [handbook.md (p. 12)](http://localhost:5173/connectors/cn1?document=d1&chunk=ch1)',
    },
    tokenizer: 'o200k_base',
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// end users and their memory (task 12)
// ---------------------------------------------------------------------------

export function makeEndUser(overrides: Partial<EndUserResponse> = {}): EndUserResponse {
  return {
    id: 'eu1',
    external_id: 'alice',
    label: null,
    first_seen_at: NOW,
    last_seen_at: NOW,
    request_count: 12,
    fact_count: 2,
    anonymous: false,
    ...overrides,
  }
}

export function makeFact(overrides: Partial<MemoryFactResponse> = {}): MemoryFactResponse {
  return {
    id: 'f1',
    end_user_id: 'eu1',
    text: 'Works in the EU and needs GDPR-compliant answers.',
    kind: 'constraint',
    confidence: 1.0,
    source_log_id: null,
    superseded_at: null,
    superseded_by_id: null,
    expires_at: null,
    created_at: NOW,
    last_seen_at: NOW,
    ...overrides,
  }
}

export function makeMemoryHit(overrides: Partial<MemorySearchHit> = {}): MemorySearchHit {
  return {
    fact: makeFact(),
    score: 0.71,
    ...overrides,
  }
}

export function makeDistillationSettings(
  overrides: Partial<DistillationSettings> = {},
): DistillationSettings {
  return {
    config: {
      version: 1,
      enabled: true,
      model_id: null,
      debounce_seconds: 30,
      dedupe_threshold: 0.92,
      max_facts_per_user: 500,
      daily_call_cap: 5000,
      per_user_daily_cap: 24,
    },
    usage: { calls_today: 0, daily_call_cap: 5000, day_started_at: NOW },
    effective_model_id: null,
    effective_model_name: null,
    using_platform_default: false,
    ...overrides,
  }
}

export function makeMemoryHealth(overrides: Partial<MemoryHealth> = {}): MemoryHealth {
  return {
    days: [],
    runs: 0,
    failures: 0,
    written: 0,
    deduped: 0,
    superseded: 0,
    evicted: 0,
    rejected: 0,
    candidates: 0,
    facts: 0,
    end_users_with_facts: 0,
    failure_rate: 0,
    dedupe_rate: 0,
    supersession_rate: 0,
    average_facts_per_end_user: 0,
    ...overrides,
  }
}


const NO_QUOTA: LimitQuota = {
  requests_per_minute: null,
  tokens_per_minute: null,
  requests_per_day: null,
  concurrent_requests: null,
}

export function makeLimitUsage(overrides: Partial<LimitUsage> = {}): LimitUsage {
  const value = overrides.value ?? 10
  const remaining = overrides.remaining ?? 6
  return {
    limit: 'requests_per_minute',
    scope: 'gateway',
    value,
    used: value - remaining,
    remaining,
    reset_seconds: 37,
    utilization: (value - remaining) / value,
    capped: false,
    ...overrides,
  }
}

export function makeGatewayLimits(overrides: Partial<GatewayLimits> = {}): GatewayLimits {
  return {
    gateway_id: 'g1',
    slug: 'acme-support',
    name: 'Support Bot',
    configured: NO_QUOTA,
    configured_per_end_user: NO_QUOTA,
    enforced: NO_QUOTA,
    enforced_per_end_user: NO_QUOTA,
    capped: [],
    ceilings: NO_QUOTA,
    global_models: false,
    usage: [],
    ...overrides,
  }
}

export function makeThrottledEndUser(
  overrides: Partial<ThrottledEndUser> = {},
): ThrottledEndUser {
  return { end_user_id: 'eu1', external_id: 'noisy-bot', rejections: 12, ...overrides }
}

export function makeAuditChange(overrides: Partial<AuditChange> = {}): AuditChange {
  return {
    path: 'system_context',
    kind: 'changed',
    before: 'Be brief.',
    after: 'Be brief and kind.',
    truncated: false,
    ...overrides,
  }
}

export function makeAuditEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: 'ae1',
    created_at: '2026-03-04T12:00:00Z',
    organization_id: 'org1',
    actor_user_id: 'u1',
    actor: 'ada@example.com',
    actor_type: 'user',
    action: 'gateway.update',
    target_type: 'gateway',
    target_id: 'g1',
    target: 'acme-support',
    changes: [makeAuditChange()],
    omitted: 0,
    summary: null,
    ip: '203.0.113.7',
    user_agent: 'pytest',
    request_id: 'req-1',
    ...overrides,
  }
}


/**
 * One comparison result, with two columns that differ in the way the screen is for.
 *
 * `proposed` cuts smaller: more chunks, more of them decided by the size limit. That is
 * the shape of the answer the Compare view exists to show, so a factory that made both
 * columns identical would let a broken table pass.
 */
export function makeChunkingPreview(
  overrides: Partial<ChunkingPreviewResponse> = {},
): ChunkingPreviewResponse {
  const chunk = (index: number, text: string) => ({
    index,
    text,
    section: null,
    token_count: 40 - index,
    embedded_text: null,
    score: index === 0 ? 0.71 : 0.32,
  })
  return {
    document_id: 'd1',
    source_name: 'handbook.md',
    media_type: 'text/markdown',
    format_kind: 'markdown',
    query: 'what does the travel policy cover?',
    candidates: [
      {
        label: 'current',
        strategy: 'recursive',
        distribution: {
          chunks: 2,
          min_tokens: 30,
          median_tokens: 38,
          p95_tokens: 40,
          max_tokens: 40,
          at_ceiling: 1,
          mid_sentence: 0,
        },
        chunks: [chunk(0, 'Expenses are reimbursed within thirty days.'), chunk(1, 'Receipts go through the portal.')],
        total_chunks: 2,
        embedded_texts: 2,
        best: 0,
      },
      {
        label: 'proposed',
        strategy: 'recursive',
        distribution: {
          chunks: 5,
          min_tokens: 8,
          median_tokens: 14,
          p95_tokens: 20,
          max_tokens: 20,
          at_ceiling: 4,
          mid_sentence: 3,
        },
        chunks: [chunk(0, 'Expenses are reimbursed'), chunk(1, 'within thirty days.')],
        total_chunks: 5,
        embedded_texts: 5,
        best: 1,
      },
    ],
    ...overrides,
  }
}
