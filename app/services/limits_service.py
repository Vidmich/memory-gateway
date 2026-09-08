"""What the Limits screen and the dashboard's warning card read.

The one thing worth stating about this module is where the numbers come from. The
*configuration* is read from the database rather than from the cached payload the data
plane serves with: the cache lags by at most a minute, and a screen that showed a stale
limit would be explaining an enforcement that is about to change. The *usage* is read
straight out of the same Redis buckets the limiter consumes from, unmodified — there is no
second aggregation and no cache, because a utilisation bar is only worth drawing if it is
true to the second.

Only the gateway scope is reported. Per-end-user buckets exist per person, and a single
number for "the per-end-user limit" would either be the worst caller (a name to publish on
a settings screen) or a meaningless average. The per-person view already has a home: the
"top throttled end users" list on Monitoring, which is built from log rows rather than from
live counters, so it survives a Redis restart and covers a whole window rather than the
current minute.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.errors import NotFound
from app.core.tenancy import Actor
from app.db.models import Gateway
from app.schemas.gateway_config import LIMIT_NAMES, LimitsConfig
from app.services.gateway_store import GatewayStore
from app.services.limit_store import LimitStore
from app.services.limits import (
    NEAR_LIMIT,
    NO_CEILINGS,
    Ceilings,
    Effective,
    Reading,
    Rule,
    effective,
    plan,
)

#: Same answer for "no such gateway" and "belongs to another organization", as everywhere
#: else. See ``tests/test_cross_tenant.py``.
NO_SUCH_GATEWAY = "No such gateway."

#: Gateways examined for the dashboard's warning card. Well above what any one
#: organization has, and a bound rather than a page because the card is a yes/no question
#: — "is anything under pressure" — and paging it would make the answer depend on which
#: page you asked for.
MAX_PRESSURE_GATEWAYS = 200


@dataclass(frozen=True, slots=True)
class LimitUsage:
    """One cap and how much of it is currently spent."""

    limit: str
    scope: str
    value: int
    used: int
    remaining: int
    reset_seconds: int
    utilization: float
    #: Whether the platform ceiling is what produced ``value``, rather than what somebody
    #: typed. The editor says so next to the input; without it the screen looks like it is
    #: ignoring the number that was saved.
    capped: bool

    @classmethod
    def of(cls, reading: Reading, *, capped: bool) -> LimitUsage:
        return cls(
            limit=reading.rule.limit,
            scope=reading.rule.scope,
            value=reading.rule.value,
            used=reading.used,
            remaining=reading.remaining,
            reset_seconds=reading.reset_seconds,
            utilization=reading.utilization,
            capped=capped,
        )

    @property
    def near_limit(self) -> bool:
        return self.utilization >= NEAR_LIMIT


@dataclass(frozen=True, slots=True)
class GatewayLimits:
    """A gateway's Limits section, with the live bars."""

    gateway_id: uuid.UUID
    slug: str
    name: str
    #: What is stored — what the form shows in its inputs.
    configured: LimitsConfig
    #: What is enforced. Differs from ``configured`` only where a ceiling applies.
    enforced: Effective
    #: The platform's maxima, whether or not they apply to this gateway. Sent even when
    #: they do not, so the editor can explain what *would* happen if this gateway were
    #: pointed at a global model — which is the moment somebody needs to know.
    ceilings: Ceilings
    global_models: bool
    usage: tuple[LimitUsage, ...]

    @property
    def under_pressure(self) -> tuple[LimitUsage, ...]:
        return tuple(entry for entry in self.usage if entry.near_limit)


@dataclass(frozen=True, slots=True)
class Pressure:
    """One row of the dashboard's warning card: a gateway and its worst limit."""

    gateway_id: uuid.UUID
    slug: str
    name: str
    worst: LimitUsage


class LimitsService:
    def __init__(
        self,
        gateways: GatewayStore,
        *,
        buckets: LimitStore,
        ceilings: Ceilings = NO_CEILINGS,
    ) -> None:
        self._gateways = gateways
        self._buckets = buckets
        self._ceilings = ceilings

    async def gateway(self, actor: Actor, gateway_id: uuid.UUID) -> GatewayLimits:
        async with self._gateways.begin(actor.scope) as transaction:
            row = await transaction.gateway(gateway_id)
            if row is None:
                raise NotFound(NO_SUCH_GATEWAY)
            view = self._describe(row)
        return await self._fill(view)

    async def pressure(self, actor: Actor) -> tuple[Pressure, ...]:
        """Every gateway currently past 80% of one of its caps, worst first.

        One ``peek`` for the whole organization rather than one per gateway: this runs on
        the dashboard, which is the screen most often open, and N round trips to Redis for
        a card that is usually empty would be the wrong thing to put there.
        """
        async with self._gateways.begin(actor.scope) as transaction:
            rows = await transaction.gateways(after=None, limit=MAX_PRESSURE_GATEWAYS)
            views = [self._describe(row) for row in rows]

        rules = [rule for view in views for rule in self._rules(view)]
        readings = await self._buckets.peek(rules)
        by_key = {reading.rule.key: reading for reading in readings}

        pressures: list[Pressure] = []
        for view in views:
            worst: LimitUsage | None = None
            for rule in self._rules(view):
                reading = by_key.get(rule.key)
                if reading is None:
                    continue
                usage = LimitUsage.of(reading, capped=rule.limit in view.enforced.capped)
                if usage.near_limit and (worst is None or usage.utilization > worst.utilization):
                    worst = usage
            if worst is not None:
                pressures.append(
                    Pressure(
                        gateway_id=view.gateway_id, slug=view.slug, name=view.name, worst=worst
                    )
                )
        return tuple(sorted(pressures, key=lambda item: -item.worst.utilization))

    # -- internals -----------------------------------------------------------

    def _describe(self, row: Gateway) -> GatewayLimits:
        configured = LimitsConfig.load(row.limits)
        # The same rule the resolver's payload uses: a *usable* target, because a disabled
        # global model cannot be routed to and so cannot spend the operator's credential.
        global_models = any(
            target.upstream_model.scope == "global" and target.upstream_model.enabled
            for target in row.targets
        )
        return GatewayLimits(
            gateway_id=row.id,
            slug=row.slug,
            name=row.name,
            configured=configured,
            enforced=effective(configured, ceilings=self._ceilings, global_models=global_models),
            ceilings=self._ceilings,
            global_models=global_models,
            usage=(),
        )

    def _rules(self, view: GatewayLimits) -> tuple[Rule, ...]:
        return plan(
            view.enforced,
            gateway_id=view.gateway_id,
            # Gateway scope only — see the module docstring.
            end_user_id=None,
            names=LIMIT_NAMES,
        )

    async def _fill(self, view: GatewayLimits) -> GatewayLimits:
        rules = self._rules(view)
        readings = await self._buckets.peek(rules)
        return GatewayLimits(
            gateway_id=view.gateway_id,
            slug=view.slug,
            name=view.name,
            configured=view.configured,
            enforced=view.enforced,
            ceilings=view.ceilings,
            global_models=view.global_models,
            usage=tuple(
                LimitUsage.of(reading, capped=reading.rule.limit in view.enforced.capped)
                for reading in readings
            ),
        )


__all__ = [
    "MAX_PRESSURE_GATEWAYS",
    "NO_SUCH_GATEWAY",
    "GatewayLimits",
    "LimitUsage",
    "LimitsService",
    "Pressure",
]
