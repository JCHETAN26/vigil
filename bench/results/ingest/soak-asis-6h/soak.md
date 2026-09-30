# ClickHouse long-uptime soak

grpc at 200 spans/s for 6.0 h into the load pipeline, from a ClickHouse with empty system logs. The history below is rebuilt from ClickHouse's own system logs.

- **Delivery:** acked 4,320,097, unacked 0, request p99 148 ms, generator CPU p95 0.01 core.
- **Reconciliation:** stored 4320097, missing 0, duplicate rows 0, trace_index 4322222, DLQ 0; clean = False.
- **Load writer restarts:** 177 since the container was created (no start count recorded) — each follows a failed ClickHouse insert.
- **ClickHouse at end:** uptime 0.8 h, tracked 1.41 GiB.
_Generated 2026-09-30T00:00:30.731237+00:00 by `bench/ch_uptime.py diagnose` for the instance up 2026-09-29 10:52:03 → 2026-09-29 16:52:01 (UTC)._

## Hour by hour (from the instance's own system logs)

| hour | tracked avg / max GiB | RSS GiB | span inserts | merges | merge memory-limit failures | other memory-limit errors | mark cache MiB | parts |
|---|---|---|---|---|---|---|---|---|
| 09-29 10:00 | 0.92 / 1.30 | 1.35 | 1122 | 1356 | 0 | 0 | 0 | 61 |
| 09-29 11:00 | 1.10 / 1.54 | 1.69 | 8516 | 10319 | 143 | 23 | 0 | 92 |
| 09-29 12:00 | 1.36 / 2.50 | 1.95 | 6704 | 29138 | 18910 | 854 | 0 | 105 |
| 09-29 13:00 | 1.38 / 2.60 | 1.90 | 36 | 26091 | 24556 | 78 | 0 | 95 |
| 09-29 14:00 | 1.38 / 2.58 | 1.93 | 37 | 29653 | 26915 | 102 | 0 | 96 |
| 09-29 15:00 | 1.37 / 2.66 | 1.93 | 34 | 28256 | 26204 | 93 | 0 | 92 |
| 09-29 16:00 | 1.37 / 2.56 | 1.88 | 36 | 21840 | 21279 | 70 | 0 | 93 |

- Memory cap (`max_server_memory_usage`) at diagnosis time: 1.50 GiB; the instance diagnosed ran with 1.5 GiB.
- Tracked memory (avg) grew **70 MiB/h**; RSS **74 MiB/h**.
- Peak tracked memory first exceeded the cap in hour **2026-09-29 11:00:00**.

## Memory-limit errors by source

| source | table | errors | first | last |
|---|---|---|---|---|
| background merge | vigil_load.spans | 111097 | 09-29 11:55 | 09-29 16:51 |
| background merge | system.metric_log | 4439 | 09-29 11:58 | 09-29 16:51 |
| background merge | system.query_log | 1290 | 09-29 11:30 | 09-29 16:49 |
| background merge | system.part_log | 761 | 09-29 12:01 | 09-29 16:48 |
| TCPHandler | — | 414 | 09-29 11:55 | 09-29 16:50 |
| executeQuery | — | 414 | 09-29 11:55 | 09-29 16:50 |
| system log flush | — | 392 | 09-29 11:58 | 09-29 16:49 |
| background merge | system.text_log | 226 | 09-29 12:08 | 09-29 16:49 |
| background merge | system.asynchronous_metric_log | 117 | 09-29 12:08 | 09-29 16:42 |
| background merge | system.processors_profile_log | 42 | 09-29 11:57 | 09-29 16:04 |
| background merge | system.query_views_log | 16 | 09-29 11:57 | 09-29 12:51 |
| background merge | vigil_load.trace_index | 14 | 09-29 12:31 | 09-29 12:51 |
| background merge | system.trace_log | 3 | 09-29 12:12 | 09-29 12:13 |
| background merge | system.error_log | 2 | 09-29 16:00 | 09-29 16:28 |
| span INSERT (writer) | — | 409 | 09-29 11:55 | 09-29 12:54 |

## Snapshot at diagnosis time

- `system.metric_log` columns: **991**; unfinished mutations: 0; running merges: 0.
- Caches: IndexMarkCacheBytes 0.0 MiB, UncompressedCacheBytes 0.0 MiB, MarkCacheBytes 0.2 MiB, MMapCacheCells 0.0 MiB

| database | table | partition | active parts | rows | max level |
|---|---|---|---|---|---|
| vigil_load | spans | 2026-09-29 | 15 | 4320097 | 14 |
| system | part_log | 202609 | 9 | 256806 | 116 |
| system | text_log | 202609 | 9 | 8337320 | 92 |
| system | query_log | 202609 | 7 | 37913 | 457 |
| system | metric_log | 202609 | 6 | 46453 | 223 |
| system | asynchronous_metric_log | 202609 | 6 | 9353563 | 2806 |
| system | query_views_log | 202609 | 6 | 32928 | 407 |
| system | processors_profile_log | 202609 | 5 | 469396 | 735 |
| system | trace_log | 202609 | 4 | 583437 | 165 |
| vigil_load | agent_version_stats_daily | 202609 | 3 | 20 | 3307 |
| vigil_load | trace_index | tuple() | 3 | 642274 | 1804 |
| system | error_log | 202609 | 2 | 26337 | 999 |
| vigil | trace_index | tuple() | 2 | 564 | 8 |
| vigil | agent_version_stats_daily | 202609 | 2 | 18 | 7 |
| vigil | spans | 2026-09-27 | 2 | 447 | 5 |
| vigil | spans | 2026-09-29 | 1 | 28 | 3 |
| vigil | spans | 2026-09-25 | 1 | 57 | 0 |
| vigil | spans | 2026-09-26 | 1 | 3144 | 0 |
