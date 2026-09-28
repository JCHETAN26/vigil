# Ingestion load test — stage 1 pilot

_Generated 2026-09-28T08:56:52.221475+00:00 by `bench/ingest_load.py pilot` (commit `d887a813e8`)._

## Hardware and setup

- **Host:** 4× Neoverse-N1 (aarch64), 23.4 GiB RAM, kernel 6.8.0-1062-oracle, Docker 29.8.1, go version go1.27.1 linux/arm64.
- **Setup:** Generator and pipeline share one host. Generator pinned to core 0 (taskset); load receiver/writer, ClickHouse and Redpanda pinned to cores 1-3 (docker cpuset). Host processes (dockerd, tailscaled, sshd, ...) are unpinned and may run on any core.
- **Containers:** `vigil-load-receiver` cpuset 1-3, mem 256 MiB; `vigil-load-writer` cpuset 1-3, mem 512 MiB; `vigil-clickhouse` cpuset 1-3, mem 2048 MiB; `vigil-redpanda` cpuset 1-3, mem 2048 MiB
- **Templates:** 515 real traces / 3464 spans (hello-agent 106/332, hotpotqa-agent 358/2325, tau2-retail-agent 51/807), sha256 `afbb664d6bea`, fidelity 0 mismatches (88 legacy rows upgraded).

## Pilot runs (correctness + calibration, not throughput)

| protocol | offered spans/s | acked | stored | missing | dup rows | unacked | trace_index | DLQ | req p99 ms | e2e p50 / p99 ms | gen CPU p95 | gen lag p99 ms | clean | flags |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| grpc | 1000 | 20326 | 20326 | 0 | 0 | 0 | 20326 | 0 | 92 | 1657 / 2753 | 0.03 | 2 | yes | — |
| http | 1000 | 20326 | 20326 | 0 | 0 | 0 | 20326 | 0 | 116 | 1657 / 2792 | 0.03 | 2 | yes | — |

## Disk calibration

- ClickHouse `vigil_load.spans`: **706 B/span** compressed (40652 rows, 0.03 GB); real eval data measured 642 B/span.
- Redpanda `otlp.spans.load`: **684 B/span** (0.03 GB for 40652 spans); retention cap 2.00 GB.
- Planning figure: **706 B/span** in ClickHouse (the larger of this replay measurement and the real-data figure; pilot parts are small and mostly unmerged), with 2× merge headroom.

### Stage-2 projection (per protocol; tables truncated between steps)

Each step = 120 s warm-up + 600 s steady state.

| offered spans/s | spans per step | peak disk per step |
|---|---|---|
| 500 | 360,000 | 0.70 GB |
| 1000 | 720,000 | 1.41 GB |
| 2000 | 1,440,000 | 2.81 GB |
| 5000 | 3,600,000 | 6.73 GB |
| 10000 | 7,200,000 | 11.47 GB |
| 20000 | 14,400,000 | 20.94 GB |

Free disk at pilot time: 30.14 GB; reserve 5.0 GB. Largest step fits: **yes**.
