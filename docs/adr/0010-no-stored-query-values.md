# 0010. MongoDB: store shapes and redacted plans, never query values

**Status:** accepted (Phase 6)

## Context

PostgreSQL hands Query Pulse normalised statements: `pg_stat_statements` replaces literals
with `$1`, and `EXPLAIN (GENERIC_PLAN)` plans that text as is. MongoDB has neither. Its
profiler records each slow operation with its real command (`{customerId: 42}`,
`{email: "..."}`), and explain needs a real command to plan.

Two designs were considered:

- **(a)** Store one recent sample command per shape, so a plan can be captured at any time,
  even after a collector restart or on demand from the UI.
- **(b)** Keep commands only in collector memory, just long enough to explain them, and
  persist only the normalised shape and a redacted plan.

Option (a) turns a monitoring product into another copy of the customer's data, in a
database the customer doesn't control.

## Decision

Option (b), as the default architecture:

- The **shape** (`workload/mongo_shape.py`) is what's stored and shown: every value is `?`,
  field names and operators are kept. Its sha256 is the fingerprint.
- The newest command of each read shape is held in the collector's `SampleCache` (process
  memory, bounded, per source, tied to the server it came from) and used only for
  `explain` at queryPlanner verbosity.
- Explain output is **redacted** before it's stored (`workload/mongo_plan.py`): filters
  normalised, index bounds kept only when they are full ranges, limit amounts and the SBE
  plan text dropped. Explain errors keep only MongoDB's error code, because its messages can
  quote the query.
- No log line carries a value: `ProfileEntry` and `Sample` hide the command from `repr`, and
  PyMongo's debug loggers (which print whole commands and replies) are held at WARNING.

## Consequences

- What Query Pulse stores about a MongoDB source holds no customer data. The integration test
  runs queries with a marker value and checks that it appears nowhere in the app database or
  the logs.
- The collector stays stateless as far as persistence is concerned: losing its memory loses
  nothing that can't be seen again.
- After a collector restart, a shape's plan can only be captured again once the profiler
  records it again. There's no on-demand "capture now" for MongoDB plans.
- A plan reflects one sample command. For a shape whose plan depends on the values (a range
  that's narrow for some values and wide for others), the stored plan is the plan for the
  most recent sample, not for every call.
