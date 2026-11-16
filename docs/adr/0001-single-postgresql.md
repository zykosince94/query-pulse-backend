# 0001. One PostgreSQL for app data and telemetry

**Status:** accepted (Phase 1)

## Context

Query Pulse stores two kinds of data: application state (users, sources, capabilities,
findings) and telemetry (per-interval observations, relation samples, plans). Telemetry is
time-series shaped, which suggests a time-series database such as TimescaleDB, InfluxDB or
ClickHouse.

## Decision

Both live in one PostgreSQL database, as plain tables with time indexes:
`(fingerprint_id, interval_start)` and `(source_id, interval_start)`. Retention is a daily
`DELETE` in the collector (default 14 days). Aggregation is ordinary SQL (`sum`, `date_trunc`).

## Consequences

- One service to run, back up and reason about, and one migration tool. Joins between telemetry
  and metadata (statement text, source names) are plain SQL.
- The volume is modest: one row per active statement per source per interval. 200 statements at
  60 s is about 290,000 rows a day, well within plain PostgreSQL.
- A hosted, multi-tenant version with many sources would want partitioning or a columnar store.
  That's out of scope for the MVP (one admin, local or small deployments).
