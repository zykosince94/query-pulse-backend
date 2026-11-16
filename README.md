# Query Pulse Backend

[![CI](https://github.com/zykosince94/query-pulse-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/zykosince94/query-pulse-backend/actions/workflows/ci.yml)

Query Pulse explains why a PostgreSQL or MongoDB workload is slow or expensive. It connects with
read-only credentials, collects workload statistics, ranks the most expensive operations, shows
their plans and turns evidence into rule-based findings. It never changes the databases it watches.

This repo holds the FastAPI API, the collector, the database adapters and the local demo stack.
The React UI lives in [query-pulse-frontend](https://github.com/zykosince94/query-pulse-frontend).

## What works today

- **Settings** validated from `QP_*` environment variables at startup. The app refuses to start
  when a secret is missing or malformed (see [.env.example](.env.example)).
- **App database** on PostgreSQL via async SQLAlchemy, with Alembic migrations.
- **Sign-in** with session cookies (HttpOnly, SameSite=Lax). Passwords are hashed with argon2id.
  An admin and an optional read-only demo user are created from the environment by
  `python -m query_pulse.accounts.bootstrap`.
- **"Try the demo"**: `GET /api/auth/config` tells the sign-in screen whether the demo is
  enabled, and `POST /api/auth/demo` opens a read-only demo session without a password. It
  returns 404 when `QP_DEMO_ENABLED` is false.
- **Access rules**: every API endpoint except health and the sign-in endpoints returns 401
  without a session; the demo user gets 403 on every write. Both are tested against every endpoint in
  `openapi.json`, so new endpoints are covered automatically.
- **API contract**: `openapi.json` is generated from code and committed; CI fails if it is stale.
- **Data sources** for PostgreSQL and MongoDB:
  - A real connection test before anything is saved, with TLS modes and a timeout
    (`QP_SOURCE_CONNECT_TIMEOUT_SECONDS`, default 5). Failures come back with a code
    (`auth_failed`, `timeout`, `unreachable`, ...) and a message saying what to fix.
  - A **capability checklist** per source: what Query Pulse can monitor there, and the exact
    grant or setting for anything missing (`pg_stat_statements`, `pg_monitor`, MongoDB
    `clusterMonitor`, read access, profiler).
  - Passwords encrypted with Fernet at rest (key rotation supported) and never returned or
    logged.
  - Read-only monitoring sessions. Query Pulse never changes a setting on a monitored database.
- **Local stack** (`make demo`): app database, a demo PostgreSQL 16 with `pg_stat_statements` and
  a read-only `pg_monitor` role, and MongoDB 7 as a single-node replica set.

- **The collector** (`python -m query_pulse.collector`): polls every enabled PostgreSQL source,
  turns `pg_stat_statements`' cumulative counters into per-interval observations, and handles
  resets, evictions and restarts without ever storing a negative or inflated number. It also
  samples table and index statistics, re-checks capabilities, backs off when a database is down
  (and recovers on its own), and deletes data older than 14 days.
- **Workload read API**: per-minute trends per source and a health summary for the home page.
- **Workload explorer API**: operations ranked by total time, mean time or calls over any time
  window (the last N minutes, or absolute start/end), filtered by source and statement text.
  Each operation has its metrics over the window and a history per minute or hour (`date_trunc`
  buckets in SQL). Mean time is total time ÷ calls over the whole window, never an average of
  averages.
- **Generic query plans**: `EXPLAIN (GENERIC_PLAN, FORMAT JSON)` on the normalised `$1` text,
  which never executes the statement (QP-AC-15 checks its call count doesn't move). The
  collector plans each source's 20 busiest operations every 5 minutes, and new hot operations
  within a cycle. Plans are stored as immutable evidence: a new row only when the plan changes.
  When a statement can't be planned (missing `SELECT` grant, cut-off text, utility statement),
  the reason is stored and shown instead of an empty viewer.
- **Hardening**: an Origin check on every state-changing request (cross-site writes get 403), a
  login rate limit (10 failures per client per 5 minutes, then 429), and JSON logs where every
  line carries the request ID, which is also returned in the `X-Request-ID` header.
- **Demo data**: a seeded e-commerce database with four planted problems (P1–P4) and a workload
  that runs them next to healthy controls, so `make demo` has real traffic to measure.

Not yet: findings (Phase 5), MongoDB collection (Phase 6), the AI assistant (Phase 7).

## Quick start

Requires Docker.

```bash
make demo            # or: docker compose up -d --build --wait
curl localhost:8000/ready
```

| Service       | Address                                          | Credentials (local demo only) |
|---------------|--------------------------------------------------|-------------------------------|
| API           | http://localhost:8000 (docs at `/docs`)          | `admin` / `admin-demo-password`, `demo` / `demo-password` |
| App database  | localhost:5432                                   | `querypulse` / `querypulse` |
| Demo Postgres | localhost:5433, database `shop`                  | monitor: `querypulse_monitor` / `monitor` |
| Demo MongoDB  | `mongodb://localhost:27017/?directConnection=true` | none |

`make demo` also starts the **collector** (every 15 s in the demo) and the **demo workload**.
Add the demo database as a source in the UI (host `demo-postgres`, port 5432, database `shop`,
user `querypulse_monitor`, password `monitor`) and the home page shows its activity within a
minute. After a couple of minutes the planted problem P1 (`orders` looked up by `customer_id`,
which has no index) is at the top of the explorer, and its plan shows the sequential scan.
[docs/demo-checklist.md](docs/demo-checklist.md) walks through the whole demo.

## Development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                  # install deps into .venv
cp .env.example .env                     # then fill in QP_FERNET_KEY
docker compose up -d app-db              # or point QP_DATABASE_URL elsewhere
uv run alembic upgrade head
uv run python -m query_pulse.accounts.bootstrap
uv run uvicorn query_pulse.main:create_app --factory --reload
```

| Task | Command | Make |
|------|---------|------|
| Tests | `uv run pytest` | `make test` |
| Lint, format, types, contract | see [Makefile](Makefile) | `make lint` |
| Auto-fix formatting | `uv run ruff format .` | `make fmt` |
| Regenerate `openapi.json` | `uv run python scripts/export_openapi.py` | `make openapi` |
| New migration | `uv run alembic revision --autogenerate -m "..."` | |

Unit tests run against a fresh SQLite database per test, built by the real Alembic migrations,
with a fake adapter for monitored databases. Integration tests in `tests/integration/` start real
PostgreSQL 16 and MongoDB 7 containers (testcontainers) for the permission matrix, connection
failures, the collector and plans. They need Docker; without it they're skipped locally, and CI
always runs them. Acceptance tests in `tests/acceptance/` run over HTTP against the full stack
under the demo workload:

```bash
make demo
QP_ACCEPTANCE_URL=http://127.0.0.1:8000 uv run pytest tests/acceptance
```

**Windows:** psycopg's async mode can't use Windows' default event loop. The tests handle this
themselves. To run the API outside Docker on Windows, use `--reload` (as above), which makes
uvicorn use a compatible loop, or run it with `make demo`.

## Documentation

- [Functionality reference](docs/functionality.md): every screen, endpoint, setting and security
  behaviour across the backend and frontend, as of today.
- [Demo checklist](docs/demo-checklist.md): the scripted demo, run by hand before every release.
- [Architecture decision records](docs/adr/): why the stack looks the way it does.

## Layout

```
query_pulse/
  main.py         app factory, /health and /ready
  http.py         request IDs and access log, Origin check
  log.py          JSON logging
  config/         settings validated from the environment
  db.py           engine, sessions, declarative base
  accounts/       users, password hashing, sessions, access guards, login rate limit, bootstrap
  sources/        data sources: models, schemas, service, API
  collector/      delta engine, collection cycle, supervisor, `python -m query_pulse.collector`
  workload/       fingerprints, observations, table stats, plans; explorer, plan and trend APIs
  demo/           demo seed and workload (not part of the product)
  adapters/       PostgreSQL and MongoDB adapters (connect, version, capabilities)
  crypto.py       Fernet encryption for stored credentials
migrations/       Alembic migrations
demo/             demo database init scripts
scripts/          export_openapi.py
tests/
```

Planned packages (`findings/`, `ai/`) are added in the phase that needs them.

## Conventions

- **Secrets** are `SecretStr` in settings and never returned by the API or logged. Source
  credentials are Fernet-encrypted at rest and decrypted only in `sources/service.py`.
- **Write endpoints** must depend on `Writer` (`query_pulse/accounts/dependencies.py`).
- **Logging**: use `logging.getLogger(__name__)`; pass structured fields with `extra=`. Never
  format a secret, a connection string or a query string into a message.
- **SQL sent to a monitored database** is fixed text in `adapters/postgres.py`, marked with
  `/* query-pulse */`. The one exception, EXPLAIN of collected statement text, goes through
  `plannable_text()` first (see ADR 0004).
- **API changes**: run `make openapi` and commit `openapi.json` in the same commit.

## License

Licensed under the [Apache License 2.0](LICENSE). Copyright 2026 Rishabh Pandey.

If you redistribute this project or a derivative of it, keep the [NOTICE](NOTICE) file with it,
as section 4(d) of the license requires.
