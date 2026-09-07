"""``/api/v1/auth`` over HTTP.

The app is the real one — routing, cookies, error envelopes, the authenticated-by-default
router. Only the auth service's storage is in memory.
"""

from __future__ import annotations

from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from app.api.control.auth import REFRESH_COOKIE, REFRESH_COOKIE_PATH
from app.core.config import get_settings
from app.core.tokens import decode_access_token
from tests.auth_support import EMAIL, PASSWORD, build_auth
from tests.conftest import AuthHarness, build_auth_app

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"
PASSWORD_URL = "/api/v1/auth/password"

NEW_PASSWORD = "a-brand-new-password"


def _hold(harness: AuthHarness, value: str) -> None:
    """Put a specific refresh token in the client's jar.

    Per-request ``cookies=`` is deprecated in httpx, and the jar is where a browser
    would keep it anyway.
    """
    harness.client.cookies.set(REFRESH_COOKIE, value, path=REFRESH_COOKIE_PATH)


def set_cookie(response: Response) -> SimpleCookie:
    """Parse the response's Set-Cookie so the flags can be asserted on directly."""
    jar = SimpleCookie()
    jar.load(response.headers["set-cookie"])
    return jar


# -- login -------------------------------------------------------------------


async def test_login_returns_an_access_token(auth_harness: AuthHarness) -> None:
    response = await auth_harness.login()

    assert response.status_code == 200
    body = response.json()
    claims = decode_access_token(body["access_token"], auth_harness.auth.settings)
    assert claims.user_id == auth_harness.auth.user.id
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0


async def test_login_describes_the_user_and_organization(auth_harness: AuthHarness) -> None:
    body = (await auth_harness.login()).json()

    assert body["user"]["email"] == EMAIL
    assert body["user"]["role"] == "org_admin"
    assert body["user"]["organization"]["slug"] == "acme"


async def test_the_response_body_never_carries_the_refresh_token(
    auth_harness: AuthHarness,
) -> None:
    """It belongs in an httpOnly cookie; putting it in the body hands it to any XSS."""
    response = await auth_harness.login()
    cookie = set_cookie(response)[REFRESH_COOKIE].value

    assert cookie not in response.text


async def test_the_refresh_cookie_is_httponly_and_scoped(auth_harness: AuthHarness) -> None:
    morsel = set_cookie(await auth_harness.login())[REFRESH_COOKIE]

    assert morsel["httponly"]
    assert morsel["samesite"].lower() == "lax"
    assert morsel["path"] == REFRESH_COOKIE_PATH


async def test_without_remember_me_the_cookie_dies_with_the_browser(
    auth_harness: AuthHarness,
) -> None:
    morsel = set_cookie(await auth_harness.login(remember=False))[REFRESH_COOKIE]

    assert not morsel["max-age"]
    assert not morsel["expires"]


async def test_remember_me_makes_the_cookie_persistent(auth_harness: AuthHarness) -> None:
    morsel = set_cookie(await auth_harness.login(remember=True))[REFRESH_COOKIE]

    assert int(morsel["max-age"]) > 0


async def test_the_cookie_is_not_secure_in_development(auth_harness: AuthHarness) -> None:
    """Requiring Secure over plain http would mean the cookie is silently dropped and
    nobody can stay logged in locally."""
    assert not set_cookie(await auth_harness.login())[REFRESH_COOKIE]["secure"]


async def test_the_cookie_is_secure_in_production() -> None:
    fixture = build_auth()
    production = get_settings().model_copy(update={"environment": "prod"})
    application = build_auth_app(fixture, settings=production)

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            harness = AuthHarness(app=application, client=client, auth=fixture)
            response = await harness.login()

    assert set_cookie(response)[REFRESH_COOKIE]["secure"]


async def test_a_wrong_password_is_a_401_in_the_control_plane_envelope(
    auth_harness: AuthHarness,
) -> None:
    response = await auth_harness.login(password="wrong-password")

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_credentials"
    assert error["request_id"]


async def test_an_unknown_email_is_indistinguishable_from_a_wrong_password(
    auth_harness: AuthHarness,
) -> None:
    unknown = await auth_harness.login(email="nobody@example.com")
    wrong = await auth_harness.login(password="wrong-password")

    assert unknown.status_code == wrong.status_code
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]


