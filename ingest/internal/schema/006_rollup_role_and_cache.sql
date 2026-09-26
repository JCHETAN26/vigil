-- Update agent_version_stats_daily (003) for the new eval columns:
--   1. Add a `role` dimension so simulator spans (vigil.role='user_simulator') aggregate
--      SEPARATELY from the agent's own spans (role=''). role must be part of the sorting key,
--      or AggregatingMergeTree would merge agent and simulator rows that share every other
--      key into one — hiding the split we want.
--   2. Exclude cache hits from cost: a dev cache hit records real tokens on its span but no
--      real spend, so cost sums must skip them (sumIf(cost_usd, cache_hit = 0)).
--
-- Adding a column to a MergeTree sorting key cannot be done with an idempotent in-place ALTER
-- (MODIFY ORDER BY only accepts freshly-added columns and errors on re-run), so the rollup is
-- recreated with the new key. This is safe: the rollup is DERIVED from `spans` and rebuilds as
-- new spans arrive (a full backfill query is in the design doc §3.7 if the pre-TTL window must
-- be restored). Re-running this migration is idempotent in effect — it converges to the schema
-- below without error — but it does reset the derived aggregates, so run it at deploy time.

DROP VIEW IF EXISTS agent_version_stats_daily_mv;
DROP TABLE IF EXISTS agent_version_stats_daily;

CREATE TABLE IF NOT EXISTS agent_version_stats_daily
(
    day                Date,
    agent_id           LowCardinality(String),
    agent_version      LowCardinality(String),
    run_kind           Enum8('unknown'=0,'live'=1,'eval'=2),
    role               LowCardinality(String),                  -- '' = agent; 'user_simulator' = sim
    gen_ai_operation_name LowCardinality(String),
    gen_ai_response_model LowCardinality(String),
    runs               AggregateFunction(uniq, String),         -- distinct run_id
    span_count         SimpleAggregateFunction(sum, UInt64),
    error_count        SimpleAggregateFunction(sum, UInt64),
    duration_quantiles AggregateFunction(quantilesTDigest(0.5, 0.95, 0.99), UInt64),
    input_tokens       SimpleAggregateFunction(sum, UInt64),
    output_tokens      SimpleAggregateFunction(sum, UInt64),
    cost_usd           SimpleAggregateFunction(sum, Float64)    -- excludes cache hits (see MV)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(day)
ORDER BY (agent_id, agent_version, run_kind, role, gen_ai_operation_name, gen_ai_response_model, day)
TTL day + INTERVAL 400 DAY
SETTINGS non_replicated_deduplication_window = 1000;

CREATE MATERIALIZED VIEW IF NOT EXISTS agent_version_stats_daily_mv TO agent_version_stats_daily AS
SELECT
    toDate(start_time)                                  AS day,
    agent_id,
    agent_version,
    run_kind,
    role,
    gen_ai_operation_name,
    gen_ai_response_model,
    uniqState(run_id)                                   AS runs,
    sum(toUInt64(1))                                    AS span_count,
    sum(toUInt64(status_code = 'ERROR'))                AS error_count,
    quantilesTDigestState(0.5, 0.95, 0.99)(duration_ns) AS duration_quantiles,
    sum(toUInt64(gen_ai_usage_input_tokens))            AS input_tokens,
    sum(toUInt64(gen_ai_usage_output_tokens))           AS output_tokens,
    sumIf(cost_usd, cache_hit = 0)                      AS cost_usd
FROM spans
GROUP BY day, agent_id, agent_version, run_kind, role,
         gen_ai_operation_name, gen_ai_response_model;
