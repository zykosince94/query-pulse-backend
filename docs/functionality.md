# Query Pulse: functionality reference

What Query Pulse does today, across the backend (`query-pulse-backend`) and the frontend
(`query-pulse-frontend`). This page describes **current behaviour only**. Planned work is listed
at the end and marked as such.

*As of 15 November 2026: Phase 1 ("Foundation") and Phase 2 ("Data sources", release target
v0.2.0).*

---

## 1. Summary

| Area | Works today | Placeholder (honest stub) |
|---|---|---|
| Sign-in | Username and password, "Try the demo", sign out, session expiry | — |
| Roles | Admin (full access) and read-only demo user | — |
| Access control | 401 without a session, 403 for the demo user on writes | — |
| App shell | Navigation, current user, read-only badge, not-found and error pages | — |
| Data sources | Add, edit, re-check and delete PostgreSQL and MongoDB sources; real connection test before saving; capability checklist with exact fixes; encrypted credentials | — |
| Workload | — | Placeholder page (Phase 4) |
| Collector | — | Status endpoint reports `not_running` (Phase 3) |
| Findings, Settings | — | Placeholder pages |
| Ops | `/health`, `/ready`, OpenAPI docs | — |
| Local stack | App database, demo PostgreSQL 16 with `pg_stat_statements`, MongoDB 7 replica set | No demo data or workload yet |

Query Pulse never writes to the databases it monitors. It connects only to test a connection and
check capabilities, with read-only sessions, and it never changes a setting (for example, it
reports that MongoDB profiling is off but never turns it on).

---

## 2. Users and roles

There is no sign-up. Users are created from environment variables by the bootstrap command
(§6.2), which runs automatically when the API container starts.

| Role | Created from | Can read | Can write | Signs in with |
|---|---|---|---|---|
| `admin` | `QP_ADMIN_USERNAME`, `QP_ADMIN_PASSWORD` | Everything | Everything | Username and password |
| `demo` | `QP_DEMO_ENABLED=true`, `QP_DEMO_USERNAME`, `QP_DEMO_PASSWORD` | Everything | Nothing (403) | "Try the demo" (no password), or username and password |

- The environment is the only source of truth for accounts. The bootstrap removes every admin or
  demo account the environment doesn't name, and the removed account's sessions stop working on
  their next request:
  - Renaming `QP_ADMIN_USERNAME` creates the new admin and removes the old one.
  - Renaming `QP_DEMO_USERNAME` does the same for the demo user.
  - Setting `QP_DEMO_ENABLED=false` removes the demo user, and "Try the demo" returns 404.
- Changing a password in the environment and restarting updates the stored hash.

---

## 3. Frontend: screens and behaviour

The frontend is a single-page app served at `http://localhost:5173` in development. It calls the
backend through a same-origin proxy (`/api` → `http://localhost:8000`), so the session cookie
works without CORS.

### 3.1 Routes

| Path | Access | Shows |
|---|---|---|
| `/login` | Public | Sign-in form; "Try the demo" when enabled |
| `/` | Signed in | Workload (placeholder, Phase 4) |
| `/sources` | Signed in | Data sources list |
| `/sources/new` | Signed in (admin) | Add a source; the demo user sees a read-only notice |
| `/sources/:id` | Signed in | Source detail and capability checklist |
| `/sources/:id/edit` | Signed in (admin) | Edit a source; the demo user sees a read-only notice |
| `/findings` | Signed in | Findings (placeholder, Phase 5) |
| `/settings` | Signed in | Settings (placeholder, Phase 7) |
| anything else | Signed in | "Page not found" with a link back to the workload |

### 3.2 Sign-in screen

- **Username and password.** The submit button stays disabled until both fields have text. While
  the request runs, it reads "Signing in…".
- **Wrong credentials** show the server's message, "Invalid username or password", in a red alert.
  The same message appears whether the username or the password was wrong.
- **"Try the demo"** appears only when the backend reports `demo_enabled: true`. It opens a
  read-only session without a password. A note below it explains that the demo is read-only.
- **Already signed in?** Visiting `/login` goes straight to the app.
- **Password managers** work: the fields use `autocomplete="username"` and
  `autocomplete="current-password"`.

### 3.3 Protected pages and returning after sign-in

- Opening any protected page while signed out sends you to
  `/login?redirect=<the page you asked for>`. After signing in you land on that page.
- Only same-origin paths are followed. The value is resolved the way a browser would resolve it
  and its origin compared, so `//evil.example`, `https://evil.example`, `javascript:…`,
  `/\evil.example` and values hiding a tab or newline are all ignored, and you land on `/`
  instead. A same-origin path keeps its query string and `#hash`.

