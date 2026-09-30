# Soak reconciliation — per table, and why only `trace_index` double-counted

Soak `soak-20260929T105202Z`: 6 h at 200 spans/s, original limits (ClickHouse 1.5 GiB cap, load
writer 512 MiB), drained after the recovery in `recovery.md`. Measured on 2026-09-30 between
00:05 and 00:20 UTC, **before** `vigil_load` and ClickHouse's system logs were emptied for the
API runs, so these numbers cannot be regenerated; the queries are listed so the method can be
repeated on the next soak. (`soak.md` / `reconcile.json` hold the automated reconciliation.)

## Result

| table | lost | counted twice |
|---|---|---|
| raw `spans` | **0** | **0 now**; ≥ 2,125 duplicate rows existed transiently and were collapsed by ReplacingMergeTree merges |
| `agent_version_stats_daily` (rollup) | **0** | **0**: `sum(span_count)` = 4,320,097 |
| `trace_index` | **0** | **319 traces / 2,125 spans**, every one exactly doubled (`c = 2·n`); none partial, none under |

Acknowledged spans: 4,320,097. Redpanda retention deleted 0 unconsumed records (`loadgen offsets`).

## Why

One `INSERT INTO spans` is not atomic across its materialized views. ClickHouse writes the
`spans` part, then pushes the block to `trace_index_mv`, then to `agent_version_stats_daily_mv`.
Under the 1.5 GiB cap, `system.query_views_log` for the soak window shows:

| view | QueryFinish | ExceptionWhileProcessing (241, MEMORY_LIMIT_EXCEEDED) |
|---|---|---|
| `vigil_load.trace_index_mv` | 16,454 | 4 |
| `vigil_load.agent_version_stats_daily_mv` | 16,398 | 60 |

So in ~56 inserts the `spans` part and the `trace_index` block were already committed when the
rollup push failed and the INSERT returned an error. The writer treated the batch as failed,
exited without committing the offset, and after restart re-read from the committed offset. The
soak's batches are time-flushed (2 s; far below 10,000 rows / 32 MiB), so the rebuilt batch ended
at a different offset and carried a **different** `insert_deduplication_token`. The retry then:

- re-inserted the rows into `spans` (duplicates, later collapsed by ReplacingMergeTree);
- added a **second** copy to `trace_index` (AggregatingMergeTree sums never self-heal);
- gave the rollup its **first** copy (its earlier push had failed) — hence exact.

This is the mechanism of the time-flush limitation in `docs/design/data-model.md` §3.5, with a
different and far more frequent trigger than the one documented there (a crash between a fully
successful insert and the offset commit): a **partially applied** insert that reports failure.

## Correction

Rebuilding `trace_index` from `spans FINAL` (the view's current SELECT, `FROM spans FINAL`)
gave 0 mismatched traces and `sum(span_count)` = 4,320,097 — the §3.7 rebuild corrects it.

## Queries

```sql
-- raw spans: rows vs distinct (trace, span), without FINAL
SELECT count(), uniqExact(trace_id, span_id) FROM vigil_load.spans WHERE run_id = '<run>';
-- rollup total
SELECT sum(span_count) FROM vigil_load.agent_version_stats_daily WHERE agent_id = 'vigil-loadtest';
-- trace_index vs spans per trace (join_algorithm = 'grace_hash' under a small memory cap)
WITH s AS (SELECT trace_id, count() n FROM vigil_load.spans WHERE run_id = '<run>' GROUP BY trace_id),
     ti AS (SELECT trace_id, sum(span_count) c FROM vigil_load.trace_index WHERE run_id = '<run>' GROUP BY trace_id)
SELECT count(), countIf(ti.c > s.n), sumIf(ti.c - s.n, ti.c > s.n), countIf(ti.c < s.n), countIf(ti.c = 2 * s.n)
FROM s INNER JOIN ti USING trace_id;
-- per-view outcomes of the writer's inserts
SELECT view_name, status, exception_code, count() FROM system.query_views_log
WHERE event_time BETWEEN '2026-09-29 10:51:00' AND '2026-09-30 00:05:00' AND view_name LIKE 'vigil_load.%'
GROUP BY view_name, status, exception_code;
```
