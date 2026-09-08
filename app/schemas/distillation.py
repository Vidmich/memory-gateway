"""What an organization has decided about writing its own memory (SPEC §6.4).

Distillation settings live on the **organization**, not on the gateway, and that is the
one design decision in this file worth arguing about.

Everything else in :mod:`app.schemas.gateway_config` is per-endpoint because it is about a
*request*: which connectors this endpoint may read, how many tokens it may inject, what to
do when retrieval fails. Distillation is not about a request. It is about a *person* — how
many things may be remembered about them, how sure the writer has to be before two
sentences count as one, which model does the remembering, and how much all of that may
cost per day. An end user typically reaches an organization through more than one gateway,
and a bound expressed per gateway would mean "at most five hundred facts, per endpoint,
about the same human being", which is not a bound.

So ``max_facts_per_user`` and ``dedupe_threshold`` are here, and ``enable_distillation``
stays on the gateway. The two are not the same switch and the split is deliberate:
``logging_config.enable_distillation`` is *this endpoint's traffic may feed memory* — a
gateway serving an internal batch job should not teach the assistant about anyone — while
:attr:`DistillationConfig.enabled` is *this organization writes memory at all*. Turning the
second off stops every gateway; turning the first off stops one.

Stored under ``organizations.settings["distillation"]``, beside the logging defaults, for
the reason that column exists: adding a knob is an object key rather than a migration on a
populated table.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.config import ConfigBlob

#: Where the blob lives inside ``organizations.settings``.
ORG_DISTILLATION = "distillation"

#: SPEC §6.4's bound on one person's durable memory. A person whose memory has grown past
#: five hundred sentences has stopped having memory and started having a transcript.
MAX_FACTS_PER_USER = 500

#: SPEC §6.4's default. High on purpose: below roughly 0.9, two genuinely different
#: preferences about the same topic — "prefers Python" and "prefers Go" — score as one
#: fact, and the second would silently bump the first's confidence instead of contradicting
#: it. The failure of a threshold set too low is invisible; the failure of one set too high
#: is a duplicate somebody can see and delete.
DEDUPE_THRESHOLD = 0.92

#: Seconds a conversation must go quiet before it is distilled (SPEC §6.4, step 1).
DEBOUNCE_SECONDS = 30

#: Extraction calls one organization may make in a day, before anything is refused. A cost
#: guard that is off by default is not a guard, and this number is roughly "a thousand
#: active conversations" at a cheap model — far above any pilot and far below a runaway.
#: Hitting it is recorded as a skipped run and shown on the Settings screen, because a cap
#: that stops memory silently is worse than no cap.
DAILY_CALL_CAP = 5000

#: Extraction calls for one *end user* in a day. The debounce coalesces a burst; this is
#: what bounds somebody who talks to the assistant steadily for eight hours, whose every
#: quiet moment would otherwise be a pass. Twenty-four is a pass an hour, which is far more
#: often than a person's durable facts actually change.
PER_USER_DAILY_CAP = 24


class DistillationConfig(ConfigBlob):
    """SPEC §6.4's knobs, per organization."""

    #: The org-wide off switch. Off means no transcript is read and no model is called,
    #: whatever any gateway says.
    enabled: bool = True
    #: Which upstream model extracts facts. Any model this organization can see, including
    #: a global one; ``None`` falls back to the platform default
    #: (``Settings.distillation_model_id``). An id rather than a name because a name is not
    #: unique across the platform and the org catalogs, and a lookup that quietly picked
    #: the other ``gpt-4o-mini`` would be an invisible bill.
    model_id: uuid.UUID | None = None
    debounce_seconds: int = Field(default=DEBOUNCE_SECONDS, ge=5, le=3600)
    dedupe_threshold: float = Field(default=DEDUPE_THRESHOLD, ge=0.5, le=1.0)
    max_facts_per_user: int = Field(default=MAX_FACTS_PER_USER, ge=1, le=10_000)
    daily_call_cap: int = Field(default=DAILY_CALL_CAP, ge=0, le=1_000_000)
    per_user_daily_cap: int = Field(default=PER_USER_DAILY_CAP, ge=0, le=10_000)


def organization_distillation(settings: Mapping[str, Any] | None) -> DistillationConfig:
    """The distillation settings for an organization, defaults filled in.

    Anything that is not an object loads as the defaults rather than raising, for the same
    reason :func:`~app.schemas.gateway_config.organization_logging_defaults` does: this is
    read by a background worker, and a settings blob somebody hand-edited badly should
    degrade to the documented behaviour rather than dead-letter every job in the
    organization.
    """
    if not isinstance(settings, Mapping):
        return DistillationConfig()
    stored = settings.get(ORG_DISTILLATION)
    return DistillationConfig.load(stored if isinstance(stored, Mapping) else None)


# ---------------------------------------------------------------------------
# API bodies
# ---------------------------------------------------------------------------


class DistillationUsage(BaseModel):
    """What has been spent against the caps today, so the form can show the guard biting.

    ``day_started_at`` is returned rather than implied: the cap resets at UTC midnight and
    an operator in Auckland reading "4,998 of 5,000 used" needs to know how long that has
    left to run.
    """

    calls_today: int = 0
    daily_call_cap: int = DAILY_CALL_CAP
    day_started_at: datetime

    @property
    def exhausted(self) -> bool:
        return self.daily_call_cap > 0 and self.calls_today >= self.daily_call_cap


class DistillationSettingsResponse(BaseModel):
    """The blob, plus the two things the screen needs that are not settings."""

    config: DistillationConfig
    usage: DistillationUsage
    #: The model that would actually be used, resolved through the platform default —
    #: including "(deleted)" when the configured id no longer exists. Null when nothing is
    #: configured anywhere, which is the state in which distillation cannot run at all.
    effective_model_id: uuid.UUID | None = None
    effective_model_name: str | None = None
    #: True when :attr:`effective_model_id` came from the platform rather than from this
    #: organization's own setting.
    using_platform_default: bool = False


class DistillationSettingsRequest(BaseModel):
    """A partial update, merged into whatever is stored.

    Every field optional and applied only when present, so a form that renders six knobs
    and a later build that renders seven do not overwrite each other's.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    model_id: uuid.UUID | None = None
    debounce_seconds: int | None = None
    dedupe_threshold: float | None = None
    max_facts_per_user: int | None = None
    daily_call_cap: int | None = None
    per_user_daily_cap: int | None = None

    def patch(self) -> dict[str, Any]:
        """Only the fields the caller actually sent.

        ``model_fields_set`` rather than a null check, because ``model_id: null`` is a
        meaningful value here — it means "go back to the platform default" — and dropping
        it would make clearing the selector impossible.
        """
        return {
            name: getattr(self, name)
            for name in type(self).model_fields
            if name in self.model_fields_set
        }


__all__ = [
    "DAILY_CALL_CAP",
    "DEBOUNCE_SECONDS",
    "DEDUPE_THRESHOLD",
    "MAX_FACTS_PER_USER",
    "ORG_DISTILLATION",
    "PER_USER_DAILY_CAP",
    "DistillationConfig",
    "DistillationSettingsRequest",
    "DistillationSettingsResponse",
    "DistillationUsage",
    "organization_distillation",
]
