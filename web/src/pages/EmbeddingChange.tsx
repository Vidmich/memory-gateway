import { useState } from 'react'

import { useTokenizers } from '@/api/models'
import { useUpdatePlatformSettings } from '@/api/platform'
import { useApiClient } from '@/auth/AuthContext'
import type { PlatformSettingsResponse, ReindexEstimate, TokenizerSpec } from '@/api/types'
import { Field, Form, Select, SubmitButton, TextInput } from '@/components/Form'
import { useToast } from '@/components/Toast'
import { TokenizerField } from '@/pages/TokenizerField'
import { deriveTokenizer, tokenizerKey } from '@/pages/tokenizers'

/**
 * Platform → Settings, the embedding panel (SPEC §9.4).
 *
 * Its own component because it is its own flow. Changing the model here does not save a
 * setting: it starts a reindex, and the setting is written when the aliases swap. Between
 * those two moments the screen shows the *old* model as current and the new one as
 * pending, which is not a rendering compromise — until the swap, the old model is the one
 * every collection agrees with, and showing the new one would be describing a state that
 * does not exist yet.
 *
 * The confirmation is the model name retyped, and the estimate is fetched from the server
 * before it is offered. Both are because this is the most expensive button in the product:
 * it re-embeds every chunk on the platform, and nobody should reach it by tabbing.
 */
