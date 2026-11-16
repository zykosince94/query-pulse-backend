# 0002. Store per-interval deltas, not cumulative totals

**Status:** accepted (Phase 3)

## Context

`pg_stat_statements` counters are cumulative since the last reset. A tool can store raw
snapshots and subtract at read time, or subtract at collection time and store the difference.

## Decision

The collector stores **deltas**: for each statement, the current counters minus the previous
snapshot, as one observation per interval. The interval's mean is Δtotal_exec_time ÷ Δcalls.
The last raw snapshot is persisted per source as the baseline, so a collector restart continues
correctly. If any counter goes down, or `stats_reset` changes, the interval is discarded and the
current snapshot becomes the new baseline. A rising `dealloc` (evictions) marks the source
degraded for an hour.

## Consequences

- Reads are simple sums over a window, with no reset logic in every query, and a reset can never
  show up as a negative or inflated number (QP-AC-10).
- Each stored number can be checked: the 500-call cross-check (QP-AC-09) compares a delta with an
  independent diff of `pg_stat_statements`.
- One interval of activity is lost at every reset, eviction gap or baseline change. That's
  reported, not hidden.
- The delta engine is table-driven and unit-tested case by case: normal, decrease, reset, new,
  evicted, restart.
