"""The model catalog — upstream providers, their credentials, and the rules around both.

Four things here are load-bearing.

**Credentials are write-only.** There is no method on this class that returns a stored
credential, for any role, and none may be added: SPEC §5.4 makes rotation a replacement
rather than a reveal. What a response carries is
``{"configured": true, "hint": "sk-...4f2a"}``, and the hint is computed from the
plaintext at write time and stored, so rendering a list never needs the master key. The
one place a credential is decrypted is the request path — here for a connectivity probe,
and in :class:`~app.services.gateway_resolver.DatabaseGatewayResolver` for a real completion.

**There are two scopes, and only one of them is writable.** Every read that lists or
shows a model uses the wide view (own models plus the global catalog); every write starts
from the narrow one. An org user editing a global model therefore gets a 404 — the same
answer as for a model that does not exist — because :meth:`CatalogTransaction.owned_model`
simply does not find it. That is one rule expressed once, rather than a role check on
each of create, update and delete.

**A model a gateway points at cannot be deleted.** The foreign key is ``ON DELETE
RESTRICT``, so the database would refuse it anyway; refusing here is what makes the answer
name the gateways instead of surfacing a constraint violation. Disabling is always
allowed, and takes effect on the next request because the resolver reads the flag.

**A change to a model reaches the gateways pointing at it immediately.** Every write here
bumps the config-cache version of every gateway with a target on this model —
:meth:`CatalogTransaction.slugs_referencing`, deliberately unscoped, because a *global*
model is referenced from organizations the writer cannot see. Without that, rotating a
credential would leave other tenants calling the provider with the old one for up to a
minute.

**A dialect with no adapter is refused at write time.** The column accepts ``anthropic``
today and the UI offers it, but there is nothing registered to serve it until task 16 —
so it is rejected here with a message that says so, and the rejection disappears when the
adapter is registered, with no change to this file.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.adapters import known_dialects
from app.adapters.base import UpstreamTarget
from app.core.config import Settings, get_settings
from app.core.crypto import DecryptionError, SecretBox, secret_hint
from app.core.errors import Conflict, Forbidden, NotFound, Validation
from app.core.ids import uuid7
from app.core.ssrf import check_url
from app.core.tenancy import Actor
from app.db.models import UpstreamModel
from app.db.models.upstream_model import DEFAULT_TIMEOUT_SECONDS
from app.services.audit_snapshots import subject
from app.services.catalog_store import CatalogStore, CatalogTransaction
from app.services.gateway_resolver import ConfigCache
from app.services.model_probe import Probe, ProbeResult
from app.services.pagination import Page, clamp_limit, decode_cursor, page_of
from app.services.params import validate_params
from app.services.permissions import Capability, allows
from app.services.rate_limit import FixedWindowLimiter

logger = logging.getLogger(__name__)

#: Same answer for "no such model", "belongs to another organization" and "global, and
#: you are not the platform". Distinguishing them is what turns an id into an oracle.
NO_SUCH_MODEL = "No such model."


class _Unset:
    """Sentinel for "this field was not sent", which ``PATCH`` must distinguish from
    "this field was sent as null" — the difference between keeping a credential and
    clearing it."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


UNSET = _Unset()

type Maybe[T] = T | _Unset


@dataclass(frozen=True, slots=True)
class ModelDraft:
    """Everything needed to call a provider, before it has been stored.

    Used for ``POST /models`` and, unchanged, for the probe on ``POST /models/test`` —
    which is the point: validating a draft exercises the same object the saved model
    becomes.
    """

    name: str
    base_url: str
    upstream_model_id: str
    dialect: str = "openai"
    auth_type: str = "bearer"
    credential: str | None = None
    description: str | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    system_context: str | None = None
    default_params: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    #: ``None`` means unknown, which switches off the assembler's overflow guard.
    context_window: int | None = None
    enabled: bool = True
    #: ``org`` or ``global``. Only a platform administrator may write ``global``.
    scope: str = "org"


@dataclass(frozen=True, slots=True)
class ModelPatch:
    """A partial update. Every field defaults to :data:`UNSET`, meaning "not sent".

    ``credential`` is the one where the distinction has teeth: omitted keeps the stored
    value, an explicit ``None`` clears it.
    """

    name: Maybe[str] = UNSET
    description: Maybe[str | None] = UNSET
    base_url: Maybe[str] = UNSET
    dialect: Maybe[str] = UNSET
    upstream_model_id: Maybe[str] = UNSET
    auth_type: Maybe[str] = UNSET
    credential: Maybe[str | None] = UNSET
    extra_headers: Maybe[Mapping[str, str]] = UNSET
    system_context: Maybe[str | None] = UNSET
    default_params: Maybe[Mapping[str, Any]] = UNSET
    timeout_seconds: Maybe[int] = UNSET
    context_window: Maybe[int | None] = UNSET
    enabled: Maybe[bool] = UNSET


