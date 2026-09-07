"""`TenantScope`: the object every isolation decision is made from.

The two halves — :meth:`permits` for a row in hand and :meth:`clause` for a query — have
to agree, because the in-memory store uses one and PostgreSQL uses the other. So they are
tested against the same table of cases.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Select, select

from app.core.errors import Forbidden
from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import Gateway, Invitation, Organization, User

ACME = uuid7()
GLOBEX = uuid7()

ORG = TenantScope(role="org_admin", organization_id=ACME)
PLATFORM = TenantScope(role="superadmin", organization_id=None)


def sql_of(scope: TenantScope, model: type) -> str:
    """The rendered WHERE clause, with values inlined so it can be asserted on."""
    statement: Select[Any] = select(model).where(scope.clause(model))
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def mentions(rendered: str, organization_id: uuid.UUID) -> bool:
    """SQLAlchemy inlines a UUID without dashes; accept either spelling."""
    return str(organization_id) in rendered or organization_id.hex in rendered


# -- permits ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "organization_id", "expected"),
    [
        (ORG, ACME, True),
        (ORG, GLOBEX, False),
        # A platform-owned row — a superadmin account, a global model — is nobody's.
        (ORG, None, False),
        (PLATFORM, ACME, True),
        (PLATFORM, GLOBEX, True),
        (PLATFORM, None, True),
    ],
)
def test_permits(scope: TenantScope, organization_id: uuid.UUID | None, expected: bool) -> None:
    assert scope.permits(organization_id) is expected


# -- clause ----------------------------------------------------------------


@pytest.mark.parametrize("model", [User, Invitation, Gateway])
def test_an_org_scope_filters_every_tenant_keyed_table(model: type) -> None:
    rendered = sql_of(ORG, model)
    assert "organization_id" in rendered
    assert mentions(rendered, ACME)


@pytest.mark.parametrize("model", [User, Invitation, Gateway])
def test_a_platform_scope_does_not_filter(model: type) -> None:
    assert not mentions(sql_of(PLATFORM, model), ACME)


def test_scoping_a_table_without_a_tenant_key_matches_nothing() -> None:
    """Not "matches everything". Asking to scope something that has no organization is a
    bug in the caller, and the safe reading of a bug is to return no rows."""
    rendered = sql_of(ORG, Organization)
    assert "where false" in rendered.lower()


def test_organizations_are_scoped_on_their_own_primary_key() -> None:
    rendered = str(
        select(Organization)
        .where(ORG.clause_on(Organization.id))
        .compile(compile_kwargs={"literal_binds": True})
    )
    assert "organizations.id" in rendered
    assert mentions(rendered, ACME)


# -- assume ----------------------------------------------------------------


def test_a_superadmin_can_assume_an_organization() -> None:
    assumed = PLATFORM.assume(GLOBEX, actor_user_id=uuid7())

    assert assumed.organization_id == GLOBEX
    assert assumed.assumed is True
    assert assumed.is_platform is False
    assert assumed.permits(GLOBEX) and not assumed.permits(ACME)


def test_assuming_is_recorded(caplog: pytest.LogCaptureFixture) -> None:
    """SPEC §5.2: every cross-org access by a superadmin is audit-logged. Task 15 routes
    the same event into the audit table; for now it has to be in the log."""
    actor = uuid7()
    with caplog.at_level("INFO", logger="app.core.tenancy"):
        PLATFORM.assume(GLOBEX, actor_user_id=actor)

    record = next(r for r in caplog.records if r.message == "superadmin assumed organization")
    assert record.organization_id == str(GLOBEX)  # type: ignore[attr-defined]
    assert record.user_id == str(actor)  # type: ignore[attr-defined]
    assert record.audit_action == "organization.assume"  # type: ignore[attr-defined]


def test_an_org_user_cannot_assume_anything() -> None:
    with pytest.raises(Forbidden):
        ORG.assume(GLOBEX, actor_user_id=uuid7())


def test_an_assumed_scope_cannot_widen_itself_again() -> None:
    """`assume` keeps the role, so a second call is still allowed — but it narrows to the
    new organization rather than returning to platform scope, and it logs again."""
    assumed = PLATFORM.assume(ACME, actor_user_id=uuid7())
    again = assumed.assume(GLOBEX, actor_user_id=uuid7())

    assert again.organization_id == GLOBEX
    assert not again.permits(ACME)


# -- derivation ------------------------------------------------------------


def test_a_scope_comes_from_the_user_row() -> None:
    user = User(id=uuid7(), organization_id=ACME, role="org_viewer", email="v@x", name="V")
    scope = TenantScope.of_user(user)

    assert scope.organization_id == ACME
    assert scope.role == "org_viewer"
    assert not scope.is_platform


def test_require_organization_refuses_platform_scope() -> None:
    """Writes need somewhere to go. A superadmin who has not assumed an organization has
    no answer, and inventing one is how a row ends up in the wrong tenant."""
    with pytest.raises(Forbidden):
        PLATFORM.require_organization()

    assert ORG.require_organization() == ACME
