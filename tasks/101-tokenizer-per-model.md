# Task 101 — The tokenizer follows the model

**Slice:** every token count in the product — chunk sizes, prompt budgets, rate-limit
estimates — is measured with the tokenizer of the model the tokens are for, derived from the
model automatically and overridable by a person, with the error against the provider's own
count shown rather than guessed.
**Depends on:** 05, 09, 10, 14, 17
**Spec:** §6.3 (budgets), §8.4 (model fields), §9.3 (chunking is measured in tokens), §9.4,
§11 — and amends §8.4 and §9.4 with a `tokenizer` field each.
**Size:** M
**Status:** **done.** 104 folds the embedding tokenizer into the fingerprint it tracks; the
fingerprint now carries it, so 104 can read it rather than add it.

---

## Why this slice

There is exactly one tokenizer in the process today: `build_tokenizer()` returns `cl100k_base`,
and it is handed to the ingestion pipeline, the prompt assembler and the rate limiter alike.
`cl100k_base` is the encoding of `text-embedding-3-*` and `gpt-4`. It is not the encoding of
`gpt-4o` (`o200k_base`, ~10% fewer tokens on English, far fewer on CJK), not Claude's, not
Llama's, not Mistral's, and not whatever a self-hosted embedding endpoint runs.

Three things are wrong at once, in three different directions:

- **Chunks are sized for the wrong model.** `chunk_size: 1000` is a promise about the
  *embedding* model's input limit. Measured with a tokenizer 20% off, a chunk at the ceiling
  either wastes a fifth of the window or is silently truncated by the provider — and the
  provider does not say which.
- **Budgets are enforced in the wrong unit.** `doc_max_tokens: 2000` is a promise about the
  *chat* model's context. A gateway routing A/B between an OpenAI and an Anthropic target
  measures both with one tokenizer and is right for at most one of them.
- **The limiter settles in one unit and estimates in another.** Task 14's `tokens_per_minute`
  is estimated before the request with our count and settled afterwards with the provider's.
  The gap is systematic per model and currently invisible.

The `Tokenizer` port already has a `name` property whose docstring says it exists *"so a later
change of tokenizer is visible rather than a mysterious shift in chunk sizes"*. Nothing has ever
recorded it. This task is the change that docstring was waiting for.

## Demo at the end of this task

Open a `gpt-4o` model in **Models**. Its tokenizer reads `o200k_base (derived)`. Open a Claude
model: `anthropic (approximate, calibrated ×1.04 from 3 120 requests)` — the ratio came from
comparing our estimates against the `prompt_tokens` the provider reported. Override a
self-hosted Llama model's tokenizer to `approximate` with a ratio you type; the field explains
what it costs.

Open the platform embedding settings. The tokenizer is `cl100k_base (derived from
text-embedding-3-small)`. Change the embedding model to a self-hosted one whose tokenizer we do
not ship; the setting falls to `approximate`, the screen says chunk sizes are now estimates,
and every connector shows as needing a recut — because a chunk sized in a different unit is a
different chunk.

Open a connector's document list: each document shows the tokenizer it was cut with.

## In scope

