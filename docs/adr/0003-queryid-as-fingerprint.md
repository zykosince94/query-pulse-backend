# 0003. PostgreSQL's `queryid` is the fingerprint

**Status:** accepted (Phase 3)

## Context

Grouping executions of "the same query" needs a fingerprint. Tools either normalise SQL text
themselves (strip literals, collapse IN lists) or use the server's own identifier.

## Decision

The fingerprint is PostgreSQL's identity from `pg_stat_statements`,
`(dbid, userid, queryid, toplevel)`, per source. The representative text is the normalised text
PostgreSQL stores (`... WHERE id = $1`). There is no custom SQL normaliser.

## Consequences

- No parser to write or get wrong. Grouping matches exactly what PostgreSQL counted, so our
  numbers line up with `pg_stat_statements` itself.
- The same statement run by two roles, or nested in a function versus at the top level, is two
  operations. That's PostgreSQL's view, and sometimes it's what you want to know.
- `queryid` can change across major versions and with `search_path` differences, so a
  statement's history may split after an upgrade.
- MongoDB has no equivalent, so Phase 6 adds a shape normaliser for MongoDB only.
