"""What an audit event is, and how one is built (SPEC §10.4).

Everything in this module is pure except the two recorder mixins at the bottom, and the
split is deliberate: the interesting half — what a snapshot of a row contains, what a
diff between two of them looks like, and which values may never appear in one — is
arithmetic over dictionaries, so it is tested without a database and cannot behave
differently in one.

**Redaction is structural.** A secret never enters a snapshot in the first place: the
snapshot functions in :mod:`app.services.audit_snapshots` wrap it in :class:`Sensitive`,
which carries a fingerprint used only for *comparison* and renders as ``"***"`` on both
sides of a change. There is no scan for values that look like credentials, because such a
scan is a guess, and the one it misses is the one that ends up in an immutable table. The
consequence worth naming: the log can say a credential was replaced, and can never say
what it was replaced with — which is what SPEC §5.4 says about credentials everywhere
else, applied here too.

**A diff is a list of paths, not two documents.** Storing the whole before and after would
make the table enormous and the screen unreadable; what somebody wants when they open a
row is the three fields that changed. Nested blobs flatten to dotted paths —
``memory_config.doc_top_k: 6 → 10`` — which is also what makes them greppable.

**Recording never fails the mutation.** :meth:`AuditRecorder.audit` catches everything a
snapshot or a diff can raise, logs it, and increments a counter, because a bug in a
snapshot function must not be able to stop somebody saving a gateway. The *write* is a
different matter: the event is added to the same session as the mutation, so it commits
with it or not at all. Those two sentences are the whole failure model — a defect in this
module loses an event, and a database problem loses the mutation too.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.core.logging import get_request_id
from app.core.tenancy import Actor
from app.db.models import AuditEvent
from app.db.models.audit import MAX_LABEL_LENGTH
from app.services.memory_db import MemoryDatabase

logger = logging.getLogger(__name__)

#: What a redacted value renders as, on both sides of a change. Fixed, and the same
#: string the task's demo names.
REDACTED = "***"

#: The longest a single value in a diff may be. A gateway system context is a prompt and
#: can be thousands of tokens; storing every revision of one in an append-only table is a
#: cost with no reader, since what the screen shows is "this changed, here is the shape
#: of it". A change whose value was cut is marked, so nobody mistakes the head for the
#: whole.
MAX_VALUE_CHARS = 500

#: The most changed fields one event records. Reached only by a save that rewrites an
#: entire configuration, and the count of what was dropped is kept.
MAX_CHANGES = 100

#: How many examples a bulk operation names. Enough to recognise what happened, few
#: enough that a forty-file upload is still one small row.
MAX_SAMPLE = 5


@dataclass(frozen=True, slots=True)
class Sensitive:
    """A value that is compared but never stored.

    ``fingerprint`` is a digest, so "the credential changed" is answerable without the
    plaintext being anywhere near this object; ``None`` means the field is *absent*,
    which is a real and useful state — ``null → "***"`` is a credential being set, and
    ``"***" → null`` is one being cleared.
    """

    fingerprint: str | None

    @classmethod
    def of(cls, value: Any) -> Sensitive:
        if value is None or value == "" or value == b"":
            return cls(None)
        raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
        return cls(hashlib.sha256(raw).hexdigest())

    @property
    def present(self) -> bool:
        return self.fingerprint is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never the fingerprint: a repr ends up in tracebacks and log lines, which is
        # precisely where a value that must not be stored should not appear either.
        return f"Sensitive({'set' if self.present else 'unset'})"


#: One row's state, as a tree of plain values and :class:`Sensitive` markers.
type Snapshot = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Target:
    """What was changed, in a form that survives the row being deleted."""

    type: str
    id: uuid.UUID | None = None
    #: The human name at the time of the event: a slug, an email, a filename.
    label: str | None = None


@dataclass(frozen=True, slots=True)
class Subject:
    """A target and its state, taken together at one moment.

    Paired because the two are always read off the same row, and because a caller that
    took the target before a mutation and the state after it would produce an event whose
    halves disagree.
    """

    target: Target
    state: Snapshot


@dataclass(frozen=True, slots=True)
class Attribution:
    """Who is acting, and from where.

    Built from an :class:`~app.core.tenancy.Actor` for a person, or by :meth:`system` for
    a background job. ``actor_type`` is derived rather than passed: a superadmin who has
    assumed an organization is *always* impersonating, and letting a call site decide
    otherwise is how a support access ends up recorded as an ordinary edit.
    """

    actor_type: str
    user_id: uuid.UUID | None
    label: str | None
    organization_id: uuid.UUID | None
    ip: str | None = None
    user_agent: str | None = None

    @classmethod
    def of(cls, actor: Actor) -> Attribution:
        return cls(
            actor_type="superadmin_impersonation" if actor.scope.assumed else "user",
            user_id=actor.user_id,
            label=actor.label,
            organization_id=actor.scope.organization_id,
            ip=actor.ip,
            user_agent=actor.user_agent,
        )

    @classmethod
    def system(cls, organization_id: uuid.UUID | None, *, job: str) -> Attribution:
        """A background job's attribution.

        ``job`` becomes the actor label, so the log reads "delete-connector" rather than
        leaving a blank where a name should be. Task 09's connector deletion and task 17's
        reindex both mutate configuration with nobody waiting on the result, and an event
        with no actor at all reads like a gap rather than like a job.
        """
        return cls(
            actor_type="system",
            user_id=None,
            label=job,
            organization_id=organization_id,
        )

    @classmethod
    def coerce(cls, source: Actor | Attribution) -> Attribution:
        return source if isinstance(source, Attribution) else cls.of(source)

    def inside(self, organization_id: uuid.UUID | None) -> Attribution:
        """The same actor, with the fact that they are inside somebody else's
        organization made explicit.

        A person acting at *platform scope* carries no organization of their own — the
        column is nullable for exactly one kind of account — so an event of theirs that
        lands in an organization's log is, by construction, a platform administrator
        touching a customer. That covers all three routes to it: the
        ``X-Assume-Organization`` header, ``DirectoryService._narrow``, and a direct call
        with neither, which is the one a call site would forget.

        Derived rather than passed for that last reason. What a customer wants to find in
        their own log is "somebody from the vendor was in here", and a filter that depends
        on every hook remembering to say so is a filter with holes in it.
        """
        if self.actor_type != "user" or self.organization_id is not None:
            return self
        if organization_id is None:
            # A platform event: the global catalog, platform settings. Nobody's log but
            # the platform's, and not impersonation of anything.
            return self
        return replace(
            self,
            actor_type="superadmin_impersonation",
            organization_id=organization_id,
        )


# ---------------------------------------------------------------------------
# diffing
# ---------------------------------------------------------------------------


def diff(before: Snapshot | None, after: Snapshot | None) -> dict[str, Any]:
    """The changed fields between two snapshots, as ``{"changes": [...]}``.

    A change is ``{"path", "before", "after"}``. A **missing** ``before`` means the field
    did not exist — a creation — and a missing ``after`` means it no longer does; that is
    different from a present ``null``, which is a field that exists and holds nothing, and
    the two are worth telling apart when the field is a credential.

    Both sides ``None`` is not an error, it is an event with no field-level detail: a
    resync, a reindex, a support access. Such an event carries a ``summary`` instead.
    """
    changes: list[dict[str, Any]] = []
    _walk("", before, after, changes)
    changes.sort(key=lambda change: str(change["path"]))

    result: dict[str, Any] = {"changes": changes[:MAX_CHANGES]}
    if len(changes) > MAX_CHANGES:
        # Named rather than silently trimmed: a diff that says 100 fields changed when
        # 140 did is a lie, and this is the one table where that matters.
        result["omitted"] = len(changes) - MAX_CHANGES
    return result


class _Missing:
    """Sentinel for "this field is not in this snapshot", distinct from ``None``."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"


