"""Operator commands. ``python -m app.cli <command>``.

``seed`` bootstraps a usable environment: a superadmin to log into the UI with, and — when
a provider credential is available — the demo organization, upstream model, gateway and
API key that the data plane needs.

Everything the gateway half does is now doable from the UI. What is left that the UI
cannot do is the first step: creating the superadmin there is nobody to log in as yet.
The gateway seeding stays because a working ``/g/demo/v1`` on a fresh checkout is what
makes the data plane testable before anyone has opened a browser.

``distil-backfill`` reads transcripts that no distillation pass has covered and covers them
(SPEC §6.4). It exists for two moments that are otherwise unrecoverable: switching the
feature on for a gateway that has been serving traffic for months, and recovering from an
outage in which the worker was down while the debounce windows quietly expired. It is
idempotent because ``transcripts.distilled_at`` is — running it twice over the same range
distils nothing the second time — and it is bounded by a date range because the alternative,
"everything", is a bill nobody sized.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import ColumnExpressionArgument, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import keys
from app.core.config import Settings, get_settings
from app.core.crypto import SecretBox, secret_hint
from app.core.ids import uuid7
from app.core.passwords import Hasher, build_hasher
from app.db.base import Base
from app.db.models import ApiKey, Gateway, GatewayTarget, Organization, UpstreamModel, User
from app.db.scoping import unscoped
from app.db.session import create_engine, create_session_factory

DEMO_SLUG = "demo"
DEMO_KEY_NAME = "demo"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_ADMIN_EMAIL = "admin@example.com"
#: 24 URL-safe characters. Long enough that nobody is tempted to keep it, which is the
#: point — it exists to get you to the password-change screen.
ADMIN_PASSWORD_BYTES = 18

#: How many conversations one backfill run covers by default. Small enough that a first
#: run is a sample rather than an invoice; the command is idempotent, so the way to do more
#: is to run it again.
DEFAULT_BACKFILL_LIMIT = 200


@dataclass
class SeedOptions:
    base_url: str
    model: str
    credential: str | None
    auth_type: str
    rotate_key: bool
    admin_email: str = DEFAULT_ADMIN_EMAIL
    rotate_admin_password: bool = False
    #: False when no provider credential is available; the superadmin is still seeded.
    seed_gateway: bool = True


@dataclass
class SeedResult:
    """What was created. ``None`` means "already existed and cannot be shown again"."""

    admin_email: str
    admin_password: str | None
    api_key: str | None
    gateway_seeded: bool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    seed = commands.add_parser(
        "seed", help="Create or update the superadmin, and the demo gateway and key"
    )
    seed.add_argument(
        "--rotate-key",
        action="store_true",
        help="Revoke the existing demo key and issue a new one (the plaintext of an "
        "existing key cannot be shown again)",
    )
    seed.add_argument(
        "--no-auth",
        action="store_true",
        help="Configure the upstream with no credential, for a local provider such as "
        "Ollama or vLLM",
    )
    seed.add_argument(
        "--admin-email",
        default=os.getenv("SEED_ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL),
        help=f"Email of the superadmin to create (default: {DEFAULT_ADMIN_EMAIL})",
    )
    seed.add_argument(
        "--rotate-admin-password",
        action="store_true",
        help="Set a new random password for the superadmin and print it once",
    )

    commands.add_parser(
        "openapi",
        help="Print the OpenAPI schema to stdout (the frontend's typed client is "
        "generated from it)",
    )

    backfill = commands.add_parser(
        "distil-backfill",
        help="Distil conversation memory from transcripts a pass has not covered yet",
    )
    backfill.add_argument(
        "--since",
        required=True,
        help="Start of the range, as a date (2026-09-01) or an ISO timestamp",
    )
    backfill.add_argument(
        "--until",
        default=None,
        help="End of the range, exclusive (default: now)",
    )
    backfill.add_argument(
        "--organization",
        default=None,
        help="Only this organization, by id (default: every organization)",
    )
    backfill.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BACKFILL_LIMIT,
        help=f"Most conversations to distil in this run (default: {DEFAULT_BACKFILL_LIMIT})",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be distilled and call no models",
    )

    args = parser.parse_args(argv)
    if args.command == "openapi":
        return dump_openapi()
    if args.command == "distil-backfill":
        return asyncio.run(
            run_backfill(
                since=_moment(args.since),
                until=_moment(args.until) if args.until else datetime.now(UTC),
                organization_id=uuid.UUID(args.organization) if args.organization else None,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )

    # `required=True` on the subparsers means argparse has already rejected anything else.
    return asyncio.run(
        run_seed(
            args.rotate_key,
            no_auth=args.no_auth,
            admin_email=args.admin_email,
            rotate_admin_password=args.rotate_admin_password,
        )
    )


def dump_openapi() -> int:
    """Write the schema the frontend's types are generated from.

    Imported lazily so that `seed` does not pay for building an application it will not
    use, and sorted so the output is byte-stable — the drift check in CI compares it.
    """
    from app.main import create_app

    print(json.dumps(create_app().openapi(), indent=2, sort_keys=True))
    return 0


def _moment(value: str) -> datetime:
    """A date or a timestamp, always in UTC.

    A bare date is accepted because that is what an operator types, and it is read as
    midnight UTC rather than local midnight: a backfill whose range shifts with the machine
    it is run from is a backfill that covers a different set of conversations each time.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"could not read {value!r} as a date or timestamp") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def run_backfill(
    *,
    since: datetime,
    until: datetime,
    organization_id: uuid.UUID | None,
    limit: int,
    dry_run: bool,
) -> int:
    """Distil every conversation in a window that no pass has covered.

    Runs the *real* pass, through the same objects the worker builds — so a backfill and a
    live distillation produce the same facts, deduplicate against the same index, and honour
    the same per-organization settings including the daily cap. A backfill that bypassed the
    cap would be the one way to spend a month's budget in an afternoon.

    Sequential, not concurrent. The work is provider calls against a model the customer is
    paying for, and the operator running this wants to be able to stop it.
    """
    from app.core.clients import Clients
    from app.core.tenancy import TenantScope
    from app.services.distillation_store import SUCCEEDED
    from app.workers.runtime import build_distillation, build_ingestion, build_queue

    settings = get_settings()
    clients = Clients.create(settings)
    try:
        ingestion = build_ingestion(clients, settings, queue=build_queue(clients.jobs))
        distillation = build_distillation(clients, settings, ingestion=ingestion)
        scope = (
            TenantScope.of_organization(organization_id)
            if organization_id is not None
            # Unrestricted, deliberately: a backfill with no ``--organization`` spans every
            # tenant, which is a thing only an operator with shell access can ask for. Each
            # pass it launches then runs under that conversation's own organization scope.
            else TenantScope(role="service", organization_id=None)
        )
        async with distillation.store.begin(scope) as transaction:
            sessions = list(await transaction.pending_sessions(start=since, end=until, limit=limit))

        print(f"{len(sessions)} conversation(s) with undistilled transcripts")
        if dry_run:
            for session in sessions:
                print(f"  {session.organization_id} {session.end_user_id} {session.session_id}")
            return 0

        written = 0
        for session in sessions:
            # No debounce token: a backfill is somebody asking for these passes now, and a
            # pending token it never armed is not its to lose to.
            outcome = await distillation.distiller.run(
                organization_id=session.organization_id,
                end_user_id=session.end_user_id,
                session_id=session.session_id,
            )
            written += outcome.inserted
            print(
                f"  {session.end_user_id} {outcome.outcome}"
                + (f" ({outcome.reason})" if outcome.outcome != SUCCEEDED else "")
                + f" +{outcome.inserted} ~{outcome.deduped} ^{outcome.superseded}"
            )
        print(f"{written} new fact(s) written")
    finally:
        await ingestion.aclose()
        await clients.aclose()
    return 0


