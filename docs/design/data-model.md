# Vigil trace data model

Status: **accepted** · Scope: how Vigil stores and identifies traces. No code yet.

Vigil ingests standard **OTLP** traces and expects producers to follow the
[OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
(`gen_ai.*` attributes). This document specifies:

1. The **ClickHouse** schema for spans — engine, `ORDER BY`, partitioning, TTL —
   tuned for three query classes.
2. How **agent runs**, **eval runs**, and **agent versions** are identified from
   resource and span attributes.
3. The **Redpanda** topic layout between the ingest service and the writer.

The three query classes we optimize for:

- **Q1 — trace view:** all spans for one `trace_id` (point lookup, dashboard trace viewer).
- **Q2 — runs of a version:** all runs of a given agent version within a time range.
- **Q3 — aggregations:** latency / token / cost rollups, grouped by agent version,
  model, or operation, over a time range (regression detection, results tables).

Design principle: ClickHouse rewards **wide, denormalized, append-only** tables.
We denormalize identity onto every span so all three queries are single-table
scans with no joins, and we accept some storage redundancy to get there.

---

## 1. Ingestion assumptions

- **Transport:** OTLP over gRPC and HTTP (protobuf `TracesData` / `ResourceSpans`).
- **Delivery:** at-least-once from Redpanda → duplicate spans are possible. We
  make ingestion **idempotent at insert time** using ClickHouse insert
  deduplication keyed on a deterministic offset-range token (§3.5), with
  `ReplacingMergeTree` on `(trace_id, span_id)` as a second-line safety net.
- **Enrichment happens in the consumer**, not the SDK: the consumer promotes known
  attribute keys to typed columns, computes `cost_usd` from model + token counts
  via a pricing table, resolves `run_kind`, and leaves everything else in maps.
- **Sampling:** live traffic may be sampled in future (head/tail sampling is an
  open door, not yet built). **Eval runs are never sampled** — every span of every
  eval case is ingested at 100%, because regression detection needs the complete
  per-run distribution, not an estimate.

---

## 2. Identity model

The hardest modeling question is *what identifies an agent, a run, and an eval*,
and *where each identifier lives* (resource vs span attribute). The rule:

> Identity that is **constant for the whole process** goes in **resource
> attributes**. Identity that **varies per execution within a process** goes in
> **span attributes** (propagated to every span via context/baggage).

A single eval worker process runs many cases back-to-back, so run/eval identity
*cannot* be a resource attribute — it changes per case. Agent identity *is* a
property of the deployed/tested artifact, so it *is* a resource attribute.

### 2.1 Resource attributes (constant per process)

| OTLP / Vigil key        | Column            | Meaning |
|-------------------------|-------------------|---------|
| `service.name`          | `service_name`    | OTel service (e.g. `support-agent`). |
| `service.version`       | `service_version` | Deploy/build version of the service. |
| `vigil.agent.id`        | `agent_id`        | **Stable logical agent** under test (e.g. `tau-bench-retail`). Does not change across versions. |
| `vigil.agent.version`   | `agent_version`   | **The thing regressions attach to.** Content hash — see 2.3. |
| `vigil.agent.git_sha`   | `git_sha`         | Source commit of the agent, recorded separately from the behavioral hash. |

### 2.2 Span attributes (vary per execution; propagated to all spans)

| Vigil key             | Column         | Meaning |
|-----------------------|----------------|---------|
| `vigil.run.id`        | `run_id`       | One agent execution. Defaults to `trace_id` when absent. |
| `vigil.run.kind`      | `run_kind`     | `live` or `eval` (enum). |
| `vigil.eval.run_id`   | `eval_run_id`  | The eval **batch** job id (one row per case shares it). |
| `vigil.eval.case_id`  | `eval_case_id` | Dataset item id (HotpotQA question id, tau-bench task id). |
| `vigil.eval.dataset`  | in `span_attributes` | Dataset name + version. |
| `gen_ai.conversation.id` | `session_id` | Multi-turn conversation/session. |

**Why denormalize run/eval identity onto *every* span** (not just the root):
Q2 and Q3 filter spans by agent version and run without knowing the root span
id. If `run_id`/`agent_version` lived only on the root, every analytical query
would need a self-join to the root span. Propagating them to all spans (the SDK
carries them in context) turns those into flat `WHERE`-clause scans. Cost: a few
`LowCardinality`/short-string columns repeated per span — cheap under ClickHouse
compression, and `LowCardinality` dictionaries make repeated `agent_version`
values nearly free.

**`run_id` is kept separate from `trace_id`** (decided). For a simple run they are
1:1 and the consumer defaults `run_id := trace_id`, but a run legitimately spans
**multiple traces**: a human-in-the-loop pause resumes as a new trace, and a retry
after a failure starts a fresh trace while belonging to the same logical run.
`run_id` is the stable key that stitches those traces (and multi-turn sessions)
together; collapsing it into `trace_id` would lose that grouping and force joins
to reconstruct a run.

### 2.3 What is an "agent version"?

`agent_version` is the unit regression detection compares against a baseline, so
it must change **exactly when the agent's behavior can change**. Decided:
`agent_version` is a **content hash** over `{code, prompt templates, model id,
decoding params, tool definitions}`. This changes on any behavior-affecting edit —
a prompt tweak with no version bump still produces a new hash, so it can't slip
past the detector. (Semver alone would be too coarse for exactly that reason.)

The **git SHA is recorded separately** as `vigil.agent.git_sha` → `git_sha`. The
two are complementary: the content hash answers "did behavior change?"; the git
SHA answers "which commit produced it?" A commit can touch unrelated files without
changing the hash (same behavior, new SHA), and — in principle — the hash can
differ within one commit if runtime config varies. Keeping both lets us group by
behavior while still linking back to source.

Full configuration per hash is stored in a Postgres **version manifest** (§3.6),
so any two `agent_version`s can be diffed to see exactly what changed.

---

## 3. ClickHouse schema

### 3.1 `spans` — the raw span store

```sql
CREATE TABLE spans
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

    -- Span events (nested): gen_ai prompt/completion + exception events
    events.timestamp  Array(DateTime64(9)),
    events.name       Array(LowCardinality(String)),
    events.attributes Array(String),          -- JSON-encoded per event

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
```

**Engine — `ReplacingMergeTree(ingested_at)`, as a safety net.** Primary
idempotency is enforced at **insert time** (§3.5); `ReplacingMergeTree` is the
second line of defence for any duplicate that a non-deterministic batch boundary
lets slip through. Rows sharing the full sorting key are collapsed to the latest
`ingested_at` on merge; the sort key ends in the globally unique
`(trace_id, span_id)`. Caveat: this dedup is **eventual** (at merge time, within a
partition) — which is exactly why it can't be the *only* mechanism: the rollup
materialized views (§3.3) fire on insert, long before a merge runs, so a duplicate
that reaches insert would be double-counted regardless of Replacing. Insert-time
dedup (§3.5) is what actually protects the rollups. Queries needing exact dedup on
raw spans still use `FINAL` or a `LIMIT 1 BY (trace_id, span_id)` wrapper.

**`ORDER BY (agent_id, agent_version, run_kind, toStartOfHour(start_time), trace_id, span_id)`.**
This is optimized for **Q2 and Q3**, the platform's core analytical value: the
primary index lets ClickHouse seek directly to a given `(agent_id,
agent_version)` and, within it, a time bucket. `run_kind` sits early so
eval-vs-live scans stay contiguous. `toStartOfHour(start_time)` clusters a
version's spans by hour for good time-range locality without exploding the index.
`trace_id` groups a trace's spans together within a version, which also speeds
Q1's granule reads.

The cost of this choice: `trace_id` is *not* the leading key, so a bare
`WHERE trace_id = …` (Q1) can't use the primary index. We cover Q1 two ways:
- the `idx_trace_id` **bloom-filter skip index**, which skips granules that can't
  contain the id (trace_ids are unique and highly selective), and
- the `trace_index` table (§3.2), which resolves a trace's time window so the
  query can also prune partitions.

**`PARTITION BY toDate(start_time)` (daily).** Daily parts keep each partition
small enough to merge cheaply on this machine and make TTL a cheap
whole-partition drop. With 30-day retention that's ≤30 active partitions. For low
volume, weekly (`toMonday`) would cut part count further; daily is the safe
default and is easy to change.

**TTL — conditional, `run_kind`-aware.** Live/other spans expire after **30 days**
(large, high-volume, short useful life). **Eval spans are kept 365 days** via a
second `DELETE WHERE run_kind = 'eval'` clause, so eval baselines and historical
regression comparisons survive well beyond the live-traffic window. TTL is
evaluated per part on merge, so both clauses coexist in one table without
splitting eval into a separate table. A tiered variant
(`… + 7 DAY TO VOLUME 'cold'`) is available if we add a slower disk later.

**Types & codecs.** `Delta` on the monotonic timestamps and `T64` on the integer
counters compress well; `LowCardinality` for the many repeated identity/model
values gives near-free dictionary encoding. Hot GenAI attributes are promoted to
typed columns (fast filters/aggregates); the long tail stays in `Map(...)` for
flexibility. Ids are `String` (hex) for ergonomics — `FixedString(16)`/`(8)`
would save space and speed comparisons at the cost of readability (open question).

### 3.2 `trace_index` — per-trace summary (powers Q1 pruning + the run list)

One row per trace: resolves the time window for Q1 and backs the dashboard's
trace/run list without scanning `spans`.

```sql
CREATE TABLE trace_index
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
ORDER BY (trace_id);
-- populated by a materialized view on inserts to `spans`
```

Q1 then becomes: look up `[start_time, end_time]` here, then query `spans` with
both `trace_id` and the time bound so partition pruning + the bloom index apply.
`trace_index` is intentionally not partitioned (one small row per trace, keyed
for point lookups); its rows age out with the source data via a periodic prune
job. *(Caveat: `any`-aggregating identity assumes a trace has one agent/version,
which holds because identity is denormalized consistently across a trace.)*

### 3.3 `agent_version_stats_daily` — pre-aggregated rollup (Q3 at scale, long retention)

```sql
CREATE TABLE agent_version_stats_daily
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
TTL day + INTERVAL 400 DAY;
-- populated by a materialized view on inserts to `spans`
```

Keeps latency (t-digest), token, and cost aggregates per version/day cheaply and
for ~13 months, so dashboards and the results tables stay fast even after raw
spans expire. Regression detection still bootstraps over **raw** per-run values
inside the retention window (it needs the distribution, not just quantiles); the
rollup serves the long-horizon trend view.

> **Correctness note:** this materialized view fires **on insert**, *before*
> `ReplacingMergeTree` deduplicates on merge. A redelivered Redpanda batch would
> therefore be counted twice in the rollup even though the raw table eventually
> collapses it. This is precisely why idempotency must be enforced at insert time
> (§3.5), not left to the engine's merge-time dedup.

### 3.4 Example queries

```sql
-- Q1: all spans for a trace (after resolving the window from trace_index)
SELECT * FROM spans
WHERE trace_id = {tid:String}
  AND start_time BETWEEN {from:DateTime64} AND {to:DateTime64}
ORDER BY start_time;

-- Q2: runs of an agent version in a time range
SELECT run_id,
       min(start_time)                         AS started,
       (max(end_time) - min(start_time)) / 1e6 AS wall_ms,
       sum(cost_usd)                           AS cost
FROM spans
WHERE agent_id = {a:String} AND agent_version = {v:String}
  AND start_time >= {from:DateTime64} AND start_time < {to:DateTime64}
GROUP BY run_id;

-- Q3: latency / token / cost aggregation by version (raw)
SELECT agent_version,
       quantile(0.50)(duration_ns) / 1e6 AS p50_ms,
       quantile(0.95)(duration_ns) / 1e6 AS p95_ms,
       sum(gen_ai_usage_total_tokens)    AS tokens,
       round(sum(cost_usd), 4)           AS cost
FROM spans
WHERE agent_id = {a:String}
  AND gen_ai_operation_name = 'chat'
  AND start_time >= now() - INTERVAL 7 DAY
GROUP BY agent_version;

-- Q3 (long horizon, from the rollup)
SELECT agent_version, day,
       quantilesTDigestMerge(0.5, 0.95)(duration_quantiles) AS q,
       uniqMerge(runs)                                       AS n_runs,
       sum(cost_usd)                                         AS cost
FROM agent_version_stats_daily
WHERE agent_id = {a:String} AND day >= today() - 180
GROUP BY agent_version, day;
```

### 3.5 Idempotent inserts (deterministic offset-range dedup)

At-least-once consumption means the same Redpanda records can be re-read after a
consumer crash or rebalance. Because the rollup MVs fire on insert (§3.3),
duplicates must be stopped *at insert*, not merely reconciled later. The scheme:

1. The consumer builds each ClickHouse insert batch from a **deterministic
   contiguous offset range per partition** — never a time- or size-triggered
   boundary that could differ across replays. A batch is exactly
   `partition P, offsets [start, end]`.
2. It sends the batch with an **`insert_deduplication_token`** derived from
   `topic : partition : start_offset : end_offset` (all four; the topic guards
   against reuse across topics).
3. The table sets `non_replicated_deduplication_window = 1000` (single-node dev),
   so ClickHouse remembers recent insert tokens and **drops a re-sent batch in
   full before writing any rows — and therefore before any MV fires.**

Determinism is the crux: the token only protects against duplication if a replay
reconstructs the *identical* batch. So the consumer must reproduce the same offset
ranges after a restart (commit offsets only for fully-inserted ranges; on
recovery, resume from the last committed offset and re-batch by the same rule).

Why keep `ReplacingMergeTree` too: the two layers cover different failure modes.
Insert dedup stops whole-batch redelivery (protecting the rollups); Replacing
catches any residual per-span duplication from an unforeseen boundary change,
manual backfill, or reprocessing from `otlp.spans.raw` with a different batching.
Defence in depth, cheap on both sides.

### 3.6 Postgres — `version_manifests`

The `agent_version` content hash is opaque; to *diff* two versions we need the
full configuration each hash was computed from. That lives in Postgres (metadata
store), not ClickHouse:

```sql
CREATE TABLE version_manifests (
    agent_id      text        NOT NULL,
    agent_version text        NOT NULL,   -- the content hash
    git_sha       text        NOT NULL,
    model         text        NOT NULL,
    params        jsonb       NOT NULL,   -- decoding params (temperature, top_p, ...)
    prompts       jsonb       NOT NULL,   -- prompt templates {name -> text}
    tools         jsonb       NOT NULL,   -- tool/function definitions
    code_ref      jsonb,                  -- optional: file hashes, entrypoint, deps
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, agent_version)
);
```

The consumer (or the SDK at startup) upserts a manifest the first time it sees a
new hash. Diffing two versions is then a plain row-vs-row `jsonb` comparison,
which powers the investigation agent's "what changed between the good and bad
version?" step. Storing full prompts/tools as `jsonb` keeps them queryable and
diffable; ClickHouse only ever holds the hash + git SHA.

### 3.7 Replay & rollup rebuild procedure

Two distinct cases:

- **Normal redelivery** (consumer crash, rebalance, at-least-once re-read):
  covered automatically by insert dedup (§3.5). The re-sent offset-range batch
  carries the same `insert_deduplication_token` and is dropped before any rows or
  rollup MV rows are written. **No manual action.**

- **Deliberate replay** (reprocessing `otlp.spans.raw` after an enrichment/cost
  fix, or a backfill older than the dedup window): the insert token may not match,
  so the same span can be re-inserted, and the on-insert rollup MVs would
  double-count. `ReplacingMergeTree` will eventually collapse the raw duplicates,
  but the rollups have no such self-healing. So after any deliberate replay, the
  affected rollup partitions must be **rebuilt from the deduplicated raw spans.**

Rebuild recipe (per affected day/month partition), to be scripted later in
`bench/` or `deploy/`:

1. Let the replay finish and let `spans` merges settle (optionally
   `OPTIMIZE TABLE spans PARTITION <p> FINAL` to force the dedup collapse).
2. `ALTER TABLE agent_version_stats_daily DROP PARTITION <month>;`
3. Re-derive the rollup from **deduplicated** raw spans with
   `INSERT INTO agent_version_stats_daily SELECT ...`, reading `spans` either with
   `FINAL` or via an explicit `argMax(..., ingested_at)` / `LIMIT 1 BY
   (trace_id, span_id)` dedup so each span is counted once. Sketch:

   ```sql
   INSERT INTO agent_version_stats_daily
   SELECT
       toDate(start_time) AS day,
       agent_id, agent_version, run_kind,
       gen_ai_operation_name, gen_ai_response_model,
       uniqState(run_id),
       sumState(1::UInt64),                          -- span_count
       sumState((status_code = 'ERROR')::UInt64),    -- error_count
       quantilesTDigestState(0.5, 0.95, 0.99)(duration_ns),
       sumState(gen_ai_usage_input_tokens),
       sumState(gen_ai_usage_output_tokens),
       sumState(cost_usd)
   FROM spans FINAL                                  -- FINAL => merge-time dedup applied
   WHERE toYYYYMM(start_time) = {month:UInt32}
   GROUP BY day, agent_id, agent_version, run_kind,
            gen_ai_operation_name, gen_ai_response_model;
   ```

   (`trace_index` rebuilds the same way if a replay touched it: drop the affected
   rows / rebuild from `spans FINAL`.) The `-State` combinators match the
   `AggregateFunction`/`SimpleAggregateFunction` columns; do the rebuild on the
   raw table's dedup, not the live MV, so the counts are exact.

The rebuild is idempotent (drop + re-derive), so it can be re-run safely if
interrupted.

---

## 4. Redpanda topic layout

The queue decouples the OTLP receiver (must never block the caller) from the
ClickHouse writer (batches for efficiency), and provides a replay buffer so we
can reprocess after a writer bug or a schema change.

| Topic              | Payload                                  | Key        | Partitions | Retention | Purpose |
|--------------------|------------------------------------------|------------|-----------|-----------|---------|
| `otlp.spans.raw`   | raw OTLP `ResourceSpans` protobuf, as received (post auth + size check) | `trace_id` | 6 | 48h *or* size cap | Lossless ingest buffer + reprocessing source. |
| `spans.dlq`        | failed record + error + original bytes   | `trace_id` | 1 | 14d | Normalization/validation failures, for debugging. |

- **Key = `trace_id`.** All spans of a trace land on one partition → per-trace
  ordering and locality (useful for `trace_index` assembly), and, since trace_ids
  are uniformly random, load spreads evenly across partitions. Risk of a single
  giant trace hot-spotting one partition is acceptable at this scale.
- **Raw, not normalized, on the wire.** We publish the bytes essentially as
  received and normalize in the consumer. This makes the normalization/enrichment
  logic **replayable**: change how we compute cost or promote a new `gen_ai.*`
  field, then re-consume `otlp.spans.raw` to backfill — no producer changes.
- **Retention is a buffer, not storage.** ClickHouse is the system of record;
  the topic keeps ~48h (bounded also by `retention.bytes` to protect disk on this
  machine). Producer compression `zstd`.
- **Single-node dev:** `replication.factor = 1`. Production would use 3.
- **Idempotency across the boundary** is handled downstream by
  `ReplacingMergeTree` on `(trace_id, span_id)`, so at-least-once consumption is
  safe end-to-end.

**Deliberately deferred:** a `spans.normalized` fan-out topic (flattened spans for
real-time consumers like live-tail or streaming regression alerts). It adds a hop
and storage; we add it only when a second consumer needs normalized spans without
touching ClickHouse. Start with raw + DLQ.

---

## 5. Trade-offs & alternatives considered

- **Analytics-first `ORDER BY` + bloom/`trace_index` for Q1** vs a trace-first
  key with a projection for analytics. We chose analytics-first because Q2/Q3 are
  the product's core and run over far more rows than the point-lookup Q1; Q1 is
  cheaply covered by the skip index and the summary table.
- **Wide denormalized table** vs normalized resource/attribute tables. Wide wins
  for a columnar OLAP store: no joins, great compression on repeated
  `LowCardinality` identity. Cost is redundancy, which compression largely erases.
- **Typed hot columns + `Map` tail** vs everything in `Map` vs the new ClickHouse
  `JSON` type. Hybrid gives fast filters on the fields we query constantly while
  keeping the long tail flexible. **Decided: `Map(LowCardinality(String), String)`**
  for the attribute tail — portable and well-understood; the native `JSON` type
  can be revisited if attribute querying becomes a bottleneck.
- **`ReplacingMergeTree`** vs plain `MergeTree` + a dedup step. Replacing gives
  idempotency for free under at-least-once delivery; the price is eventual dedup
  and occasional `FINAL`.
- **Daily partitions** vs weekly/monthly. Daily balances part count against
  cheap TTL drops; trivially tunable.
- **`String` hex ids** vs `FixedString(16)/(8)`. **Decided: `String`** for
  readability and ergonomics; fixed-width remains the perf/space optimization to
  reach for if profiling calls for it.

---

## 6. Resolved decisions

All open questions are now decided; this section records the final calls.

1. **Retention:** live/other raw spans **30 days**, **eval raw spans 365 days**
   (conditional TTL, §3.1); rollup **~400 days**.
2. **`agent_version`:** **content hash** of {code, prompts, model, params, tools};
   **git SHA recorded separately** as `vigil.agent.git_sha`; full config per hash
   stored in the Postgres `version_manifests` table for diffing (§3.6).
3. **Multi-trace runs:** `run_id` **stays first-class and separate from
   `trace_id`** to support HITL pauses and retries that resume as new traces (§2.2).
4. **Sampling:** live traffic may be sampled later; **eval runs are never sampled**
   (100% ingestion) — the detector needs the full per-run distribution (§1).
5. **Idempotency:** enforced **at insert time** via deterministic offset-range
   `insert_deduplication_token` + `non_replicated_deduplication_window` (§3.5),
   with `ReplacingMergeTree` as a safety net. This is what protects the on-insert
   rollup MVs from double-counting redelivered batches.
6. **`Map` vs native `JSON`:** **`Map(LowCardinality(String), String)`** for the
   attribute tail.
7. **Id storage:** **`String`** (hex).
8. **Redpanda retention:** **48 hours** on `otlp.spans.raw` (buffer, not storage).
```
