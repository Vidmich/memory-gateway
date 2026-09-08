/**
 * Queries for the audit log (SPEC §10.4).
 *
 * Reads only, and there is no mutation here nor anywhere else: the table is append-only
 * and the server has no route that would change a row. That is worth saying in the client
 * too, because "there is no way to edit this" is the property the whole screen rests on.
 *
 * Two shapes, one endpoint. The full screen pages through everything with filters the
 * person chose; the contextual panel on a gateway, model or connector asks the same
 * endpoint for one object's history. The second is the one that actually gets read —
 * somebody looking at a misbehaving endpoint wants "what changed here", not a search.
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { useApiClient } from '@/auth/AuthContext'
import type { AuditEventPage } from '@/api/types'

/**
 * What the screen is asking for. Every field optional, because the honest default for
 * this table is the whole history: "when did this change" is usually answered by
 * something older than a day, and a silent window would hide it.
 */
export type AuditFilters = {
  action?: string | undefined
  targetType?: string | undefined
  targetId?: string | undefined
  actorUserId?: string | undefined
  from?: string | undefined
  to?: string | undefined
  cursor?: string | null | undefined
}

/** One key shape, because there is one query. Nothing here writes, so nothing
 *  invalidates — which is why there is no `all` to invalidate against. */
export const keys = {
  list: (query: string) => ['audit-events', query] as const,
}

/** The query string both the list and the export are built from, so they cannot drift. */
export function auditQuery(filters: AuditFilters): string {
  const query = new URLSearchParams()
  if (filters.action) query.set('action', filters.action)
  if (filters.targetType) query.set('target_type', filters.targetType)
  if (filters.targetId) query.set('target_id', filters.targetId)
  if (filters.actorUserId) query.set('actor_user_id', filters.actorUserId)
  if (filters.from) query.set('from', filters.from)
  if (filters.to) query.set('to', filters.to)
  if (filters.cursor) query.set('cursor', filters.cursor)
  return query.toString()
}

function path(filters: AuditFilters): string {
  const query = auditQuery(filters)
  return `/api/v1/audit-events${query ? `?${query}` : ''}`
}

export function useAuditEvents(filters: AuditFilters): UseQueryResult<AuditEventPage> {
  const client = useApiClient()
  const query = auditQuery(filters)
  return useQuery({
    queryKey: keys.list(query),
    queryFn: () => client.get<AuditEventPage>(path(filters)),
  })
}

/**
 * One object's history, for the panel on its detail screen.
 *
 * `enabled` is off while the id is undefined — a gateway that has not been saved has no
 * history, and asking for one would be a request for every event in the organization.
 */
export function useObjectHistory(
  targetType: string,
  targetId: string | undefined,
): UseQueryResult<AuditEventPage> {
  const client = useApiClient()
  const filters: AuditFilters = { targetType, targetId }
  const query = auditQuery(filters)
  return useQuery({
    queryKey: keys.list(query),
    enabled: targetId !== undefined,
    queryFn: () => client.get<AuditEventPage>(path(filters)),
  })
}

/** The export URL for a filter — the same one the list is showing, minus the cursor. */
export function exportPath(filters: AuditFilters): string {
  const query = auditQuery({ ...filters, cursor: null })
  return `/api/v1/audit-events/export${query ? `?${query}` : ''}`
}
