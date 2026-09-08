"""The behaviour every audit store must have, written once.

Same arrangement as the other store contracts: the in-memory store is only worth having
if it answers like PostgreSQL does, so the questions
:class:`~app.services.audit_service.AuditService` asks are asked here and both
implementations run the same list.

The subject is three events — one per organization plus one owned by nobody — because
this table has the same two scopes the model catalog does, with the opposite rule: a
*platform* event belongs to no customer and must not appear in any customer's log. A
fixture with one organization would let "your events" and "every event" pass identically.

Not a test module itself; it is the shared body the two halves parametrize over.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import AuditEvent
from app.services.audit_store import AuditFilters, AuditStore

#: A fixed clock, so the window checks name real boundaries rather than "recently".
NOW = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


def make_event(
    *,
    organization_id: uuid.UUID | None,
    action: str = "gateway.update",
    actor_user_id: uuid.UUID | None = None,
    actor_label: str | None = "ada@example.com",
    actor_type: str = "user",
    target_type: str = "gateway",
    target_id: uuid.UUID | None = None,
    target_label: str | None = "support",
    created_at: datetime = NOW,
    changes: list[dict[str, object]] | None = None,
) -> AuditEvent:
    return AuditEvent(
        id=uuid7(),
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        actor_type=actor_type,
        action=action,
        target_type=target_type,
        target_id=target_id or uuid7(),
        target_label=target_label,
        diff={"changes": changes if changes is not None else []},
        ip="203.0.113.7",
        user_agent="pytest",
        request_id="req-1",
        created_at=created_at,
    )


@dataclass
class Fixture:
    """Two organizations with events of their own, plus one platform event."""

    store: AuditStore
    acme: uuid.UUID
    globex: uuid.UUID
    acme_event: AuditEvent
    acme_older: AuditEvent
    globex_event: AuditEvent
    platform_event: AuditEvent

    @property
    def acme_scope(self) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=self.acme)

    @property
    def platform_scope(self) -> TenantScope:
        return TenantScope(role="superadmin", organization_id=None)

    async def read(
        self, scope: TenantScope, filters: AuditFilters | None = None, *, limit: int = 50
    ) -> list[AuditEvent]:
        async with self.store.begin(scope) as reader:
            rows = await reader.events(filters or AuditFilters(), after=None, limit=limit)
        return list(rows)


def events(*rows: AuditEvent) -> list[uuid.UUID]:
    return [row.id for row in rows]


# ---------------------------------------------------------------------------
# scoping
# ---------------------------------------------------------------------------


async def an_organization_reads_its_own_events(fixture: Fixture) -> None:
    found = await fixture.read(fixture.acme_scope)
    assert {row.id for row in found} == {fixture.acme_event.id, fixture.acme_older.id}


async def an_organization_cannot_read_anothers_events(fixture: Fixture) -> None:
    found = await fixture.read(fixture.acme_scope)
    assert fixture.globex_event.id not in {row.id for row in found}


async def a_platform_event_is_not_in_any_customers_log(fixture: Fixture) -> None:
    """A global model or a platform setting belongs to nobody; it is the platform's own
    record, and an organization has no business seeing it."""
    found = await fixture.read(fixture.acme_scope)
    assert fixture.platform_event.id not in {row.id for row in found}


async def a_platform_scope_reads_everything(fixture: Fixture) -> None:
    found = await fixture.read(fixture.platform_scope)
    assert len(found) == 4


async def an_organization_filter_only_narrows(fixture: Fixture) -> None:
    """Asking for somebody else's organization returns nothing, never their rows."""
    found = await fixture.read(fixture.acme_scope, AuditFilters(organization_id=fixture.globex))
    assert found == []


async def the_platform_can_narrow_to_one_customer(fixture: Fixture) -> None:
    found = await fixture.read(fixture.platform_scope, AuditFilters(organization_id=fixture.globex))
    assert events(*found) == events(fixture.globex_event)


# ---------------------------------------------------------------------------
# ordering and paging
# ---------------------------------------------------------------------------


async def events_come_back_newest_first(fixture: Fixture) -> None:
    found = await fixture.read(fixture.acme_scope)
    assert events(*found) == events(fixture.acme_event, fixture.acme_older)


