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
