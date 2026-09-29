# Ingestion load test — stage 2 baseline (`asis`)

_Generated 2026-09-28T09:27:11.822110+00:00 by `bench/ingest_load.py baseline --label asis` (commit `0a655241bc`)._

**Limits:** As-is (IdeaPad-era) limits: load receiver 256 MiB, load writer 512 MiB, ClickHouse 2 GiB container / 1.5 GiB max_server_memory_usage, Redpanda --smp=1 --memory=1536M (2 GiB container).

- **Host:** 4× Neoverse-N1 (aarch64), 23.4 GiB RAM, kernel 6.8.0-1062-oracle, Docker 29.8.1.
- **Setup:** Generator and pipeline share one host. Generator pinned to core 0 (taskset); load receiver/writer, ClickHouse and Redpanda pinned to cores 1-3 (docker cpuset). Host processes (dockerd, tailscaled, sshd, ...) are unpinned and may run on any core.
- **Containers:** `vigil-load-receiver` cpuset 1-3, mem 256 MiB; `vigil-load-writer` cpuset 1-3, mem 512 MiB; `vigil-clickhouse` cpuset 1-3, mem 2048 MiB; `vigil-redpanda` cpuset 1-3, mem 2048 MiB
- **Writer:** VIGIL_BATCH_MAX_ROWS=10000, VIGIL_BATCH_MAX_BYTES=33554432, VIGIL_FLUSH_INTERVAL=2s
- **Method:** open-loop offered load of real replayed traces; each step = 120 s warm-up + 600 s steady-state window; load tables truncated between steps. **Sustained** = highest rate with zero loss (nothing missing / duplicated / unacked / DLQ'd) and steady-state e2e p99 < 5 s. A step where the generator is CPU-bound or behind schedule is *inconclusive* (it measured the generator).

## Sustained throughput

| protocol | sustained offered spans/s | measured acked spans/s (window) | non-passing steps | ramp saturated at |
|---|---|---|---|---|
| grpc | none | — | 500 | 500 |
| http | none | — | 500 | 500 |

## Steps

| protocol | offered | acked/s (window) | e2e p50 / p95 / p99 ms (window) | req p99 ms | missing | dup | unacked | DLQ | max lag | CPU cores (recv / writer / CH / RP, mean) | gen CPU p95 | gen lag p99 ms | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| grpc | 500 | 499 | 2169 / 4209 / 20817 | 125 | 0 | 0 | 0 | 0 | 1801 | 0.06 / 0.06 / 0.45 / 0.03 | 0.01 | 3 | fail: trace_index inconsistent; e2e p99 20817 ms >= 5000 ms (ramp saturated) |
| http | 500 | 499 | not drained | 136 | — | — | — | — | 298 | 0.08 / 0.07 / 0.44 / 0.04 | 0.02 | 2 | fail: writer did not drain within the timeout (no reconciliation) (ramp saturated) |
