"""``/api/v1/end-users`` and ``/api/v1/memory-facts`` over the real app.

The service tests cover the rules; these cover the envelope — status codes, capability
gates, and the two shapes most likely to be "simplified" later into something wrong.

``DELETE .../memory`` returns a body. 204 would be tidier and would throw away the only
evidence an erasure produces: how much it removed. This is the request somebody will be
asked about afterwards.

``PATCH /memory-facts/{id}`` distinguishes ``expires_at: null`` from an omitted
``expires_at``. They arrive as the same ``None`` and mean opposite things, so the
distinction lives in ``model_fields_set`` and is asserted here rather than trusted.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import AuthHarness


@pytest.fixture
async def token(auth_harness: AuthHarness) -> str:
    return await auth_harness.sign_in()


async def seed(harness: AuthHarness, external_id: str = "alice") -> uuid.UUID:
    """An end user, created the way a request through the data plane would create one."""
    fixture = harness.auth.end_users
    assert fixture is not None
    return (await fixture.end_user(external_id)).id


async def add(
    harness: AuthHarness, token: str, end_user_id: uuid.UUID, **body: Any
) -> dict[str, Any]:
    response = await harness.client.post(
        f"/api/v1/end-users/{end_user_id}/memory",
        headers=harness.bearer(token),
        json={"text": "Works in the EU and needs GDPR-compliant answers.", **body},
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


# ---------------------------------------------------------------------------
# the list
# ---------------------------------------------------------------------------


async def test_the_list_carries_counts_and_timestamps(
    auth_harness: AuthHarness, token: str
) -> None:
    end_user_id = await seed(auth_harness)
    await add(auth_harness, token, end_user_id)

    response = await auth_harness.client.get(
        "/api/v1/end-users", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200
    (row,) = [item for item in response.json()["items"] if item["id"] == str(end_user_id)]
    assert row["external_id"] == "alice"
    assert row["fact_count"] == 1
    assert row["anonymous"] is False
    assert row["first_seen_at"] and row["last_seen_at"]


async def test_an_anonymous_identity_is_flagged(auth_harness: AuthHarness, token: str) -> None:
    """An operator looking at a page of ``anon:…`` rows should be able to see at a glance
    that the customer's integration is not sending an identity."""
    await seed(auth_harness, "anon:abc123def4567890")

    response = await auth_harness.client.get(
        "/api/v1/end-users?search=anon", headers=auth_harness.bearer(token)
    )

    assert [item["anonymous"] for item in response.json()["items"]] == [True]


async def test_the_list_can_be_searched(auth_harness: AuthHarness, token: str) -> None:
    await seed(auth_harness, "alice@example.com")
    await seed(auth_harness, "bob@example.com")

    response = await auth_harness.client.get(
        "/api/v1/end-users?search=bob", headers=auth_harness.bearer(token)
    )

    assert [item["external_id"] for item in response.json()["items"]] == ["bob@example.com"]


