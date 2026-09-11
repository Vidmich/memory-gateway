/**
 * One vocabulary for "how is this thing doing", shared by every list screen from task 05
 * on. The server owns the enum, so an unrecognised value renders neutrally rather than
 * breaking the page the day a new one is added.
 */

export type Tone = 'ok' | 'warn' | 'error' | 'neutral' | 'info'

const KNOWN: Record<string, Tone> = {
  active: 'ok',
  enabled: 'ok',
  ready: 'ok',
  indexed: 'ok',
  pending: 'info',
  invited: 'info',
  processing: 'info',
  // The middle of SPEC 9.5's pipeline. Blue rather than green: work in progress is not
  // the same as work finished, and a row that looked done while it was still embedding
  // would be the one thing this table must not say.
  extracting: 'info',
  summarizing: 'info',
  chunking: 'info',
  embedding: 'info',
  syncing: 'info',
  // Recognised, deliberately not ingested. Amber, because a customer who dropped in a
  // folder of PDFs needs to notice, and grey is what people scroll past.
  skipped: 'warn',
  deleting: 'warn',
  suspended: 'warn',
  disabled: 'warn',
  degraded: 'warn',
  failed: 'error',
  error: 'error',
  revoked: 'error',
}

export function toneFor(status: string): Tone {
  return KNOWN[status.toLowerCase()] ?? 'neutral'
}
