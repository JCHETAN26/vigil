-- Prompt-cache token columns. Anthropic reports cache-write (cache_creation) and cache-read
-- input tokens separately from ordinary input tokens; the writer stores them here and prices
-- them with CostWithCache (cache_write_5m / cache_read rates in prices.json), so a span's
-- cost_usd reflects caching. Ordinary (uncached) tokens stay in gen_ai_usage_input_tokens.
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS is idempotent; existing rows default to 0 (no
-- caching), which prices identically to before this migration.

ALTER TABLE spans
    ADD COLUMN IF NOT EXISTS gen_ai_usage_cache_creation_input_tokens UInt32 DEFAULT 0
        CODEC(T64, ZSTD(1)) AFTER gen_ai_usage_output_tokens;

ALTER TABLE spans
    ADD COLUMN IF NOT EXISTS gen_ai_usage_cache_read_input_tokens UInt32 DEFAULT 0
        CODEC(T64, ZSTD(1)) AFTER gen_ai_usage_cache_creation_input_tokens;
