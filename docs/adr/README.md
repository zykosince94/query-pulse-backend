# Architecture decision records

Short records of the decisions that shape Query Pulse: the context, the decision, and what it
costs. When a decision changes, a new record supersedes the old one.

| ADR | Decision |
|---|---|
| [0001](0001-single-postgresql.md) | One PostgreSQL for app data and telemetry |
| [0002](0002-deltas-over-totals.md) | Store per-interval deltas, not cumulative totals |
| [0003](0003-queryid-as-fingerprint.md) | PostgreSQL's `queryid` is the fingerprint |
| [0004](0004-generic-plans-only.md) | Generic plans only; never EXPLAIN ANALYZE by default |
| [0005](0005-cookie-sessions.md) | Signed cookie sessions, same origin, no tokens |
| [0006](0006-one-collector-instance.md) | One collector instance |
| [0007](0007-fernet-credentials.md) | Fernet for stored credentials |
| [0008](0008-rules-before-ai.md) | Rules detect problems; AI only explains |
| [0009](0009-finding-lifecycle.md) | One finding per rule and target; dismissals stick |
| [0010](0010-no-stored-query-values.md) | MongoDB: store shapes and redacted plans, never query values |
