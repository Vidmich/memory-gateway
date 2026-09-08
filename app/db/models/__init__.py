"""Every mapped table.

Alembic's autogenerate compares ``Base.metadata`` against the database, and a model that
is never imported is invisible to it — which produces a migration that silently drops
tables. Importing them all here, and importing this package from ``migrations/env.py``,
is what keeps that from happening.
"""

from __future__ import annotations

from app.db.models.api_key import ApiKey
from app.db.models.connector import Connector, Document, JobDeadLetter
from app.db.models.distillation import DistillationRun
from app.db.models.end_user import EndUser, MemoryFact
from app.db.models.gateway import Gateway, GatewayTarget
from app.db.models.invitation import Invitation
from app.db.models.organization import Organization
from app.db.models.request_log import RequestLog, Transcript
from app.db.models.session import UserSession
from app.db.models.upstream_model import UpstreamModel
from app.db.models.user import User

__all__ = [
    "ApiKey",
    "Connector",
    "DistillationRun",
    "Document",
    "EndUser",
    "Gateway",
    "GatewayTarget",
    "Invitation",
    "JobDeadLetter",
    "MemoryFact",
    "Organization",
    "RequestLog",
    "Transcript",
    "UpstreamModel",
    "User",
    "UserSession",
]