async def a_cursor_skips_what_came_before_it(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as reader:
        rows = await reader.events(AuditFilters(), after=fixture.acme_event.id, limit=50)
    assert events(*rows) == events(fixture.acme_older)


async def a_page_over_fetches_by_one(fixture: Fixture) -> None:
    """How ``page_of`` answers "is there more" without a second COUNT."""
    async with fixture.store.begin(fixture.acme_scope) as reader:
        rows = await reader.events(AuditFilters(), after=None, limit=1)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


async def filtering_by_action_selects_one_kind(fixture: Fixture) -> None:
    found = await fixture.read(fixture.acme_scope, AuditFilters(action="key.revoke"))
    assert events(*found) == events(fixture.acme_older)


async def filtering_by_target_is_the_contextual_history(fixture: Fixture) -> None:
    found = await fixture.read(
        fixture.acme_scope,
        AuditFilters(target_type="gateway", target_id=fixture.acme_event.target_id),
    )
    assert events(*found) == events(fixture.acme_event)


async def filtering_by_actor_selects_one_persons_work(fixture: Fixture) -> None:
    found = await fixture.read(
        fixture.acme_scope, AuditFilters(actor_user_id=fixture.acme_event.actor_user_id)
    )
    assert events(*found) == events(fixture.acme_event)


async def the_window_is_half_open(fixture: Fixture) -> None:
    """``[from, to)``, like every other window in this product: an event exactly on the
    upper bound belongs to the next window, not to two of them."""
    found = await fixture.read(
        fixture.acme_scope, AuditFilters(start=NOW, end=NOW + timedelta(seconds=1))
    )
    assert events(*found) == events(fixture.acme_event)

    excluded = await fixture.read(fixture.acme_scope, AuditFilters(start=NOW, end=NOW))
    assert excluded == []


async def a_window_excludes_what_falls_outside_it(fixture: Fixture) -> None:
    found = await fixture.read(fixture.acme_scope, AuditFilters(start=NOW - timedelta(hours=1)))
    assert events(*found) == events(fixture.acme_event)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


async def an_appended_event_is_readable(fixture: Fixture) -> None:
    """The one write path: a support access, which has no mutation to ride along with."""
    event = make_event(
        organization_id=fixture.acme,
        action="organization.assume",
        actor_type="superadmin_impersonation",
        target_type="organization",
    )
    await fixture.store.append(event)
    found = await fixture.read(fixture.acme_scope, AuditFilters(action="organization.assume"))
    assert events(*found) == events(event)
    assert found[0].actor_type == "superadmin_impersonation"


async def the_stored_diff_survives_the_round_trip(fixture: Fixture) -> None:
    """JSONB in one implementation and a dict in the other; the reader must not care."""
    event = make_event(
        organization_id=fixture.acme,
        action="model.update",
        changes=[{"path": "credential", "before": "***", "after": "***"}],
    )
    await fixture.store.append(event)
    found = await fixture.read(fixture.acme_scope, AuditFilters(action="model.update"))
    assert found[0].diff["changes"] == [{"path": "credential", "before": "***", "after": "***"}]


async def the_reader_exposes_no_way_to_change_an_event(fixture: Fixture) -> None:
    """Immutability, as a property of the interface rather than of the database.

    The database refuses an ``UPDATE`` too — ``tests/test_audit_db.py`` proves that — but
    a reader with no mutating method is what a reviewer sees first.
    """
    async with fixture.store.begin(fixture.acme_scope) as reader:
        for name in ("update", "delete", "remove", "save", "set", "add"):
            assert not hasattr(reader, name), name


Check = Callable[[Fixture], Awaitable[None]]

#: Every check, in one list, so neither implementation can be given a shorter exam.
CHECKS: tuple[Check, ...] = (
    an_organization_reads_its_own_events,
    an_organization_cannot_read_anothers_events,
    a_platform_event_is_not_in_any_customers_log,
    a_platform_scope_reads_everything,
    an_organization_filter_only_narrows,
    the_platform_can_narrow_to_one_customer,
    events_come_back_newest_first,
    a_cursor_skips_what_came_before_it,
    a_page_over_fetches_by_one,
    filtering_by_action_selects_one_kind,
    filtering_by_target_is_the_contextual_history,
    filtering_by_actor_selects_one_persons_work,
    the_window_is_half_open,
    a_window_excludes_what_falls_outside_it,
    an_appended_event_is_readable,
    the_stored_diff_survives_the_round_trip,
    the_reader_exposes_no_way_to_change_an_event,
)
