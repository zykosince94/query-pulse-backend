# 0006. One collector instance

**Status:** accepted (Phase 3)

## Context

Collection could be spread over workers with a queue (Celery, Redis) and leader election, or run
as one asyncio process.

## Decision

One process, `python -m query_pulse.collector`, runs one asyncio task per enabled PostgreSQL
source, with start jitter, per-cycle timeouts and exponential backoff. It reconciles sources
every 30 s and writes a heartbeat. Running two instances is unsupported, and documented as such.

## Consequences

- No broker, no locks and no duplicate observations: each source's baseline has one writer.
- Async I/O makes hundreds of sources feasible from one process. The limit is the monitored
  databases, not the collector.
- A crashed collector stops collection until it restarts. Compose restarts it, and the UI shows
  "not running" from the stale heartbeat. High availability is out of scope.