### 3.4 App shell

- **Sidebar** (a horizontal bar on narrow screens) with Workload, Data sources, Findings and
  Settings. The current page is highlighted and marked `aria-current="page"`.
- **Header**: "Signed in as *username*", a **"Read-only demo"** badge for the demo user, and
  **Sign out**.
- **Sign out** ends the server session, clears all cached data in the browser, and shows the
  sign-in screen. It does this even if the sign-out request itself fails.

### 3.5 Session expiry and errors

- If any API request returns 401 (the session expired after 12 hours, or the server's session
  secret changed), the app marks you signed out and shows the sign-in screen.
- If the backend can't be reached, the app shows **"Can't reach the Query Pulse server"** with a
  **Try again** button that retries once the backend is back.
- Other errors show "Something went wrong" with the server's message and **Try again**.

### 3.6 Appearance

- Light and dark themes, following the operating system's setting at page load.
- All colours come from shared design tokens (`src/styles/tokens.css`), which API Pulse will
  reuse.

### 3.7 Data sources

**List** (`/sources`)
- A table of sources: name (links to the detail page), engine badge, `host:port/database`,
  server version, status badge (Healthy, Degraded, Failing; hover for an explanation) and when it
  was last checked. On narrow screens only name, engine and status are shown.
- States: "Loading data sources…", an empty state that explains what adding a source does, and an
  error message with **Try again**. Server errors are retried twice before the error shows.
- **Add source** is disabled for the demo user.

**Add and edit** (`/sources/new`, `/sources/:id/edit`)
- Fields: name, engine, TLS mode (with an explanation), host, port, database, username, password,
  description, and an "Enabled" checkbox. Switching the engine swaps the default port (5432 /
  27017) unless you changed it. For MongoDB the username hint says it can be blank.
- **Test connection** shows either a green "Connected to version X in N ms" with the capability
  checklist, or the server's error message (for example "PostgreSQL rejected the username or
  password for user 'monitor'").
- **Save** is enabled only after a successful test of the *current* connection settings. Changing
  host, port, database, username, password, TLS mode or engine afterwards disables it and says
  "Test again before saving". Changing only the name or description doesn't.
- A source with missing capabilities can still be saved; the result says how many features are
  unavailable and that they can be fixed later.
- Editing: the password field starts empty. Leaving it blank keeps the saved password (the test
  then uses the stored one); typing a new one replaces it.
- Errors from saving (for example "A source named 'Orders DB' exists") appear above the buttons.

**Detail** (`/sources/:id`)
- Name, engine and status badges, description, and a connection summary: host and port,
  database, username, whether a password is saved ("Saved (encrypted)"), TLS mode, server version
  and last check time.
- The **capability checklist**: a tick or cross per feature, the reason when unavailable, and a
  "How to fix" box with the exact command and a **Copy** button.
- A failing source shows its last error. The checklist then reflects the last successful check.
- Admin actions: **Re-check**, **Edit**, and **Delete** (asks for confirmation, then returns to the
  list). The demo user doesn't see them.
- A missing source shows "This data source doesn't exist."; a non-numeric id shows the not-found
  page.

---

## 4. Backend: HTTP API

Base URL in development: `http://localhost:8000`. Interactive documentation: `/docs`. The
machine-readable contract is `openapi.json`, committed in the backend repo.

**Conventions**

- Request and response bodies are JSON.
- Errors use FastAPI's shape: `{"detail": "message"}`. Invalid input returns **422** with a list of
  field errors in `detail`.
- Authentication is a signed session cookie named `qp_session` (§5).
- Every API endpoint requires a session unless it is marked **Public** below.

### 4.1 Operations

| Method and path | Access | Success | Errors |
|---|---|---|---|
| `GET /health` | Public | `200 {"status": "ok"}` | — |
| `GET /ready` | Public | `200 {"status": "ready", "database": "ok"}` | `503 {"status": "not_ready", "database": "unreachable"}` |

`/health` only says the process is up (liveness). `/ready` also runs `SELECT 1` against the app
database (readiness) and is what Docker's healthcheck uses.

### 4.2 Authentication

| Method and path | Access | Request | Success | Errors |
|---|---|---|---|---|
| `GET /api/auth/config` | Public | — | `200 {"demo_enabled": true}` | — |
| `POST /api/auth/login` | Public | `{"username": "…", "password": "…"}` | `200` user, sets cookie | `401` invalid credentials, `422` empty or too-long fields |
| `POST /api/auth/demo` | Public | — | `200` demo user, sets cookie | `404 {"detail": "The demo is not enabled"}` |
| `POST /api/auth/logout` | Public | — | `204`, clears the session | — |
| `GET /api/auth/me` | Session | — | `200` user | `401 {"detail": "Not signed in"}` |

