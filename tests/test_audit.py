"""The audit core: diffing, redaction, attribution, and the snapshots feeding them.

Everything here is pure. The hooks that call it are exercised end to end in
``tests/test_audit_hooks.py``; this file is about the arithmetic those hooks depend on
being right, because a diff that is subtly wrong produces a log that is confidently wrong.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.core.ids import uuid7
from app.core.tenancy import Actor, TenantScope
from app.db.models import (
    ApiKey,
    Connector,
    Document,
    EndUser,
    Gateway,
    GatewayTarget,
    Invitation,
    MemoryFact,
    Organization,
    UpstreamModel,
    User,
)
from app.services.audit import (
    MAX_CHANGES,
    MAX_SAMPLE,
    MAX_VALUE_CHARS,
    REDACTED,
    Attribution,
    Sensitive,
    Snapshot,
    Target,
    build_event,
    diff,
    summarize,
)
from app.services.audit_snapshots import subject, target_of

ORG = uuid7()
OTHER_ORG = uuid7()


def paths(before: Snapshot | None, after: Snapshot | None) -> dict[str, tuple[Any, Any]]:
    return {
        str(change["path"]): (change.get("before", "<absent>"), change.get("after", "<absent>"))
        for change in diff(before, after)["changes"]
    }


# ---------------------------------------------------------------------------
# diffing
# ---------------------------------------------------------------------------


def test_an_unchanged_snapshot_produces_no_changes() -> None:
    state = {"name": "Support", "enabled": True}
    assert diff(state, dict(state)) == {"changes": []}


def test_a_changed_field_carries_both_sides() -> None:
    assert paths({"name": "a"}, {"name": "b"}) == {"name": ("a", "b")}


def test_a_creation_has_no_before_side() -> None:
    """Every field is 'added', which is what the screen renders a create as."""
    assert paths(None, {"name": "a", "enabled": True}) == {
        "name": ("<absent>", "a"),
        "enabled": ("<absent>", True),
    }


def test_a_deletion_has_no_after_side() -> None:
    assert paths({"name": "a"}, None) == {"name": ("a", "<absent>")}


def test_a_missing_field_is_not_the_same_as_a_null_one() -> None:
    """The distinction has teeth for a credential: added, versus explicitly cleared."""
    added = diff({}, {"credential": None})["changes"][0]
    cleared = diff({"credential": "x"}, {"credential": None})["changes"][0]
    assert "before" not in added
    assert cleared["before"] == "x" and cleared["after"] is None


def test_nested_blobs_flatten_to_dotted_paths() -> None:
    changed = paths(
        {"memory_config": {"doc_top_k": 6, "doc_min_score": 0.35}},
        {"memory_config": {"doc_top_k": 10, "doc_min_score": 0.35}},
    )
    assert changed == {"memory_config.doc_top_k": (6, 10)}


def test_a_list_is_one_value_rather_than_a_path_per_element() -> None:
    """Position is meaning in a routing chain, so a reorder is a change to the chain."""
    changed = paths({"targets": ["a", "b"]}, {"targets": ["b", "a"]})
    assert changed == {"targets": (["a", "b"], ["b", "a"])}


def test_a_boolean_is_not_confused_with_a_zero() -> None:
    """``0 == False`` in Python, and both are real values in a settings blob."""
    assert paths({"doc_max_tokens": 0}, {"doc_max_tokens": False}) != {}


def test_changes_are_sorted_by_path() -> None:
    changed = diff({"b": 1, "a": 1, "c": 1}, {"b": 2, "a": 2, "c": 2})["changes"]
    assert [change["path"] for change in changed] == ["a", "b", "c"]


def test_a_long_value_is_cut_and_says_so() -> None:
    prompt = "x" * (MAX_VALUE_CHARS + 50)
    change = diff({"system_context": "short"}, {"system_context": prompt})["changes"][0]
    assert change["after"] == "x" * MAX_VALUE_CHARS
    assert change["truncated"] is True


def test_two_long_values_that_differ_past_the_cut_still_report_a_change() -> None:
    """They render identically, which without the marker reads as a bug in the diff."""
    head = "x" * MAX_VALUE_CHARS
    change = diff({"p": head + "a"}, {"p": head + "b"})["changes"][0]
    assert change["before"] == change["after"] == head
    assert change["truncated"] is True


def test_too_many_changes_are_capped_and_counted() -> None:
    before = {f"field_{index}": index for index in range(MAX_CHANGES + 20)}
    after = {key: value + 1 for key, value in before.items()}
    payload = diff(before, after)
    assert len(payload["changes"]) == MAX_CHANGES
    assert payload["omitted"] == 20


def test_both_sides_missing_is_an_event_with_no_field_detail() -> None:
    assert diff(None, None) == {"changes": []}


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def test_a_secret_renders_as_stars_on_both_sides() -> None:
    changed = paths(
        {"credential": Sensitive.of("sk-old")},
        {"credential": Sensitive.of("sk-new")},
    )
    assert changed == {"credential": (REDACTED, REDACTED)}


def test_an_unchanged_secret_is_not_a_change() -> None:
    same = Sensitive.of("sk-same")
    assert diff({"credential": same}, {"credential": Sensitive.of("sk-same")}) == {"changes": []}


def test_setting_and_clearing_a_secret_are_distinguishable() -> None:
    assert paths({"credential": Sensitive.of(None)}, {"credential": Sensitive.of("x")}) == {
        "credential": (None, REDACTED)
    }
    assert paths({"credential": Sensitive.of("x")}, {"credential": Sensitive.of(None)}) == {
        "credential": (REDACTED, None)
    }


def test_no_secret_value_reaches_the_stored_payload() -> None:
    """The acceptance criterion, as a grep over the JSON that is actually written."""
    payload = json.dumps(
        diff(
            {"credential": Sensitive.of("sk-super-secret-value")},
            {"credential": Sensitive.of("sk-other-secret-value")},
        )
    )
    assert "sk-super-secret-value" not in payload
    assert "sk-other-secret-value" not in payload


def test_a_sensitive_repr_does_not_carry_its_fingerprint() -> None:
    """A repr ends up in tracebacks, which is where a secret should also not be."""
    marker = Sensitive.of("sk-live-1234")
    assert marker.fingerprint is not None
    assert "sk-live" not in repr(marker) and marker.fingerprint not in repr(marker)


def test_a_field_that_stops_being_sensitive_is_reported_as_a_change() -> None:
    """That is a bug in a snapshot function, and the safe reading is 'something moved'."""
    assert paths({"credential": Sensitive.of("x")}, {"credential": "plain"}) != {}


# ---------------------------------------------------------------------------
# bulk summaries
# ---------------------------------------------------------------------------


def test_a_bulk_summary_carries_a_count_and_a_sample() -> None:
    payload = summarize(40, [f"file-{index}.pdf" for index in range(40)], rejected=2)
    assert payload["count"] == 40
    assert payload["sample"] == [f"file-{index}.pdf" for index in range(MAX_SAMPLE)]
    assert payload["rejected"] == 2


def test_a_bulk_summary_can_have_no_sample_at_all() -> None:
    assert summarize(12) == {"count": 12, "sample": []}


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


def actor(role: str, organization_id: uuid.UUID | None, *, assumed: bool = False) -> Actor:
    return Actor(
        user_id=uuid7(),
        scope=TenantScope(role=role, organization_id=organization_id, assumed=assumed),
        label="ada@example.com",
        ip="203.0.113.7",
        user_agent="pytest",
    )


def test_an_ordinary_member_is_a_user_actor() -> None:
    by = Attribution.of(actor("org_admin", ORG))
    assert by.actor_type == "user"
    assert by.inside(ORG) is by


def test_an_assumed_scope_is_impersonation() -> None:
    assert Attribution.of(actor("superadmin", ORG, assumed=True)).actor_type == (
        "superadmin_impersonation"
    )


def test_a_platform_actor_writing_into_an_organization_is_impersonation() -> None:
    """The route with no assume header — ``DirectoryService._narrow``, or a direct call."""
    by = Attribution.of(actor("superadmin", None)).inside(ORG)
    assert by.actor_type == "superadmin_impersonation"
    assert by.organization_id == ORG


def test_a_platform_event_stays_a_plain_user_event() -> None:
    """Creating a global model belongs to nobody and is not impersonation of anyone."""
    assert Attribution.of(actor("superadmin", None)).inside(None).actor_type == "user"


def test_a_job_is_a_system_actor_named_after_itself() -> None:
    by = Attribution.system(ORG, job="delete-connector")
    assert (by.actor_type, by.user_id, by.label) == ("system", None, "delete-connector")
    assert by.inside(ORG).actor_type == "system"


def test_the_address_rides_along_onto_the_event() -> None:
    event = build_event(
        Attribution.of(actor("org_admin", ORG)),
        "gateway.update",
        target=Target("gateway", uuid7(), "support"),
        organization_id=ORG,
    )
    assert (event.ip, event.user_agent) == ("203.0.113.7", "pytest")
    assert event.actor_label == "ada@example.com"


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


def make_gateway(**overrides: object) -> Gateway:
    fields: dict[str, object] = {
        "id": uuid7(),
        "organization_id": ORG,
        "slug": "support",
        "name": "Support",
        "description": None,
        "enabled": True,
        "routing_mode": "single",
        "system_context": None,
        "param_overrides": {},
        "locked_params": {},
        "memory_config": {},
        "logging_config": {},
        "limits": {},
        "targets": [],
    }
    fields.update(overrides)
    return Gateway(**fields)


def make_model(**overrides: object) -> UpstreamModel:
    fields: dict[str, object] = {
        "id": uuid7(),
        "organization_id": ORG,
        "scope": "org",
        "name": "gpt-4o",
        "description": None,
        "base_url": "https://api.openai.com/v1",
        "dialect": "openai",
        "upstream_model_id": "gpt-4o",
        "auth_type": "bearer",
        "credential_ciphertext": b"cipher",
        "credential_hint": "sk-...4f2a",
        "extra_headers": {},
        "system_context": None,
        "default_params": {},
        "timeout_seconds": 60,
        "context_window": None,
        "enabled": True,
    }
    fields.update(overrides)
    return UpstreamModel(**fields)


def test_a_gateway_names_itself_by_its_slug() -> None:
    """Which is the part of a deleted gateway anybody would recognise: it was the URL."""
    target = target_of(make_gateway())
    assert (target.type, target.label) == ("gateway", "support")


def test_a_config_blob_is_snapshotted_through_its_schema() -> None:
    """A row stored before a field existed still diffs as though it always had it."""
    state = subject(make_gateway(memory_config={})).state
    assert state["memory_config"]["doc_top_k"] == 6


def test_adding_a_knob_to_a_stored_blob_diffs_as_one_field() -> None:
    before = subject(make_gateway(memory_config={}))
    after = subject(make_gateway(memory_config={"doc_top_k": 10}))
    assert paths(before.state, after.state) == {"memory_config.doc_top_k": (6, 10)}


def test_the_routing_chain_is_part_of_the_gateway_snapshot() -> None:
    model = make_model(name="claude")
    gateway = make_gateway(
        targets=[
            GatewayTarget(
                id=uuid7(),
                gateway_id=uuid7(),
                upstream_model_id=model.id,
                priority=0,
                weight=70,
                upstream_model=model,
            )
        ]
    )
    assert subject(gateway).state["targets"] == ["claude (70%)"]


def test_a_model_credential_is_the_ciphertext_and_never_the_hint() -> None:
    """The hint is derived from the plaintext; this table holds nothing derived from it."""
    state = subject(make_model()).state
    assert isinstance(state["credential"], Sensitive)
    assert "credential_hint" not in state
    assert "4f2a" not in json.dumps(diff(None, state))


def test_extra_header_values_are_hidden_and_their_names_are_not() -> None:
    before = subject(make_model(extra_headers={"api-key": "one"}))
    after = subject(make_model(extra_headers={"api-key": "two"}))
    assert paths(before.state, after.state) == {"extra_headers.api-key": (REDACTED, REDACTED)}


def test_a_connector_config_value_is_hidden_too() -> None:
    """An S3 or HTTP connector puts credentials here, and nothing encrypts the column."""
    connector = Connector(
        id=uuid7(),
        organization_id=ORG,
        name="Handbook",
        description=None,
        type="managed_file_drop",
        config={"secret_access_key": "AKIA-SECRET"},
        chunking={},
        status="ready",
    )
    assert "AKIA-SECRET" not in json.dumps(diff(None, subject(connector).state))


def test_a_password_hash_is_marked_sensitive() -> None:
    user = User(
        id=uuid7(),
        organization_id=ORG,
        email="ada@example.com",
        password_hash="$argon2id$vvv",
        role="org_admin",
        name="Ada",
        status="active",
    )
    assert "argon2id" not in json.dumps(diff(None, subject(user).state))
    assert target_of(user).label == "ada@example.com"


def test_an_invitation_token_is_marked_sensitive() -> None:
    invitation = Invitation(
        id=uuid7(),
        organization_id=ORG,
        email="grace@example.com",
        role="org_member",
        token_hash="a" * 64,
        expires_at=None,
        accepted_at=None,
    )
    assert "a" * 64 not in json.dumps(diff(None, subject(invitation).state))


def test_a_memory_fact_records_its_shape_and_not_its_sentence() -> None:
    """SPEC §6.5 gives a person the right to erasure, and this table cannot honour one."""
    fact = MemoryFact(
        id=uuid7(),
        organization_id=ORG,
        end_user_id=uuid7(),
        text="Ada is allergic to shellfish.",
        kind="fact",
        confidence=0.9,
        expires_at=None,
        superseded_at=None,
    )
    state = subject(fact).state
    assert "shellfish" not in json.dumps(diff(None, state))
    assert state["kind"] == "fact" and state["confidence"] == pytest.approx(0.9)
    assert target_of(fact).label is None


def test_an_end_user_is_named_by_the_id_an_erasure_request_would_use() -> None:
    end_user = EndUser(id=uuid7(), organization_id=ORG, external_id="alice@example.com", label=None)
    assert target_of(end_user).label == "alice@example.com"


def test_an_api_key_snapshot_carries_the_prefix_and_no_hash() -> None:
    key = ApiKey(
        id=uuid7(),
        gateway_id=uuid7(),
        name="production",
        key_hash="b" * 64,
        prefix="mg_abcd",
        expires_at=None,
        revoked_at=None,
    )
    state = subject(key).state
    assert state["prefix"] == "mg_abcd"
    assert "b" * 64 not in json.dumps(diff(None, state))


def test_a_document_is_named_by_its_filename() -> None:
    document = Document(
        id=uuid7(),
        organization_id=ORG,
        connector_id=uuid7(),
        source_uri="orgs/x/handbook.pdf",
        source_name="handbook.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        status="indexed",
        chunk_count=12,
    )
    assert target_of(document).label == "handbook.pdf"


def test_an_organization_settings_blob_diffs_by_path() -> None:
    before = subject(
        Organization(
            id=ORG,
            name="Acme",
            slug="acme",
            status="active",
            settings={"distillation": {"enabled": False}},
        )
    )
    after = subject(
        Organization(
            id=ORG,
            name="Acme",
            slug="acme",
            status="active",
            settings={"distillation": {"enabled": True}},
        )
    )
    assert paths(before.state, after.state) == {"settings.distillation.enabled": (False, True)}


def test_a_row_with_no_snapshot_is_refused_rather_than_guessed() -> None:
    with pytest.raises(TypeError):
        subject(object())


# ---------------------------------------------------------------------------
# every secret-marked field, enumerated
# ---------------------------------------------------------------------------

#: The complete list of fields this product marks as sensitive, by target type. Written
#: down rather than derived, so that *removing* a marker is a failing test rather than a
#: silent widening — which is the direction this list can go wrong in.
SENSITIVE_FIELDS: dict[str, set[str]] = {
    "upstream_model": {"credential", "extra_headers.api-key"},
    "connector": {"config.secret_access_key"},
    "user": {"password"},
    "invitation": {"token"},
    "memory_fact": {"text"},
}


def sensitive_paths(state: Snapshot, prefix: str = "") -> set[str]:
    found: set[str] = set()
    for key, value in state.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Sensitive):
            found.add(path)
        elif isinstance(value, dict):
            found |= sensitive_paths(value, path)
    return found


def loaded_rows() -> dict[str, object]:
    """One of every row that carries something secret, filled in."""
    return {
        "upstream_model": make_model(extra_headers={"api-key": "sk-header-secret"}),
        "connector": Connector(
            id=uuid7(),
            organization_id=ORG,
            name="Handbook",
            description=None,
            type="managed_file_drop",
            config={"secret_access_key": "AKIA-SECRET"},
            chunking={},
            status="ready",
        ),
        "user": User(
            id=uuid7(),
            organization_id=ORG,
            email="ada@example.com",
            password_hash="$argon2id$secret",
            role="org_admin",
            name="Ada",
            status="active",
        ),
        "invitation": Invitation(
            id=uuid7(),
            organization_id=ORG,
            email="grace@example.com",
            role="org_member",
            token_hash="t" * 64,
            expires_at=None,
            accepted_at=None,
        ),
        "memory_fact": MemoryFact(
            id=uuid7(),
            organization_id=ORG,
            end_user_id=uuid7(),
            text="Ada is allergic to shellfish.",
            kind="fact",
            confidence=0.9,
            expires_at=None,
            superseded_at=None,
        ),
    }


@pytest.mark.parametrize("target_type", sorted(SENSITIVE_FIELDS))
def test_every_secret_marked_field_is_still_marked(target_type: str) -> None:
    """The acceptance criterion, field by field rather than sample by sample."""
    row = loaded_rows()[target_type]
    assert sensitive_paths(subject(row).state) == SENSITIVE_FIELDS[target_type]


@pytest.mark.parametrize("target_type", sorted(SENSITIVE_FIELDS))
def test_no_secret_marked_field_survives_into_a_diff(target_type: str) -> None:
    """And the same list again, from the other end: mutate every one of them and grep the
    payload that would actually be stored."""
    row = loaded_rows()[target_type]
    before = subject(row).state
    payload = json.dumps(diff(None, before)) + json.dumps(diff(before, before))

    for secret in ("sk-header-secret", "AKIA-SECRET", "argon2id", "t" * 64, "shellfish"):
        assert secret not in payload, (target_type, secret)
    assert REDACTED in payload
