# Task 03 — Control-plane auth & UI shell

**Slice:** a person can log into a web UI and land on an application shell.
**Depends on:** 02
**Spec:** §5.1 (control plane), §13.1 (Login, Dashboard), §13.2
**Size:** M

---

## Why this slice

The proxy works but is configured by CLI. Before any configuration screen can exist, there must
be a way to log in and a shell to hang screens on. Building the SPA scaffold once, properly,
means tasks 04–13 each add a route instead of re-litigating routing, data fetching, and layout.

## Demo at the end of this task

```bash
docker compose up
open http://localhost:5173
```

Log in with the seeded superadmin. You land on a dashboard with the app shell — sidebar,
org name, user menu — and placeholder cards. Reloading keeps you logged in. Logging out returns
you to the login page and the back button does not restore the session.

## In scope

- `users` and `sessions` tables, password hashing, the auth endpoints.
- JWT access tokens plus rotating refresh tokens in httpOnly cookies.
- React SPA scaffold: build, routing, layout, data fetching, protected routes, login, dashboard
  placeholder.

## Out of scope

- Organizations beyond the single seeded one, roles, invitations, member management (04).
- Any configuration screen (05+).
- OIDC — but the auth provider seam must exist (see Notes).

## Work items

### Backend
- [x] `users(id, organization_id NULL, email CITEXT UNIQUE, password_hash, role, name, status,
      last_login_at, created_at)`. `role` is an enum with all four values from SPEC §5.2 even
      though enforcement lands in task 04 — the column should not need a migration then.
- [x] `sessions(id, user_id, refresh_token_hash, expires_at, ip, user_agent, revoked_at,
      created_at)`.
- [x] Argon2id hashing with tuned parameters; a `verify_and_upgrade` path that rehashes on login
      when parameters change.
- [x] `POST /api/v1/auth/login` — email + password → access JWT in the body, refresh token as an
      httpOnly, SameSite=Lax, Secure-in-prod cookie.
- [x] `POST /api/v1/auth/refresh` — **rotates** the refresh token; reuse of a consumed token
      revokes the whole session family and logs a warning (detects token theft).
- [x] `POST /api/v1/auth/logout` — revokes the session and clears the cookie.
- [x] `GET /api/v1/auth/me` — current user, role, and organization summary.
- [x] `POST /api/v1/auth/password` — change own password; revokes all other sessions.
- [x] Access token: 15 min TTL, `sub`, `org`, `role`, `jti`. Refresh: 14 days, sliding.
- [x] `CurrentUser` FastAPI dependency; the control-plane router requires it by default so a new
      endpoint is authenticated unless it explicitly opts out.
- [x] Login rate limiting: per-IP and per-email backoff. Hardened further in task 18.
- [x] Generic failure message — never distinguish "unknown email" from "wrong password".
- [x] Seed the superadmin in `make seed`, printing the generated password once.

### Frontend
- [x] `web/` scaffold: Vite + React 18 + TypeScript (strict) + Tailwind.
- [x] TanStack Query for server state; TanStack Router (or React Router) for routing.
- [x] Typed API client generated from the FastAPI OpenAPI schema (`openapi-typescript`), wired
      into `make` so it regenerates and fails CI when drifted.
- [x] Auth context: access token in memory only (never `localStorage` — XSS reads it), refresh
      via the cookie on 401 with a single-flight queue so concurrent 401s trigger one refresh.
- [x] `<ProtectedRoute>` redirecting to `/login` with a `next` param.
- [x] App shell: sidebar navigation (entries added by later tasks), org name, user menu with
      logout, breadcrumb slot, toast host.
- [x] Login page: validation, loading state, error display, "remember me" affecting refresh TTL.
- [x] Dashboard placeholder with named empty cards for the metrics task 07 will populate.
- [x] Shared primitives used by every later screen: `DataTable` (sortable, cursor-paginated),
      `Form` wrapper with field-level errors from the API, `ConfirmDialog` requiring a typed
      resource name, `EmptyState`, `StatusBadge`, `CopyButton`.
- [x] Dev: Vite proxies `/api` to the API container. Prod: the API serves the built assets with
      SPA history fallback.
- [x] Frontend CI: `tsc --noEmit`, ESLint, Vitest, and a production build.

## Acceptance criteria

- [x] Login, reload, and logout behave correctly; the session survives a page refresh.
      *(Verified in jsdom against a scripted server, and over HTTP against the real
      app; not yet in a real browser.)*
- [x] The access token is absent from `localStorage` and `sessionStorage`.
- [x] An expired access token triggers exactly one refresh, and the original request is retried
      transparently — verify with two concurrent requests firing at expiry.
- [x] Replaying a used refresh token revokes the session family and forces re-login.
- [x] Repeated bad logins are throttled.
- [x] Password change invalidates other sessions but not the current one.
- [x] The generated API client matches the server schema; CI fails if it drifts.