export function EmbeddingChange({ settings }: { settings: PlatformSettingsResponse }) {
  const client = useApiClient()
  const update = useUpdatePlatformSettings()
  const { notify } = useToast()

  const current = settings.settings.embedding
  const pending = settings.pending_embedding
  const running = settings.reindex

  const [provider, setProvider] = useState(current?.provider ?? 'hash')
  const [name, setName] = useState(current?.name ?? '')
  const [dimension, setDimension] = useState(String(current?.dimension ?? ''))
  const [confirm, setConfirm] = useState('')
  const [estimate, setEstimate] = useState<ReindexEstimate | null>(null)
  const [estimating, setEstimating] = useState(false)
  //  Task 101. `null` derives the chunker's tokenizer from the model above; an override
  //  is the one tokenizer setting that changes *chunking*, and the panel says so.
  const [tokenizer, setTokenizer] = useState<TokenizerSpec | null>(current?.tokenizer ?? null)
  const tokenizers = useTokenizers()

  const changed = name !== current?.name || Number(dimension) !== current?.dimension
  const derived = tokenizers.data ? deriveTokenizer(tokenizers.data, provider, name) : null
  const effectiveKey = tokenizer ? tokenizerKey(tokenizer) : derived ? tokenizerKey(derived) : null
  const tokenizerMoved =
    settings.embedding_tokenizer !== null &&
    settings.embedding_tokenizer !== undefined &&
    effectiveKey !== null &&
    effectiveKey !== tokenizerKey(settings.embedding_tokenizer.spec)

  const preview = async () => {
    setEstimating(true)
    try {
      // From the server, not computed here: the number an operator agrees to has to be
      // the number the run will actually work through, and a browser-side guess would
      // drift from it the moment the scope rules change.
      setEstimate(
        await client.post<ReindexEstimate>('/api/v1/platform/reindex', { dry_run: true }),
      )
    } finally {
      setEstimating(false)
    }
  }

  const apply = () =>
    update.mutate(
      {
        embedding: { provider, name, dimension: Number(dimension), tokenizer },
        confirm_reindex: confirm,
      },
      {
        onSuccess: () => {
          setConfirm('')
          setEstimate(null)
          notify('Reindex started. Retrieval keeps using the current model until it swaps.')
        },
      },
    )

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-6">
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <h2 className="text-sm font-semibold text-slate-900">Embedding model</h2>
        <span className="text-xs text-slate-500">
          One model for the whole platform (SPEC §9.4)
        </span>
      </div>

      <p className="mb-4 text-sm text-slate-600">
        Currently <strong className="font-mono">{current?.name}</strong> at{' '}
        {current?.dimension} dimensions.
        {pending ? (
          <>
            {' '}
            Moving to <strong className="font-mono">{pending.name}</strong> at{' '}
            {pending.dimension} dimensions — searches keep using the current model until the
            new collections are complete.
          </>
        ) : null}
      </p>

      {running ? (
        <p
          role="status"
          className="mb-4 rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-800"
        >
          A reindex is running. Watch its progress on Platform → Maintenance.
        </p>
      ) : null}

      <Form onSubmit={apply} error={update.error}>
        <Field name="embedding.provider" label="Provider">
          {(props) => (
            <Select
              {...props}
              value={provider}
              disabled={Boolean(running)}
              onChange={(event) => setProvider(event.target.value as 'openai' | 'hash')}
            >
              <option value="openai">OpenAI-compatible</option>
              <option value="hash">Local hashing (development only)</option>
            </Select>
          )}
        </Field>

        <Field
          name="embedding.name"
          label="Model"
          hint="Changing this re-embeds every collection. The endpoint and the API key stay in the environment."
        >
          {(props) => (
            <TextInput
              {...props}
              value={name}
              disabled={Boolean(running)}
              onChange={(event) => setName(event.target.value)}
            />
          )}
        </Field>

        <Field name="embedding.dimension" label="Dimensions">
          {(props) => (
            <TextInput
              {...props}
              inputMode="numeric"
              value={dimension}
              disabled={Boolean(running)}
              onChange={(event) => setDimension(event.target.value)}
            />
          )}
        </Field>

        {tokenizers.data && derived ? (
          <TokenizerField
            namePrefix="embedding.tokenizer"
            derived={derived}
            effective={settings.embedding_tokenizer}
            value={tokenizer}
            onChange={setTokenizer}
            names={tokenizers.data.names}
            disabled={Boolean(running)}
          />
        ) : null}
        {effectiveKey?.startsWith('approximate') ? (
          <p role="note" className="mb-4 text-xs text-slate-600">
            No vocabulary ships for this model, so chunk sizes are estimates: a chunk of
            &ldquo;1000 tokens&rdquo; is 1000 &times; the ratio in characters. Calibrate the
            ratio from a chat model on the same tokenizer family, or accept a few percent of
            error at the window edge.
          </p>
        ) : null}
        {tokenizerMoved && !running ? (
          <p
            role="status"
            className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
          >
            Changing the tokenizer changes what <span className="font-mono">chunk_size</span>{' '}
            means. Every indexed document becomes stale — each connector will show its
            documents as needing a recut — without starting a platform reindex.
          </p>
        ) : null}

        {changed && !running ? (
          <div className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            <p className="font-medium">This starts a reindex.</p>
            {estimate ? (
              <ul className="mt-2 list-disc space-y-1 pl-5">
                <li>
                  {estimate.organizations} collection
                  {estimate.organizations === 1 ? '' : 's'}:{' '}
                  <span className="font-mono text-xs">
                    {(estimate.collections ?? []).join(', ') || 'none'}
                  </span>
                </li>
                <li>
                  {estimate.points.toLocaleString()} chunks, about{' '}
                  {estimate.tokens.toLocaleString()} tokens to embed
                </li>
              </ul>
            ) : (
              <button
                type="button"
                onClick={() => void preview()}
                className="mt-2 rounded-md border border-amber-300 bg-white px-3 py-1.5 text-sm font-medium text-amber-900 hover:bg-amber-100"
              >
                {estimating ? 'Counting…' : 'Show what this will cost'}
              </button>
            )}
          </div>
        ) : null}

        {changed && !running ? (
          <Field
            name="confirm_reindex"
            label="Confirm"
            hint={`Type ${name} to start the reindex.`}
          >
            {(props) => (
              <TextInput
                {...props}
                value={confirm}
                autoComplete="off"
                onChange={(event) => setConfirm(event.target.value)}
              />
            )}
          </Field>
        ) : null}

        <SubmitButton
          busy={update.isPending}
          disabled={Boolean(running) || (changed && confirm !== name)}
          className="w-auto"
        >
          {changed ? 'Start reindex' : 'Save'}
        </SubmitButton>
      </Form>
    </section>
  )
}
