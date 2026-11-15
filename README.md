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

Not yet: the collector (Phase 3) and everything built on it. `GET /api/collector/status`
reports `not_running` until then.

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
PostgreSQL 16 and MongoDB 7 containers (testcontainers) for the permission matrix and connection
failures. They need Docker; without it they're skipped locally, and CI always runs them.

**Windows:** psycopg's async mode can't use Windows' default event loop. The tests handle this
themselves. To run the API outside Docker on Windows, use `--reload` (as above), which makes
uvicorn use a compatible loop, or run it with `make demo`.

## Documentation

- [Functionality reference](docs/functionality.md): every screen, endpoint, setting and security
  behaviour across the backend and frontend, as of today.

## Layout

```
query_pulse/
  main.py         app factory, /health and /ready
  config/         settings validated from the environment
  db.py           engine, sessions, declarative base
  accounts/       users, password hashing, sessions, access guards, bootstrap command
  sources/        data sources: models, schemas, service, API
  adapters/       PostgreSQL and MongoDB adapters (connect, version, capabilities)
  crypto.py       Fernet encryption for stored credentials
migrations/       Alembic migrations
demo/             demo database init scripts
scripts/          export_openapi.py
tests/
```

Planned packages (`collector/`, `workload/`, `findings/`, `ai/`) are added in the
phase that needs them.

## Conventions

- **Secrets** are `SecretStr` in settings and never returned by the API or logged. Source
  credentials are Fernet-encrypted at rest and decrypted only in `sources/service.py`.
- **Write endpoints** must depend on `Writer` (`query_pulse/accounts/dependencies.py`).
- **API changes**: run `make openapi` and commit `openapi.json` in the same commit.