@dataclass(frozen=True, slots=True)
class ModelView:
    """A model plus what *this* caller may do with it.

    ``editable`` is answered by the server rather than re-derived in the browser, because
    it is the same question :meth:`CatalogTransaction.owned_model` answers and the two
    must not drift: a UI that offers an edit form the API will 404 is worse than one that
    offers nothing.
    """

    model: UpstreamModel
    editable: bool

    @property
    def credential_hint(self) -> str | None:
        """Withheld on a global model from anyone who cannot edit it (SPEC §5.3): an org
        user may see that the operator's model is configured, never any part of the key."""
        if self.model.organization_id is None and not self.editable:
            return None
        return self.model.credential_hint

    @property
    def extra_headers(self) -> dict[str, str]:
        """Same rule, and for a sharper reason: ``extra_headers`` is applied last and can
        therefore *contain* an auth header. Showing an operator's to a tenant would hand
        over exactly what the credential field protects."""
        if self.model.organization_id is None and not self.editable:
            return {}
        return dict(self.model.extra_headers or {})


class CatalogService:
    def __init__(
        self,
        store: CatalogStore,
        *,
        secret_box: SecretBox,
        probe: Probe,
        cache: ConfigCache | None = None,
        test_limiter: FixedWindowLimiter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._store = store
        self._secret_box = secret_box
        self._probe = probe
        self._cache = cache
        self._limiter = test_limiter
        self._settings = settings or get_settings()

    # -- reads ------------------------------------------------------------

    async def list_models(
        self,
        actor: Actor,
        *,
        scope_filter: str | None = None,
        enabled: bool | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page[ModelView]:
        """Own models plus the global catalog, newest first.

        An org user is not refused the global rows — they need them to point a gateway at
        one — but every row comes back with ``editable`` saying whether they may touch it.
        """
        if scope_filter is not None and scope_filter not in ("global", "org"):
            raise Validation("Scope must be 'global' or 'org'.", param="scope")

        size = clamp_limit(limit)
        after = decode_cursor(cursor)

        async with self._store.begin(actor.scope) as transaction:
            rows = await transaction.models(
                after=after, limit=size, scope_filter=scope_filter, enabled=enabled
            )
            page = page_of(rows, limit=size, cursor_of=lambda row: row.id)

        return Page(
            items=tuple(self._view(actor, model) for model in page.items),
            next_cursor=page.next_cursor,
        )

    async def get_model(self, actor: Actor, model_id: uuid.UUID) -> ModelView:
        async with self._store.begin(actor.scope) as transaction:
            model = await transaction.model(model_id)
            if model is None:
                raise NotFound(NO_SUCH_MODEL)
            return self._view(actor, model)

    # -- writes -----------------------------------------------------------

    async def create_model(self, actor: Actor, draft: ModelDraft) -> ModelView:
        is_global = draft.scope == "global"
        if is_global and not allows(actor.scope.role, Capability.PLATFORM_ADMINISTER):
            # 403 rather than 404: nothing is being hidden, the caller simply may not
            # write into the platform's catalog.
            raise Forbidden("Only a platform administrator can add a model to the global catalog.")

        self._check_base_url(draft.base_url)
        self._check_dialect(draft.dialect)
        self._check_auth(draft.auth_type, draft.credential)
        headers = _check_headers(draft.extra_headers)
        params = validate_params(draft.default_params)

        async with self._store.begin(actor.scope) as transaction:
            await self._check_name(transaction, draft.name, is_global=is_global)

            model = UpstreamModel(
                id=uuid7(),
                scope="global" if is_global else "org",
                name=draft.name.strip(),
                description=draft.description,
                base_url=draft.base_url,
                dialect=draft.dialect,
                upstream_model_id=draft.upstream_model_id.strip(),
                auth_type=draft.auth_type,
                extra_headers=headers,
                system_context=draft.system_context,
                default_params=params,
                timeout_seconds=draft.timeout_seconds,
                context_window=draft.context_window,
                enabled=draft.enabled,
            )
            self._store_credential(model, draft.credential)

            if is_global:
                await transaction.add_global_model(model)
            else:
                # Stamps the scope's organization; a superadmin at platform scope has
                # none, so creating an org model means opening that organization first.
                await transaction.add_model(model)
            # A global model belongs to nobody, so its event has no organization and
            # appears in no customer's log — which is right: they cannot see the row
            # either, only that it exists in the catalog.
            transaction.audit(
                actor,
                "model.create",
                after=subject(model),
                organization_id=model.organization_id,
            )
            await transaction.commit()

        self._log("model created", actor, model, action="model.create")
        return ModelView(model=model, editable=True)

    async def update_model(self, actor: Actor, model_id: uuid.UUID, patch: ModelPatch) -> ModelView:
        if not isinstance(patch.dialect, _Unset):
            self._check_dialect(patch.dialect)
        if not isinstance(patch.base_url, _Unset) and patch.base_url is not None:
            self._check_base_url(patch.base_url)

        async with self._store.begin(actor.scope) as transaction:
            model = await self._writable(transaction, model_id)
            before = subject(model)

            auth_type = _picked(patch.auth_type, model.auth_type)
            credential_after = (
                model.credential_ciphertext is not None
                if isinstance(patch.credential, _Unset)
                else patch.credential is not None
            )
            self._check_auth(auth_type, "kept" if credential_after else None)

            if not isinstance(patch.name, _Unset) and patch.name.strip() != model.name:
                await self._check_name(
                    transaction,
                    patch.name,
                    is_global=model.organization_id is None,
                    excluding=model.id,
                )
                model.name = patch.name.strip()

            _apply(model, "description", patch.description)
            _apply(model, "base_url", patch.base_url)
            _apply(model, "dialect", patch.dialect)
            _apply(model, "auth_type", patch.auth_type)
            _apply(model, "system_context", patch.system_context)
            _apply(model, "timeout_seconds", patch.timeout_seconds)
            _apply(model, "context_window", patch.context_window)
            _apply(model, "enabled", patch.enabled)
            if not isinstance(patch.upstream_model_id, _Unset):
                model.upstream_model_id = patch.upstream_model_id.strip()
            if not isinstance(patch.extra_headers, _Unset):
                model.extra_headers = _check_headers(patch.extra_headers)
            if not isinstance(patch.default_params, _Unset):
                model.default_params = validate_params(patch.default_params)
            if not isinstance(patch.credential, _Unset):
                # Reached only when the field was actually sent: omitted keeps whatever
                # is stored, `null` clears it, a string replaces it. Rotation is
                # replacement, which is why there is no reveal to compare against.
                self._store_credential(model, patch.credential)

            # The credential is compared on its ciphertext and rendered as `"***"` on
            # both sides, so a rotation is visible as an event and invisible as a value.
            transaction.audit(
                actor,
                "model.update",
                before=before,
                after=subject(model),
                organization_id=model.organization_id,
            )
            await transaction.commit()
            # After the commit, so nothing can repopulate the cache from a row this
            # transaction has not written yet.
            await self._invalidate(transaction, model.id)

        self._log("model updated", actor, model, action="model.update")
        return ModelView(model=model, editable=True)

    async def delete_model(self, actor: Actor, model_id: uuid.UUID) -> None:
        async with self._store.begin(actor.scope) as transaction:
            model = await self._writable(transaction, model_id)

            referencing = await transaction.gateways_referencing(model.id)
            if referencing:
                raise Conflict(_in_use_message(referencing), details=_gateway_details(referencing))

            organization_id = model.organization_id
            name = model.name
            transaction.audit(
                actor,
                "model.delete",
                before=subject(model),
                organization_id=organization_id,
            )
            await transaction.delete_model(model)
            await transaction.commit()

        logger.info(
            "model deleted",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(organization_id) if organization_id else None,
                "model_id": str(model_id),
                "model_name": name,
                "audit_action": "model.delete",
            },
        )

    # -- connectivity -----------------------------------------------------

    async def test_model(self, actor: Actor, model_id: uuid.UUID) -> ProbeResult:
        """Probe a stored model, using its stored credential and its stored base URL.

        Deliberately no overrides. Letting a caller aim a *stored* credential at a URL of
        their choosing would be a credential-reveal endpoint wearing a different hat, and
        SPEC §5.4 says there is not one. Editing the model first is the honest path, and
        that is a recorded configuration change to a model they already own.

        Scoped to models the caller may edit rather than merely see: a probe spends the
        owner's tokens, and for a global model the owner is the platform.
        """
        await self._rate_limit(actor)

        async with self._store.begin(actor.scope) as transaction:
            model = await self._writable(transaction, model_id)
            target = self._target_of(model)

        result = await self._probe.run(target)
        self._log_probe(actor, name=model.name, model_id=model.id, result=result)
        return result

    async def test_draft(self, actor: Actor, draft: ModelDraft) -> ProbeResult:
        """Probe an unsaved configuration, so it can be validated before it is stored.

        Everything comes from the request body, including the credential. That is the
        only way to check a key before writing it, and it discloses nothing: the caller
        supplied the value they are sending.
        """
        await self._rate_limit(actor)

        self._check_base_url(draft.base_url)
        self._check_dialect(draft.dialect)
        _check_headers(draft.extra_headers)

        result = await self._probe.run(
            UpstreamTarget(
                id=uuid7(),
                name=draft.name or "draft",
                base_url=draft.base_url,
                dialect=draft.dialect,
                upstream_model_id=draft.upstream_model_id,
                auth_type=draft.auth_type,
                credential=draft.credential,
                extra_headers=dict(draft.extra_headers or {}),
                system_context=None,
                default_params={},
                timeout_seconds=draft.timeout_seconds,
            )
        )
        self._log_probe(actor, name=draft.name or "draft", model_id=None, result=result)
        return result

    # -- internals --------------------------------------------------------

    def _view(self, actor: Actor, model: UpstreamModel) -> ModelView:
        return ModelView(model=model, editable=actor.scope.permits(model.organization_id))

    async def _writable(
        self, transaction: CatalogTransaction, model_id: uuid.UUID
    ) -> UpstreamModel:
        """The narrow read. A global model is the platform's, so an org user finds
        nothing here and gets the same 404 as for an id that never existed."""
        model = await transaction.owned_model(model_id)
        if model is None:
            raise NotFound(NO_SUCH_MODEL)
        return model

    async def _check_name(
        self,
        transaction: CatalogTransaction,
        name: str,
        *,
        is_global: bool,
        excluding: uuid.UUID | None = None,
    ) -> None:
        """Names are unique within their namespace, and the two namespaces are separate.

        Two organizations may both have a model called ``gpt-4o``; the global catalog has
        one of each name. The global check cannot ride on the scope, because a superadmin
        who has opened an organization still writes into the one global namespace.
        """
        wanted = name.strip()
        taken = (
            await transaction.global_name_taken(wanted, excluding=excluding)
            if is_global
            else await transaction.name_taken(wanted, excluding=excluding)
        )
        if taken:
            where = "the global catalog" if is_global else "this organization"
            raise Conflict(f"A model called '{wanted}' already exists in {where}.", param="name")

    def _check_base_url(self, base_url: str) -> None:
        """Refuse a URL that points back inside this network (task 18).

        The immediate half of the SSRF guard: it exists so somebody typing a base URL gets
        a message under the field, not so an attacker is stopped — the transport is what
        stops an attacker, because a name that resolves somewhere harmless today can
        resolve somewhere else at the moment the request is made. Both read the same policy
        off ``Settings``, so what is refused here is exactly what would be refused there.
        """
        try:
            check_url(base_url, self._settings.upstream_url_policy)
        except ValueError as exc:
            raise Validation(f"base_url {exc}", param="base_url") from exc

    def _check_dialect(self, dialect: str) -> None:
        if dialect in known_dialects():
            return
        raise Validation(
            f"The '{dialect}' dialect is not yet supported by this build. "
            f"Available: {', '.join(known_dialects())}.",
            param="dialect",
        )

    def _check_auth(self, auth_type: str, credential: str | None) -> None:
        if auth_type == "none" and credential is not None:
            # Refused rather than silently dropped: "I set auth to none and it still
            # sends my key" and "I set a key and it is never sent" are both worse than
            # being told to pick one.
            raise Validation(
                "An auth type of 'none' sends no credential. Clear the credential, "
                "or choose how it should be sent.",
                param="auth_type",
            )

    def _store_credential(self, model: UpstreamModel, credential: str | None) -> None:
        """Encrypt and stash, or clear. The hint is derived here, from the plaintext,
        which is the only moment it is available."""
        if credential is None:
            model.credential_ciphertext = None
            model.credential_hint = None
            return
        model.credential_ciphertext = self._secret_box.encrypt(credential)
        model.credential_hint = secret_hint(credential)

    def _target_of(self, model: UpstreamModel) -> UpstreamTarget:
        return UpstreamTarget(
            id=model.id,
            name=model.name,
            base_url=model.base_url,
            dialect=model.dialect,
            upstream_model_id=model.upstream_model_id,
            auth_type=model.auth_type,
            credential=self._decrypt(model),
            extra_headers=dict(model.extra_headers or {}),
            system_context=None,  # a probe tests connectivity, not prompt assembly
            default_params={},
            timeout_seconds=model.timeout_seconds,
        )

    def _decrypt(self, model: UpstreamModel) -> str | None:
        if not model.credential_ciphertext:
            return None
        try:
            return self._secret_box.decrypt(model.credential_ciphertext)
        except DecryptionError:
            # Almost always a master key that does not match the one the credential was
            # written under. Saying so is the useful answer; probing without auth and
            # reporting the provider's 401 would send the operator after the wrong bug.
            logger.error("could not decrypt upstream credential", extra={"model_id": str(model.id)})
            raise Conflict(
                "This model's stored credential cannot be decrypted with the current "
                "encryption key. Set the credential again to replace it."
            ) from None

    async def _invalidate(self, transaction: CatalogTransaction, model_id: uuid.UUID) -> None:
        """Bump the config-cache version of every gateway pointing at this model.

        A no-op when no cache is wired, which is how the service tests run.
        """
        if self._cache is None:
            return
        await self._cache.invalidate(await transaction.slugs_referencing(model_id))

    async def _rate_limit(self, actor: Actor) -> None:
        if self._limiter is not None:
            await self._limiter.check(str(actor.user_id))

    def _log(self, message: str, actor: Actor, model: UpstreamModel, *, action: str) -> None:
        logger.info(
            message,
            extra={
                "user_id": str(actor.user_id),
                "organization_id": (str(model.organization_id) if model.organization_id else None),
                "model_id": str(model.id),
                "model_scope": model.scope,
                "audit_action": action,
            },
        )

    def _log_probe(
        self,
        actor: Actor,
        *,
        name: str,
        model_id: uuid.UUID | None,
        result: ProbeResult,
    ) -> None:
        logger.info(
            "model connection tested",
            extra={
                "user_id": str(actor.user_id),
                "model_id": str(model_id) if model_id else None,
                "model_name": name,
                "ok": result.ok,
                "latency_ms": result.latency_ms,
                "upstream_status": result.upstream_status,
                "audit_action": "model.test",
            },
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

#: Headers the transport owns. Setting one does not customise the request, it corrupts
#: it — a stale `content-length` truncates the body, and `host` breaks TLS SNI matching.
FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "content-type", "transfer-encoding", "connection"}
)
MAX_EXTRA_HEADERS = 20
MAX_HEADER_NAME = 64
MAX_HEADER_VALUE = 2048