The user object returned by login, demo and me:

```json
{ "username": "admin", "role": "admin", "read_only": false }
```

Field limits: `username` 1–64 characters, `password` 1–256 characters.

Logout is public on purpose: the demo user must be able to sign out, and signing out without a
session is harmless.

### 4.3 Data sources

Sources are stored in the app database. Passwords are encrypted with Fernet before they're
stored and are decrypted only to connect. A source is saved only after a successful
connection test.

| Method and path | Access | Success | Errors |
|---|---|---|---|
| `GET /api/sources` | Session | `200` list of sources, sorted by name | `401` |
| `GET /api/sources/{source_id}` | Session | `200` source with its capability checklist | `401`, `404` |
| `POST /api/sources/test` | Admin | `200` connection test result (never saves) | `401`, `403`, `404` unknown `source_id`, `422` |
| `POST /api/sources` | Admin | `201` source with checklist | `400` connection failed (nothing saved), `401`, `403`, `409` name in use, `422` |
| `PUT /api/sources/{source_id}` | Admin | `200` source with checklist | `400` connection failed (nothing changed), `401`, `403`, `404`, `409`, `422` |
| `POST /api/sources/{source_id}/check` | Admin | `200` source, re-checked with its stored settings | `401`, `403`, `404` |
| `DELETE /api/sources/{source_id}` | Admin | `204` | `401`, `403`, `404` |
| `GET /api/collector/status` | Session | `200 {"status": "not_running", "source_count": 2, "message": "…"}` (placeholder until Phase 3) | `401` |

**Fields** (create and update):

| Field | Type | Rules |
|---|---|---|
| `name` | string | 2–120 characters, unique (ignoring case) |
| `engine` | `"postgres"` or `"mongodb"` | required |
| `host` | string | hostname or IP only: no scheme, path or spaces |
| `port` | integer | 1–65535 |
| `database` | string | 1–120 characters |
| `username` | string | required for PostgreSQL; optional for MongoDB without auth |
| `password` | string | create: optional, default empty. Update: omit or `null` keeps the stored password, `""` removes it. |
| `tls_mode` | `"disable"`, `"require"` or `"verify-full"` | default `disable`. `require` encrypts without checking the certificate; `verify-full` also checks it. |
| `enabled` | boolean | default `true` (used by the collector from Phase 3) |
| `description` | string | up to 500 characters |

**A source in a response** has every field above except `password`, plus:

| Field | Meaning |
|---|---|
| `id` | Number used in URLs |
| `has_password` | Whether a password is stored. The password itself is never returned. |
| `server_version` | Version reported by the server at the last successful check |
| `status` | `healthy`: connected and every capability available. `degraded`: connected, some capabilities missing. `failing`: the last check couldn't connect. |
| `last_checked_at`, `last_error` | When the last check ran; the error message if it failed |
| `created_at`, `updated_at` | Timestamps |
| `capabilities` | Detail responses only: the checklist below |

**Connection test.** Send the connection fields (`engine`, `host`, `port`, `database`,
`username`, `tls_mode`) and a `password`. When editing an existing source, send `source_id` and
leave out `password` to test with the stored one. A failed connection is a normal result, not an
HTTP error:

```json
{ "ok": false, "error": { "code": "auth_failed", "message": "PostgreSQL rejected the username or password for user 'monitor'." },
  "server_version": null, "latency_ms": null, "capabilities": [] }
```

Error codes: `unreachable` (connection refused or DNS failure), `timeout` (no answer within
`QP_SOURCE_CONNECT_TIMEOUT_SECONDS`, default 5), `auth_failed`, `tls`, `database_not_found`,
`permission_denied`, `unknown`. Every attempt gives up within the timeout plus a small margin.

**Capability checklist.** Each entry has `feature`, `label`, `available`, and, when unavailable,
a `reason` and the exact `fix`:

| Engine | Feature | Unavailable when | Fix shown |
|---|---|---|---|
| PostgreSQL | `pg_stat_statements` | Not in `shared_preload_libraries` | Add it to `shared_preload_libraries` and restart (then `CREATE EXTENSION` if needed) |
| | | Loaded but not created in the database | `CREATE EXTENSION pg_stat_statements;` in that database |
| | | Not created, and the user can't see whether it's loaded | Both steps |
| PostgreSQL | `read_all_stats` | User lacks `pg_monitor` (can only see its own queries) | `GRANT pg_monitor TO "<user>";` |
| PostgreSQL | `generic_plans` | Server older than 16 | Upgrade to PostgreSQL 16+ |
| MongoDB | `server_status` | User lacks `clusterMonitor` | `db.getSiblingDB("admin").grantRolesToUser(…clusterMonitor…)` |
| MongoDB | `explain` | User can't read the database | Grant `read` on the database |
| MongoDB | `profiler` | Profiling is off on the database | `db.setProfilingLevel(1, { slowms: 100 })`. Query Pulse never turns profiling on itself. |
| Both | `supported_version` | Shown only for PostgreSQL < 16 or MongoDB < 7.0 | Upgrade |