- A tokenizer registry with the encodings that ship (`tiktoken`'s), an `approximate`
  tokenizer parameterised by a characters-per-token ratio, and `words` as the existing
  fallback.
- Derivation from the model — dialect and model id — with an explicit override per upstream
  model and on the platform embedding setting.
- Threading the right tokenizer to the right consumer: the embedding model's to chunking, the
  gateway's target's to the assembler and the limiter's estimate.
- Calibration: measuring our count against the provider's reported count per model, and
  showing the drift.
- Recording the tokenizer name on documents, and treating a change of it as a chunking change.

## Out of scope

- **Shipping Hugging Face vocabularies** (Llama, Mistral, Qwen). The `tokenizers` library is
  a fine dependency; the vocabularies are a download per model, some behind licence gates, and
  an air-gapped deployment cannot fetch them — the exact failure the `WordTokenizer` fallback
  exists to survive. Calibrated `approximate` covers these models within a few percent, and
  the calibration display says how far off it is. Revisit if a deployment needs exact counts
  for a model we cannot ship.
- **Anthropic's `count_tokens` API** as a live tokenizer. A network round trip per chunk
  during ingestion and per request during assembly is the wrong shape; it is fine as a
  *calibration source*, and calibration already gets a better one for free from every
  response's usage block.
- **Per-gateway tokenizer overrides.** A gateway has targets; a target has a model; a model
  has a tokenizer. A gateway-level override would be a fourth place for the same fact, and the
  A/B case it might seem to help (two targets, two tokenizers) is handled by measuring with
  the primary target's and showing the drift for the other.
- **Changing what `chunk_size` means.** It stays a token count. The unit becomes correct; the
  number does not move.

## Work items

### The registry

- [x] `app/services/tokenizers.py`: `TOKENIZERS`, a closed registry of names →
      constructors: `cl100k_base`, `o200k_base`, `p50k_base` (tiktoken), `approximate`
      (ratio-parameterised), `words`. `resolve(spec) -> Tokenizer`, cached per spec — loading
      a BPE vocabulary is what `workers/runtime.py` already says is expensive.
- [x] `TokenizerSpec(name, ratio=None)` as the stored form. `approximate` requires a ratio;
      others reject one. Validated through the same `ConfigBlob` machinery as everything else,
      so a misspelled name is a 422 and not a silent `words`.
- [x] `ApproximateTokenizer(ratio)`: character offsets at every `ratio` characters, snapped to
      whitespace where one is within reach, so `token_span` still lands on word boundaries and
      the chunker's "never mid-word" invariant survives. Its `name` includes the ratio —
      `approximate:3.6` — because two approximations with different ratios cut different
      chunks and the fingerprint has to say so.
- [x] `derive(dialect, model_id) -> TokenizerSpec`: a preset table keyed by model-id prefix
      per dialect — `gpt-4o*`/`o1*`/`o3*` → `o200k_base`; `gpt-4*`/`gpt-3.5*`/
      `text-embedding-3*`/`text-embedding-ada*` → `cl100k_base`; `claude*` →
      `approximate:3.5`; anything else → `approximate:4.0`. The table lives in one place next
      to the provider presets the UI already has (`web/src/pages/providerPresets.ts` has the
      same knowledge for a different purpose; generate one from the other, or the two will
      disagree within a month).
- [x] The `TiktokenCounter` fallback to `WordTokenizer` on a failed vocabulary download stays,
      and is now **visible**: a tokenizer that degraded reports `name = "words (cl100k_base
      unavailable)"`, which lands on the document row and on the model page. Today the
      degradation is a log line nobody reads.

### Where it is configured

- [x] `UpstreamModel.tokenizer: TokenizerSpec | None`. `None` means *derived*; the API returns
      both the stored value and the `effective` one with its origin, the way task 20 returns
      `effective_chunking`. The form shows the derived value greyed with an **Override** toggle.
- [x] `EmbeddingChoice.tokenizer: TokenizerSpec | None`, same shape. This is the one that
      **changes chunking**: the fingerprint from task 20 gains the tokenizer name, and a change
      to it — derived or overridden — makes every connector's documents stale (104 shows it;
      until then `requires_reindex` reports it).
- [x] `Document.tokenizer` recorded at ingestion, beside `chunk_strategy` and
      `chunk_fingerprint`. Nullable, no backfill, for the reason task 20 gave: a guess in a
      drift-detection column is worse than a blank.

### Where it is used

- [x] **Chunking** gets the embedding model's tokenizer. `IngestionPipeline` stops taking a
      tokenizer at construction and asks the platform settings for it per document — the
      settings are cached per worker already, and a mid-run change is exactly the case that
      must be recorded per document rather than per process.
- [x] **Assembly and the limiter estimate** get the gateway's *primary* target's tokenizer
      (the single target, the first failover, or the heaviest A/B weight). `ProxyService`
      resolves it alongside the target; `estimate_tokens` and `prepare` use the same one, which
      keeps the property its docstring promises — `tokens_per_minute` and `doc_max_tokens`
      stay one unit.
