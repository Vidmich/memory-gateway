# Un-deleting an organization inside its grace period

**Situation.** A superadmin scheduled an organization for deletion and it should not have
been. Its members cannot sign in and its gateways are refusing traffic.

**The good news.** Scheduling a deletion sets `status = 'deleting'` and `purge_after` to a
date in the future. Nothing is destroyed until the purge pass runs after that date. Inside
the grace period the fix is one API call and everything comes back — rows, documents,
vectors, memory, keys.

## Check how long is left

```sql
select id, name, slug, status, purge_after
from organizations where status = 'deleting';
```

If `purge_after` is in the future, continue. If the purge has already run, this runbook
does not apply — see [backup-restore.md](backup-restore.md), and expect to restore the
whole database to a point in time rather than one tenant.

## Cancel it

**Platform → Organizations → the organization → cancel deletion**, or:

```bash
curl -sS -X DELETE \
  https://<host>/api/v1/platform/organizations/<id>/deletion \
  -H "Authorization: Bearer <superadmin token>"
```

The status returns to `active` and the members can sign in immediately — suspension is
enforced at login, at refresh and on every control-plane request, so it lifts everywhere at
once rather than at the next token expiry.

Both the schedule and the cancellation are audit events (`organization.delete.request`,
`organization.delete.cancel`), so who did what and when is already recorded.

## Verify

* a member can sign in;
* the organization's gateways serve a completion;
* **Try retrieval** on one of them returns chunks, confirming vectors were never touched.

## If this happens more than once

The deletion flow already requires the slug to be typed to confirm. If it is still being
triggered by accident, lengthen the grace period — it is the whole safety mechanism, and
its only cost is storage.
