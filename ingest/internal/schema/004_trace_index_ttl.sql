-- Add the conditional TTL to trace_index for databases created before it was part of the
-- CREATE (002). CREATE TABLE IF NOT EXISTS does not alter an existing table, so this
-- ALTER brings older installs in line; on a fresh install it re-applies the same TTL as a
-- no-op. Matches spans (§3.1): eval traces 365 days, others 30 days, so the run list never
-- points at traces whose spans have already expired.
ALTER TABLE trace_index MODIFY TTL
    toDateTime(start_time) + INTERVAL 365 DAY DELETE WHERE run_kind = 'eval',
    toDateTime(start_time) + INTERVAL 30  DAY DELETE WHERE run_kind != 'eval';
