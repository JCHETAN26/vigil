# Ingestion load test — stage 2 baseline (`sized`)

_Generated 2026-09-29T03:05:56.689452+00:00 by `bench/ingest_load.py baseline --label sized` (commit `d88e9cfdd4`)._

**Limits:** Sized for this host (deploy/docker-compose.sized.yml): load receiver 1 GiB, load writer 2 GiB, ClickHouse 8 GiB container / 6.4 GiB max_server_memory_usage, Redpanda --smp=1 --memory=4G (5 GiB container). CPU allocation identical to as-is (pipeline pinned to cores 1-3). Each protocol ramp started from a fresh ClickHouse restart.

- **Host:** 4× Neoverse-N1 (aarch64), 23.4 GiB RAM, kernel 6.8.0-1062-oracle, Docker 29.8.1.
- **Setup:** Generator and pipeline share one host. Generator pinned to core 0 (taskset); load receiver/writer, ClickHouse and Redpanda pinned to cores 1-3 (docker cpuset). Host processes (dockerd, tailscaled, sshd, ...) are unpinned and may run on any core.
- **Containers:** `vigil-load-receiver` cpuset 1-3, mem 1024 MiB; `vigil-load-writer` cpuset 1-3, mem 2048 MiB; `vigil-clickhouse` cpuset 1-3, mem 8192 MiB; `vigil-redpanda` cpuset 1-3, mem 5120 MiB
- **Writer:** VIGIL_BATCH_MAX_ROWS=10000, VIGIL_BATCH_MAX_BYTES=33554432, VIGIL_FLUSH_INTERVAL=2s
- **ClickHouse** max_server_memory_usage 6.4 GiB; **Redpanda** `--memory=4G`. ClickHouse at start: uptime 0.0 h, tracked memory 0.57 GiB.
- **Method:** open-loop offered load of real replayed traces; each step = 120 s warm-up + 600 s steady-state window; load tables truncated between steps. **Sustained** = highest rate with zero loss (nothing missing / duplicated / unacked / DLQ'd) and steady-state e2e p99 < 5 s. A step where the generator is CPU-bound or behind schedule is *inconclusive* (it measured the generator).

## Sustained throughput

| protocol | sustained offered spans/s | measured acked spans/s (window) | non-passing steps | ramp saturated at |
|---|---|---|---|---|
| grpc | 2000 | 2000 | 5000, 10000 | 10000 |
| http | 2000 | 2000 | 5000, 10000 | 10000 |

## Steps

| protocol | offered | acked/s (window) | e2e p50 / p95 / p99 ms (window) | req p99 ms | missing | dup | unacked | DLQ | max lag | CPU cores (recv / writer / CH / RP, mean) | gen CPU p95 | gen lag p99 ms | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| grpc | 500 | 499 | 2146 / 3234 / 3272 | 125 | 0 | 0 | 0 | 0 | 253 | 0.06 / 0.07 / 0.28 / 0.04 | 0.01 | 3 | pass |
| grpc | 1000 | 1000 | 1656 / 2736 / 2802 | 130 | 0 | 0 | 0 | 0 | 424 | 0.12 / 0.13 / 0.36 / 0.07 | 0.02 | 3 | pass |
| grpc | 2000 | 2000 | 1449 / 2593 / 2919 | 146 | 0 | 0 | 0 | 0 | 915 | 0.24 / 0.25 / 0.50 / 0.12 | 0.05 | 4 | pass |
| grpc | 5000 | 5000 | 1687 / 3729 / 6682 | 1732 | 0 | 0 | 0 | 0 | 4922 | 0.60 / 0.55 / 0.70 / 0.24 | 0.10 | 5 | fail: e2e p99 6682 ms >= 5000 ms |
| grpc | 10000 | 10000 | 98533 / 176304 / 178206 | 6088 | 0 | 0 | 0 | 0 | 260177 | 0.95 / 0.64 / 0.86 / 0.20 | 0.30 | 4467 | inconclusive: generator behind schedule (lag p99 4467 ms > 100 ms) (ramp saturated) |
| http | 500 | 499 | 2153 / 3246 / 3300 | 136 | 0 | 0 | 0 | 0 | 296 | 0.08 / 0.07 / 0.32 / 0.04 | 0.02 | 2 | pass |
| http | 1000 | 1000 | 1667 / 2746 / 2814 | 149 | 0 | 0 | 0 | 0 | 445 | 0.15 / 0.14 / 0.40 / 0.07 | 0.03 | 3 | pass |
| http | 2000 | 2000 | 1379 / 2434 / 3848 | 170 | 0 | 0 | 0 | 0 | 1171 | 0.29 / 0.26 / 0.52 / 0.11 | 0.05 | 4 | pass |
| http | 5000 | 5000 | 1697 / 3531 / 5296 | 1612 | 0 | 0 | 0 | 0 | 4105 | 0.69 / 0.56 / 0.79 / 0.24 | 0.11 | 5 | fail: e2e p99 5296 ms >= 5000 ms |
| http | 10000 | 10000 | 98512 / 177356 / 181025 | 23141 | 0 | 0 | 0 | 0 | 234724 | 1.07 / 0.63 / 0.93 / 0.16 | 0.31 | 21509 | inconclusive: generator behind schedule (lag p99 21509 ms > 100 ms) (ramp saturated) |