async def test_one_end_user_can_be_fetched(auth_harness: AuthHarness, token: str) -> None:
    end_user_id = await seed(auth_harness)

    response = await auth_harness.client.get(
        f"/api/v1/end-users/{end_user_id}", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200
    assert response.json()["external_id"] == "alice"


async def test_an_unknown_end_user_is_a_404(auth_harness: AuthHarness, token: str) -> None:
    response = await auth_harness.client.get(
        f"/api/v1/end-users/{uuid.uuid4()}", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------


async def test_a_fact_is_created_with_full_confidence(
    auth_harness: AuthHarness, token: str
) -> None:
    """A person typed it; nothing the model infers should outrank that."""
    end_user_id = await seed(auth_harness)

    body = await add(auth_harness, token, end_user_id)

    assert body["confidence"] == 1.0
    assert body["kind"] == "fact"
    assert body["superseded_at"] is None
    assert body["end_user_id"] == str(end_user_id)


async def test_an_unknown_kind_is_a_422(auth_harness: AuthHarness, token: str) -> None:
    end_user_id = await seed(auth_harness)

    response = await auth_harness.client.post(
        f"/api/v1/end-users/{end_user_id}/memory",
        headers=auth_harness.bearer(token),
        json={"text": "Something.", "kind": "rumour"},
    )

    assert response.status_code == 422


async def test_an_unknown_field_is_refused_rather_than_ignored(
    auth_harness: AuthHarness, token: str
) -> None:
    end_user_id = await seed(auth_harness)

    response = await auth_harness.client.post(
        f"/api/v1/end-users/{end_user_id}/memory",
        headers=auth_harness.bearer(token),
        json={"text": "Something.", "confidance": 0.5},
    )

    assert response.status_code == 422


async def test_facts_are_listed_newest_first(auth_harness: AuthHarness, token: str) -> None:
    end_user_id = await seed(auth_harness)
    await add(auth_harness, token, end_user_id, text="First.")
    await add(auth_harness, token, end_user_id, text="Second.")

    response = await auth_harness.client.get(
        f"/api/v1/end-users/{end_user_id}/memory", headers=auth_harness.bearer(token)
    )

    assert [item["text"] for item in response.json()["items"]] == ["Second.", "First."]


async def test_the_live_only_filter_hides_retracted_facts(
    auth_harness: AuthHarness, token: str
) -> None:
    end_user_id = await seed(auth_harness)
    kept = await add(auth_harness, token, end_user_id, text="Kept.")
    gone = await add(auth_harness, token, end_user_id, text="Retracted.")
    await auth_harness.client.patch(
        f"/api/v1/memory-facts/{gone['id']}",
        headers=auth_harness.bearer(token),
        json={"superseded": True},
    )

    everything = await auth_harness.client.get(
        f"/api/v1/end-users/{end_user_id}/memory", headers=auth_harness.bearer(token)
    )
    live = await auth_harness.client.get(
        f"/api/v1/end-users/{end_user_id}/memory?live_only=true",
        headers=auth_harness.bearer(token),
    )

    assert len(everything.json()["items"]) == 2
    assert [item["id"] for item in live.json()["items"]] == [kept["id"]]


async def test_retracting_a_fact_stamps_the_date(auth_harness: AuthHarness, token: str) -> None:
    end_user_id = await seed(auth_harness)
    fact = await add(auth_harness, token, end_user_id)

    response = await auth_harness.client.patch(
        f"/api/v1/memory-facts/{fact['id']}",
        headers=auth_harness.bearer(token),
        json={"superseded": True},
    )

    assert response.status_code == 200
    assert response.json()["superseded_at"] is not None


async def test_an_omitted_expiry_is_left_alone_and_an_explicit_null_clears_it(
    auth_harness: AuthHarness, token: str
) -> None:
    end_user_id = await seed(auth_harness)
    fact = await add(auth_harness, token, end_user_id, expires_at="2099-01-01T00:00:00Z")

    untouched = await auth_harness.client.patch(
        f"/api/v1/memory-facts/{fact['id']}",
        headers=auth_harness.bearer(token),
        json={"text": "Still true."},
    )
    cleared = await auth_harness.client.patch(
        f"/api/v1/memory-facts/{fact['id']}",
        headers=auth_harness.bearer(token),
        json={"expires_at": None},
    )

    assert untouched.json()["expires_at"] is not None
    assert cleared.json()["expires_at"] is None


async def test_deleting_a_fact_answers_204(auth_harness: AuthHarness, token: str) -> None:
    end_user_id = await seed(auth_harness)
    fact = await add(auth_harness, token, end_user_id)

    response = await auth_harness.client.delete(
        f"/api/v1/memory-facts/{fact['id']}", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 204
    listing = await auth_harness.client.get(
        f"/api/v1/end-users/{end_user_id}/memory", headers=auth_harness.bearer(token)
    )
    assert listing.json()["items"] == []


# ---------------------------------------------------------------------------
# search and purge
# ---------------------------------------------------------------------------


async def test_search_returns_scored_hits_and_names_the_embedding_model(
    auth_harness: AuthHarness, token: str
) -> None:
    end_user_id = await seed(auth_harness)
    await add(auth_harness, token, end_user_id, text="Refunds go to the billing team.")
    await add(auth_harness, token, end_user_id, text="Likes cats.")

    response = await auth_harness.client.post(
        f"/api/v1/end-users/{end_user_id}/memory/search",
        headers=auth_harness.bearer(token),
        json={"query": "refunds billing"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["hits"][0]["fact"]["text"] == "Refunds go to the billing team."
    assert body["hits"][0]["score"] > 0
    assert body["embedding_model"]


async def test_a_purge_says_what_it_removed(auth_harness: AuthHarness, token: str) -> None:
    end_user_id = await seed(auth_harness)
    await add(auth_harness, token, end_user_id, text="One.")
    await add(auth_harness, token, end_user_id, text="Two.")

    response = await auth_harness.client.delete(
        f"/api/v1/end-users/{end_user_id}/memory", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200
    assert response.json() == {"facts": 2, "transcripts": 0}


async def test_a_purge_leaves_the_end_user_row(auth_harness: AuthHarness, token: str) -> None:
    """Erasure forgets what was learned; it does not deny that the requests happened."""
    end_user_id = await seed(auth_harness)
    await add(auth_harness, token, end_user_id)

    await auth_harness.client.delete(
        f"/api/v1/end-users/{end_user_id}/memory", headers=auth_harness.bearer(token)
    )
    response = await auth_harness.client.get(
        f"/api/v1/end-users/{end_user_id}", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200
    assert response.json()["fact_count"] == 0


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


async def test_a_viewer_may_read_memory(auth_harness: AuthHarness) -> None:
    """The person triaging "the assistant told my customer the wrong thing" needs to see
    what it believed, and should not need the permission to reconfigure production."""
    auth_harness.auth.user.role = "org_viewer"
    token = await auth_harness.sign_in()
    end_user_id = await seed(auth_harness)

    response = await auth_harness.client.get(
        f"/api/v1/end-users/{end_user_id}/memory", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200


async def test_a_viewer_may_not_write_memory(auth_harness: AuthHarness) -> None:
    auth_harness.auth.user.role = "org_viewer"
    token = await auth_harness.sign_in()
    end_user_id = await seed(auth_harness)

    response = await auth_harness.client.post(
        f"/api/v1/end-users/{end_user_id}/memory",
        headers=auth_harness.bearer(token),
        json={"text": "Added by a viewer."},
    )

    assert response.status_code == 403


async def test_a_viewer_may_not_purge_memory(auth_harness: AuthHarness) -> None:
    auth_harness.auth.user.role = "org_viewer"
    token = await auth_harness.sign_in()
    end_user_id = await seed(auth_harness)

    response = await auth_harness.client.delete(
        f"/api/v1/end-users/{end_user_id}/memory", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 403


async def test_memory_is_not_readable_without_a_token(auth_harness: AuthHarness) -> None:
    end_user_id = await seed(auth_harness)

    response = await auth_harness.client.get(f"/api/v1/end-users/{end_user_id}/memory")

    assert response.status_code == 401