_MISSING = _Missing()


def _walk(prefix: str, before: Any, after: Any, changes: list[dict[str, Any]]) -> None:
    if isinstance(before, Mapping) or isinstance(after, Mapping):
        left: Mapping[str, Any] = before if isinstance(before, Mapping) else {}
        right: Mapping[str, Any] = after if isinstance(after, Mapping) else {}
        for key in sorted(set(left) | set(right), key=str):
            _walk(
                f"{prefix}.{key}" if prefix else str(key),
                left.get(key, _MISSING),
                right.get(key, _MISSING),
                changes,
            )
        return

    if _same(before, after):
        return

    change: dict[str, Any] = {"path": prefix}
    truncated = False
    if not isinstance(before, _Missing):
        change["before"], cut = _render(before)
        truncated = truncated or cut
    if not isinstance(after, _Missing):
        change["after"], cut = _render(after)
        truncated = truncated or cut
    if truncated:
        # Two long strings that differ past the cut would otherwise render identically,
        # which reads as a bug in the diff rather than as a value too long to show.
        change["truncated"] = True
    changes.append(change)


def _same(before: Any, after: Any) -> bool:
    if isinstance(before, Sensitive) or isinstance(after, Sensitive):
        # Compared on the fingerprint, never on the value. A field that is sensitive on
        # one side and not on the other is a bug in a snapshot function, and reporting it
        # as a change is the safe reading.
        if not (isinstance(before, Sensitive) and isinstance(after, Sensitive)):
            return False
        return before.fingerprint == after.fingerprint
    if isinstance(before, _Missing) or isinstance(after, _Missing):
        return before is after
    # The type check is what keeps ``0`` from equalling ``False`` and ``1`` from
    # equalling ``1.0`` — both of which are real transitions in a settings blob.
    return type(before) is type(after) and bool(before == after)


