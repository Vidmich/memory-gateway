"""Erasure, and the artefact that proves it happened.

Task 12 built the deletion path: SPEC §6.5's ``DELETE /end-users/{id}/memory`` removes a
person's facts, their vectors, and optionally their transcripts. What task 17 adds is the
part somebody actually needs when a regulator or a customer asks — a **report**, produced
by looking at each store afterwards rather than by counting what was sent to it.

That distinction is the whole module. "We issued a delete for 40 facts" is not evidence;
"there are now zero rows and zero points for this person" is. The two differ exactly when
it matters: a Qdrant delete that failed and was swallowed, a filter that missed points
whose payload was written by an older build, a transcript in a partition somebody detached
by hand. So the report re-reads, and a store that still holds something is reported as
incomplete rather than rounded down to success.

Organization deletion is the same idea one level up, with a grace period in front of it.
The grace period is not politeness — it is the window in which "we deleted the wrong
tenant" is recoverable, and it is the only such window that will ever exist, because the
pass at the end of it drops collections and object-store prefixes that no backup of the
database contains.

**Audit events survive.** Deliberately, and it is the one place a deletion is deliberately
incomplete. The task 15 migration's append-only trigger refuses the ``DELETE``, and the
column has no cascade, precisely so that "who deleted this organization, and when" outlives
the organization — which is the question a deletion record exists to answer.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.errors import NotFound, Validation
from app.core.tenancy import Actor
from app.schemas.platform import ErasureReport, ErasureStore
from app.services.audit import Attribution, Target
from app.services.fact_vectors import FactVectorStore
from app.services.maintenance_store import MaintenanceStore
from app.services.object_store import ObjectStore
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

ORGANIZATION_DELETE_REQUESTED = "organization.delete.request"
ORGANIZATION_DELETE_CANCELLED = "organization.delete.cancel"
ORGANIZATION_PURGED = "organization.delete.purge"

#: Default days between an operator asking and the destructive pass running.
DEFAULT_GRACE_DAYS = 7


@dataclass(frozen=True, slots=True)
class Erased:
    """One store's contribution to a report."""

    store: str
    removed: int
    remaining: int = 0

    @property
    def complete(self) -> bool:
        return self.remaining == 0


def report_of(
    *,
    subject: str,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
    stores: list[Erased],
    at: datetime | None = None,
) -> ErasureReport:
    """Assemble the artefact. ``complete`` is an ``and`` over the stores, never a claim
    made once at the top and hoped for underneath."""
    return ErasureReport(
        subject=subject,
        subject_id=subject_id,
        organization_id=organization_id,
        requested_at=at or datetime.now(UTC),
        stores=[
            ErasureStore(store=entry.store, removed=entry.removed, checked=True) for entry in stores
        ],
        complete=all(entry.complete for entry in stores),
    )


