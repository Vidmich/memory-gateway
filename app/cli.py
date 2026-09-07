"""Operator commands. ``python -m app.cli <command>``.

Task 02 has no configuration API yet, so the demo gateway is created here. Everything this
does will be doable from the UI after task 06; the command stays useful for bootstrapping
a fresh environment.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import ColumnExpressionArgument, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import keys
from app.core.config import Settings, get_settings
from app.core.crypto import SecretBox, secret_hint
from app.core.ids import uuid7
from app.db.base import Base
from app.db.models import ApiKey, Gateway, GatewayTarget, Organization, UpstreamModel
from app.db.session import create_engine, create_session_factory

DEMO_SLUG = "demo"
DEMO_KEY_NAME = "demo"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


@dataclass
class SeedOptions:
    base_url: str
    model: str
    credential: str | None
    auth_type: str
    rotate_key: bool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    seed = commands.add_parser("seed", help="Create or update the demo org, model, gateway and key")
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

    args = parser.parse_args(argv)
    # `required=True` on the subparsers means argparse has already rejected anything else.
    return asyncio.run(run_seed(args.rotate_key, no_auth=args.no_auth))


async def run_seed(rotate_key: bool, *, no_auth: bool) -> int:
    settings = get_settings()

    credential = os.getenv("OPENAI_API_KEY")
    if not credential and not no_auth:
        print(
            "OPENAI_API_KEY is not set.\n"
            "  Set it to seed a gateway that talks to a real provider, or pass --no-auth\n"
            "  to seed one pointing at a local, unauthenticated upstream.",
            file=sys.stderr,
        )
        return 1

    options = SeedOptions(
        base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        model=os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
        credential=None if no_auth else credential,
        auth_type="none" if no_auth else "bearer",
        rotate_key=rotate_key,
    )

    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            token = await seed_demo(session, options, SecretBox.from_settings(settings))
            await session.commit()
    finally:
        await engine.dispose()

    _report(settings, options, token)
    return 0


async def seed_demo(
    session: AsyncSession,
    options: SeedOptions,
    secret_box: SecretBox,
) -> str | None:
    """Create or update the demo configuration.

    Idempotent: every object is looked up by its natural key and updated in place, so
    re-running after changing ``OPENAI_MODEL`` repoints the existing gateway instead of
    creating a second one. Returns the new key's plaintext, or ``None`` when an existing
    key was kept.
    """
    organization = await _upsert_organization(session)
    model = await _upsert_model(session, organization, options, secret_box)
    gateway = await _upsert_gateway(session, organization)
    await _upsert_target(session, gateway, model)
    return await _ensure_key(session, gateway, rotate=options.rotate_key)


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
    gateway.param_overrides = {}
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
    return (await session.execute(select(model).where(*where))).scalars().first()


def _report(settings: Settings, options: SeedOptions, token: str | None) -> None:
    base = f"{settings.public_base_url}/g/{DEMO_SLUG}/v1"
    print("Demo gateway ready.\n")
    print(f"  base_url : {base}")
    print(f"  model    : {DEMO_SLUG}")
    print(f"  upstream : {options.model} at {options.base_url}")
    if options.credential:
        print(f"  provider key: {secret_hint(options.credential)} (encrypted at rest)")
    print()
    if token is None:
        print("  An API key named 'demo' already exists and its plaintext cannot be shown")
        print("  again. Re-run with --rotate-key to revoke it and issue a new one.")
    else:
        print("  API key (shown once, store it now):")
        print(f"    {token}")
    print()
    print("Try it:")
    print(f'  curl {base}/models -H "Authorization: Bearer $MG_KEY"')


if __name__ == "__main__":
    raise SystemExit(main())