def _render(value: Any) -> tuple[Any, bool]:
    """A value as it is stored in the diff, and whether it had to be cut."""
    if isinstance(value, Sensitive):
        return (REDACTED if value.present else None), False
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS:
            return value[:MAX_VALUE_CHARS], True
        return value, False
    if value is None or isinstance(value, bool | int | float):
        return value, False
    if isinstance(value, uuid.UUID):
        return str(value), False
    if isinstance(value, datetime):
        return value.isoformat(), False
    if isinstance(value, Mapping):
        # Reached only for a mapping nested inside a list; a top-level one is walked.
        return {str(key): _render(item)[0] for key, item in value.items()}, False
    if isinstance(value, Sequence):
        return [_render(item)[0] for item in value], False
    return str(value), False


def summarize(count: int, sample: Iterable[Any] = (), **extra: Any) -> dict[str, Any]:
    """The payload for a bulk operation: how many, and a few examples.

    One event with a count beats N events for the same reason a log line per row would be
    unreadable — a resync that touched four hundred documents is one thing that happened,
    and the four hundred rows are on the connector's own screen.
    """
    payload: dict[str, Any] = {
        "count": count,
        "sample": [_render(item)[0] for item in list(sample)[:MAX_SAMPLE]],
    }
    payload.update({key: _render(value)[0] for key, value in extra.items()})
    return payload


# ---------------------------------------------------------------------------
# building the row
# ---------------------------------------------------------------------------