async def run_seed(
    rotate_key: bool,
    *,
    no_auth: bool,
    admin_email: str = DEFAULT_ADMIN_EMAIL,
    rotate_admin_password: bool = False,
) -> int:
    settings = get_settings()

    credential = os.getenv("OPENAI_API_KEY")
    # A missing provider key is no longer fatal. Task 03's demo needs the superadmin, and
    # that does not depend on an upstream; the gateway half is simply skipped, and the
    # report says so.
    seed_gateway = bool(credential) or no_auth

    options = SeedOptions(
        base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        model=os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
        credential=None if no_auth else credential,
        auth_type="none" if no_auth else "bearer",
        rotate_key=rotate_key,
        admin_email=admin_email,
        rotate_admin_password=rotate_admin_password,
        seed_gateway=seed_gateway,
    )

    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            result = await seed_demo(
                session,
                options,
                SecretBox.from_settings(settings),
                build_hasher(settings),
            )
            await session.commit()
    finally:
        await engine.dispose()

    _report(settings, options, result)
    return 0


async def seed_demo(
    session: AsyncSession,
    options: SeedOptions,
    secret_box: SecretBox,
    hasher: Hasher,
) -> SeedResult:
    """Create or update the seeded configuration.

    Idempotent: every object is looked up by its natural key and updated in place, so
    re-running after changing ``OPENAI_MODEL`` repoints the existing gateway instead of
    creating a second one. Secrets that already exist are never re-shown — printing a
    password nobody set would be worse than admitting it cannot be recovered.
    """
    organization = await _upsert_organization(session)
    admin_password = await _ensure_superadmin(session, options, hasher)

    token: str | None = None
    if options.seed_gateway:
        model = await _upsert_model(session, organization, options, secret_box)
        gateway = await _upsert_gateway(session, organization)
        await _upsert_target(session, gateway, model)
        token = await _ensure_key(session, gateway, rotate=options.rotate_key)

    return SeedResult(
        admin_email=options.admin_email,
        admin_password=admin_password,
        api_key=token,
        gateway_seeded=options.seed_gateway,
    )


