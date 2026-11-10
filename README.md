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
- **Access rules**: every API endpoint except health, sign-in and sign-out returns 401 without a
  session; the demo user gets 403 on every write. Both are tested against every endpoint in
  `openapi.json`, so new endpoints are covered automatically.
- **API contract**: `openapi.json` is generated from code and committed; CI fails if it is stale.
- **Local stack** (`make demo`): app database, a demo PostgreSQL 16 with `pg_stat_statements` and
  a read-only `pg_monitor` role, and MongoDB 7 as a single-node replica set.

**Placeholders until Week 2:** the `/api/sources` endpoints use an in-memory registry. Test
connection only validates parameters, health reports `unknown`, and the workload preview returns
canned data. Phase 2 replaces them with real adapters and stored, encrypted sources.

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

Tests run against a fresh SQLite database per test, built by the real Alembic migrations, so
they need no Docker. Integration tests against real PostgreSQL and MongoDB (testcontainers)
arrive with Phase 2.

## Layout

```
query_pulse/
  main.py         app factory, /health and /ready
  config/         settings validated from the environment
  db.py           engine, sessions, declarative base
  accounts/       users, password hashing, sessions, access guards, bootstrap command
  sources/        data sources (Phase 1 placeholder)
migrations/       Alembic migrations
demo/             demo database init scripts
scripts/          export_openapi.py
tests/
```

Planned packages (`adapters/`, `collector/`, `workload/`, `findings/`, `ai/`) are added in the
phase that needs them.

## Conventions

- **Secrets** are `SecretStr` in settings and never returned by the API or logged. Source
  credentials will be Fernet-encrypted at rest (Phase 2).
- **Write endpoints** must depend on `Writer` (`query_pulse/accounts/dependencies.py`).
- **API changes**: run `make openapi` and commit `openapi.json` in the same commit.
