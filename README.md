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

- **The collector** (`python -m query_pulse.collector`): polls every enabled source.
  - PostgreSQL: turns `pg_stat_statements`' cumulative counters into per-interval
    observations, and handles resets, evictions and restarts without ever storing a negative
    or inflated number. It also samples table and index statistics.
  - MongoDB: reads the operations the user's profiler recorded (`system.profile`) after a
    cursor, normalises each into a **query shape** (every value becomes `?`, field names and
    operators stay; golden files pin the rules) and stores one observation per shape per
    cycle, with documents and index keys examined. It also samples `serverStatus`
    (operation rates, connections, cache) and flags a full `system.profile`.
  - For both: re-checks capabilities, backs off when a database is down (and recovers on its
    own), and deletes data older than 14 days.
- **Workload read API**: per-minute trends per source and a health summary for the home page.
- **Workload explorer API**: PostgreSQL and MongoDB operations in one ranking, by total time,
  mean time or calls over any time window (the last N minutes, or absolute start/end),
  filtered by source and statement or shape text.
  Each operation has its metrics over the window and a history per minute or hour (`date_trunc`
  buckets in SQL). Mean time is total time ÷ calls over the whole window, never an average of
  averages.
- **Generic query plans**: `EXPLAIN (GENERIC_PLAN, FORMAT JSON)` on the normalised `$1` text,
  which never executes the statement (QP-AC-15 checks its call count doesn't move). The
  collector plans each source's 20 busiest operations every 5 minutes, and new hot operations
  within a cycle. Plans are stored as immutable evidence: a new row only when the plan changes.
  When a statement can't be planned (missing `SELECT` grant, cut-off text, utility statement),
  the reason is stored and shown instead of an empty viewer.
- **MongoDB plans**: `explain` at queryPlanner verbosity (nothing executes) of each busy read
  shape, from a recent profiled command the collector holds **in memory only**. The stored plan
  is redacted: filters normalised, index bounds kept only when they cover the whole index.
  Query Pulse never stores MongoDB query values ([ADR 0010](docs/adr/0010-no-stored-query-values.md)).
- **Rule-based findings**: after collection, a declarative rule engine checks each source's
  evidence and records findings, each with a confidence, the kind of evidence (a captured plan,
  or a statistics heuristic), the facts behind it, a suggested fix as SQL to review (never run),
  and links to the exact observations, plan and table samples it cites. Seven rules cover the
  demo's planted problems: PG-SEQSCAN-HOT (P1), PG-SORT-NO-INDEX (P2), PG-LEADING-WILDCARD (P3)
  and PG-UNUSED-INDEX (P4) for PostgreSQL; MDB-COLLSCAN (M1), MDB-BLOCKING-SORT (M2) and
  MDB-EXAMINED-RATIO (M3) for MongoDB, whose fixes are mongosh commands to review. There is
  one finding per rule and target: a dismissed finding stays dismissed, a resolved one reopens
  only on newer evidence, and open ones resolve on their own once the problem is gone.
- **AI assistant (optional, off by default)**: an admin saves an OpenAI key and a model in
  Settings; the key is checked with OpenAI first, stored encrypted and shown only by its last
  four characters. Then any finding can be **explained** or get a **draft fix** (SQL or a
  mongosh command to review, never run). The model sees only an **evidence bundle** built by
  code from the finding (literals redacted; no credentials, hosts or connection strings), which
  anyone can view, and must cite its items claim by claim: an answer citing evidence that
  wasn't sent, or drafting a destructive statement, is rejected. Answers are cached by the
  evidence, so the same question isn't paid for twice ([ADR 0011](docs/adr/0011-ai-evidence-and-citations.md)).
- **Hardening**: an Origin check on every state-changing request (cross-site writes get 403), a
  login rate limit (10 failures per client per 5 minutes, then 429), and JSON logs where every
  line carries the request ID, which is also returned in the `X-Request-ID` header.
  Schemathesis fuzzes every API operation from `openapi.json` on every push (500 examples each
  nightly); the integration tests also run on the newest PostgreSQL and MongoDB 8; a nightly
  one-hour soak test watches the collector's memory and cycle times.
- **Demo data**: a seeded e-commerce database in PostgreSQL with four planted problems (P1–P4),
  and orders and products in MongoDB with three (M1–M3), with the profiler on for `shop`. A
  workload runs them next to healthy controls, so `make demo` has real traffic to measure.

## Architecture

```
 React app ──/api──▶ FastAPI (API) ──────────────────────▶ PostgreSQL (app data: sources,
 (frontend repo)        │    │                              observations, plans, findings)
                        │    └─ optional, admin-enabled ─▶ OpenAI (redacted evidence bundle only)
                        │ test connection, capabilities          ▲
                        ▼                                        │ batched writes
                    adapters ◀──────────── collector (asyncio, one task per source)
                        │                    │  PostgreSQL: pg_stat_statements → deltas
                        ▼                    │  MongoDB: profiler → query shapes
        monitored PostgreSQL / MongoDB       └─ plans, rules → findings
        (read-only credentials)
```

Two processes from one codebase share one PostgreSQL database: the API serves the UI, and the
collector polls the monitored databases. Nothing generated (SQL, mongosh, AI drafts) is ever
run against them.

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
Within about three minutes the Findings page shows one finding for each planted problem, P1–P4,
and none for the healthy controls.

To try the AI assistant, open Settings, choose OpenAI, enter a model id and your API key, tick
"Turn the AI assistant on" and save; then open any finding's **AI assistant**. It stays off
otherwise, and makes no request.

Add the demo MongoDB the same way (engine MongoDB, host `mongo`, port 27017, database `shop`,
no username: the local demo runs without auth). The demo seed has already turned its profiler
on, as a user would; Query Pulse only reads it. Within a minute its query shapes join the same
explorer, and the Findings page shows M1 (lookups by `customerId`, a collection scan), M2 (the
latest orders sorted in memory) and M3 (an unanchored `$regex` reading the whole `sku` index).
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

Hardening runs as part of the suites and in CI:

| Check | Command | When |
|---|---|---|
| API fuzzing (Schemathesis) | `make fuzz` (`QP_FUZZ_EXAMPLES`, default 20) | every push; 500 examples nightly |
| Version matrix | `QP_TEST_POSTGRES_IMAGE=postgres:latest QP_TEST_MONGO_IMAGE=mongo:8 uv run pytest -m integration` | every push |
| Soak test (collector memory, cycle times) | `make soak` (needs `make demo`) | nightly, one hour |

**Windows:** psycopg's async mode can't use Windows' default event loop. The tests handle this
themselves. To run the API outside Docker on Windows, use `--reload` (as above), which makes
uvicorn use a compatible loop, or run it with `make demo`.

## Documentation

- [Functionality reference](docs/functionality.md): every screen, endpoint, setting and security
  behaviour across the backend and frontend, as of today.
- [Demo checklist](docs/demo-checklist.md): the scripted demo, run by hand before every release.
- [Architecture decision records](docs/adr/): why the stack looks the way it does.

## AI evaluation

`make ai-eval` asks the configured model to explain and draft a fix for each of the seven
planted problems (saved evidence bundles in `evals/bundles/`), runs the same checks as the API,
and scores the answers: the right cause named, citations valid, no forbidden action. It needs a
real key (`QP_AI_EVAL_API_KEY`, `QP_AI_EVAL_MODEL`) and writes `evals/results.md`.

Scores: not yet recorded. `uv run python scripts/ai_eval.py --fake` checks the harness without a key.

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
  collector/      delta engine, collection cycle (PostgreSQL; MongoDB in mongo.py), supervisor
  workload/       fingerprints, observations, table stats, plans; explorer, plan and trend APIs;
                  MongoDB shape normaliser (mongo_shape.py) and plan redaction (mongo_plan.py)
  findings/       rules (rules.py), rule engine and lifecycle (engine.py), findings API
  ai/             evidence bundles, the provider interface (OpenAI, fake), citation checks, API
  demo/           demo seed and workload (not part of the product)
  adapters/       PostgreSQL and MongoDB adapters (connect, capabilities, collection reads)
  crypto.py       Fernet encryption for stored credentials
migrations/       Alembic migrations
demo/             demo database init scripts
scripts/          export_openapi.py, ai_eval.py, save_eval_bundles.py, soak.py
evals/            the AI evaluation set and its results
tests/
```

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
