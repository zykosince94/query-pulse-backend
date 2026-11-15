# Query Pulse: functionality reference

What Query Pulse does today, across the backend (`query-pulse-backend`) and the frontend
(`query-pulse-frontend`). This page describes **current behaviour only**. Planned work is listed
at the end and marked as such.

*As of 15 November 2026, backend commit `975c0d9`, release target v0.1.0 (Phase 1, "Foundation").*

---

## 1. Summary

| Area | Works today | Placeholder (honest stub) |
|---|---|---|
| Sign-in | Username and password, "Try the demo", sign out, session expiry | — |
| Roles | Admin (full access) and read-only demo user | — |
| Access control | 401 without a session, 403 for the demo user on writes | — |
| App shell | Navigation, current user, read-only badge, not-found and error pages | — |
| Data sources | List and create (in memory), credentials never returned | Test connection, health |
| Workload | — | Canned preview data per source |
| Collector | — | Status endpoint reports `not_running` |
| Findings, Settings | — | Placeholder pages |
| Ops | `/health`, `/ready`, OpenAPI docs | — |
| Local stack | App database, demo PostgreSQL 16 with `pg_stat_statements`, MongoDB 7 replica set | No demo data or workload yet |

Query Pulse never writes to the databases it monitors. Nothing in the current code connects to
them yet; the local stack only prepares them for Phase 2.

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
| `/sources` | Signed in | Data sources (placeholder, Phase 2) |
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

### 4.3 Data sources (Phase 1 placeholders)

These endpoints keep sources **in memory**: they reset when the API restarts, and each API
instance has its own list. Phase 2 replaces them with stored, encrypted sources and real
connection checks.

| Method and path | Access | Success | Errors |
|---|---|---|---|
| `GET /api/sources` | Session | `200 {"count": 2, "items": [source, …]}` | `401` |
| `POST /api/sources` | Admin | `201` source | `401`, `403` demo user, `409` id already exists, `422` |
| `POST /api/sources/test` | Admin | `200` validation result (no connection is made) | `401`, `403`, `422` |
| `GET /api/sources/{source_id}/health` | Session | `200` health with `status: "unknown"` | `401`, `404` |
| `GET /api/sources/{source_id}/workload-preview` | Session | `200` canned observations | `401`, `404` |
| `GET /api/collector/status` | Session | `200 {"status": "not_running", …}` | `401` |

**Creating a source.** Request body:

| Field | Type | Rules |
|---|---|---|
| `id` | string | 1–64 characters, lowercase letters, digits and `-`, starting with a letter or digit |
| `name` | string | 2–120 characters |
| `type` | `"postgres"` or `"mongodb"` | required |
| `host` | string | 1–255 characters |
| `port` | integer | 1–65535 |
| `database` | string | 1–120 characters |
| `username` | string | 1–120 characters |
| `password` | string | optional, default empty |
| `ssl` | boolean | default `false` |
| `enabled` | boolean | default `true` |
| `description` | string | default empty |

**Responses never include the password.** A source comes back with every field above except
`password`, plus `has_password: true` or `false`.

**Built-in demo sources** (present on every start):

| `id` | Type | Host and port (inside the compose network) | Database | Username |
|---|---|---|---|---|
| `demo-postgres` | postgres | `demo-postgres:5432` | `shop` | `querypulse_monitor` |
| `demo-mongodb` | mongodb | `mongo:27017` | `shop` | `monitor` |

**Placeholder responses**, which say plainly that they are placeholders:

- Test connection: `{"status": "validated", "engine": "postgres", "connected": false, "message": "Parameters are valid. Live connection checks arrive in Phase 2."}`
- Health: `{"source_id": "…", "status": "unknown", "engine": "…", "ready": false, "message": "Health is not checked yet; live checks arrive in Phase 2."}`
- Workload preview: two fixed example operations per engine, a summary (`total_queries`,
  `avg_duration_ms`, `slowest_query_ms`) and `"notes": "Canned demo data; …"`.
- Collector status: `{"status": "not_running", "read_only": true, "source_count": 2, "engines": ["mongodb", "postgres"], "message": "The collector process arrives in Phase 3."}`

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
| Credentials in responses | Never returned. Source responses only say `has_password`. |
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
| `QP_FERNET_KEY` | **yes** | — | Encryption key for stored source credentials (used from Phase 2); must be a valid Fernet key |
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
| QP-AC-01: `make demo` starts all services healthy | Partly: the demo workload doesn't exist yet | Backend CI `stack` job (not yet run locally; Docker isn't installed on the dev machine) |
| QP-AC-02: admin signs in and out; protected endpoints return 401 without a session | Done | Backend `test_auth.py` and `test_access.py` (every endpoint from the spec); frontend Playwright `sign-in.spec.ts` |
| QP-AC-03: demo user gets 403 on every write endpoint in `openapi.json` | Done | Backend `test_access.py`, generated from the spec |
| QP-AC-04: CI fails when `openapi.json` or the frontend types are stale | Done | Backend `export_openapi.py --check`; frontend `gen:api:check` |

Test counts: 51 backend (pytest), 20 frontend (Vitest), 3 end-to-end (Playwright).

---

## 9. Not built yet

| Planned | Phase | Release |
|---|---|---|
| CSRF protection beyond SameSite, login rate limit, JSON logs with request IDs | 1 | v0.1.0 |
| Demo seed data and the planted-problem workload (P1–P4) | 1 | v0.1.0 |
| Stored data sources with encrypted credentials, real test connection, capability checklist; the Data sources screen | 2 | v0.2.0 |
| PostgreSQL collector with per-interval deltas | 3 | v0.3.0 |
| Workload explorer and query plans | 4 | v0.4.0 |
| Rule-based findings for P1–P4 (first showable release) | 5 | v0.5.0 |
| MongoDB collection and findings | 6 | v0.6.0 |
| Optional AI explanations and drafts, hardening | 7 | v1.0.0 |