**How connections behave.** PostgreSQL sessions are read-only (`default_transaction_read_only`),
use a 5-second `statement_timeout`, and identify themselves as `application_name=query-pulse`.
MongoDB connections go directly to the given node (`directConnection`), authenticate against
`admin` when a username is set, and only read the profiling level, never change it.

---

## 5. Sessions and security behaviour

| Property | Behaviour |
|---|---|
| Session storage | A cookie, `qp_session`, holding only the user id. It is **signed** (tamper-proof) but **not encrypted** (the browser can read it). |
| Cookie flags | `HttpOnly` (JavaScript can't read it), `SameSite=Lax` (not sent on cross-site POSTs), `Secure` when `QP_SESSION_HTTPS_ONLY=true` |
| Lifetime | 12 hours by default (`QP_SESSION_MAX_AGE_SECONDS`), checked by the server |
| Sign-in | Starts a fresh session every time, so a session set before sign-in can't be reused (session fixation) |
| Each request | Loads the user from the database, so a deleted user is signed out immediately |
| Passwords | Hashed with argon2id, never stored or logged in plain text. Hashes are upgraded on sign-in if the hashing parameters change. |
| Username probing | Wrong username and wrong password take the same time and return the same response |
| Credentials in responses | Never returned. Source responses only say `has_password`. A test checks that a marker password never appears in any response body or log line (QP-AC-08). |
| Credentials at rest | Encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using `QP_FERNET_KEY`, decrypted only in the service layer right before an adapter connects. Several comma-separated keys allow rotation: the first encrypts, all decrypt. |
| Monitored databases | Connections are read-only and time out (§4.3). Log lines carry the source name and error code, never credentials. |
| Secrets in memory | Held as `SecretStr`, shown as `**********` in logs and error output |
| Revoking sessions | Not possible per session. Changing `QP_SESSION_SECRET` signs everyone out. |

---

## 6. Configuration and operation

### 6.1 Environment variables (backend)

All settings are validated when the API starts. A missing or invalid value stops it with a clear
error.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `QP_ENVIRONMENT` | no | `dev` | `dev`, `test` or `prod` |
| `QP_DATABASE_URL` | no | `postgresql+psycopg://querypulse:querypulse@localhost:5432/querypulse` | The app's own database |
| `QP_SESSION_SECRET` | **yes** | — | Signs session cookies; at least 32 characters |
| `QP_SESSION_HTTPS_ONLY` | no (**yes** when `prod`) | `false` | Adds the `Secure` cookie flag. The API refuses to start with `QP_ENVIRONMENT=prod` unless this is `true`. |
| `QP_SESSION_MAX_AGE_SECONDS` | no | `43200` (12 h) | Session lifetime |
| `QP_FERNET_KEY` | **yes** | — | Encryption key(s) for stored source credentials. One Fernet key, or several comma-separated for rotation (the first encrypts, all decrypt). Losing it makes stored passwords unreadable. |
| `QP_SOURCE_CONNECT_TIMEOUT_SECONDS` | no | `5` | How long a connection test or check waits for a monitored database (up to 60) |
| `QP_ADMIN_USERNAME` | no | `admin` | Admin username |
| `QP_ADMIN_PASSWORD` | **yes** | — | Admin password; at least 8 characters |
| `QP_DEMO_ENABLED` | no | `false` | Creates the demo user and enables "Try the demo" |
| `QP_DEMO_USERNAME` | no | `demo` | Must differ from the admin username |
| `QP_DEMO_PASSWORD` | if demo enabled | — | Demo user's password |

Values can come from the environment or a local `.env` file (see `.env.example`).

### 6.2 Commands (backend)

| Command | Does |
|---|---|
| `alembic upgrade head` | Creates or updates the app database schema |
| `python -m query_pulse.accounts.bootstrap` | Makes the accounts match the environment and prints `created`, `updated`, `unchanged` or `removed` for each. Safe to run repeatedly. |
| `uvicorn query_pulse.main:create_app --factory` | Runs the API |
| `python scripts/export_openapi.py [--check]` | Writes `openapi.json`, or fails if it is out of date |

The API container runs the first three in order on every start.

### 6.3 Local stack (`make demo` in the backend repo)

| Service | Host port | Purpose | Local demo credentials |
|---|---|---|---|
| `app` | 8000 | The API | `admin` / `admin-demo-password`, `demo` / `demo-password` |
| `app-db` | 5432 | PostgreSQL 16, the app's own database | `querypulse` / `querypulse` |
| `demo-postgres` | 5433 | PostgreSQL 16 to be monitored, with `pg_stat_statements` and the read-only role `querypulse_monitor` (`pg_monitor`) | `postgres` / `postgres`; monitor: `querypulse_monitor` / `monitor` |
| `mongo` | 27017 | MongoDB 7, single-node replica set `rs0`, to be monitored. From the host use `mongodb://localhost:27017/?directConnection=true`. | none |

All ports listen on `127.0.0.1` only, so the databases, with their default credentials and
MongoDB without auth, can't be reached from other machines on your network. The API starts only
after all three databases report healthy. Data persists in named Docker volumes;
`docker compose down -v` deletes it.

### 6.4 Frontend configuration

| Variable | Default | Purpose |
|---|---|---|
| `QP_BACKEND_URL` | `http://localhost:8000` | Where the dev server proxies `/api` |
| `QP_OPENAPI_SOURCE` | `../query-pulse-backend/openapi.json` | Spec used by `npm run gen:api` (a path or URL) |
| `E2E_ADMIN_USERNAME`, `E2E_ADMIN_PASSWORD` | `admin`, `admin-demo-password` | Credentials for the Playwright tests |

---

## 7. The API contract between the repos

1. The backend generates `openapi.json` from its code and commits it. Its CI fails if the file is
   out of date.
2. The frontend generates TypeScript types from that file (`npm run gen:api`) and sends every
   request through one typed client, so a backend change the frontend hasn't absorbed is a
   compile error.
3. The frontend's CI regenerates the types from the backend's `main` branch and fails if the
   committed types differ.

Push backend API changes first, then regenerate and push the frontend.

---

## 8. Verification

| Acceptance criterion | Status | Proven by |
|---|---|---|
| QP-AC-01: `make demo` starts all services healthy | Partly: all services start healthy (verified locally); the demo workload doesn't exist yet | Backend CI `stack` job |
| QP-AC-02: admin signs in and out; protected endpoints return 401 without a session | Done | Backend `test_auth.py` and `test_access.py` (every endpoint from the spec); frontend Playwright `sign-in.spec.ts` |
| QP-AC-03: demo user gets 403 on every write endpoint in `openapi.json` | Done | Backend `test_access.py`, generated from the spec |
| QP-AC-04: CI fails when `openapi.json` or the frontend types are stale | Done | Backend `export_openapi.py --check`; frontend `gen:api:check` |
| QP-AC-05: the demo PostgreSQL and MongoDB sources can be added after a successful test connection | Done | Backend `test_sources.py`; frontend `sources.test.tsx`; Playwright `sources.spec.ts` against the Docker stack |
| QP-AC-06: missing extension, missing role and profiler-off each show as unavailable with the correct fix | Done | Backend `tests/integration/test_adapters.py` (real PostgreSQL and MongoDB via testcontainers): extension not loaded, not created, missing `pg_monitor`, missing `clusterMonitor`, profiler off and on |
| QP-AC-07: an unreachable host or wrong password fails within the timeout with a clear error, and nothing is saved | Done | Integration tests (wrong password, unknown database, closed port, unresolvable host, timed); `test_sources.py` (nothing saved); Playwright wrong-password journey |
| QP-AC-08: a marker password never appears in any response body or log line | Done | `test_sources.py::test_marker_password_never_leaks` (every source endpoint, success and failure, plus all log records) |

Test counts: 86 backend unit and 14 backend integration (pytest), 39 frontend (Vitest), 6
end-to-end (Playwright).

---

## 9. Not built yet

| Planned | Phase | Release |
|---|---|---|
| CSRF protection beyond SameSite, login rate limit, JSON logs with request IDs | 1 | v0.1.0 |
| Demo seed data and the planted-problem workload (P1–P4) | 1 | v0.1.0 |
| PostgreSQL collector with per-interval deltas | 3 | v0.3.0 |
| Workload explorer and query plans | 4 | v0.4.0 |
| Rule-based findings for P1–P4 (first showable release) | 5 | v0.5.0 |
| MongoDB collection and findings | 6 | v0.6.0 |
| Optional AI explanations and drafts, hardening | 7 | v1.0.0 |
