"""Which organization the current request is allowed to touch.

The single rule this module exists to enforce: **the scope comes from the session, never
from the request**. Every multi-tenant leak has the same shape — an endpoint that accepts
``organization_id`` and trusts it — so the value is derived once, from the authenticated
identity, and the only way to widen it is :meth:`TenantScope.assume`, which a superadmin
alone can call and which writes a record when it happens.

Scoping is expressed twice, from one definition, because there are two things to check
it against:

* :meth:`TenantScope.clause` builds the SQL ``WHERE`` that :mod:`app.db.scoping` injects
  into every read;
* :meth:`TenantScope.permits` answers the same question about a row already in hand.

They are two lines apart so they cannot disagree, and a contract test runs the same
assertions through both.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, false, true

from app.core.errors import Forbidden

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.auth import Identity

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TenantScope:
    """The tenant boundary for one request.

    ``organization_id is None`` means *unrestricted*, which only a superadmin who has not
    assumed an organization can hold. Every other caller carries exactly one id, because
    ``users.role_matches_organization`` guarantees a non-superadmin has one.
    """

    role: str
    organization_id: uuid.UUID | None
    #: True when a superadmin deliberately narrowed to one organization for support.
    assumed: bool = False

    @classmethod
    def of(cls, identity: Identity) -> TenantScope:
        return cls.of_user(identity.user)

    @classmethod
    def of_user(cls, user: Any) -> TenantScope:
        """The scope a user carries. ``Any`` avoids importing the model into what is
        otherwise a pure module; the two attributes it reads are on every ``User``."""
        return cls(role=user.role, organization_id=user.organization_id)

    @classmethod
    def of_organization(cls, organization_id: uuid.UUID) -> TenantScope:
        """The scope a background job runs under.

        A worker has no session and no user — it has an organization id that was written
        into the job payload by a request that *was* scoped. Naming the role ``service``
        rather than borrowing ``org_admin`` keeps two things true: the log says which kind
        of actor touched a row, and :meth:`assume` still refuses, because a job must never
        be able to widen itself into another tenant.
        """
        return cls(role="service", organization_id=organization_id)

    @property
    def is_platform(self) -> bool:
        """Unrestricted: sees across organizations."""
        return self.organization_id is None

    def assume(self, organization_id: uuid.UUID, *, actor_user_id: uuid.UUID) -> TenantScope:
        """Narrow a platform scope to one organization, for support.

        SPEC §5.2 requires every such access to be recorded. It goes to the structured
        log now; task 15 routes the same event into ``audit_events``.
        """
        if self.role != "superadmin":
            # Unreachable through the API — the header is ignored for everyone else —
            # but a direct caller should not be able to widen its own scope quietly.
            raise Forbidden("Only a platform administrator can view another organization.")

        logger.info(
            "superadmin assumed organization",
            extra={
                "user_id": str(actor_user_id),
                "organization_id": str(organization_id),
                "audit_action": "organization.assume",
            },
        )
        return replace(self, organization_id=organization_id, assumed=True)

    def permits(self, organization_id: uuid.UUID | None) -> bool:
        """Whether a row belonging to ``organization_id`` is inside this scope."""
        if self.is_platform:
            return True
        if organization_id is None:
            # A platform-owned row (a superadmin user, a global model) is never an
            # organization's to see.
            return False
        return organization_id == self.organization_id

    def clause_on(self, column: Any) -> ColumnElement[bool]:
        """The scope expressed against an arbitrary column.

        ``organizations`` is the one table whose tenant key is its own primary key, so it
        cannot use :meth:`clause`; this is what it uses instead.
        """
        if self.is_platform:
            return true()
        result: ColumnElement[bool] = column == self.organization_id
        return result

    def clause(self, model: type[Any]) -> ColumnElement[bool]:
        """The ``WHERE`` fragment that makes a query obey this scope.

        ``type[Any]``, not a protocol: a mapped class exposes ``organization_id`` as a
        descriptor at class level and a UUID at instance level, which no single static
        type describes. The missing-column case is therefore handled at runtime, and
        handled by refusing.
        """
        if self.is_platform:
            return true()
        column = getattr(model, "organization_id", None)
        if column is None:
            # Refusing is the only safe answer: a caller asked to scope something that
            # has no tenant key, and silently returning everything is the bug this
            # module exists to prevent.
            return false()
        return self.clause_on(column)

    def require_organization(self) -> uuid.UUID:
        """The organization to write into. Platform scope has no answer."""
        if self.organization_id is None:
            raise Forbidden("This action must be performed inside an organization.")
        return self.organization_id


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is asking, and what they may see. Both come from the session.

    Lives here rather than beside any one service because every domain service takes one:
    the directory in task 04, the model catalog in 05, gateways in 06.
    """

    user_id: uuid.UUID
    scope: TenantScope
