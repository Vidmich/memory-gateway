"""What each kind of row looks like in an audit event.

One function per aggregate, all of them returning a :class:`~app.services.audit.Subject`
— the target that names the row and the state the diff is computed from — and one
dispatcher, :func:`subject`, so a call site says ``subject(gateway)`` rather than
remembering which builder to use.

Three rules run through every function here.

**Configuration blobs go through their Pydantic schema, not through the raw JSONB.** A row
stored before a field existed has no key for it; the schema fills the default, so the diff
of a save that touched nothing else is empty instead of inventing a change for every
knob added since the row was written. It is also what makes a path readable:
``memory_config.doc_top_k``, not ``memory_config`` with two documents in it.

**Every value that could be a secret is marked, whatever it currently holds.** The
credential ciphertext, obviously. But also the *values* of ``extra_headers`` and of a
connector's ``config``: those are free-form maps that already have somewhere for an
``api-key`` to go, and an immutable table is the wrong place to discover that somebody
used them for one. Keys stay visible, so ``extra_headers.api-key: "***" → "***"`` still
says which header changed.

**End-user content stays out.** A memory fact's *text* is a sentence about a person, and
SPEC §6.5 gives that person the right to have it erased — which is the one thing an
append-only table cannot do. So the fact's text is marked sensitive and the event records
the shape of the edit instead: which fact, by whom, and what happened to its kind,
confidence and expiry. The external id is a different question and is recorded, because
an erasure request is *made* with it: a log that cannot say whose memory was purged
cannot be used to show that it was.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.db.models import (
    ApiKey,
    Connector,
    Document,
    EndUser,
    EvaluationItem,
    EvaluationRun,
    EvaluationSet,
    Gateway,
    Invitation,
    MemoryFact,
    Organization,
    UpstreamModel,
    User,
)
from app.schemas.connector_config import ChunkingConfig
from app.schemas.gateway_config import LimitsConfig, LoggingConfig, MemoryConfig
from app.schemas.summarization import SummarizationConfig
from app.services.audit import Sensitive, Snapshot, Subject, Target


def _opaque_map(values: Mapping[str, Any] | None) -> dict[str, Sensitive]:
    """A free-form map with its values hidden and its keys kept.

    See the module docstring: ``extra_headers`` and a connector's ``config`` are the two
    places an operator can put a credential into a column nothing encrypts.
    """
    return {str(key): Sensitive.of(value) for key, value in (values or {}).items()}


def _blob(schema: type[Any], stored: Mapping[str, Any] | None) -> dict[str, Any]:
    """A stored JSONB blob as its schema sees it, defaults filled in."""
    return dict(schema.load(stored).model_dump(mode="json"))


# ---------------------------------------------------------------------------
# organizations, members, invitations
# ---------------------------------------------------------------------------


def organization_subject(organization: Organization) -> Subject:
    return Subject(
        target=Target("organization", organization.id, organization.name),
        state={
            "name": organization.name,
            "slug": organization.slug,
            "status": organization.status,
            # Settings are walked as a nested map, so an org-level logging default or a
            # distillation switch renders as `settings.distillation.enabled`.
            "settings": dict(organization.settings or {}),
        },
    )


def user_subject(user: User) -> Subject:
    return Subject(
        target=Target("user", user.id, user.email),
        state={
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "status": user.status,
            # Marked rather than omitted: "the password changed" is exactly the kind of
            # thing this log is read for, and the hash is still a secret.
            "password": Sensitive.of(user.password_hash),
        },
    )


def invitation_subject(invitation: Invitation) -> Subject:
    return Subject(
        target=Target("invitation", invitation.id, invitation.email),
        state={
            "email": invitation.email,
            "role": invitation.role,
            "expires_at": invitation.expires_at,
            "accepted_at": invitation.accepted_at,
            # The link is a bearer credential handed out by email. A resend mints a new
            # one, and this is how the log says so without saying what it is.
            "token": Sensitive.of(invitation.token_hash),
        },
    )


# ---------------------------------------------------------------------------
# the model catalog
# ---------------------------------------------------------------------------


def model_subject(model: UpstreamModel) -> Subject:
    return Subject(
        target=Target("upstream_model", model.id, model.name),
        state={
            "name": model.name,
            "description": model.description,
            "scope": model.scope,
            "base_url": model.base_url,
            "dialect": model.dialect,
            "upstream_model_id": model.upstream_model_id,
            "auth_type": model.auth_type,
            # The ciphertext, so rotation shows as a change; never the plaintext, and not
            # the display hint either — a hint is derived from the plaintext, and this
            # table is the one place that should hold nothing derived from it at all.
            "credential": Sensitive.of(model.credential_ciphertext),
            "extra_headers": _opaque_map(model.extra_headers),
            "system_context": model.system_context,
            "default_params": dict(model.default_params or {}),
            "timeout_seconds": model.timeout_seconds,
            "context_window": model.context_window,
            "tokenizer": dict(model.tokenizer) if model.tokenizer else None,
            "enabled": model.enabled,
        },
    )


# ---------------------------------------------------------------------------
# connectors and documents
# ---------------------------------------------------------------------------


def connector_subject(connector: Connector) -> Subject:
    return Subject(
        target=Target("connector", connector.id, connector.name),
        state={
            "name": connector.name,
            "description": connector.description,
            "type": connector.type,
            "status": connector.status,
            "config": _opaque_map(connector.config),
            "chunking": _blob(ChunkingConfig, connector.chunking),
            # Task 102: a mode change is a spending decision and, under `contextual`, a
            # reindex — both worth a line in the trail that names `summarization.mode`.
            "summarization": _blob(SummarizationConfig, connector.summarization),
        },
    )


def document_subject(document: Document) -> Subject:
    return Subject(
        target=Target("document", document.id, document.source_name),
        state={
            "source_name": document.source_name,
            "source_uri": document.source_uri,
            "mime_type": document.mime_type,
            "size_bytes": document.size_bytes,
            "status": document.status,
            "chunk_count": document.chunk_count,
            # Task 102. The summary is content an operator can rewrite, and the diff of
            # an edit is what says it was rewritten rather than regenerated.
            "summary": document.summary,
            "summary_model": document.summary_model,
        },
    )


# ---------------------------------------------------------------------------
# gateways and keys
# ---------------------------------------------------------------------------


def gateway_subject(gateway: Gateway) -> Subject:
    """The whole endpoint, routing chain included.

    The chain is one value rather than one path per link. Position *is* meaning here —
    index 0 is the primary in a failover — so a diff that reported ``targets.0.weight``
    after a reorder would be describing something other than what happened; a reordered
    chain is a changed chain, and reads as one.
    """
    return Subject(
        target=Target("gateway", gateway.id, gateway.slug),
        state={
            "slug": gateway.slug,
            "name": gateway.name,
            "description": gateway.description,
            "enabled": gateway.enabled,
            "routing_mode": gateway.routing_mode,
            "targets": [
                f"{target.upstream_model.name} ({target.weight}%)"
                if target.upstream_model is not None
                else str(target.upstream_model_id)
                for target in sorted(gateway.targets or [], key=lambda row: row.priority)
            ],
            "system_context": gateway.system_context,
            "param_overrides": dict(gateway.param_overrides or {}),
            "locked_params": dict(gateway.locked_params or {}),
            "memory_config": _blob(MemoryConfig, gateway.memory_config),
            "logging_config": _blob(LoggingConfig, gateway.logging_config),
            "limits": _blob(LimitsConfig, gateway.limits),
        },
    )


def api_key_subject(key: ApiKey) -> Subject:
    return Subject(
        target=Target("api_key", key.id, key.name),
        state={
            "name": key.name,
            # Safe and useful: the prefix is the display form the UI shows forever, and it
            # is what somebody matches against the key in a client's configuration.
            "prefix": key.prefix,
            "expires_at": key.expires_at,
            "revoked_at": key.revoked_at,
        },
    )


# ---------------------------------------------------------------------------
# conversation memory
# ---------------------------------------------------------------------------


def end_user_subject(end_user: EndUser) -> Subject:
    return Subject(
        target=Target("end_user", end_user.id, end_user.external_id),
        state={
            "external_id": end_user.external_id,
            "label": end_user.label,
        },
    )


def fact_subject(fact: MemoryFact) -> Subject:
    """A memory fact, without the sentence.

    No label either: a fact has no name, and the only string that could stand in for one
    is the text this deliberately does not store. The row is found through ``target_id``,
    and the memory browser is where the content lives — which is also the table an
    erasure request can actually empty.
    """
    return Subject(
        target=Target("memory_fact", fact.id, None),
        state={
            "text": Sensitive.of(fact.text),
            "kind": fact.kind,
            "confidence": float(fact.confidence),
            "expires_at": fact.expires_at,
            "superseded": fact.superseded_at is not None,
        },
    )


def evaluation_set_subject(row: EvaluationSet) -> Subject:
    """An evaluation set (task 103): a name and a description, nothing that is content."""
    return Subject(
        target=Target("evaluation_set", row.id, row.name),
        state={"name": row.name, "description": row.description, "gateway_id": str(row.gateway_id)},
    )


def evaluation_item_subject(row: EvaluationItem) -> Subject:
    """An evaluation item, without the question.

    The question is usually an end user's message, imported from the log, and the same
    rule as a memory fact applies: content that a person may ask to have erased does not
    go into an append-only table. What is recorded is the shape of the label — how many
    chunks and documents, which source, whether verified — which is what a diff of a
    labelling decision needs.
    """
    return Subject(
        target=Target("evaluation_item", row.id, None),
        state={
            "question": Sensitive.of(row.question),
            "set_id": str(row.set_id),
            "relevant_chunks": sorted(
                str(entry.get("chunk_id"))
                for entry in (row.relevant or [])
                if isinstance(entry, dict)
            ),
            "relevant_document_ids": sorted(str(v) for v in (row.relevant_document_ids or [])),
            "source": row.source,
            "verified": row.verified,
            "notes": row.notes,
        },
    )


def evaluation_run_subject(row: EvaluationRun) -> Subject:
    return Subject(
        target=Target("evaluation_run", row.id, None),
        state={"set_id": str(row.set_id), "status": row.status, "patch": dict(row.patch or {})},
    )


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_BUILDERS: dict[type[Any], Any] = {
    ApiKey: api_key_subject,
    Connector: connector_subject,
    Document: document_subject,
    EndUser: end_user_subject,
    EvaluationItem: evaluation_item_subject,
    EvaluationRun: evaluation_run_subject,
    EvaluationSet: evaluation_set_subject,
    Gateway: gateway_subject,
    Invitation: invitation_subject,
    MemoryFact: fact_subject,
    Organization: organization_subject,
    UpstreamModel: model_subject,
    User: user_subject,
}


def subject(row: Any) -> Subject:
    """The audit view of a mapped row: what it is, and what it currently holds.

    Call it twice around a mutation — once before, once after — and hand both to
    :meth:`~app.services.audit.AuditRecorder.audit`. The returned state is a fresh tree of
    plain values, so the first call is not quietly rewritten by the second.
    """
    builder = _BUILDERS.get(type(row))
    if builder is None:
        raise TypeError(f"no audit snapshot for {type(row).__name__}")
    result: Subject = builder(row)
    return result


def target_of(row: Any) -> Target:
    """Just the naming half, for an event that has no field-level diff — a resync, a
    reindex, a purge. Cheap enough that it goes through the same builder."""
    return subject(row).target


__all__ = [
    "Snapshot",
    "Subject",
    "Target",
    "api_key_subject",
    "connector_subject",
    "document_subject",
    "end_user_subject",
    "evaluation_item_subject",
    "evaluation_run_subject",
    "evaluation_set_subject",
    "fact_subject",
    "gateway_subject",
    "invitation_subject",
    "model_subject",
    "organization_subject",
    "subject",
    "target_of",
    "user_subject",
]
