/**
 * Prefilled base URLs and auth styles for the providers people actually configure.
 *
 * Almost every failed "Test connection" is a wrong base URL: a missing `/v1`, a trailing
 * slash, or an Azure resource URL with the deployment in the wrong place. A dropdown that
 * fills the field in correctly removes the whole class, which is worth more than it looks
 * for a screen most people use once per provider and then never again.
 *
 * Everything here is OpenAI-shaped, so they all take the `openai` dialect — the
 * differences between them are exactly base URL and auth style, which is what
 * `UpstreamTarget` carries (`app/adapters/openai.py`).
 *
 * A preset is a starting point, never a constraint: every field stays editable, and
 * `custom` fills in nothing.
 */

export type Preset = {
  id: string
  label: string
  baseUrl: string
  authType: 'bearer' | 'api_key_header' | 'azure' | 'none'
  /** A model id that exists on that provider, so the field is not left blank. */
  modelId: string
  /** Shown under the base URL when this preset is chosen. */
  hint?: string
}

export const PRESETS: readonly Preset[] = [
  {
    id: 'openai',
    label: 'OpenAI',
    baseUrl: 'https://api.openai.com/v1',
    authType: 'bearer',
    modelId: 'gpt-4o-mini',
  },
  {
    id: 'azure',
    label: 'Azure OpenAI',
    baseUrl: 'https://YOUR-RESOURCE.openai.azure.com/openai/deployments/YOUR-DEPLOYMENT',
    authType: 'azure',
    modelId: 'gpt-4o-mini',
    hint: 'Keep the ?api-version=… query string on the URL — it is preserved and sent.',
  },
  {
    id: 'groq',
    label: 'Groq',
    baseUrl: 'https://api.groq.com/openai/v1',
    authType: 'bearer',
    modelId: 'llama-3.3-70b-versatile',
  },
  {
    id: 'together',
    label: 'Together',
    baseUrl: 'https://api.together.xyz/v1',
    authType: 'bearer',
    modelId: 'meta-llama/Llama-3.3-70B-Instruct-Turbo',
  },
  {
    id: 'openrouter',
    label: 'OpenRouter',
    baseUrl: 'https://openrouter.ai/api/v1',
    authType: 'bearer',
    modelId: 'openai/gpt-4o-mini',
  },
  {
    id: 'vllm',
    label: 'vLLM',
    baseUrl: 'http://localhost:8000/v1',
    authType: 'none',
    modelId: 'meta-llama/Llama-3.1-8B-Instruct',
    hint: 'Self-hosted, so usually no credential. Add one if you started vLLM with an API key.',
  },
  {
    id: 'ollama',
    label: 'Ollama',
    baseUrl: 'http://localhost:11434/v1',
    authType: 'none',
    modelId: 'llama3.2',
  },
]

export function presetById(id: string): Preset | undefined {
  return PRESETS.find((preset) => preset.id === id)
}

/**
 * Which preset a saved model came from, if any — matched on base URL so reopening an
 * OpenAI model shows "OpenAI" rather than "Custom". Falls back to custom, which is the
 * honest answer for a URL nobody here recognises.
 */
export function presetFor(baseUrl: string): string {
  const origin = baseUrl.trim().replace(/\/+$/, '')
  return PRESETS.find((preset) => preset.baseUrl === origin)?.id ?? 'custom'
}