def _check_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    if len(headers) > MAX_EXTRA_HEADERS:
        raise Validation(f"At most {MAX_EXTRA_HEADERS} extra headers.", param="extra_headers")

    checked: dict[str, str] = {}
    for name, value in headers.items():
        key = name.strip()
        if not key or len(key) > MAX_HEADER_NAME or not _is_header_token(key):
            raise Validation(f"'{name}' is not a valid HTTP header name.", param="extra_headers")
        if key.lower() in FORBIDDEN_HEADERS:
            raise Validation(
                f"'{key}' is set by the gateway and cannot be overridden.",
                param="extra_headers",
            )
        if not isinstance(value, str) or len(value) > MAX_HEADER_VALUE:
            raise Validation(
                f"The value of '{key}' must be a string of at most {MAX_HEADER_VALUE} characters.",
                param="extra_headers",
            )
        checked[key] = value
    return checked


def _is_header_token(name: str) -> bool:
    """RFC 9110 token characters, minus the ones nobody uses in a header name."""
    return all(character.isalnum() or character in "-_." for character in name)


def _picked[T](value: Maybe[T], current: T) -> T:
    return current if isinstance(value, _Unset) else value


def _apply(model: UpstreamModel, attribute: str, value: Maybe[Any]) -> None:
    if not isinstance(value, _Unset):
        setattr(model, attribute, value)


def _in_use_message(gateways: Sequence[Any]) -> str:
    names = ", ".join(f"'{gateway.name}'" for gateway in gateways)
    subject = "gateway" if len(gateways) == 1 else "gateways"
    return (
        f"This model is still used by the {subject} {names}. Point them at another model, "
        f"or delete them first. Disabling this model instead takes effect immediately."
    )


def _gateway_details(gateways: Sequence[Any]) -> dict[str, Any]:
    return {
        "gateways": [
            {"id": str(gateway.id), "slug": gateway.slug, "name": gateway.name}
            for gateway in gateways
        ]
    }


__all__ = [
    "UNSET",
    "CatalogService",
    "ModelDraft",
    "ModelPatch",
    "ModelView",
]
