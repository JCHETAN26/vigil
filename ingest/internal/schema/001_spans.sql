-- spans: the raw span store (see docs/design/data-model.md §3.1).
-- Idempotency is enforced at insert time via a deterministic offset-range
-- insert_deduplication_token (§3.5); non_replicated_deduplication_window remembers
-- recent tokens on this single-node, non-replicated table. ReplacingMergeTree is the
-- merge-time safety net for any residual per-span duplication.
CREATE TABLE IF NOT EXISTS spans
(
    -- Trace/span identity (OTLP)
    trace_id        String CODEC(ZSTD(1)),
    span_id         String CODEC(ZSTD(1)),
    parent_span_id  String CODEC(ZSTD(1)),
    trace_state     String CODEC(ZSTD(1)),

    -- Timing
    start_time      DateTime64(9) CODEC(Delta, ZSTD(1)),
    end_time        DateTime64(9) CODEC(Delta, ZSTD(1)),
    duration_ns     UInt64 MATERIALIZED
                      toUInt64(dateDiff('nanosecond', start_time, end_time))
                      CODEC(T64, ZSTD(1)),

    -- Span descriptors
    span_name       LowCardinality(String),
    span_kind       Enum8('UNSPECIFIED'=0,'INTERNAL'=1,'SERVER'=2,
                          'CLIENT'=3,'PRODUCER'=4,'CONSUMER'=5),
    status_code     Enum8('UNSET'=0,'OK'=1,'ERROR'=2),
    status_message  String CODEC(ZSTD(1)),

    -- Resource identity (constant per process)
    service_name    LowCardinality(String),
    service_version LowCardinality(String),

    -- Vigil identity (resolved by the consumer)
    agent_id        LowCardinality(String),
    agent_version   LowCardinality(String),   -- content hash of {code,prompts,model,params,tools}
    git_sha         LowCardinality(String),   -- source commit, recorded alongside the hash
    run_id          String CODEC(ZSTD(1)),
    run_kind        Enum8('unknown'=0,'live'=1,'eval'=2),
    eval_run_id     String CODEC(ZSTD(1)),
    eval_case_id    String CODEC(ZSTD(1)),
    session_id      String CODEC(ZSTD(1)),

    -- GenAI semantic conventions (promoted hot columns)
    gen_ai_system              LowCardinality(String),
    gen_ai_operation_name      LowCardinality(String),   -- chat | embeddings | ...
    gen_ai_request_model       LowCardinality(String),
    gen_ai_response_model      LowCardinality(String),
    gen_ai_usage_input_tokens  UInt32 CODEC(T64, ZSTD(1)),
    gen_ai_usage_output_tokens UInt32 CODEC(T64, ZSTD(1)),
    gen_ai_usage_total_tokens  UInt32 MATERIALIZED
                      gen_ai_usage_input_tokens + gen_ai_usage_output_tokens,
    cost_usd        Float64 CODEC(ZSTD(1)),   -- computed by consumer; 0 if unknown

    -- Everything else, kept flexible
    resource_attributes Map(LowCardinality(String), String),
    span_attributes     Map(LowCardinality(String), String),

    -- Span events (nested): gen_ai prompt/completion + exception events.
    -- Declared as Nested, which ClickHouse flattens into the array subcolumns
    -- events.timestamp / events.name / events.attributes (attributes JSON-encoded).
    events Nested(
        timestamp  DateTime64(9),
        name       LowCardinality(String),
        attributes String
    ),

    -- Bookkeeping / idempotency version
    ingested_at     DateTime64(3) DEFAULT now64(3) CODEC(ZSTD(1)),

    -- Skip indexes
    INDEX idx_trace_id trace_id           TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_keys mapKeys(span_attributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_duration  duration_ns       TYPE minmax GRANULARITY 1
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toDate(start_time)
ORDER BY (agent_id, agent_version, run_kind, toStartOfHour(start_time), trace_id, span_id)
-- Conditional TTL: eval spans kept 1 year, everything else 30 days.
TTL toDateTime(start_time) + INTERVAL 365 DAY DELETE WHERE run_kind = 'eval',
    toDateTime(start_time) + INTERVAL 30  DAY DELETE WHERE run_kind != 'eval'
SETTINGS
    index_granularity = 8192,
    -- Insert deduplication for non-replicated MergeTree (single-node dev).
    -- Remembers the last N insert-block tokens; a redelivered batch with the
    -- same token is dropped before any rows (or MV rows) are written.
    non_replicated_deduplication_window = 1000;
