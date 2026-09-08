import { describe, expect, it } from 'vitest'

import { auditQuery, exportPath } from '@/api/audit'
import { makeAuditChange, makeAuditEvent } from '@/test/factories'
import {
  ABSENT,
  actorLabel,
  actorTone,
  afterOf,
  beforeOf,
  changeSummary,
  describeEvent,
  emptyHint,
  hasFilters,
  isRedacted,
  renderValue,
  summaryLine,
  targetLabel,
} from '@/pages/audit'

describe('describeEvent', () => {
  it('reads as a sentence rather than as a schema', () => {
    expect(describeEvent(makeAuditEvent())).toBe('ada@example.com updated gateway acme-support')
  })

  it('falls back to the raw action for a verb it has not been taught', () => {
    // Better ugly than blank: an action a later task adds still says what it was.
    const event = makeAuditEvent({ action: 'gateway.recalibrate' })
    expect(describeEvent(event)).toContain('gateway.recalibrate')
  })

  it('does not say "user somebody" for a password change', () => {
    const event = makeAuditEvent({
      action: 'user.password_change',
      target_type: 'user',
      target: 'ada@example.com',
    })
    expect(describeEvent(event)).toBe('ada@example.com changed their password')
  })

  it('names a target with no label by its kind alone', () => {
    // A memory fact has no name on purpose — its text is deliberately not recorded.
    const event = makeAuditEvent({
      action: 'memory_fact.delete',
      target_type: 'memory_fact',
      target: null,
    })
    expect(describeEvent(event)).toBe('ada@example.com deleted memory fact')
  })

  it('survives an event with no actor at all', () => {
    expect(describeEvent(makeAuditEvent({ actor: null }))).toMatch(/^Somebody /)
  })
})

describe('rendering a value', () => {
  it('tells "the field held null" from "the field was not there"', () => {
    // The first is a cleared credential; the second is one being set. Both would be an
    // empty cell if this collapsed them.
    expect(renderValue(null, true)).toBe('null')
    expect(renderValue(null, false)).toBe(ABSENT)
  })

  it('says so when a string is empty rather than showing nothing', () => {
    expect(renderValue('', true)).toBe('(empty)')
  })

  it('shows false as false, not as blank', () => {
    expect(renderValue(false, true)).toBe('false')
  })

  it('renders a list as JSON, because a routing chain is one value', () => {
    expect(renderValue(['a (100%)'], true)).toBe('["a (100%)"]')
  })

  it('leaves the missing side of an added field absent', () => {
    const change = makeAuditChange({ kind: 'added', before: null, after: 'eu' })
    expect(beforeOf(change)).toBe(ABSENT)
    expect(afterOf(change)).toBe('eu')
  })

  it('leaves the missing side of a removed field absent', () => {
    const change = makeAuditChange({ kind: 'removed', before: 'eu', after: null })
    expect(beforeOf(change)).toBe('eu')
    expect(afterOf(change)).toBe(ABSENT)
  })

  it('recognises a value the server refused to keep', () => {
    expect(isRedacted(makeAuditChange({ before: '***', after: '***' }))).toBe(true)
    expect(isRedacted(makeAuditChange())).toBe(false)
  })
})

describe('the collapsed summary', () => {
  it('names the fields that changed rather than counting them', () => {
    const event = makeAuditEvent({
      changes: [
        makeAuditChange({ path: 'system_context' }),
        makeAuditChange({ path: 'memory_config.doc_top_k' }),
      ],
    })
    expect(changeSummary(event)).toBe('system_context, memory_config.doc_top_k')
  })

  it('counts the rest once there are more than three', () => {
    const event = makeAuditEvent({
      changes: ['a', 'b', 'c', 'd', 'e'].map((path) => makeAuditChange({ path })),
    })
    expect(changeSummary(event)).toBe('a, b, c and 2 more')
  })

  it('includes fields the server could not fit in the diff', () => {
    const event = makeAuditEvent({
      changes: ['a', 'b', 'c'].map((path) => makeAuditChange({ path })),
      omitted: 40,
    })
    expect(changeSummary(event)).toBe('a, b, c and 40 more')
  })

  it('says so plainly when there is no field-level detail', () => {
    expect(changeSummary(makeAuditEvent({ changes: [] }))).toBe('No field-level detail')
  })

  it('shows a bulk operation as a count and a sample', () => {
    const event = makeAuditEvent({
      action: 'connector.upload',
      changes: [],
      summary: { count: 8, sample: ['a.md', 'b.md'], rejected: 1 },
    })
    expect(changeSummary(event)).toBe('8 items — a.md, b.md · rejected: 1')
  })

  it('reads correctly for a single item', () => {
    expect(summaryLine({ count: 1, sample: [] })).toBe('1 item')
  })
})

describe('how an actor is drawn', () => {
  it('marks support access distinctly, which is the point of recording it', () => {
    const event = makeAuditEvent({ actor_type: 'superadmin_impersonation' })
    expect(actorTone(event)).toBe('support')
  })

  it('marks a background job as automated rather than as a person', () => {
    const event = makeAuditEvent({ actor_type: 'system', actor: 'delete-connector' })
    expect(actorTone(event)).toBe('system')
    expect(actorLabel(event)).toBe('delete-connector (automated)')
  })

  it('leaves an ordinary member unbadged', () => {
    expect(actorTone(makeAuditEvent())).toBe('person')
    expect(actorLabel(makeAuditEvent())).toBe('ada@example.com')
  })
})

describe('labels and empty states', () => {
  it('gives a target type a human name', () => {
    expect(targetLabel('upstream_model')).toBe('Model')
  })

  it('renders an unknown target type as itself rather than as blank', () => {
    expect(targetLabel('spaceship')).toBe('spaceship')
  })

  it('distinguishes "nothing yet" from "nothing matching"', () => {
    // The two send somebody in opposite directions, and "No results" sends half of them
    // the wrong way.
    expect(emptyHint(false)).toContain('appears in this list')
    expect(emptyHint(true)).toContain('Widen the date range')
  })

  it('knows whether anything is being filtered', () => {
    expect(hasFilters({})).toBe(false)
    expect(hasFilters({ action: undefined })).toBe(false)
    expect(hasFilters({ action: 'gateway.update' })).toBe(true)
  })
})

describe('the query string', () => {
  it('omits everything that was not asked for', () => {
    expect(auditQuery({})).toBe('')
  })

  it('uses the wire names the API documents', () => {
    const query = auditQuery({ targetType: 'gateway', targetId: 'g1', from: '2026-01-01T00:00:00Z' })
    expect(query).toContain('target_type=gateway')
    expect(query).toContain('target_id=g1')
    expect(query).toContain('from=2026-01-01')
  })

  it('drops the cursor from an export, which is the whole result and not one page', () => {
    expect(exportPath({ action: 'key.revoke', cursor: 'abc' })).toBe(
      '/api/v1/audit-events/export?action=key.revoke',
    )
  })
})
