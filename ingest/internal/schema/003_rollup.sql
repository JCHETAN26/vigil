-- agent_version_stats_daily: pre-aggregated per-version/day rollup (see §3.3). Keeps
-- latency (t-digest), token, and cost aggregates cheap and long-lived (~13 months) so
-- dashboards and results tables stay fast after raw spans expire.
--
-- This MV fires ON INSERT, before ReplacingMergeTree can dedup on merge, so a
-- redelivered batch would double-count here. The insert-time dedup token (§3.5) plus
-- deduplicate_blocks_in_dependent_materialized_views=1 is what prevents that; this
-- table therefore needs its own non_replicated_deduplication_window (addition to the
-- design doc's DDL) to remember the propagated per-view token.
CREATE TABLE IF NOT EXISTS agent_version_stats_daily
(
    day                Date,
    agent_id           LowCardinality(String),
    agent_version      LowCardinality(String),
    run_kind           Enum8('unknown'=0,'live'=1,'eval'=2),
    gen_ai_operation_name LowCardinality(String),
    gen_ai_response_model LowCardinality(String),
    runs               AggregateFunction(uniq, String),        -- distinct run_id
    span_count         SimpleAggregateFunction(sum, UInt64),
    error_count        SimpleAggregateFunction(sum, UInt64),
    duration_quantiles AggregateFunction(quantilesTDigest(0.5, 0.95, 0.99), UInt64),
    input_tokens       SimpleAggregateFunction(sum, UInt64),
    output_tokens      SimpleAggregateFunction(sum, UInt64),
    cost_usd           SimpleAggregateFunction(sum, Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(day)
ORDER BY (agent_id, agent_version, run_kind, gen_ai_operation_name, gen_ai_response_model, day)
TTL day + INTERVAL 400 DAY
SETTINGS non_replicated_deduplication_window = 1000;

-- Materialized view feeding the rollup on every insert into spans. AggregateFunction
-- columns (runs, duration_quantiles) use -State combinators; SimpleAggregateFunction
-- columns use plain sums. Mirrors the rebuild query in §3.7.
CREATE MATERIALIZED VIEW IF NOT EXISTS agent_version_stats_daily_mv TO agent_version_stats_daily AS
SELECT
    toDate(start_time)                                  AS day,
    agent_id,
    agent_version,
    run_kind,
    gen_ai_operation_name,
    gen_ai_response_model,
    uniqState(run_id)                                   AS runs,
    sum(toUInt64(1))                                    AS span_count,
    sum(toUInt64(status_code = 'ERROR'))                AS error_count,
    quantilesTDigestState(0.5, 0.95, 0.99)(duration_ns) AS duration_quantiles,
    sum(toUInt64(gen_ai_usage_input_tokens))            AS input_tokens,
    sum(toUInt64(gen_ai_usage_output_tokens))           AS output_tokens,
    sum(cost_usd)                                       AS cost_usd
FROM spans
GROUP BY day, agent_id, agent_version, run_kind,
         gen_ai_operation_name, gen_ai_response_model;
