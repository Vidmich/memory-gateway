"""Login, refresh-token rotation, logout, and password change.

The interesting part is rotation. Every refresh issues a *new* token and marks the old
one replaced, and a replaced token presented a second time revokes the whole family. That
is the only way a bearer token in a cookie can be made to notice theft at all: the
attacker and the victim both hold something that works, and whichever one uses the stale
copy trips the alarm. Neither gets to keep the session.

The cost is that a client which retries a refresh after a dropped response logs the user
out. That is the accepted trade (OAuth 2.1 §4.14.2 makes the same one), and it is why the
frontend serialises refreshes through a single-flight queue instead of retrying.

Persistence is behind :mod:`app.services.auth_store` — see that module for why.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core import tokens
from app.core.config import Settings, get_settings
from app.core.errors import AppError, Unauthorized
from app.core.ids import uuid7
from app.core.passwords import Hasher, PasswordPolicy
from app.db.models import Organization, User, UserSession
from app.services.auth_provider import AuthProvider, Credentials
from app.services.auth_store import AuthStore, AuthTransaction
from app.services.login_throttle import Attempt, LoginThrottle, TooManyAttempts

logger = logging.getLogger(__name__)

#: The same sentence for every login failure. Distinguishing "no such account" from
#: "wrong password" hands an attacker a free account-enumeration endpoint.
GENERIC_LOGIN_FAILURE = "Incorrect email or password."
SESSION_OVER = "Your session has expired. Please sign in again."
SUSPENDED = "This organization has been suspended. Contact your administrator."


class InvalidCredentials(Unauthorized):
    code = "invalid_credentials"


class AuthenticationRequired(Unauthorized):
    code = "not_authenticated"


class SessionExpired(Unauthorized):
    code = "session_expired"


class OrganizationSuspended(Unauthorized):
    """The account is fine; the organization it belongs to is not.

    Distinguished from a bad password because it is not a credential problem and the
    caller has already proved who they are — telling them the truth here saves a support
    ticket and discloses nothing they did not already know.
    """

    code = "organization_suspended"


class LoginThrottled(AppError):
    status_code = 429
    code = "too_many_attempts"


class WeakPassword(AppError):
    status_code = 422
    code = "weak_password"


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Where a login or refresh came from. Recorded on the session row so an operator
    reviewing a suspicious family can see what changed."""

    ip: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Everything the route needs to answer, including what to put in the cookie."""

    user: User
    #: Loaded alongside the user so login answers in the same shape as ``/auth/me``;
    #: ``None`` for a superadmin, who belongs to the platform rather than a tenant.
    organization: Organization | None
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    persistent: bool
    family_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Identity:
    """The authenticated caller, as every control-plane endpoint sees it."""

    user: User
    organization: Organization | None
    family_id: uuid.UUID


class AuthService:
    def __init__(
        self,
        store: AuthStore,
        *,
        provider: AuthProvider,
        hasher: Hasher,
        throttle: LoginThrottle,
        settings: Settings | None = None,
        policy: PasswordPolicy | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._hasher = hasher
        self._throttle = throttle
        self._settings = settings or get_settings()
        self._policy = policy or PasswordPolicy()

    # -- login ------------------------------------------------------------

    async def login(
        self,
        credentials: Credentials,
        *,
        context: RequestContext,
        remember: bool = False,
    ) -> IssuedSession:
        attempt = Attempt(email=_email_of(credentials), ip=context.ip)
        try:
            await self._throttle.check(attempt)
        except TooManyAttempts as exc:
            raise LoginThrottled(
                "Too many login attempts. Try again later.",
                details={"retry_after_seconds": exc.retry_after_seconds},
                headers={"retry-after": str(exc.retry_after_seconds)},
            ) from exc

        async with self._store.begin() as transaction:
            user = await self._provider.authenticate(transaction, credentials)
            if user is None:
                # Commit anyway: `authenticate` may have upgraded a password hash before
                # deciding the account is suspended, and that work should not be lost.
                await transaction.commit()
                await self._throttle.record_failure(attempt)
                raise InvalidCredentials(GENERIC_LOGIN_FAILURE)

            await self._require_live_organization(transaction, user)

            user.last_login_at = datetime.now(UTC)
            issued = await self._open_session(
                transaction, user, context=context, persistent=remember
            )
            await transaction.commit()

        await self._throttle.record_success(attempt)
        logger.info(
            "login succeeded",
            extra={"user_id": str(issued.user.id), "provider": self._provider.name},
        )
        return issued

    async def open_session_for(
        self, user_id: uuid.UUID, *, context: RequestContext, remember: bool = False
    ) -> IssuedSession:
        """Sign a user in without credentials.

        The one caller is invitation acceptance, which has just created this account from
        a token that only the invited address could have received — proof of identity that
        a password re-entry would not add to. It takes a user *id* rather than a ``User``
        so it cannot be handed an object assembled by a caller that never checked
        anything.
        """
        async with self._store.begin() as transaction:
            user = await transaction.user_by_id(user_id)
            if user is None or not user.is_active:
                raise AuthenticationRequired("Not authenticated.")
            await self._require_live_organization(transaction, user)

            user.last_login_at = datetime.now(UTC)
            issued = await self._open_session(
                transaction, user, context=context, persistent=remember
            )
            await transaction.commit()

        logger.info("session opened without credentials", extra={"user_id": str(user_id)})
        return issued

    # -- refresh ----------------------------------------------------------

    async def refresh(self, refresh_token: str, *, context: RequestContext) -> IssuedSession:
        token_hash = tokens.hash_refresh_token(refresh_token)

        async with self._store.begin() as transaction:
            record = await transaction.session_by_token_hash(token_hash)
            if record is None:
                raise SessionExpired(SESSION_OVER)

            now = datetime.now(UTC)

            if record.replaced_at is not None or record.revoked_at is not None:
                # Someone is holding a copy of a token that was already spent. There is
                # no way to tell the thief from the victim, so neither keeps the session.
                await transaction.revoke_family(record.family_id, reason="reuse_detected", at=now)
                await transaction.commit()
                logger.warning(
                    "refresh token reuse detected; session family revoked",
                    extra={
                        "user_id": str(record.user_id),
                        "family_id": str(record.family_id),
                        "ip": context.ip,
                    },
                )
                raise SessionExpired(SESSION_OVER)

            if record.expires_at <= now:
                await transaction.revoke_family(record.family_id, reason="expired", at=now)
                await transaction.commit()
                raise SessionExpired(SESSION_OVER)

            user = await transaction.user_by_id(record.user_id)
            if user is None or not user.is_active:
                await transaction.revoke_family(record.family_id, reason="logout", at=now)
                await transaction.commit()
                raise SessionExpired(SESSION_OVER)

            # A suspended organization stops refreshing too, or a session opened before
            # the suspension would survive for as long as the client kept rotating.
            await self._require_live_organization(transaction, user)

            record.replaced_at = now
            issued = await self._open_session(
                transaction,
                user,
                context=context,
                persistent=record.persistent,
                family_id=record.family_id,
            )
            await transaction.commit()

        return issued

    # -- logout -----------------------------------------------------------

    async def logout(self, refresh_token: str | None) -> None:
        """Idempotent by design: a logout that 401s leaves the user staring at a page
        they cannot leave. An unknown token is simply nothing to revoke."""
        if not refresh_token:
            return

        token_hash = tokens.hash_refresh_token(refresh_token)
        async with self._store.begin() as transaction:
            record = await transaction.session_by_token_hash(token_hash)
            if record is None:
                return
            await transaction.revoke_family(record.family_id, reason="logout", at=datetime.now(UTC))
            await transaction.commit()
            logger.info("logout", extra={"user_id": str(record.user_id)})

    # -- current user -----------------------------------------------------

    async def identify(self, access_token: str) -> Identity:
        """Resolve an access token to the caller.

        This costs one database round trip per request, which a self-contained JWT was
        supposed to avoid. It is spent deliberately: without it, logout and theft
        detection would not take effect for up to a full access-token TTL, and a
        suspended account would keep working for fifteen minutes. Control-plane traffic
        is a handful of requests per screen — the data plane, which is the one with a
        latency budget (SPEC §4.2), authenticates against ``api_keys`` and never comes
        through here.
        """
        try:
            claims = tokens.decode_access_token(access_token, self._settings)
        except tokens.InvalidToken as exc:
            raise AuthenticationRequired("Not authenticated.") from exc

        async with self._store.begin() as transaction:
            user = await self._provider.user_from_claims(transaction, claims)
            if user is None:
                raise AuthenticationRequired("Not authenticated.")

            if not await transaction.family_is_live(claims.session_id):
                raise SessionExpired(SESSION_OVER)

            organization = await _organization_of(transaction, user)
            if organization is not None and not organization.is_active:
                # Checked on every request, not only at login: suspending an organization
                # has to take effect for the sessions that are already open.
                raise OrganizationSuspended(SUSPENDED)
            return Identity(user=user, organization=organization, family_id=claims.session_id)

    # -- password ---------------------------------------------------------

    async def change_password(
        self,
        user_id: uuid.UUID,
        *,
        current_password: str,
        new_password: str,
        keep_family_id: uuid.UUID,
    ) -> None:
        reason = self._policy.check(new_password)
        if reason is not None:
            raise WeakPassword(reason)

        async with self._store.begin() as transaction:
            user = await transaction.user_by_id(user_id)
            if user is None or user.password_hash is None:
                raise InvalidCredentials("Your current password is incorrect.")
            if not self._hasher.verify(user.password_hash, current_password):
                raise InvalidCredentials("Your current password is incorrect.")

            user.password_hash = self._hasher.hash(new_password)
            await transaction.revoke_other_families(
                user_id=user_id,
                keep_family_id=keep_family_id,
                reason="password_change",
                at=datetime.now(UTC),
            )
            await transaction.commit()

        logger.info("password changed", extra={"user_id": str(user_id)})

    # -- internals --------------------------------------------------------

    async def _require_live_organization(self, transaction: AuthTransaction, user: User) -> None:
        organization = await _organization_of(transaction, user)
        if organization is not None and not organization.is_active:
            raise OrganizationSuspended(SUSPENDED)

    async def _open_session(
        self,
        transaction: AuthTransaction,
        user: User,
        *,
        context: RequestContext,
        persistent: bool,
        family_id: uuid.UUID | None = None,
    ) -> IssuedSession:
        family = family_id or uuid7()
        ttl_seconds = (
            self._settings.refresh_token_ttl_seconds
            if persistent
            else self._settings.refresh_token_short_ttl_seconds
        )
        refresh = tokens.mint_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

        await transaction.add_session(
            UserSession(
                id=uuid7(),
                user_id=user.id,
                family_id=family,
                refresh_token_hash=refresh.token_hash,
                expires_at=expires_at,
                persistent=persistent,
                replaced_at=None,
                revoked_at=None,
                revoked_reason=None,
                ip=context.ip,
                user_agent=_truncate(context.user_agent),
            )
        )

        access_token, access_expires_at = tokens.issue_access_token(
            user_id=user.id,
            organization_id=user.organization_id,
            role=user.role,
            # The *family* is the session, not the individual token row: rotating a
            # refresh token must not invalidate an access token the client is still
            # legitimately using.
            session_id=family,
            settings=self._settings,
        )
        return IssuedSession(
            user=user,
            organization=await _organization_of(transaction, user),
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=refresh.token,
            refresh_expires_at=expires_at,
            persistent=persistent,
            family_id=family,
        )


async def _organization_of(transaction: AuthTransaction, user: User) -> Organization | None:
    if user.organization_id is None:
        return None
    return await transaction.organization(user.organization_id)


def _email_of(credentials: Credentials) -> str | None:
    # One credential shape today. When OIDC adds a second, the throttle key comes from
    # whatever identifies the subject there — hence the indirection rather than reading
    # ``.email`` at the call site.
    return credentials.email


def _truncate(value: str | None, limit: int = 500) -> str | None:
    """User agents are attacker-controlled and unbounded; the column is not."""
    if value is None:
        return None
    return value[:limit]