- [x] **Chunking Compare and Try retrieval** measure with whichever tokenizer ingestion or
      assembly would — they are the same code path, and if they took a tokenizer of their own
      they would stop being one.
- [x] Every place that constructs `WordTokenizer()` as a default (`prompt.py`,
      `memory_preview.py`) is audited: a default is fine in a unit test and wrong in a service.

### Calibration

- [x] Every response already carries the provider's `prompt_tokens`, and every request already
      has our estimate. Record the ratio per `(upstream_model_id)` as a rolling window in the
      metrics store — `estimated`, `reported`, `samples`. Non-streaming and streaming both
      (streaming usage arrives on the final frame when the client asked for it; when it did
      not, the sample is skipped rather than guessed).
- [x] The model page shows **"our count vs. the provider's: ×1.04 over 3 120 requests"** and,
      for `approximate`, a **Calibrate** button that sets the ratio to what the window
      measured. It is a button and not automatic because a ratio that moves by itself moves
      the chunk fingerprint by itself.
- [x] A drift over a threshold (say 15%) is a warning on the model page and on the gateway
      that routes to it — not an alert. It is a configuration problem with a one-click fix, and
      the operator who fixes it is looking at the screen, not the pager.
- [x] `tokenizer_drift_ratio{model}` as a gauge, so a deployment that wants the alert can have
      it.

### Spec

- [x] Amend §8.4 (a `tokenizer` field, derived unless overridden) and §9.4 (the embedding
      tokenizer is part of the chunking configuration, and changing it is a recut). Amend §11
      to say which tokenizer the estimate uses and that the drift is measured.

## Acceptance criteria

- [x] A `gpt-4o` model derives `o200k_base`; a `claude-*` model derives a calibrated
      approximation; a model with an override uses the override, and the API says which.
- [x] Two gateways with `doc_max_tokens: 2000`, one routing to a `cl100k_base` model and one to
      an `o200k_base` model, inject different numbers of characters for the same chunks, and
      each stays within budget *as measured by its own tokenizer*.
- [x] `estimate_tokens` and `prepare` agree on the tokenizer for every routing mode.
- [x] Changing the embedding tokenizer marks documents stale via the chunk fingerprint;
      changing an upstream model's tokenizer marks nothing stale (nothing indexed depends on it).
- [x] A document ingested under `approximate:3.6` records that on its row; the connector's
      document list shows it.
- [x] After N requests through a model, the calibration ratio equals `reported / estimated`
      over those requests, and **Calibrate** stores it as the override.
- [x] `ApproximateTokenizer` passes the shared chunking invariant suite (`tests/chunking_contract.py`)
      under every strategy: no word lost, no word invented, nothing over the ceiling.
- [x] A tiktoken vocabulary that fails to load produces a document row that *says so*, not a
      row that claims `cl100k_base`.

## Tests

- Derivation table: every preset, an unknown model, an unknown dialect; and the parity test
  between the Python table and the web presets.
- `ApproximateTokenizer` against the tokenizer port's contract (offsets non-empty, ends at
  `len(text)`, monotone) and against the chunking invariant suite.
- Threading: the pipeline uses the embedding tokenizer, the proxy uses the primary target's,
  asserted by giving them distinguishable doubles and checking the recorded name.
- Fingerprint: the tokenizer name is in it, and `changed_formats` reports a tokenizer change.
- Calibration arithmetic over a recorded window, including the streaming-without-usage skip.
- The degraded-vocabulary name reaching the document row.

## Implementation notes

- **Where things landed.** `app/services/tokenizers.py` is the registry, the derivation
  table, `Effective` (spec + origin) and the calibration arithmetic; `app/services/tokenizer.py`
  keeps the port and gains `ApproximateTokenizer` and the degraded name. The override is
  `upstream_models.tokenizer` (JSONB, `NULL` = derived) and `EmbeddingChoice.tokenizer`; the
  record is `documents.tokenizer`, the chunk payload's `tokenizer`, and
  `request_logs.tokenizer` + `estimated_prompt_tokens`. Migration `0020_tokenizer_follows_model`.
