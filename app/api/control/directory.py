"""``/api/v1`` — organizations, members and invitations.

Every route here follows the same three-line shape: name a capability in the decorator,
take :data:`CurrentActor` (which carries the scope, derived from the session), and hand
both to :class:`DirectoryService`. There is no ``organization_id`` argument that an
endpoint decides whether to trust — where one appears in the path, SPEC §12.2 put it
there, and it is checked against the scope rather than used as one.

Acceptance is the exception and is deliberately unauthenticated: a person following an
invitation link has no account yet. Those two routes live on the public router, which is
the visible, reviewable act that makes them reachable.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.control.auth import session_response, set_refresh_cookie
from app.api.control.deps import (
    CurrentActor,
    get_directory_service,
    get_settings_from_app,
    request_context,
    require_capability,
)
from app.core.config import Settings
from app.core.errors import Forbidden
from app.schemas.auth import SessionResponse
from app.schemas.directory import (
    InvitationAcceptRequest,
    InvitationCreateRequest,
    InvitationPreviewResponse,
    InvitationResponse,
    IssuedInvitationResponse,
    MemberResponse,
    MemberUpdateRequest,
    OrganizationCreateRequest,
    OrganizationResponse,
    OrganizationUpdateRequest,
    Page,
)
from app.services.auth import AuthService, RequestContext
from app.services.directory import DirectoryService
from app.services.permissions import Capability

public_router = APIRouter(tags=["directory"])
router = APIRouter(tags=["directory"])

_Service = Annotated[DirectoryService, Depends(get_directory_service)]
_Settings = Annotated[Settings, Depends(get_settings_from_app)]
_Context = Annotated[RequestContext, Depends(request_context)]

#: Every list endpoint takes the same two, so they are declared once.
_Cursor = Annotated[str | None, Query(max_length=64)]
_Limit = Annotated[int | None, Query(ge=1, le=200)]

_reads = Depends(require_capability(Capability.ORG_READ))
_administers = Depends(require_capability(Capability.ORG_ADMINISTER))
_platform = Depends(require_capability(Capability.PLATFORM_ADMINISTER))


# ---------------------------------------------------------------------------
# organizations
# ---------------------------------------------------------------------------


@router.get("/organizations", dependencies=[_reads])
async def list_organizations(
    actor: CurrentActor,
    service: _Service,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[OrganizationResponse]:
    """Every organization for a platform admin; the caller's own, alone, for anyone else.

    Not gated on ``platform:administer``: an org user needs this to render their own
    organization, and the scope already reduces the answer to one row. A second endpoint
    shape for the same information is how the two drift apart.
    """
    page = await service.list_organizations(actor, cursor=cursor, limit=limit)
    return Page(
        items=[OrganizationResponse.of_view(view) for view in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/organizations", status_code=status.HTTP_201_CREATED, dependencies=[_platform])
async def create_organization(
    body: OrganizationCreateRequest,
    actor: CurrentActor,
    service: _Service,
) -> OrganizationResponse:
    organization = await service.create_organization(actor, name=body.name, slug=body.slug)
    return OrganizationResponse.of(organization)


@router.get("/organizations/{organization_id}", dependencies=[_reads])
async def get_organization(
    organization_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> OrganizationResponse:
    return OrganizationResponse.of(await service.get_organization(actor, organization_id))


@router.patch("/organizations/{organization_id}", dependencies=[_administers])
async def update_organization(
    organization_id: uuid.UUID,
    body: OrganizationUpdateRequest,
    actor: CurrentActor,
    service: _Service,
) -> OrganizationResponse:
    """Name, slug and defaults for an org admin; ``status`` for a platform admin only.

    Refused rather than ignored: an organization that could suspend itself could also
    un-suspend itself, which would make suspension advisory.
    """
    if body.status is not None and not actor.scope.is_platform:
        raise Forbidden("Only a platform administrator can change an organization's status.")

    organization = await service.update_organization(
        actor,
        organization_id,
        name=body.name,
        slug=body.slug,
        status=body.status,
        settings=body.settings,
    )
    return OrganizationResponse.of(organization)


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------


@router.get("/organizations/{organization_id}/members", dependencies=[_reads])
async def list_members(
    organization_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[MemberResponse]:
    """Readable by every role. Seeing who your colleagues are is not an admin power;
    changing them is, and that lives on the two routes below."""
    page = await service.list_members(actor, organization_id, cursor=cursor, limit=limit)
    return Page(
        items=[MemberResponse.of(member) for member in page.items],
        next_cursor=page.next_cursor,
    )


@router.patch("/members/{member_id}", dependencies=[_administers])
async def update_member(
    member_id: uuid.UUID,
    body: MemberUpdateRequest,
    actor: CurrentActor,
    service: _Service,
) -> MemberResponse:
    member = await service.update_member(actor, member_id, role=body.role, status=body.status)
    return MemberResponse.of(member)


@router.delete(
    "/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_administers],
)
async def remove_member(
    member_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> None:
    await service.remove_member(actor, member_id)


# ---------------------------------------------------------------------------
# invitations
# ---------------------------------------------------------------------------


@router.post(
    "/organizations/{organization_id}/invitations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[_administers],
)
async def create_invitation(
    organization_id: uuid.UUID,
    body: InvitationCreateRequest,
    actor: CurrentActor,
    service: _Service,
    settings: _Settings,
) -> IssuedInvitationResponse:
    """Creates the invitation and returns the link — the only time it exists.

    There is no email delivery in v1 (task 04, out of scope), so the admin copies the URL
    and sends it however they already talk to the person.
    """
    issued = await service.invite(actor, organization_id, email=str(body.email), role=body.role)
    return IssuedInvitationResponse.of(issued, base_url=settings.ui_base_url)


@router.get("/invitations", dependencies=[_administers])
async def list_invitations(
    actor: CurrentActor,
    service: _Service,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[InvitationResponse]:
    """No ``accept_url`` here, and there cannot be one: only the hash is stored. Use
    resend to mint a fresh link."""
    page = await service.list_invitations(actor, cursor=cursor, limit=limit)
    return Page(
        items=[InvitationResponse.of(invitation) for invitation in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/invitations/{invitation_id}/resend", dependencies=[_administers])
async def resend_invitation(
    invitation_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    settings: _Settings,
) -> IssuedInvitationResponse:
    """Mints a new link and invalidates the previous one."""
    issued = await service.resend_invitation(actor, invitation_id)
    return IssuedInvitationResponse.of(issued, base_url=settings.ui_base_url)


@router.delete(
    "/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_administers],
)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> None:
    await service.revoke_invitation(actor, invitation_id)


# ---------------------------------------------------------------------------
# acceptance — public
# ---------------------------------------------------------------------------


@public_router.get("/invitations/accept/{token}")
async def preview_invitation(token: str, service: _Service) -> InvitationPreviewResponse:
    """Validate a link so the page can render before anyone types a password.

    Answers 404 for unknown, expired and already-accepted alike: the token is a bearer
    credential, and a caller holding the wrong one learns nothing about which it was.
    """
    return InvitationPreviewResponse.of(await service.preview_invitation(token))


@public_router.post("/invitations/accept/{token}")
async def accept_invitation(
    token: str,
    body: InvitationAcceptRequest,
    request: Request,
    response: Response,
    service: _Service,
    context: _Context,
    settings: _Settings,
) -> SessionResponse:
    """Create the account and sign it in.

    Two transactions, not one: the directory creates the user and burns the invitation,
    then auth opens a session. If the second half fails the account still exists and the
    person can sign in normally, which is a better failure than a consumed invitation and
    no account.
    """
    user = await service.accept_invitation(token, name=body.name, password=body.password)

    auth: AuthService = request.app.state.auth_service
    issued = await auth.open_session_for(user.id, context=context)
    set_refresh_cookie(response, issued, settings)

    return session_response(issued)


__all__ = ["public_router", "router"]
