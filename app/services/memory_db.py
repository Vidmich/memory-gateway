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

from app.db.models import (
    ApiKey,
    Connector,
    Document,
    EndUser,
    Gateway,
    GatewayTarget,
    Invitation,
    MemoryFact,
    Organization,
    RequestLog,
    Transcript,
    UpstreamModel,
    User,
    UserSession,
)


@dataclass
class MemoryDatabase:
    users: dict[uuid.UUID, User] = field(default_factory=dict)
    organizations: dict[uuid.UUID, Organization] = field(default_factory=dict)
    sessions: dict[uuid.UUID, UserSession] = field(default_factory=dict)
    invitations: dict[uuid.UUID, Invitation] = field(default_factory=dict)
    upstream_models: dict[uuid.UUID, UpstreamModel] = field(default_factory=dict)
    gateways: dict[uuid.UUID, Gateway] = field(default_factory=dict)
    gateway_targets: dict[uuid.UUID, GatewayTarget] = field(default_factory=dict)
    api_keys: dict[uuid.UUID, ApiKey] = field(default_factory=dict)
    connectors: dict[uuid.UUID, Connector] = field(default_factory=dict)
    documents: dict[uuid.UUID, Document] = field(default_factory=dict)
    end_users: dict[uuid.UUID, EndUser] = field(default_factory=dict)
    memory_facts: dict[uuid.UUID, MemoryFact] = field(default_factory=dict)
    #: Keyed by request-log id, which is also the transcript's key — the two tables
    #: are one row split in half, and keeping them in step here is what makes the
    #: memory store a fair test of the read side.
    request_logs: dict[uuid.UUID, RequestLog] = field(default_factory=dict)
    transcripts: dict[uuid.UUID, Transcript] = field(default_factory=dict)

    def add_user(self, user: User) -> User:
        self.users[user.id] = _stamped(user)
        return user

    def add_organization(self, organization: Organization) -> Organization:
        self.organizations[organization.id] = _stamped(organization)
        return organization

    def add_invitation(self, invitation: Invitation) -> Invitation:
        self.invitations[invitation.id] = _stamped(invitation)
        return invitation

    def add_model(self, model: UpstreamModel) -> UpstreamModel:
        self.upstream_models[model.id] = _stamped(model)
        return model

    def add_gateway(self, gateway: Gateway) -> Gateway:
        self.gateways[gateway.id] = _stamped(gateway)
        return gateway

    def add_target(self, target: GatewayTarget) -> GatewayTarget:
        self.gateway_targets[target.id] = _stamped(target)
        return target

    def add_key(self, key: ApiKey) -> ApiKey:
        self.api_keys[key.id] = _stamped(key)
        return key

    def add_connector(self, connector: Connector) -> Connector:
        self.connectors[connector.id] = _stamped(connector)
        return connector

    def add_document(self, document: Document) -> Document:
        self.documents[document.id] = _stamped(document)
        return document

    def add_end_user(self, end_user: EndUser) -> EndUser:
        self.end_users[end_user.id] = _stamped(end_user, "first_seen_at", "last_seen_at")
        return end_user

    def add_fact(self, fact: MemoryFact) -> MemoryFact:
        # No `updated_at` on this one — see the note on its two timestamps in
        # `app.db.models.end_user` — so the pair that is stamped is named explicitly.
        self.memory_facts[fact.id] = _stamped(fact, "created_at", "last_seen_at")
        return fact


def _stamped[T: Any](row: T, *columns: str) -> T:
    """Fill in what ``server_default now()`` would have.

    Without this the timestamps are ``None`` here and a datetime in PostgreSQL, and a
    response model that requires ``created_at`` fails in exactly one of the two — which
    is the kind of divergence the store contract exists to prevent.

    ``columns`` names the fields to stamp for a table whose timestamps are not the usual
    pair; the default is that pair, which is what every table before task 12 has.
    """
    now = datetime.now(UTC)
    for name in columns or ("created_at", "updated_at"):
        if getattr(row, name, None) is None:
            setattr(row, name, now)
    return row
