"""The control-plane router.

Authenticated by default. ``authenticated_router`` carries ``require_identity`` as a
router-level dependency, so every route added to it — by tasks 04 through 17 — is
protected without the author remembering to say so. FastAPI cannot remove a router-level
dependency from an individual route, and that is exactly the property wanted here: an
endpoint can only be public by being registered on ``public_router``, which is a visible,
reviewable act rather than a missing decorator.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.control import (
    audit,
    auth,
    connectors,
    directory,
    distillation,
    end_users,
    gateways,
    limits,
    models,
    monitoring,
    platform,
    summarization,
)
from app.api.control.deps import require_identity

API_PREFIX = "/api/v1"

#: Everything reachable without an access token. Keep it short; each addition here is a
#: piece of the control plane that anyone on the network can reach.
public_router = APIRouter(prefix=API_PREFIX)

#: Where later tasks register their routes.
authenticated_router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_identity)])

public_router.include_router(auth.public_router)
public_router.include_router(directory.public_router)
authenticated_router.include_router(auth.router)
authenticated_router.include_router(audit.router)
authenticated_router.include_router(directory.router)
authenticated_router.include_router(models.router)
authenticated_router.include_router(gateways.router)
authenticated_router.include_router(limits.router)
authenticated_router.include_router(connectors.router)
authenticated_router.include_router(monitoring.router)
authenticated_router.include_router(end_users.router)
authenticated_router.include_router(distillation.router)
authenticated_router.include_router(summarization.router)
authenticated_router.include_router(platform.router)
authenticated_router.include_router(platform.ceilings_router)


def build_control_router() -> APIRouter:
    router = APIRouter()
    router.include_router(public_router)
    router.include_router(authenticated_router)
    return router