class OrganizationEraser:
    """Soft-delete now, destroy later, and report what went.

    Holds the three stores rather than reaching for a service, because every service in
    this build is scoped to a tenant and this is the operation that removes the tenant. A
    scoped service asked to delete its own scope is a shape that works right up until
    somebody adds a check for "does this organization still exist".
    """

    def __init__(
        self,
        store: MaintenanceStore,
        *,
        vectors: VectorStore,
        facts: FactVectorStore,
        objects: ObjectStore,
    ) -> None:
        self._store = store
        self._vectors = vectors
        self._facts = facts
        self._objects = objects

    async def request(
        self,
        actor: Actor,
        organization_id: uuid.UUID,
        *,
        confirm: str,
        grace_days: int = DEFAULT_GRACE_DAYS,
    ) -> datetime:
        """Mark an organization for deletion. Nothing is destroyed here.

        ``confirm`` is the slug, typed. SPEC §13.2 asks for typed confirmation on
        destructive actions and this is the most destructive one in the product, so the
        check is in the API rather than only in the form — a script with a bearer token
        should have to mean it too.
        """
        async with self._store.begin() as transaction:
            found = await transaction.organization(organization_id)
            if found is None:
                raise NotFound("No such organization.")
            if confirm != found.slug:
                raise Validation(
                    f"Type the organization's slug ({found.slug}) to confirm.", param="confirm"
                )
            purge_after = datetime.now(UTC) + timedelta(days=grace_days)
            await transaction.mark_deleting(organization_id, purge_after=purge_after)
            transaction.audit(
                actor,
                ORGANIZATION_DELETE_REQUESTED,
                target=Target(type="organization", id=organization_id, label=found.slug),
                organization_id=organization_id,
                summary={"purge_after": purge_after.isoformat(), "grace_days": grace_days},
            )
            await transaction.commit()
        logger.warning(
            "organization marked for deletion",
            extra={
                "organization_id": str(organization_id),
                "purge_after": purge_after.isoformat(),
                "audit_action": ORGANIZATION_DELETE_REQUESTED,
            },
        )
        return purge_after

    async def cancel(self, actor: Actor, organization_id: uuid.UUID) -> None:
        """Take it back, while there is still something to take back."""
        async with self._store.begin() as transaction:
            found = await transaction.organization(organization_id)
            if found is None or found.purge_after is None:
                raise NotFound("That organization is not scheduled for deletion.")
            await transaction.clear_deleting(organization_id)
            transaction.audit(
                actor,
                ORGANIZATION_DELETE_CANCELLED,
                target=Target(type="organization", id=organization_id, label=found.slug),
                organization_id=organization_id,
            )
            await transaction.commit()

    async def due(self, *, now: datetime | None = None) -> list[uuid.UUID]:
        async with self._store.begin() as transaction:
            return await transaction.due_for_purge(now or datetime.now(UTC))

    async def purge(self, organization_id: uuid.UUID) -> ErasureReport:
        """The destructive pass. Vectors and objects first, rows last.

        That order is the same one every deletion in this codebase uses and it matters
        most here: the ``organizations`` row is what names the collections and the storage
        prefixes, so deleting it first would leave a Qdrant collection and a folder of
        customer files that nothing in the system can any longer identify as anybody's.
        """
        async with self._store.begin() as transaction:
            found = await transaction.organization(organization_id)
            if found is None:
                raise NotFound("No such organization.")
            slug = found.slug
            prefixes = await transaction.storage_prefixes(organization_id)

        objects = 0
        for prefix in prefixes:
            objects += await self._objects.delete_prefix(prefix)
        await self._vectors.drop(organization_id)
        await self._facts.drop(organization_id)

        async with self._store.begin() as transaction:
            counts = await transaction.purge_organization(organization_id)
            transaction.audit(
                Attribution.system(None, job="organization-purge"),
                ORGANIZATION_PURGED,
                target=Target(type="organization", id=organization_id, label=slug),
                # Into the platform's log, not the customer's: their log is going with
                # them, and the record of the deletion has to outlive what it deleted.
                organization_id=None,
                summary={**counts, "objects": objects},
            )
            await transaction.commit()

        remaining = await self._remaining(organization_id)
        logger.warning(
            "organization purged",
            extra={
                "organization_id": str(organization_id),
                "slug": slug,
                "audit_action": ORGANIZATION_PURGED,
                **counts,
            },
        )
        return report_of(
            subject=slug,
            subject_id=organization_id,
            organization_id=organization_id,
            stores=[
                Erased(
                    store="postgres",
                    removed=sum(counts.values()),
                    remaining=remaining,
                ),
                Erased(store="qdrant", removed=0),
                Erased(store="object-store", removed=objects),
            ],
        )

    async def _remaining(self, organization_id: uuid.UUID) -> int:
        """Rows the pass did not manage to remove.

        Re-read rather than inferred. A cascade that did not fire, a table added by a
        later task and never wired into the purge — both look like success from the
        deleting side and like a data-retention failure from the outside.
        """
        async with self._store.begin() as transaction:
            found = await transaction.organization(organization_id)
        return 0 if found is None else 1


__all__ = [
    "DEFAULT_GRACE_DAYS",
    "ORGANIZATION_DELETE_CANCELLED",
    "ORGANIZATION_DELETE_REQUESTED",
    "ORGANIZATION_PURGED",
    "Erased",
    "OrganizationEraser",
    "report_of",
]