- **The count is exact and the chunker keeps words whole — not the other way round.** The
  work item asked the approximate tokenizer to snap its boundaries to whitespace so "never
  mid-word" survives. Snapping far enough to guarantee that makes the count depend on word
  length (two ideal offsets collapse onto one word start), and then the ratio stops meaning
  "characters per token", which is the quantity the calibration corrects. So the tokenizer
  snaps only within *half a token* (count stays `len / ratio` ± 1) and the invariant moved to
  where it belongs: `_split` in `chunking.py` now walks a cut off the inside of a word, for the
  closing edge and for the overlap's opening edge. That is a no-op for `WordTokenizer` (its
  boundaries were word starts already) and a real fix for `cl100k_base`, whose sub-word tokens
  could open a `fixed` chunk with the second half of `extraordinary` before this. The shared
  invariant suite runs under `approximate:3.6` for every strategy.
- **"Primary target" became "the target being prepared".** The work item said assembly and
  the limiter estimate get the *primary* target's tokenizer. `prepare()` runs once per routing
  attempt, so the tokenizer is simply the attempt's own target's; for `single` and `ab_split`
  that *is* the primary, and for `failover` the retry is budgeted in the unit of the model that
  will actually read it. `estimate_tokens` reads the count off the assembly the same call made,
  so the two cannot disagree for any routing mode. The estimate for the limiter is still taken
  from the first target, as before, and settlement corrects the difference either way.
- **Calibration is a query, not a table.** The request log already had the provider's
  `prompt_tokens`; it now has ours and the tokenizer's name beside it, and the "rolling window"
  is `GET /models/calibration` grouping the last 30 days by `(model, tokenizer)` — grouped by
  tokenizer name so an override starts a fresh window rather than inheriting the old unit's
  error. Rows without both counts (a stream whose client never asked for usage, a refused
  request) are skipped, not guessed. The Prometheus gauge is fed from an in-process window of
  the last 256 samples per model, because a gauge is a process-local number.
- **What "stale" means.** The connector's document listing compares each row's
  `chunk_fingerprint` against the one ingestion would write now (settings, embedding model
  where the strategy depends on it, tokenizer) and returns `stale` per row. A row with no
  fingerprint — indexed before task 20 — is not stale, it is unknown. A tokenizer change on the
  platform therefore starts no reindex run; it makes every connector show its documents as
  stale, and **Reindex** on the connector is the recut.
- **The UI's derivation is the server's table.** `GET /tokenizers` serves the registry and the
  derivation rows; `web/src/pages/tokenizers.ts` applies the same longest-prefix rule to them
  while somebody types. The parity test in `tests/test_tokenizers.py` reads
  `providerPresets.ts` and asserts every preset model derives something better than the
  fallback unless it is a family we knowingly approximate.
- **Behaviour change on existing deployments.** The `hash` development embedder and any
  self-hosted embedding model now derive `approximate:4` where they used to get `cl100k_base`;
  `text-embedding-3-*` keeps `cl100k_base` and its chunks stay valid. Everything else is a
  stale document list and a reindex — see `docs/runbooks/chunking-change.md`.

## Notes

- **"Derived unless overridden" is the whole design.** Nobody should have to know what
  `o200k_base` is to get correct counts for `gpt-4o`; anyone running something we have not
  heard of should be able to say what it is. Both must show their origin, because the day a
  derivation is wrong for a new model the fastest fix is an override and the fastest diagnosis
  is seeing that no override is set.
- **The calibration is the honest part.** We will never have every tokenizer. Showing the
  measured error against the one count that is authoritative — the provider's — turns an
  approximation from a lie into an estimate with an error bar. It also catches the case where
  a *derived* tokenizer is wrong, which is the case nobody would otherwise look for.
- **This changes chunk sizes for existing connectors on non-OpenAI embedding models.** They
  were being cut with the wrong tokenizer; after this they are cut with a better one, and the
  fingerprint says every document is stale. That is correct and it is a reindex bill. The
  runbook for it is `docs/runbooks/chunking-change.md`, which gains a paragraph.
