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
- [ ] `users(id, organization_id NULL, email CITEXT UNIQUE, password_hash, role, name, status,
      last_login_at, created_at)`. `role` is an enum with all four values from SPEC §5.2 even
      though enforcement lands in task 04 — the column should not need a migration then.
- [ ] `sessions(id, user_id, refresh_token_hash, expires_at, ip, user_agent, revoked_at,
      created_at)`.
- [ ] Argon2id hashing with tuned parameters; a `verify_and_upgrade` path that rehashes on login
      when parameters change.
- [ ] `POST /api/v1/auth/login` — email + password → access JWT in the body, refresh token as an
      httpOnly, SameSite=Lax, Secure-in-prod cookie.
- [ ] `POST /api/v1/auth/refresh` — **rotates** the refresh token; reuse of a consumed token
      revokes the whole session family and logs a warning (detects token theft).
- [ ] `POST /api/v1/auth/logout` — revokes the session and clears the cookie.
- [ ] `GET /api/v1/auth/me` — current user, role, and organization summary.
- [ ] `POST /api/v1/auth/password` — change own password; revokes all other sessions.
- [ ] Access token: 15 min TTL, `sub`, `org`, `role`, `jti`. Refresh: 14 days, sliding.
- [ ] `CurrentUser` FastAPI dependency; the control-plane router requires it by default so a new
      endpoint is authenticated unless it explicitly opts out.
- [ ] Login rate limiting: per-IP and per-email backoff. Hardened further in task 18.
- [ ] Generic failure message — never distinguish "unknown email" from "wrong password".
- [ ] Seed the superadmin in `make seed`, printing the generated password once.

### Frontend
- [ ] `web/` scaffold: Vite + React 18 + TypeScript (strict) + Tailwind.
- [ ] TanStack Query for server state; TanStack Router (or React Router) for routing.
- [ ] Typed API client generated from the FastAPI OpenAPI schema (`openapi-typescript`), wired
      into `make` so it regenerates and fails CI when drifted.
- [ ] Auth context: access token in memory only (never `localStorage` — XSS reads it), refresh
      via the cookie on 401 with a single-flight queue so concurrent 401s trigger one refresh.
- [ ] `<ProtectedRoute>` redirecting to `/login` with a `next` param.
- [ ] App shell: sidebar navigation (entries added by later tasks), org name, user menu with
      logout, breadcrumb slot, toast host.
- [ ] Login page: validation, loading state, error display, "remember me" affecting refresh TTL.
- [ ] Dashboard placeholder with named empty cards for the metrics task 07 will populate.
- [ ] Shared primitives used by every later screen: `DataTable` (sortable, cursor-paginated),
      `Form` wrapper with field-level errors from the API, `ConfirmDialog` requiring a typed
      resource name, `EmptyState`, `StatusBadge`, `CopyButton`.
- [ ] Dev: Vite proxies `/api` to the API container. Prod: the API serves the built assets with
      SPA history fallback.
- [ ] Frontend CI: `tsc --noEmit`, ESLint, Vitest, and a production build.

## Acceptance criteria

- [ ] Login, reload, and logout behave correctly; the session survives a page refresh.
- [ ] The access token is absent from `localStorage` and `sessionStorage`.
- [ ] An expired access token triggers exactly one refresh, and the original request is retried
      transparently — verify with two concurrent requests firing at expiry.
- [ ] Replaying a used refresh token revokes the session family and forces re-login.
- [ ] Repeated bad logins are throttled.
- [ ] Password change invalidates other sessions but not the current one.
- [ ] The generated API client matches the server schema; CI fails if it drifts.

## Tests

- Backend: full login/refresh/rotate/reuse/logout matrix; password hashing and upgrade; token
  claims and expiry; login throttling.
- Frontend: auth context reducer, the single-flight refresh interceptor under concurrency,
  protected-route redirect including `next` restoration.
- One end-to-end (Playwright) covering log in → reload → log out.

## Notes

- Put local password auth behind an `AuthProvider` interface with `authenticate(credentials)` and
  `user_from_claims(claims)`. OIDC (SPEC §16.7) then becomes a second implementation instead of a
  refactor of every call site.
- The `role` column exists but is not enforced yet. Task 04 adds the guards; do not scatter role
  checks into endpoints here.
