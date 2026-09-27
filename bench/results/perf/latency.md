# Dashboard API query latency

_Base http://127.0.0.1:8080 · 60 requests/endpoint (3 warmup) · local read-only API._

| endpoint | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|
| GET /runs | 0.89 | 1.12 | 1.14 | 1.15 |
| GET /runs/{id} | 1.34 | 2.0 | 6.52 | 10.19 |
| GET /runs/{id}/cases | 2.39 | 3.24 | 3.58 | 3.75 |
| GET /traces/{trace_id} | 9.01 | 10.29 | 11.34 | 12.03 |
| GET /compare | 3.85 | 4.14 | 4.26 | 4.31 |

**Worst-endpoint p95: 10.29 ms.**

