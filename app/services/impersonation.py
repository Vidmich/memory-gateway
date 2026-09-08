"""Recording that a platform administrator opened a customer's organization.

SPEC §5.2: "Can view any org for support purposes — every such access is audit-logged."
The word doing the work there is *access*, and this module is about the gap between that
word and what actually arrives at the server.

A support session is not one request. Opening an organization and reading three screens is
forty or fifty requests, each carrying the same ``X-Assume-Organization`` header, and a row
per request would produce a log nobody can read and a table growing at the rate of somebody
scrolling. What a customer wants to find is "somebody from the vendor was in here on
Tuesday afternoon", so the unit recorded is a **session**: the first assumed request writes
an event, and the next :data:`SUPPORT_SESSION_SECONDS` of them are the same visit.

The gate is the counter in :mod:`app.services.login_throttle`, which is Redis-backed in
production — so two replicas serving one support session still write one event — and it
**fails open**: if the counter is unreachable, the event is written. Duplicate records of a
support access are noise; a missing one is the failure this exists to prevent.

The write is off the request path. A superadmin's read must not wait on it and must not
fail because of it, and the event is not part of any unit of work the request has — which
is why this is the one place that reaches for :meth:`AuditStore.append` rather than
recording through a transaction.
"""

from __future__ import annotations

import logging
import uuid

from app.core.background import spawn
from app.services.audit import Attribution, Target, build_event
from app.services.audit_store import AuditStore
from app.services.login_throttle import ThrottleStore

logger = logging.getLogger(__name__)

KEY_PREFIX = "support-access"

#: How long one recorded access covers. Fifteen minutes is a compromise with two sides:
#: shorter and a single afternoon of support becomes a wall of rows; longer and the
#: timeline stops being able to say *when* somebody was looking.
SUPPORT_SESSION_SECONDS = 900

ACTION = "organization.assume"


class SupportAccessRecorder:
    """Writes at most one ``organization.assume`` event per administrator, per
    organization, per window."""

    def __init__(self, store: AuditStore, throttle: ThrottleStore) -> None:
        self._store = store
        self._throttle = throttle

    def note(
        self,
        *,
        actor_user_id: uuid.UUID,
        actor_label: str | None,
        organization_id: uuid.UUID,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Note an assumed request, without making it wait."""
        spawn(
            self._record(
                actor_user_id=actor_user_id,
                actor_label=actor_label,
                organization_id=organization_id,
                ip=ip,
                user_agent=user_agent,
            ),
            name=f"audit-support-access:{organization_id}",
        )

    async def _record(
        self,
        *,
        actor_user_id: uuid.UUID,
        actor_label: str | None,
        organization_id: uuid.UUID,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        if not await self._first_of_the_session(actor_user_id, organization_id):
            return
        event = build_event(
            Attribution(
                actor_type="superadmin_impersonation",
                user_id=actor_user_id,
                label=actor_label,
                organization_id=organization_id,
                ip=ip,
                user_agent=user_agent,
            ),
            ACTION,
            target=Target("organization", organization_id, None),
            # The *customer's* log, which is the whole point: this is the event that
            # makes support access visible to the organization it was performed on.
            organization_id=organization_id,
        )
        try:
            await self._store.append(event)
        except Exception:
            logger.exception(
                "could not record a support access",
                extra={"organization_id": str(organization_id), "audit_action": ACTION},
            )

    async def _first_of_the_session(
        self, actor_user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> bool:
        key = f"{KEY_PREFIX}:{actor_user_id}:{organization_id}"
        try:
            return await self._throttle.increment(key, SUPPORT_SESSION_SECONDS) == 1
        except Exception:
            logger.warning("support-access debounce unavailable; recording anyway", exc_info=True)
            return True


__all__ = ["ACTION", "KEY_PREFIX", "SUPPORT_SESSION_SECONDS", "SupportAccessRecorder"]