## Tests

- Backend: full login/refresh/rotate/reuse/logout matrix; password hashing and upgrade; token
  claims and expiry; login throttling.
- Frontend: auth context reducer, the single-flight refresh interceptor under concurrency,
  protected-route redirect including `next` restoration.
- One end-to-end (Playwright) covering log in → reload → log out. *(Written in
  `web/e2e/auth.spec.ts`; not run here — no browser binaries and no stack. See
  Verification status.)*

## Notes

- Put local password auth behind an `AuthProvider` interface with `authenticate(credentials)` and
  `user_from_claims(claims)`. OIDC (SPEC §16.7) then becomes a second implementation instead of a
  refactor of every call site.
- The `role` column exists but is not enforced yet. Task 04 adds the guards; do not scatter role
  checks into endpoints here.

---

## Verification status

Everything below was run on this machine unless the table says otherwise.

```
uv run ruff check .              All checks passed!
uv run ruff format --check .     91 files already formatted
uv run mypy                      Success: no issues found in 87 source files
uv run pytest -q                 518 passed, 64 skipped

npm --prefix web run lint        clean
npm --prefix web run typecheck   clean
npm --prefix web run test        70 passed
npm --prefix web run build       built in 1.37s
```

A live smoke test ran the real app on a real port with the built SPA mounted
(`WEB_DIST_DIR=web/dist`) and every backing service down:

| Request | Result |
|---|---|
| `GET /` and `GET /login` | 200 `text/html` — the built SPA |
| `GET /gateways/abc?tab=keys` | 200 `text/html` — history fallback |
| `GET /assets/nope-deadbeef.js` | 404 JSON — a missing asset is not silently HTML |
| `GET /api/v1/auth/me` | 401 `not_authenticated`, `WWW-Authenticate: Bearer` |
| `POST /api/v1/auth/refresh` | 401 `session_expired`, cookie cleared |
| `POST /g/demo/v1/chat/completions` | 401 in the **OpenAI** envelope, not this one |
| `GET /healthz` / `GET /readyz` | 200 / 503 (naming the unreachable services) |

That smoke test found a real bug: with Redis down, `POST /auth/login` returned 500 because
the throttle's connection error was unhandled. It now fails open with a warning — see the
rationale in `app/services/login_throttle.py`.

### Not verifiable here

| Item | Why | What stands in for it |
|---|---|---|
| The migration against a real PostgreSQL | No PostgreSQL and no Docker on this machine | `tests/test_migration_offline.py` renders the DDL and compares every table, column and constraint name against the models; the `db`-marked tests run in CI via `REQUIRE_DB_TESTS=1` |
| `make seed` end to end | Same | `tests/test_auth_db.py` covers `seed_demo` — superadmin creation, idempotency, rotation, and signing in with the password it printed |
| The Playwright suite | No browser binaries, and it needs the API, the database and the Vite server all up | `web/src/pages/LoginPage.test.tsx` drives the same flow in jsdom; `tests/test_auth_api.py` drives it over HTTP |
| `docker compose up` | Docker is unavailable (inherited from task 01) | The compose file and Dockerfile parse and are internally consistent; the image build runs in CI |
| The Redis-backed throttle | No Redis on this machine | The policy is tested against the in-memory store; the Redis store has a test that runs in CI, which now has a `redis` service |

### Notes for later tasks

- **Task 04** adds role enforcement. The `role` column already holds all four SPEC §5.2
  values, and `app/services/auth_store.py` is where tenant scoping goes.
- **Task 06** will want `ConfirmDialog` for key revocation and `CopyButton` for the
  once-shown key; both are in place.
- **The sidebar** is extended by adding an entry to `NAVIGATION` in
  `web/src/layout/navigation.ts`. Its `roles` field decides only what is *drawn* — a
  hidden link is not a permission check.
- **New control-plane endpoints** go on `authenticated_router` in
  `app/api/control/router.py` and are protected without anyone remembering to say so;
  `tests/test_auth_api.py` asserts that every `/api/v1` operation outside a short
  allow-list answers 401 without a token.

### Deliberate deviations

- **`sessions` carries `family_id`, `replaced_at`, `revoked_reason` and `persistent`,
  which SPEC §14 does not list.** The first two are what make reuse detection possible at
  all (the acceptance criterion asks for it); `revoked_reason` distinguishes "logged out"
  from "we caught a stolen token" when someone reads the table during an incident; and
  `persistent` carries the "remember me" choice through rotation, so a session the user
  asked not to persist cannot quietly become one.
- **`make seed` no longer exits 1 when `OPENAI_API_KEY` is missing.** It seeds the
  superadmin — which is what this task's demo needs — and skips the gateway with a message
  saying so. Task 02's flow is unchanged when the key is set.
