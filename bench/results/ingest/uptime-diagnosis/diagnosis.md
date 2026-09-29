# ClickHouse long-uptime degradation — diagnosis

_Generated 2026-09-29T10:12:37.556870+00:00 by `bench/ch_uptime.py diagnose` for the instance up 2026-09-27 22:13:05 → 2026-09-28 09:30:40 (UTC)._

## Hour by hour (from the instance's own system logs)

| hour | tracked avg / max GiB | RSS GiB | span inserts | merges | merge memory-limit failures | other memory-limit errors | mark cache MiB | parts |
|---|---|---|---|---|---|---|---|---|
| 09-27 22:00 | 0.56 / 0.66 | 0.66 | 24 | 290 | 0 | 0 | 0 | 44 |
| 09-27 23:00 | 0.68 / 0.92 | 0.89 | 151 | 1134 | 0 | 0 | 0 | 58 |
| 09-28 00:00 | 0.74 / 0.84 | 0.83 | 0 | 1270 | 0 | 0 | 0 | 46 |
| 09-28 01:00 | 0.79 / 1.53 | 0.88 | 0 | 1471 | 388 | 1 | 0 | 45 |
| 09-28 02:00 | 0.85 / 1.91 | 0.92 | 0 | 2408 | 1531 | 1 | 0 | 46 |
| 09-28 03:00 | 0.88 / 1.67 | 0.98 | 0 | 836 | 128 | 0 | 0 | 44 |
| 09-28 04:00 | 0.92 / 1.62 | 1.03 | 0 | 2774 | 2044 | 1 | 0 | 46 |
| 09-28 05:00 | 0.98 / 1.58 | 1.09 | 0 | 3237 | 2784 | 2 | 0 | 46 |
| 09-28 06:00 | 1.03 / 1.59 | 1.11 | 0 | 2973 | 2542 | 0 | 0 | 44 |
| 09-28 07:00 | 1.06 / 1.63 | 1.18 | 0 | 4398 | 3749 | 0 | 0 | 49 |
| 09-28 08:00 | 1.09 / 1.65 | 1.29 | 108 | 5677 | 4784 | 1 | 0 | 67 |
| 09-28 09:00 | 1.25 / 1.77 | 1.84 | 2962 | 6216 | 2892 | 143 | 0 | 76 |

- Memory cap (`max_server_memory_usage`) at diagnosis time: 6.40 GiB; the instance diagnosed ran with 1.5 GiB.
- Tracked memory (avg) grew **55 MiB/h**; RSS **74 MiB/h**.
- Peak tracked memory first exceeded the cap in hour **2026-09-28 01:00:00**.

## Memory-limit errors by source

| source | table | errors | first | last |
|---|---|---|---|---|
| background merge | system.metric_log | 20271 | 09-28 01:39 | 09-28 09:30 |
| background merge | vigil_load.spans | 533 | 09-28 08:56 | 09-28 09:27 |
| TCPHandler | — | 69 | 09-28 09:04 | 09-28 09:27 |
| executeQuery | — | 69 | 09-28 09:04 | 09-28 09:27 |
| background merge | system.text_log | 13 | 09-28 05:20 | 09-28 09:11 |
| background merge | system.asynchronous_metric_log | 11 | 09-28 02:25 | 09-28 09:29 |
| system log flush | — | 11 | 09-28 01:54 | 09-28 09:18 |
| background merge | vigil_load.trace_index | 10 | 09-28 09:12 | 09-28 09:23 |
| background merge | system.processors_profile_log | 1 | 09-28 09:22 | 09-28 09:22 |
| background merge | system.query_log | 1 | 09-28 09:03 | 09-28 09:03 |
| background merge | system.part_log | 1 | 09-28 09:17 | 09-28 09:17 |
| background merge | system.error_log | 1 | 09-28 08:17 | 09-28 08:17 |
| span INSERT (writer) | — | 72 | 09-28 09:04 | 09-28 09:27 |

## Snapshot at diagnosis time

- `system.metric_log` columns: **991**; unfinished mutations: 0; running merges: 0.
- Caches: IndexMarkCacheBytes 0.0 MiB, UncompressedCacheBytes 0.0 MiB, MarkCacheBytes 1.0 MiB, MMapCacheCells 0.0 MiB

| database | table | partition | active parts | rows | max level |
|---|---|---|---|---|---|
| system | processors_profile_log | 202609 | 8 | 823209 | 156 |
| system | part_log | 202609 | 8 | 237991 | 186 |
| system | text_log | 202609 | 8 | 3524994 | 656 |
| system | asynchronous_metric_log | 202609 | 7 | 25909974 | 3060 |
| system | trace_log | 202609 | 6 | 2516915 | 2237 |
| system | query_log | 202609 | 5 | 62571 | 285 |
| system | query_views_log | 202609 | 3 | 59398 | 501 |
| system | metric_log | 202609 | 2 | 129495 | 385 |
| vigil | spans | 2026-09-29 | 2 | 14 | 1 |
| vigil | spans | 2026-09-27 | 2 | 447 | 5 |
| system | error_log | 202609 | 1 | 16574 | 602 |
| vigil | trace_index | tuple() | 1 | 557 | 7 |
| vigil | spans | 2026-09-25 | 1 | 57 | 0 |
| vigil | agent_version_stats_daily | 202609 | 1 | 16 | 6 |
| vigil | spans | 2026-09-26 | 1 | 3144 | 0 |

## Merge probe: one `system.metric_log` merge

Scratch copies in `vigil_load` of 129,442 rows × 991 columns, inserted as 3 parts, merged with `OPTIMIZE FINAL`; peak memory sampled from `system.merges` every 0.1 s.

| algorithm | peak merge memory | duration s |
|---|---|---|
| Horizontal | 4.10 GiB | 8.7 |
| Vertical | 0.02 GiB | 9.1 |