async def test_a_failed_login_sets_no_cookie(auth_harness: AuthHarness) -> None:
    response = await auth_harness.login(password="wrong-password")

    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"email": "ada@example.com"},
        {"password": PASSWORD},
        {"email": "not-an-email", "password": PASSWORD},
        {"email": EMAIL, "password": ""},
        {"email": EMAIL, "password": PASSWORD, "role": "superadmin"},
    ],
    ids=["empty", "no-password", "no-email", "bad-email", "blank-password", "extra-field"],
)
async def test_a_malformed_login_is_rejected(
    auth_harness: AuthHarness, body: dict[str, str]
) -> None:
    """The last case matters most: `extra="forbid"` means a client cannot smuggle in a
    field a later version of this endpoint might start honouring."""
    response = await auth_harness.client.post(LOGIN, json=body)

    assert response.status_code == 422


async def test_repeated_failures_are_throttled(auth_harness: AuthHarness) -> None:
    for _ in range(auth_harness.auth.settings.login_max_attempts):
        await auth_harness.login(password="wrong-password")

    response = await auth_harness.login()

    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert response.json()["error"]["code"] == "too_many_attempts"


# -- me ----------------------------------------------------------------------


async def test_me_describes_the_caller(auth_harness: AuthHarness) -> None:
    token = await auth_harness.sign_in()

    response = await auth_harness.client.get(ME, headers=auth_harness.bearer(token))

    assert response.status_code == 200
    assert response.json()["email"] == EMAIL
    assert response.json()["organization"]["name"] == "Acme"


async def test_me_never_returns_the_password_hash(auth_harness: AuthHarness) -> None:
    token = await auth_harness.sign_in()

    response = await auth_harness.client.get(ME, headers=auth_harness.bearer(token))

    assert "password" not in response.text.lower()


async def test_me_without_a_token_is_a_401_with_a_challenge(auth_harness: AuthHarness) -> None:
    response = await auth_harness.client.get(ME)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "not_authenticated"


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer ", "Basic abc", "Bearer not-a-jwt", "not-a-scheme token"],
    ids=["empty", "scheme-only", "scheme-space", "basic", "garbage-token", "no-scheme"],
)
async def test_me_rejects_a_malformed_authorization_header(
    auth_harness: AuthHarness, header: str
) -> None:
    response = await auth_harness.client.get(ME, headers={"Authorization": header})

    assert response.status_code == 401


# -- refresh -----------------------------------------------------------------


async def test_refresh_issues_a_new_access_token(auth_harness: AuthHarness) -> None:
    first = await auth_harness.login()

    second = await auth_harness.client.post(REFRESH)

    assert second.status_code == 200
    assert second.json()["access_token"] != first.json()["access_token"]


async def test_refresh_rotates_the_cookie(auth_harness: AuthHarness) -> None:
    first = set_cookie(await auth_harness.login())[REFRESH_COOKIE].value

    second = set_cookie(await auth_harness.client.post(REFRESH))[REFRESH_COOKIE].value

    assert second != first


async def test_the_session_survives_a_reload(auth_harness: AuthHarness) -> None:
    """What "reloading keeps you logged in" actually means: the access token is gone
    with the page, and the cookie alone gets a new one."""
    await auth_harness.login()

    refreshed = await auth_harness.client.post(REFRESH)
    me = await auth_harness.client.get(
        ME, headers=auth_harness.bearer(refreshed.json()["access_token"])
    )

    assert me.status_code == 200