async def _ensure_superadmin(
    session: AsyncSession, options: SeedOptions, hasher: Hasher
) -> str | None:
    """Create the platform superadmin, or reset its password on request.

    Returns the plaintext when one was generated, ``None`` when an existing account was
    left alone. A superadmin has no ``organization_id`` — it is a platform account, and
    the CHECK constraint on ``users`` enforces that rather than trusting this code.
    """
    existing = await _by(session, User, User.email == options.admin_email)

    if existing is not None and not options.rotate_admin_password:
        return None

    password = secrets.token_urlsafe(ADMIN_PASSWORD_BYTES)
    if existing is None:
        existing = User(
            id=uuid7(),
            organization_id=None,
            email=options.admin_email,
            role="superadmin",
            name="Platform Admin",
            status="active",
        )
        session.add(existing)

    existing.password_hash = hasher.hash(password)
    existing.status = "active"
    await session.flush()
    return password


async def _upsert_organization(session: AsyncSession) -> Organization:
    existing = await _by(session, Organization, Organization.slug == DEMO_SLUG)
    if existing is not None:
        return existing
    organization = Organization(
        id=uuid7(), name="Demo Organization", slug=DEMO_SLUG, status="active"
    )
    session.add(organization)
    await session.flush()
    return organization


async def _upsert_model(
    session: AsyncSession,
    organization: Organization,
    options: SeedOptions,
    secret_box: SecretBox,
) -> UpstreamModel:
    name = "demo-upstream"
    model = await _by(
        session,
        UpstreamModel,
        UpstreamModel.organization_id == organization.id,
        UpstreamModel.name == name,
    )
    if model is None:
        model = UpstreamModel(id=uuid7(), organization_id=organization.id, name=name)
        session.add(model)

    model.scope = "org"
    model.description = "Seeded by `python -m app.cli seed`."
    model.base_url = options.base_url
    model.dialect = "openai"
    model.upstream_model_id = options.model
    model.auth_type = options.auth_type
    model.credential_ciphertext = (
        secret_box.encrypt(options.credential) if options.credential else None
    )
    # Stored alongside the ciphertext so the Models screen renders without the master key.
    model.credential_hint = secret_hint(options.credential) if options.credential else None
    model.extra_headers = {}
    model.default_params = {}
    model.timeout_seconds = 60
    model.enabled = True
    await session.flush()
    return model


