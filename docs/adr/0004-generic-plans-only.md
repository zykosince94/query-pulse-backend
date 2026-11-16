# 0004. Generic plans only; never EXPLAIN ANALYZE by default

**Status:** accepted (Phase 4)

## Context

To show a statement's plan, a tool can run `EXPLAIN ANALYZE` (which executes the statement and
needs real parameter values), capture plans from logs (`auto_explain`, which needs server
configuration), or plan the normalised text. `pg_stat_statements` stores text with `$1`
placeholders and no values.

## Decision

Plans come from `EXPLAIN (GENERIC_PLAN, FORMAT JSON)` (PostgreSQL 16+) on the stored `$1` text.
It needs no parameter values and **never executes the statement**: QP-AC-15 proves the call
count doesn't move. The collector plans each source's busiest operations in the background and
stores plans as immutable rows (a new row only when the plan changes), so findings can cite
them. EXPLAIN goes over the simple query protocol (`prepare=False`), because a prepared EXPLAIN
treats `$1` as its own parameter and fails when none is bound.

Collected text is influenced by every user of the monitored database, so before planning:
text containing a `;` other than a trailing one is refused, only plannable statement types are
sent, and cut-off text is skipped. The session stays read-only with a 5 s statement timeout.

## Consequences

- Works with only `pg_monitor` plus `SELECT` on the tables, and no server configuration.
- The plan is the planner's choice for *unknown* parameter values. For skewed data, the plan for
  a specific value can differ, so the UI labels plans as generic estimates.
- On PostgreSQL older than 16, plans show as unavailable, with the upgrade as the fix.
- Planning still evaluates immutable functions during constant folding, with the monitoring
  role's rights. Read-only sessions and the timeout bound what that can do.
- EXPLAIN ANALYZE, behind explicit confirmation and in a rolled-back read-only transaction, stays
  a stretch goal.
