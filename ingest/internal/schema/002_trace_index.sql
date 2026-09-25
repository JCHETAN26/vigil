-- trace_index: one aggregated row per trace (see §3.2). Powers Q1 window-pruning and
-- the dashboard trace/run list without scanning `spans`.
--
-- non_replicated_deduplication_window is set here too (an addition to the design doc's
-- DDL): with deduplicate_blocks_in_dependent_materialized_views=1 on the insert,
-- ClickHouse derives a per-view dedup token from the source token, but this target
-- table still needs its own window to remember that token — otherwise a redelivered
-- batch's MV rows would be written regardless.
CREATE TABLE IF NOT EXISTS trace_index
(
    trace_id       String,
    start_time     SimpleAggregateFunction(min, DateTime64(9)),
    end_time       SimpleAggregateFunction(max, DateTime64(9)),
    agent_id       SimpleAggregateFunction(any, LowCardinality(String)),
    agent_version  SimpleAggregateFunction(any, LowCardinality(String)),
    run_id         SimpleAggregateFunction(any, String),
    run_kind       SimpleAggregateFunction(any, Enum8('unknown'=0,'live'=1,'eval'=2)),
    service_name   SimpleAggregateFunction(any, LowCardinality(String)),
    span_count     SimpleAggregateFunction(sum, UInt64),
    error_count    SimpleAggregateFunction(sum, UInt64),
    total_tokens   SimpleAggregateFunction(sum, UInt64),
    total_cost_usd SimpleAggregateFunction(sum, Float64)
)
ENGINE = AggregatingMergeTree
ORDER BY (trace_id)
-- Same conditional TTL as spans (§3.1): eval traces kept 365 days, others 30 days, so
-- the run list this table backs never points at traces whose spans have expired. The
-- daily rollup keeps its own longer retention by design. start_time/run_kind here are the
-- min/any aggregates, which carry the underlying values TTL evaluates against.
TTL toDateTime(start_time) + INTERVAL 365 DAY DELETE WHERE run_kind = 'eval',
    toDateTime(start_time) + INTERVAL 30  DAY DELETE WHERE run_kind != 'eval'
SETTINGS non_replicated_deduplication_window = 1000;

-- Materialized view feeding trace_index on every insert into spans. All target columns
-- are SimpleAggregateFunction, so plain aggregates in the SELECT are correct (no -State).
CREATE MATERIALIZED VIEW IF NOT EXISTS trace_index_mv TO trace_index AS
SELECT
    trace_id,
    min(start_time)                       AS start_time,
    max(end_time)                         AS end_time,
    any(agent_id)                         AS agent_id,
    any(agent_version)                    AS agent_version,
    any(run_id)                           AS run_id,
    any(run_kind)                         AS run_kind,
    any(service_name)                     AS service_name,
    sum(toUInt64(1))                      AS span_count,
    sum(toUInt64(status_code = 'ERROR'))  AS error_count,
    sum(toUInt64(gen_ai_usage_total_tokens)) AS total_tokens,
    sum(cost_usd)                         AS total_cost_usd
FROM spans
GROUP BY trace_id;