async def _upsert_gateway(session: AsyncSession, organization: Organization) -> Gateway:
    gateway = await _by(session, Gateway, Gateway.slug == DEMO_SLUG)
    if gateway is None:
        gateway = Gateway(id=uuid7(), organization_id=organization.id, slug=DEMO_SLUG)
        session.add(gateway)

    gateway.name = "Demo Gateway"
    gateway.description = "Seeded by `python -m app.cli seed`."
    gateway.enabled = True
    gateway.routing_mode = "single"
    gateway.param_overrides = {}
    gateway.locked_params = {}
    # Left empty rather than filled in: `app.schemas.gateway_config` supplies the defaults
    # on read, and a second copy of them here is a second place to keep in step.
    gateway.memory_config = {}
    gateway.logging_config = {}
    gateway.limits = {}
    await session.flush()
    return gateway


async def _upsert_target(session: AsyncSession, gateway: Gateway, model: UpstreamModel) -> None:
    target = await _by(
        session,
        GatewayTarget,
        GatewayTarget.gateway_id == gateway.id,
        GatewayTarget.upstream_model_id == model.id,
    )
    if target is None:
        session.add(
            GatewayTarget(
                id=uuid7(),
                gateway_id=gateway.id,
                upstream_model_id=model.id,
                priority=0,
                weight=100,
            )
        )
        await session.flush()


async def _ensure_key(session: AsyncSession, gateway: Gateway, *, rotate: bool) -> str | None:
    existing = await _by(
        session,
        ApiKey,
        ApiKey.gateway_id == gateway.id,
        ApiKey.name == DEMO_KEY_NAME,
        ApiKey.revoked_at.is_(None),
    )
    if existing is not None and not rotate:
        # The plaintext is unrecoverable by design, so re-running seed must not silently
        # hand back something that does not work.
        return None
    if existing is not None:
        existing.revoked_at = datetime.now(UTC)

    minted = keys.mint(uuid7())
    session.add(
        ApiKey(
            id=minted.key_id,
            gateway_id=gateway.id,
            name=DEMO_KEY_NAME,
            key_hash=minted.key_hash,
            prefix=minted.prefix,
        )
    )
    await session.flush()
    return minted.token


async def _by[T: Base](
    session: AsyncSession,
    model: type[T],
    *where: ColumnExpressionArgument[bool],
) -> T | None:
    statement = (
        select(model)
        .where(*where)
        .execution_options(
            # Seeding runs as the operator, before any organization exists to be scoped to.
            # It is a shell command on the deployment host, not a request.
            **unscoped("operator CLI: seeding creates the organizations it would scope to")
        )
    )
    return (await session.execute(statement)).scalars().first()


def _report(settings: Settings, options: SeedOptions, result: SeedResult) -> None:
    print("Sign in to the UI:\n")
    print(f"  email    : {result.admin_email}")
    if result.admin_password is None:
        print("  password : (unchanged — an account with this email already exists)")
        print("             Re-run with --rotate-admin-password to set a new one.")
    else:
        print(f"  password : {result.admin_password}")
        print("             Shown once. Change it after signing in.")
    print()

    if not result.gateway_seeded:
        print("Skipped the demo gateway: OPENAI_API_KEY is not set.")
        print("  Set it and re-run to seed a gateway that talks to a real provider, or")
        print("  pass --no-auth for a local, unauthenticated upstream.")
        return

    base = f"{settings.public_base_url}/g/{DEMO_SLUG}/v1"
    print("Demo gateway ready.\n")
    print(f"  base_url : {base}")
    print(f"  model    : {DEMO_SLUG}")
    print(f"  upstream : {options.model} at {options.base_url}")
    if options.credential:
        print(f"  provider key: {secret_hint(options.credential)} (encrypted at rest)")
    print()
    if result.api_key is None:
        print("  An API key named 'demo' already exists and its plaintext cannot be shown")
        print("  again. Re-run with --rotate-key to revoke it and issue a new one.")
    else:
        print("  API key (shown once, store it now):")
        print(f"    {result.api_key}")
    print()
    print("Try it:")
    print(f'  curl {base}/models -H "Authorization: Bearer $MG_KEY"')


if __name__ == "__main__":
    raise SystemExit(main())
