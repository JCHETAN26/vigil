-- Promote the eval-only span attributes to typed columns, so eval queries and the daily
-- rollup can read them as columns instead of digging through the span_attributes map.
-- These stay in span_attributes only for spans that predate this migration; the writer now
-- takes them out and fills the columns (see ingest/internal/normalize).
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS is idempotent and leaves existing span data
-- intact (existing rows get the defaults: eval_trial=-1, role='', cache_hit=0).

ALTER TABLE spans
    ADD COLUMN IF NOT EXISTS eval_trial Int32 DEFAULT -1 CODEC(T64, ZSTD(1)) AFTER eval_case_id;

ALTER TABLE spans
    ADD COLUMN IF NOT EXISTS role LowCardinality(String) DEFAULT '' AFTER session_id;

ALTER TABLE spans
    ADD COLUMN IF NOT EXISTS cache_hit UInt8 DEFAULT 0 CODEC(ZSTD(1)) AFTER cost_usd;
