-- Runs once, when the demo-postgres volume is first created.
-- Sets up what Query Pulse needs on a monitored PostgreSQL (see the blueprint's documented
-- minimums): pg_stat_statements, and a read-only role with pg_monitor.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- LOCAL DEMO credentials only.
CREATE ROLE querypulse_monitor LOGIN PASSWORD 'monitor';
GRANT pg_monitor TO querypulse_monitor;
GRANT CONNECT ON DATABASE shop TO querypulse_monitor;
GRANT USAGE ON SCHEMA public TO querypulse_monitor;
-- SELECT is needed to EXPLAIN (GENERIC_PLAN) queries against these tables in Phase 4.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO querypulse_monitor;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO querypulse_monitor;