async def test_refresh_without_a_cookie_is_a_401(auth_harness: AuthHarness) -> None:
    response = await auth_harness.client.post(REFRESH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"


async def test_a_failed_refresh_clears_the_cookie(auth_harness: AuthHarness) -> None:
    """Otherwise the SPA retries the same doomed refresh on every reload, forever."""
    auth_harness.client.cookies.set(REFRESH_COOKIE, "not-a-real-token", path=REFRESH_COOKIE_PATH)

    response = await auth_harness.client.post(REFRESH)

    assert response.status_code == 401
    assert set_cookie(response)[REFRESH_COOKIE].value == ""


async def test_replaying_a_refresh_token_ends_the_session(auth_harness: AuthHarness) -> None:
    """The acceptance criterion: a stolen cookie replayed after the real client has
    rotated forces everyone back to the login page."""
    stolen = set_cookie(await auth_harness.login())[REFRESH_COOKIE].value
    live = await auth_harness.client.post(REFRESH)
    assert live.status_code == 200
    rotated = set_cookie(live)[REFRESH_COOKIE].value

    _hold(auth_harness, stolen)
    replay = await auth_harness.client.post(REFRESH)

    assert replay.status_code == 401
    # And the legitimate client is signed out too, because there is no way to tell which
    # of the two is legitimate.
    _hold(auth_harness, rotated)
    assert (await auth_harness.client.post(REFRESH)).status_code == 401
    assert (
        await auth_harness.client.get(ME, headers=auth_harness.bearer(live.json()["access_token"]))
    ).status_code == 401


# -- logout ------------------------------------------------------------------


async def test_logout_clears_the_cookie(auth_harness: AuthHarness) -> None:
    await auth_harness.login()

    response = await auth_harness.client.post(LOGOUT)

    assert response.status_code == 204
    assert set_cookie(response)[REFRESH_COOKIE].value == ""


async def test_logout_ends_the_session(auth_harness: AuthHarness) -> None:
    token = await auth_harness.sign_in()

    await auth_harness.client.post(LOGOUT)

    assert (
        await auth_harness.client.get(ME, headers=auth_harness.bearer(token))
    ).status_code == 401


async def test_the_back_button_does_not_restore_the_session(auth_harness: AuthHarness) -> None:
    """A cached page can re-present an old access token; the session behind it is gone."""
    token = await auth_harness.sign_in()
    await auth_harness.client.post(LOGOUT)

    replayed = await auth_harness.client.get(ME, headers=auth_harness.bearer(token))

    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "session_expired"


async def test_logout_works_without_being_logged_in(auth_harness: AuthHarness) -> None:
    """A logout that 401s leaves the browser holding a live refresh cookie."""
    assert (await auth_harness.client.post(LOGOUT)).status_code == 204


async def test_logout_is_idempotent(auth_harness: AuthHarness) -> None:
    await auth_harness.login()

    assert (await auth_harness.client.post(LOGOUT)).status_code == 204
    assert (await auth_harness.client.post(LOGOUT)).status_code == 204


# -- password ----------------------------------------------------------------


async def test_changing_the_password(auth_harness: AuthHarness) -> None:
    token = await auth_harness.sign_in()

    response = await auth_harness.client.post(
        PASSWORD_URL,
        headers=auth_harness.bearer(token),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 204
    assert (await auth_harness.login(password=NEW_PASSWORD)).status_code == 200


async def test_changing_the_password_keeps_the_current_session(
    auth_harness: AuthHarness,
) -> None:
    token = await auth_harness.sign_in()

    await auth_harness.client.post(
        PASSWORD_URL,
        headers=auth_harness.bearer(token),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert (
        await auth_harness.client.get(ME, headers=auth_harness.bearer(token))
    ).status_code == 200


async def test_changing_the_password_signs_other_sessions_out(
    auth_harness: AuthHarness,
) -> None:
    other = await auth_harness.sign_in()  # a second browser
    current = await auth_harness.sign_in()

    await auth_harness.client.post(
        PASSWORD_URL,
        headers=auth_harness.bearer(current),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert (
        await auth_harness.client.get(ME, headers=auth_harness.bearer(other))
    ).status_code == 401


async def test_the_wrong_current_password_is_refused(auth_harness: AuthHarness) -> None:
    token = await auth_harness.sign_in()

    response = await auth_harness.client.post(
        PASSWORD_URL,
        headers=auth_harness.bearer(token),
        json={"current_password": "not-it", "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 401
    assert (await auth_harness.login()).status_code == 200, "the password is unchanged"


async def test_a_short_new_password_is_refused(auth_harness: AuthHarness) -> None:
    token = await auth_harness.sign_in()

    response = await auth_harness.client.post(
        PASSWORD_URL,
        headers=auth_harness.bearer(token),
        json={"current_password": PASSWORD, "new_password": "short"},
    )

    assert response.status_code == 422


async def test_changing_the_password_requires_authentication(
    auth_harness: AuthHarness,
) -> None:
    response = await auth_harness.client.post(
        PASSWORD_URL, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 401


# -- the router's default --------------------------------------------------


PUBLIC_PATHS = {
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
}


def control_plane_operations(app: FastAPI) -> list[tuple[str, str]]:
    schema = app.openapi()
    return [
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        if path.startswith("/api/v1/")
        for method in operations
    ]


async def test_there_is_something_to_check(auth_harness: AuthHarness) -> None:
    """Otherwise the guard below would pass by finding nothing."""
    assert len(control_plane_operations(auth_harness.app)) >= 5


async def test_every_control_plane_endpoint_requires_authentication(
    auth_harness: AuthHarness,
) -> None:
    """The structural guarantee behind ``authenticated_router``: a route added by a later
    task is protected unless somebody deliberately put it on ``public_router``. This test
    is what turns "unless somebody deliberately" into something a reviewer can see."""
    unprotected = []
    for method, path in control_plane_operations(auth_harness.app):
        if path in PUBLIC_PATHS:
            continue
        response = await auth_harness.client.request(method, path, json={})
        if response.status_code != 401:
            unprotected.append(f"{method} {path} -> {response.status_code}")

    assert not unprotected, f"reachable without a token: {unprotected}"