def build_event(
    by: Attribution,
    action: str,
    *,
    target: Target,
    organization_id: uuid.UUID | None,
    before: Snapshot | None = None,
    after: Snapshot | None = None,
    summary: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """One row, ready to be added to whichever session is doing the mutation."""
    payload = diff(before, after)
    if summary is not None:
        payload["summary"] = dict(summary)
    return AuditEvent(
        # Minted here rather than at flush, as every other row in this codebase is: the
        # in-memory store never flushes, and an event with no id could not be keyed.
        id=uuid7(),
        organization_id=organization_id,
        actor_user_id=by.user_id,
        actor_label=_label(by.label),
        actor_type=by.actor_type,
        action=action,
        target_type=target.type,
        target_id=target.id,
        target_label=_label(target.label),
        diff=payload,
        ip=by.ip,
        user_agent=by.user_agent,
        request_id=get_request_id(),
    )


def _label(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text[:MAX_LABEL_LENGTH] if text else None


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


class AuditFailureCounter(Protocol):
    """Counts events that could not be built. See :func:`count_audit_failures_with`."""

    def labels(self, *values: str) -> Any: ...


class _NoCounter:
    def labels(self, *values: str) -> Any:
        return self

    def inc(self, amount: float = 1) -> None:
        return None


_failures: Any = _NoCounter()


def count_audit_failures_with(counter: AuditFailureCounter) -> None:
    """Install the metric the recorder increments when it cannot build an event.

    Process-level, like :func:`app.db.scoping.install_scope_guard`, and for the same kind
    of reason: the code that has to increment this is a mixin on a transaction, reached
    from six stores that share no metrics handle between them. Threading a registry
    through all of them to count something that should never happen would cost more than
    it is worth, and a counter that is a no-op until installed is the correct default.
    """
    global _failures
    _failures = counter


class AuditingTransaction(Protocol):
    """The recording half of a store transaction.

    Every store whose transaction can change configuration inherits this, so a service
    can write ``transaction.audit(...)`` beside the change it just made and know the two
    land together. The concrete implementations are :class:`PostgresAuditRecorder` and
    :class:`MemoryAuditRecorder`; this is only the shape the protocols advertise.
    """

    def audit(
        self,
        by: Actor | Attribution,
        action: str,
        *,
        target: Target | None = None,
        organization_id: uuid.UUID | None = None,
        before: Subject | Snapshot | None = None,
        after: Subject | Snapshot | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> None: ...


class AuditRecorder:
    """Records events into whatever unit of work the concrete transaction is using.

    Mixed into every store transaction that can mutate configuration, so recording is
    literally part of the same transaction as the change — a committed mutation cannot
    lack its event, and a rolled-back one cannot leave a phantom.
    """

    def audit(
        self,
        by: Actor | Attribution,
        action: str,
        *,
        target: Target | None = None,
        organization_id: uuid.UUID | None = None,
        before: Subject | Snapshot | None = None,
        after: Subject | Snapshot | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> None:
        """Record one event. Never raises.

        ``target`` and ``organization_id`` are usually left out: the first comes from
        whichever :class:`Subject` was given, and the second from the actor's scope. Both
        are overridable for the cases where that is wrong — a superadmin at platform
        scope creating an organization writes into *that* organization's log.
        """
        try:
            attribution = Attribution.coerce(by)
            resolved = target or _target_of(after) or _target_of(before)
            if resolved is None:
                raise ValueError(f"'{action}' has no target")
            organization = (
                organization_id if organization_id is not None else attribution.organization_id
            )
            event = build_event(
                attribution.inside(organization),
                action,
                target=resolved,
                organization_id=organization,
                before=_state_of(before),
                after=_state_of(after),
                summary=summary,
            )
        except Exception:
            # A defect here loses one event; letting it propagate would lose the change
            # the user was making, which is the worse of the two.
            logger.exception("could not build an audit event", extra={"audit_action": action})
            _failures.labels(action).inc()
            return
        self._store_audit_event(event)

    def _store_audit_event(self, event: AuditEvent) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


def _target_of(source: Subject | Snapshot | None) -> Target | None:
    return source.target if isinstance(source, Subject) else None


def _state_of(source: Subject | Snapshot | None) -> Snapshot | None:
    if source is None:
        return None
    return source.state if isinstance(source, Subject) else source


class PostgresAuditRecorder(AuditRecorder):
    """Adds the row to the session the mutation is already using."""

    _session: AsyncSession

    def _store_audit_event(self, event: AuditEvent) -> None:
        self._session.add(event)


class MemoryAuditRecorder(AuditRecorder):
    """The same, into the shared in-memory rows."""

    _db: MemoryDatabase

    def _store_audit_event(self, event: AuditEvent) -> None:
        self._db.add_audit_event(event)


__all__ = [
    "MAX_CHANGES",
    "MAX_SAMPLE",
    "MAX_VALUE_CHARS",
    "REDACTED",
    "Attribution",
    "AuditRecorder",
    "AuditingTransaction",
    "MemoryAuditRecorder",
    "PostgresAuditRecorder",
    "Sensitive",
    "Snapshot",
    "Subject",
    "Target",
    "build_event",
    "count_audit_failures_with",
    "diff",
    "summarize",
]
