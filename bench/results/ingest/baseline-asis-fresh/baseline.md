# Ingestion load test — stage 2 baseline (`asis-fresh`)

_Generated 2026-09-29T10:51:41.887531+00:00 by `bench/ingest_load.py baseline --label asis-fresh` (commit `d50400cb86`)._

**Limits:** As-is (original, IdeaPad-era) limits: load receiver 256 MiB, load writer 512 MiB, ClickHouse 2 GiB container / 1.5 GiB max_server_memory_usage, Redpanda --smp=1 --memory=1536M (2 GiB container). ClickHouse freshly restarted with empty system logs (clean start).

- **Host:** 4× Neoverse-N1 (aarch64), 23.4 GiB RAM, kernel 6.8.0-1062-oracle, Docker 29.8.1.
- **Setup:** Generator and pipeline share one host. Generator pinned to core 0 (taskset); load receiver/writer, ClickHouse and Redpanda pinned to cores 1-3 (docker cpuset). Host processes (dockerd, tailscaled, sshd, ...) are unpinned and may run on any core.
- **Containers:** `vigil-load-receiver` cpuset 1-3, mem 256 MiB; `vigil-load-writer` cpuset 1-3, mem 512 MiB; `vigil-clickhouse` cpuset 1-3, mem 2048 MiB; `vigil-redpanda` cpuset 1-3, mem 2048 MiB
- **Writer:** VIGIL_BATCH_MAX_ROWS=10000, VIGIL_BATCH_MAX_BYTES=33554432, VIGIL_FLUSH_INTERVAL=2s
- **ClickHouse** max_server_memory_usage 1.5 GiB; **Redpanda** `--memory=1536M`. ClickHouse at start: uptime 0.0 h, tracked memory 0.51 GiB.
- **Method:** open-loop offered load of real replayed traces; each step = 120 s warm-up + 600 s steady-state window; load tables truncated between steps. **Sustained** = highest rate with zero loss (nothing missing / duplicated / unacked / DLQ'd) and steady-state e2e p99 < 5 s. A step where the generator is CPU-bound or behind schedule is *inconclusive* (it measured the generator).

## Sustained throughput

| protocol | sustained offered spans/s | measured acked spans/s (window) | non-passing steps | ramp saturated at |
|---|---|---|---|---|
| grpc | 1000 | 1000 | 2000 | 2000 |

## Steps

| protocol | offered | acked/s (window) | e2e p50 / p95 / p99 ms (window) | req p99 ms | missing | dup | unacked | DLQ | max lag | CPU cores (recv / writer / CH / RP, mean) | gen CPU p95 | gen lag p99 ms | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| grpc | 500 | 499 | 2143 / 3231 / 3271 | 113 | 0 | 0 | 0 | 0 | 266 | 0.06 / 0.07 / 0.18 / 0.04 | 0.01 | 2 | pass; started after a pipeline reset (0 backlog records discarded) |
| grpc | 1000 | 1000 | 1657 / 2734 / 2796 | 118 | 0 | 0 | 0 | 0 | 484 | 0.12 / 0.14 / 0.25 / 0.07 | 0.02 | 3 | pass |
| grpc | 2000 | 2000 | not drained | 189 | — | — | — | — | 2092 | 0.24 / 0.24 / 0.62 / 0.11 | 0.05 | 3 | fail: writer did not drain within the timeout (no reconciliation) (ramp saturated) |
