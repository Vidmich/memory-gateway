"""In-memory rows, shared by the control-plane stores that fake persistence in tests.

There is exactly one of these per test, and both :class:`MemoryAuthStore` and
:class:`MemoryDirectoryStore` read and write it. That matters for the flows that cross
the two — accepting an invitation creates a user through the directory and then opens a
session through auth — which would otherwise pass against two disconnected dictionaries
and fail against one database.

Nothing here pretends to be a database. There is no isolation, no rollback, and writes
are visible the moment they happen; the contract tests in ``tests/`` run the same
assertions against PostgreSQL, which is what keeps the difference from mattering.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.db.models import Invitation, Organization, User, UserSession


@dataclass
class MemoryDatabase:
    users: dict[uuid.UUID, User] = field(default_factory=dict)
    organizations: dict[uuid.UUID, Organization] = field(default_factory=dict)
    sessions: dict[uuid.UUID, UserSession] = field(default_factory=dict)
    invitations: dict[uuid.UUID, Invitation] = field(default_factory=dict)

    def add_user(self, user: User) -> User:
        self.users[user.id] = _stamped(user)
        return user

    def add_organization(self, organization: Organization) -> Organization:
        self.organizations[organization.id] = _stamped(organization)
        return organization

    def add_invitation(self, invitation: Invitation) -> Invitation:
        self.invitations[invitation.id] = _stamped(invitation)
        return invitation


def _stamped[T: Any](row: T) -> T:
    """Fill in what ``server_default now()`` would have.

    Without this the timestamps are ``None`` here and a datetime in PostgreSQL, and a
    response model that requires ``created_at`` fails in exactly one of the two — which
    is the kind of divergence the store contract exists to prevent.
    """
    now = datetime.now(UTC)
    if getattr(row, "created_at", None) is None:
        row.created_at = now
    if getattr(row, "updated_at", None) is None:
        row.updated_at = now
    return row
