"""Diagnose ClickHouse's long-uptime degradation and measure it (Week 6, before stage 3).

A ClickHouse up ~11 h with the as-is limits (1.5 GiB max_server_memory_usage) failed the load
test at 500 spans/s, while a freshly restarted one sustained 1,000. This script makes the
investigation reproducible:

    python bench/ch_uptime.py diagnose --from '2026-09-27 22:13:05' --to '2026-09-28 09:30:40' \\
        --out bench/results/ingest/uptime-diagnosis
        # hour-by-hour memory / merges / memory-limit errors for one ClickHouse instance (from
        # its own system logs), plus a snapshot of parts, merges, mutations, caches, settings
    python bench/ch_uptime.py merge-probe --out bench/results/ingest/uptime-diagnosis
        # peak memory of one system.metric_log merge, horizontal vs vertical algorithm, on a
        # scratch copy in vigil_load (the only database this script writes to)
    python bench/ch_uptime.py reset-system-logs --yes
        # empty ClickHouse's own log tables so "fresh" and "long-uptime" start equal
    python bench/ch_uptime.py soak --rate 200 --hours 6 --out bench/results/ingest/soak-asis-6h
        # hold a realistic load for hours, reconcile it, and rebuild ClickHouse's memory /
        # merge / error history over the soak from its system logs (--finish-only: report an
        # existing soak run)
    python bench/ch_uptime.py recover --out bench/results/ingest/soak-asis-6h
        # a soak left undrained on a degraded ClickHouse: snapshot it and measure throughput
        # with no load, check retention headroom (stop if unconsumed records are at risk),
        # restart only ClickHouse, time the writer's recovery and drain, reconcile the soak
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ingest_load as il

GIB = 1024**3

# ClickHouse's own log tables (system.*). Emptied by reset-system-logs; never user data.
SYSTEM_LOGS = (
    "asynchronous_metric_log", "error_log", "metric_log", "part_log", "processors_profile_log",
    "query_log", "query_views_log", "text_log", "trace_log",
)  # fmt: skip
PROBE_TABLES = ("probe_metric_log_horizontal", "probe_metric_log_vertical")


# ---------------------------------------------------------------- pure helpers (tested)


def growth_per_hour(rows: list[dict], key: str) -> float:
    """Least-squares slope of rows[i][key] per hour (rows are hourly, in order)."""
    ys = [float(r[key]) for r in rows if r.get(key) is not None]
    n = len(ys)
    if n < 2:
        return 0.0
    mx, my = (n - 1) / 2, sum(ys) / n
    return sum((i - mx) * (y - my) for i, y in enumerate(ys)) / sum(
        (i - mx) ** 2 for i in range(n)
    )


def first_hour_over(rows: list[dict], key: str, limit: float) -> str | None:
    return next((r["h"] for r in rows if float(r[key]) >= limit), None)


def render_diagnosis(d: dict) -> str:
    cap = d["settings"].get("max_server_memory_usage", 0) / GIB
    lines = [
        "# ClickHouse long-uptime degradation — diagnosis",
        "",
        (
            f"_Generated {d['generated_at']} by `bench/ch_uptime.py diagnose` for the instance up "
            f"{d['window']['from']} → {d['window']['to']} (UTC)._"
        ),
        "",
        "## Hour by hour (from the instance's own system logs)",
        "",
        (
            "| hour | tracked avg / max GiB | RSS GiB | span inserts | merges | merge memory-limit "
            "failures | other memory-limit errors | mark cache MiB | parts |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    fails = {r["h"]: r for r in d["memory_errors_hourly"]}
    for r in d["hourly"]:
        f = fails.get(r["h"], {})
        lines.append(
            f"| {r['h'][5:16]} | {r['tracked_avg']:.2f} / {r['tracked_max']:.2f} | {r['rss']:.2f} "
            f"| {r['inserts']} | {r['merges']} | {f.get('merge_failures', 0)} "
            f"| {f.get('other_failures', 0)} | {r['mark_mib']:.0f} | {r['parts']:.0f} |"
        )
    lines += [
        "",
        (
            f"- Memory cap (`max_server_memory_usage`) at diagnosis time: {cap:.2f} GiB; the "
            f"instance diagnosed ran with {d['window'].get('cap_gib', '?')} GiB."
        ),
        (
            f"- Tracked memory (avg) grew **{d['trend']['tracked_avg_gib_per_h'] * 1024:.0f} MiB/h**; "
            f"RSS **{d['trend']['rss_gib_per_h'] * 1024:.0f} MiB/h**."
        ),
        f"- Peak tracked memory first exceeded the cap in hour **{d['trend']['first_hour_over_cap']}**.",
        "",
        "## Memory-limit errors by source",
        "",
        "| source | table | errors | first | last |",
        "|---|---|---|---|---|",
    ]
    for s in d["memory_errors_by_source"]:
        lines.append(
            f"| {s['source']} | {s['table'] or '—'} | {s['n']} | {s['first'][5:16]} | {s['last'][5:16]} |"
        )
    lines += [
        "",
        "## Snapshot at diagnosis time",
        "",
        (
            f"- `system.metric_log` columns: **{d['metric_log_columns']}**; unfinished mutations: "
            f"{d['unfinished_mutations']}; running merges: {len(d['merges'])}."
        ),
        "- Caches: "
        + ", ".join(f"{k} {v / 1048576:.1f} MiB" for k, v in d["caches"].items()),
        "",
        "| database | table | partition | active parts | rows | max level |",
        "|---|---|---|---|---|---|",
    ]
    for p in d["parts"]:
        lines.append(
            f"| {p['database']} | {p['table']} | {p['partition']} | {p['active_parts']} "
            f"| {p['total_rows']} | {p['max_level']} |"
        )
    probe = d.get("merge_probe")
    if probe:
        lines += [
            "",
            "## Merge probe: one `system.metric_log` merge",
            "",
            (
                f"Scratch copies in `vigil_load` of {probe['rows']:,} rows × {probe['columns']} columns, "
                f"inserted as {probe['parts']} parts, merged with `OPTIMIZE FINAL`; peak memory sampled "
                "from `system.merges` every 0.1 s."
            ),
            "",
            "| algorithm | peak merge memory | duration s |",
            "|---|---|---|",
        ]
        for r in probe["results"]:
            lines.append(
                f"| {r['algorithm']} | {r['peak_bytes'] / GIB:.2f} GiB | {r['seconds']:.1f} |"
            )
    lines.append("")
    return "\n".join(lines)


def render_soak(r: dict) -> str:
    s, cfg = r["summary"], r["summary"]["config"]
    rec = r.get("reconcile") or {}
    rc = rec.get("reconciliation", {})
    start_restarts = r.get("start", {}).get("writer_restarts")
    end_restarts = r.get("writer_restarts_at_end", -1)
    restarts = end_restarts - start_restarts if start_restarts is not None else None
    end = r["clickhouse_at_end"]
    lines = [
        "# ClickHouse long-uptime soak",
        "",
        (
            f"{cfg['protocol']} at {cfg['rate_spans_per_sec']:.0f} spans/s for "
            f"{cfg['duration_ns'] / 3.6e12:.1f} h into the load pipeline, from a ClickHouse with "
            "empty system logs. The history below is rebuilt from ClickHouse's own system logs."
        ),
        "",
        (
            f"- **Delivery:** acked {s['spans_acked']:,}, unacked {s['spans_unacked']:,}, "
            f"request p99 {s['request_latency_ms']['p99']:.0f} ms, generator CPU p95 "
            f"{s['cpu_fraction']['p95']:.2f} core."
        ),
        (
            f"- **Reconciliation:** stored {rc.get('stored_acked_unique_spans', '—')}, missing "
            f"{rc.get('missing_spans', '—')}, duplicate rows {rc.get('duplicate_rows', '—')}, "
            f"trace_index {rec.get('trace_index_span_count', '—')}, DLQ "
            f"{rec.get('dlq_records', '—')}; clean = {rec.get('clean', 'not reconciled')}."
        ),
        (
            "- **Load writer restarts:** "
            + (
                f"{restarts} during the soak"
                if restarts is not None
                else f"{end_restarts} since the container was created (no start count recorded)"
            )
            + " — each follows a failed ClickHouse insert."
        ),
        (
            f"- **ClickHouse at end:** uptime {end['uptime_s'] / 3600:.1f} h, tracked "
            f"{end['memory_tracking_bytes'] / GIB:.2f} GiB."
        ),
        "",
    ]
    body = render_diagnosis(r["history"]).split("\n", 2)[2]  # drop the diagnosis title
    return "\n".join(lines) + body


def retention_risks(
    offsets: dict, oldest_pending_ms: list[int], retention_ms: int, now_ms: int,
    min_time_headroom_h: float = 6.0,
) -> list[str]:  # fmt: skip
    """Reasons the load topic's unconsumed records are at risk from retention (empty = safe
    to proceed, provided nothing new is written to the topic)."""
    risks = []
    if offsets["deleted_unconsumed"] > 0:
        risks.append(
            f"retention already deleted {offsets['deleted_unconsumed']} unconsumed records"
        )
    if oldest_pending_ms:
        headroom_h = (min(oldest_pending_ms) + retention_ms - now_ms) / 3.6e6
        if headroom_h < min_time_headroom_h:
            risks.append(
                f"oldest pending record expires by time in {headroom_h:.1f} h "
                f"(< {min_time_headroom_h} h)"
            )
    return risks


def recovery_milestones(samples: list[dict], t_restart: float) -> dict:
    """From recovery samples ({t, stored, drained, deleted_unconsumed}), the seconds from the
    ClickHouse restart to the first stored-row increase (writer recovered) and to the drain
    (group drained and stored count unchanged since the previous sample)."""
    first_progress = drained = None
    for prev, cur in itertools.pairwise(samples):
        if first_progress is None and cur["stored"] > prev["stored"]:
            first_progress = cur["t"] - t_restart
        if cur["drained"] and cur["stored"] == prev["stored"] and cur["stored"] > 0:
            drained = cur["t"] - t_restart
            break
    return {
        "writer_recovered_after_s": first_progress,
        "drained_after_s": drained,
        "max_deleted_unconsumed": max(
            (s["deleted_unconsumed"] for s in samples), default=0
        ),
    }


def render_recovery(r: dict) -> str:
    snap, m = r["degraded"], r["milestones"]
    fmt_s = lambda s: f"{s / 60:.1f} min" if s is not None else "not reached"
    probe = snap["progress_probe"]
    return "\n".join([
        "# Long-uptime result and recovery",
        "",
        "## The degraded instance (not restarted since the soak's clean start)",
        "",
        (f"- ClickHouse uptime {snap['server'].get('uptime_h', '?')} h, tracked memory "
        f"{snap['server'].get('tracked_gib', '?')} GiB against the {snap['cap_gib']:.1f} GiB cap, "
        f"{snap['server'].get('memory_limit_errors', '?')} memory-limit errors since start."),
        (f"- **Throughput with zero offered load: {probe['stored_delta']} spans stored in "
        f"{probe['seconds'] / 60:.0f} min**, while the load writer restarted "
        f"{probe['writer_restarts_delta']} times; backlog {probe['pending_records']:,} records."),
        (f"- Snapshot queries that themselves failed on memory: {len(snap['failed_queries'])} "
        f"({', '.join(snap['failed_queries']) or 'none'})."),
        ("- The long-uptime ramp was not run: this instance cannot drain even with no load, so "
        "every rate fails, and the ramp would have discarded the soak backlog."),
        "",
        "## Recovery: restart ClickHouse only (Redpanda backlog kept)",
        "",
        (f"- Retention check before restart: {'; '.join(r['preflight_risks']) or 'no risk'} "
        f"(smallest consumed-but-retained cushion {r['preflight_offsets']['min_consumed_retained']:,} "
        "records; the load receiver was stopped so nothing could write to the topic)."),
        *recovery_lines(r, fmt_s),
        (f"- Records deleted by retention before being consumed, at any point: "
        f"**{m['max_deleted_unconsumed']}**."),
        "",
    ])  # fmt: skip


def recovery_lines(r: dict, fmt_s) -> list[str]:
    """What restored the drain. Without interventions, the restart did; with one, the
    restart's own effect is the time before it, and recovery is timed from the intervention."""
    m, ivs = r["milestones"], r.get("interventions", [])
    if not ivs:
        return [
            (
                f"- Writer recovered (first stored rows) **{fmt_s(m['writer_recovered_after_s'])}** "
                f"after the restart; backlog drained **{fmt_s(m['drained_after_s'])}** after it."
            )
        ]
    first = ivs[0]["at_s"]
    progressed = (
        m["writer_recovered_after_s"] is not None
        and m["writer_recovered_after_s"] < first
    )
    lines = [
        "- Restarting ClickHouse alone: "
        + ("the writer resumed before any intervention."
           if progressed else f"**no progress** in the {first / 60:.1f} min before the intervention below.")
    ]  # fmt: skip
    for iv in ivs:
        im = iv["milestones"]
        lines.append(
            f"- **Intervention at +{iv['at_s'] / 60:.1f} min:** {iv['action']} ({iv['reason']}). "
            f"After it: first stored rows in **{fmt_s(im['writer_recovered_after_s'])}**, backlog "
            f"drained in **{fmt_s(im['drained_after_s'])}**"
            + (f"; peak writer memory **{iv['peak_writer_mib']:.0f} MiB**." if iv.get("peak_writer_mib") else ".")
        )  # fmt: skip
    return lines


# ---------------------------------------------------------------- I/O


def q_json(env: dict, sql: str) -> list[dict]:
    out = il.ch_query(env, sql + " FORMAT JSONEachRow")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def diagnose(env: dict, t_from: str, t_to: str) -> dict:
    w = f"event_time BETWEEN '{t_from}' AND '{t_to}'"
    hourly_async = {
        r["h"]: r
        for r in q_json(
            env,
            f"""
SELECT toString(toStartOfHour(event_time)) h,
  maxIf(value, metric='MemoryResident')/{GIB} rss,
  maxIf(value, metric='MarkCacheBytes')/1048576 mark_mib,
  maxIf(value, metric='TotalPartsOfMergeTreeTables') parts
FROM system.asynchronous_metric_log WHERE {w} GROUP BY h ORDER BY h""",
        )
    }
    hourly = []
    for r in q_json(
        env,
        f"""
SELECT toString(toStartOfHour(event_time)) h,
  max(CurrentMetric_MemoryTracking)/{GIB} tracked_max, avg(CurrentMetric_MemoryTracking)/{GIB} tracked_avg,
  sum(ProfileEvent_InsertQuery) inserts, sum(ProfileEvent_Merge) merges
FROM system.metric_log WHERE {w} GROUP BY h ORDER BY h""",
    ):
        r.update({k: v for k, v in hourly_async.get(r["h"], {}).items() if k != "h"})
        r.setdefault("rss", 0.0)
        r.setdefault("mark_mib", 0.0)
        r.setdefault("parts", 0.0)
        hourly.append(r)
    errors_hourly = q_json(
        env,
        f"""
SELECT toString(toStartOfHour(event_time)) h,
  countIf(logger_name = 'MergeTreeBackgroundExecutor') merge_failures,
  countIf(logger_name != 'MergeTreeBackgroundExecutor') other_failures
FROM system.text_log WHERE {w} AND level = 'Error' AND message LIKE '%MEMORY_LIMIT_EXCEEDED%'
GROUP BY h ORDER BY h""",
    )
    by_source = q_json(
        env,
        f"""
SELECT if(logger_name = 'MergeTreeBackgroundExecutor', 'background merge',
          if(logger_name LIKE '%SystemLog%', 'system log flush', logger_name)) source,
  extract(message, '\\\\{{([0-9a-f-]{{36}})::') uuid,
  count() n, toString(min(event_time)) first, toString(max(event_time)) last
FROM system.text_log WHERE {w} AND level = 'Error' AND message LIKE '%MEMORY_LIMIT_EXCEEDED%'
GROUP BY source, uuid ORDER BY n DESC LIMIT 20""",
    )
    names = {
        r["uuid"]: f"{r['database']}.{r['name']}"
        for r in q_json(
            env, "SELECT toString(uuid) uuid, database, name FROM system.tables"
        )
    }
    for s in by_source:
        s["table"] = names.get(s.pop("uuid"), "")
    # Span-insert failures come from query_log (the writer's INSERTs), not text_log.
    by_source += q_json(
        env,
        f"""
SELECT 'span INSERT (writer)' source, '' table, count() n, toString(min(event_time)) first,
  toString(max(event_time)) last
FROM system.query_log WHERE {w} AND exception_code = 241 AND query_kind = 'Insert'
HAVING n > 0""",
    )
    settings = {
        r["name"]: float(r["value"])
        if r["value"].replace(".", "", 1).isdigit()
        else r["value"]
        for r in q_json(
            env,
            """
SELECT name, value FROM system.server_settings WHERE name IN ('max_server_memory_usage',
  'mark_cache_size', 'uncompressed_cache_size', 'background_pool_size',
  'merges_mutations_memory_usage_soft_limit', 'merges_mutations_memory_usage_to_ram_ratio')""",
        )
    }
    for r in q_json(
        env,
        """
SELECT name, value FROM system.merge_tree_settings WHERE name IN (
  'vertical_merge_algorithm_min_rows_to_activate', 'vertical_merge_algorithm_min_columns_to_activate')""",
    ):
        settings[r["name"]] = float(r["value"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"from": t_from, "to": t_to},
        "hourly": hourly,
        "memory_errors_hourly": errors_hourly,
        "memory_errors_by_source": by_source,
        "trend": {
            "tracked_avg_gib_per_h": growth_per_hour(hourly, "tracked_avg"),
            "rss_gib_per_h": growth_per_hour(hourly, "rss"),
            "first_hour_over_cap": first_hour_over(hourly, "tracked_max", 1.5),
        },
        "parts": q_json(env, """
SELECT database, table, partition, count() active_parts, sum(rows) AS total_rows, max(level) max_level
FROM system.parts WHERE active GROUP BY database, table, partition ORDER BY active_parts DESC LIMIT 25"""),
        "merges": q_json(env, "SELECT database, table, elapsed, memory_usage, merge_algorithm FROM system.merges"),
        "unfinished_mutations": int(il.ch_query(env, "SELECT count() FROM system.mutations WHERE NOT is_done")),
        "caches": {
            r["metric"]: float(r["value"])
            for r in q_json(env, """
SELECT metric, value FROM system.asynchronous_metrics
WHERE metric IN ('MarkCacheBytes', 'UncompressedCacheBytes', 'IndexMarkCacheBytes', 'MMapCacheCells')""")
        },
        "metric_log_columns": int(il.ch_query(env, "SELECT count() FROM system.columns WHERE database='system' AND table='metric_log'")),
        "settings": settings,
    }  # fmt: skip


def merge_probe(env: dict, parts: int = 3) -> dict:
    """Copy system.metric_log into two scratch tables in vigil_load (same schema; one with the
    default horizontal-merge thresholds, one forced vertical), insert as `parts` parts, then
    merge each and sample its peak memory from system.merges. Drops the tables afterwards."""
    db = il.assert_load_db(il.LOAD_DB)
    rows = int(il.ch_query(env, "SELECT count() FROM system.metric_log"))
    cols = int(
        il.ch_query(
            env,
            "SELECT count() FROM system.columns WHERE database='system' AND table='metric_log'",
        )
    )
    settings = {
        "horizontal": "",
        "vertical": " SETTINGS vertical_merge_algorithm_min_rows_to_activate = 1, "
                    "vertical_merge_algorithm_min_columns_to_activate = 1",
    }  # fmt: skip
    results = []
    for table, (algo, extra) in zip(PROBE_TABLES, settings.items(), strict=True):
        il.ch_query(env, f"DROP TABLE IF EXISTS {db}.{table}")
        il.ch_query(env, f"CREATE TABLE {db}.{table} AS system.metric_log "
                         f"ENGINE = MergeTree ORDER BY (event_date, event_time){extra}")  # fmt: skip
        il.ch_query(env, f"SYSTEM STOP MERGES {db}.{table}")
        for i in range(parts):
            il.ch_query(env, f"INSERT INTO {db}.{table} SELECT * FROM system.metric_log "
                             f"WHERE cityHash64(event_time) % {parts} = {i}")  # fmt: skip
        il.ch_query(env, f"SYSTEM START MERGES {db}.{table}")
        peak, algo_seen = 0, ""
        t0 = time.time()
        opt = subprocess.Popen(
            ["docker", "exec", "vigil-clickhouse", "clickhouse-client", "-u", env["CLICKHOUSE_USER"],
             "--password", env["CLICKHOUSE_PASSWORD"], "-q", f"OPTIMIZE TABLE {db}.{table} FINAL"],
        )  # fmt: skip
        while opt.poll() is None:
            for r in q_json(env, f"SELECT memory_usage, merge_algorithm FROM system.merges "
                                 f"WHERE database = '{db}' AND table = '{table}'"):  # fmt: skip
                peak, algo_seen = (
                    max(peak, int(r["memory_usage"])),
                    r["merge_algorithm"],
                )
            time.sleep(0.1)
        seconds = time.time() - t0
        if opt.returncode != 0:
            raise RuntimeError(f"OPTIMIZE {table} failed ({opt.returncode})")
        results.append({"algorithm": algo_seen or algo, "requested": algo, "peak_bytes": peak,
                        "seconds": seconds})  # fmt: skip
        il.ch_query(env, f"DROP TABLE {db}.{table}")
    return {"rows": rows, "columns": cols, "parts": parts, "results": results}


def writer_restarts() -> int:
    out = il.sh(
        ["docker", "inspect", "-f", "{{.RestartCount}}", "vigil-load-writer"],
        check=False,
    )
    return int(out) if out.isdigit() else -1


def finish_soak(env: dict, out: Path) -> int:
    """Reconcile a soak run and rebuild ClickHouse's memory / merge / error history over its
    window from ClickHouse's own system logs, so nothing depends on the soak process staying
    alive for hours."""
    il.require_load_pipeline()
    run_dir = out / "run"
    summary = json.loads((run_dir / "summary.json").read_text())
    start_path = out / "start.json"
    start = json.loads(start_path.read_text()) if start_path.exists() else {}
    rec = il.reconcile(env, run_dir, drain_timeout="30m")
    fmt = "%Y-%m-%d %H:%M:%S"
    t_from = datetime.fromtimestamp(summary["start_ns"] / 1e9, timezone.utc).strftime(
        fmt
    )
    t_to = datetime.fromtimestamp(summary["end_ns"] / 1e9, timezone.utc).strftime(fmt)
    hist = diagnose(env, t_from, t_to)
    hist["window"]["cap_gib"] = hist["settings"].get("max_server_memory_usage", 0) / GIB
    keys = ("config", "spans_acked", "spans_unacked", "requests_failed", "retries",
            "request_latency_ms", "cpu_fraction", "schedule_lag_ms")  # fmt: skip
    report = {
        "start": start,
        "summary": {k: summary[k] for k in keys},
        "reconcile": rec,
        "clickhouse_at_end": il.clickhouse_state(env),
        "writer_restarts_at_end": writer_restarts(),
        "history": hist,
    }
    (out / "soak.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "soak.md").write_text(render_soak(report))
    print((out / "soak.md").read_text())
    return 0 if rec and rec.get("clean") else 1


def loadgen_json(env: dict, sub: str) -> dict:
    out = subprocess.run([str(il.LOADGEN), sub], cwd=il.INGEST, env=il.loadgen_env(env),
                         check=True, capture_output=True, text=True).stdout  # fmt: skip
    return json.loads(out)


def stored_rows(env: dict, run_id: str) -> int:
    return int(
        il.ch_query(
            env,
            f"SELECT count() FROM {il.assert_load_db(il.LOAD_DB)}.spans "
            f"WHERE run_id = '{run_id}' SETTINGS max_threads = 1",
        )
    )


def oldest_pending_ms(offsets: dict) -> list[int]:
    """Timestamp (ms) of the first unconsumed record in each partition with a backlog."""
    out = []
    topic = il.assert_load_topic(offsets["topic"])
    for p in offsets["partitions"]:
        if p["end"] > max(p["committed"], p["log_start"]):
            at = max(p["committed"], p["log_start"])
            ts = il.sh(["docker", "exec", "vigil-redpanda", "rpk", "topic", "consume", topic,
                        "-p", str(p["partition"]), "-o", str(at), "-n", "1", "-f", "%d\\n"])  # fmt: skip
            out.append(int(ts))
    return out


def snapshot(env: dict, run_id: str, probe_s: int) -> dict:
    """Best-effort diagnostic state of a (possibly memory-starved) ClickHouse: every query
    runs single-threaded, and a query that fails is recorded, not fatal. Ends with a progress
    probe: stored rows, writer restarts and backlog now and after probe_s seconds."""
    snap: dict = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "failed_queries": [],
    }
    queries = {
        "server": f"""SELECT round(uptime() / 3600, 2) uptime_h,
  round((SELECT value FROM system.metrics WHERE metric = 'MemoryTracking') / {GIB}, 2) tracked_gib,
  round((SELECT value FROM system.asynchronous_metrics WHERE metric = 'MemoryResident') / {GIB}, 2) rss_gib,
  (SELECT sum(value) FROM system.errors WHERE name = 'MEMORY_LIMIT_EXCEEDED') memory_limit_errors,
  (SELECT value FROM system.server_settings WHERE name = 'max_server_memory_usage') cap_bytes""",
        "errors": "SELECT name, value, toString(last_error_time) last FROM system.errors "
                  "ORDER BY value DESC LIMIT 15",
        "parts": """SELECT database, table, partition, count() active_parts, sum(rows) AS total_rows,
  sum(bytes_on_disk) AS bytes, max(level) max_level FROM system.parts WHERE active
  AND database IN ('system', 'vigil_load') GROUP BY database, table, partition
  ORDER BY active_parts DESC LIMIT 30""",
        "merges": "SELECT database, table, elapsed, progress, num_parts, memory_usage, "
                  "merge_algorithm FROM system.merges",
        "mutations": "SELECT database, table, mutation_id, command, latest_fail_reason "
                     "FROM system.mutations WHERE NOT is_done",
        "recent_errors": """SELECT logger_name, count() n, any(substring(message, 1, 240)) example
  FROM system.text_log WHERE event_time > now() - INTERVAL 30 MINUTE AND level = 'Error'
  GROUP BY logger_name ORDER BY n DESC LIMIT 15""",
    }  # fmt: skip
    for name, sql in queries.items():
        try:
            rows = q_json(env, sql + " SETTINGS max_threads = 1")
            snap[name] = rows[0] if name == "server" and rows else rows
        except RuntimeError as e:
            snap[name] = {"error": str(e)[-300:]}
            snap["failed_queries"].append(name)
    snap["cap_gib"] = float(snap["server"].get("cap_bytes", 0) or 0) / GIB
    snap["offsets"] = loadgen_json(env, "offsets")
    snap["group"] = loadgen_json(env, "lag")
    t0, s0, r0 = time.time(), stored_rows(env, run_id), writer_restarts()
    time.sleep(probe_s)
    s1, r1 = stored_rows(env, run_id), writer_restarts()
    snap["progress_probe"] = {
        "seconds": round(time.time() - t0),
        "stored_before": s0, "stored_delta": s1 - s0, "writer_restarts_delta": r1 - r0,
        "pending_records": loadgen_json(env, "offsets")["pending"],
    }  # fmt: skip
    return snap


def recover(env: dict, run_id: str, timeout_s: int = 5400) -> dict:
    """Restart only ClickHouse (the Redpanda backlog stays) and sample every 10 s until the
    load writer has drained it: stored rows, group state, and retention (records deleted
    before being consumed). The load receiver stays stopped so nothing writes to the topic."""
    t_restart = time.time()
    il.sh(["docker", "restart", "vigil-clickhouse"])
    for _ in range(120):
        if (
            il.sh(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Health.Status}}",
                    "vigil-clickhouse",
                ]
            )
            == "healthy"
        ):
            break
        time.sleep(2)
    t_healthy = time.time()
    samples = []
    while time.time() - t_restart < timeout_s:
        try:
            off, grp = loadgen_json(env, "offsets"), loadgen_json(env, "lag")
            samples.append({"t": time.time(), "stored": stored_rows(env, run_id),
                            "drained": bool(grp["drained"]), "lag": grp["lag"],
                            "state": grp["state"], "writer_restarts": writer_restarts(),
                            "deleted_unconsumed": off["deleted_unconsumed"]})  # fmt: skip
        except (RuntimeError, subprocess.CalledProcessError, ValueError) as e:
            samples.append({"t": time.time(), "error": str(e)[-200:], "stored": 0,
                            "drained": False, "deleted_unconsumed": 0})  # fmt: skip
        good = [s for s in samples if "error" not in s]
        if recovery_milestones(good, t_restart)["drained_after_s"] is not None:
            break
        time.sleep(10)
    good = [s for s in samples if "error" not in s]
    return {"t_restart": t_restart, "clickhouse_healthy_after_s": t_healthy - t_restart,
            "samples": samples, "milestones": recovery_milestones(good, t_restart)}  # fmt: skip


# ---------------------------------------------------------------- commands


def cmd_diagnose(args) -> int:
    env = il.base_env()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    d = diagnose(env, args.t_from, args.t_to)
    d["window"]["cap_gib"] = args.cap_gib
    probe_path = out / "merge_probe.json"
    if probe_path.exists():
        d["merge_probe"] = json.loads(probe_path.read_text())
    (out / "diagnosis.json").write_text(json.dumps(d, indent=2) + "\n")
    (out / "diagnosis.md").write_text(render_diagnosis(d))
    print((out / "diagnosis.md").read_text())
    return 0


def cmd_merge_probe(args) -> int:
    env = il.base_env()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    p = merge_probe(env)
    p["measured_at"] = datetime.now(timezone.utc).isoformat()
    p["max_server_memory_usage"] = float(il.ch_query(
        env, "SELECT value FROM system.server_settings WHERE name = 'max_server_memory_usage'"))  # fmt: skip
    (out / "merge_probe.json").write_text(json.dumps(p, indent=2) + "\n")
    print(json.dumps(p, indent=2))
    return 0


def cmd_reset_system_logs(args) -> int:
    if not args.yes:
        print(f"Would TRUNCATE system.{{{', '.join(SYSTEM_LOGS)}}} (ClickHouse's own logs; "
              "save any diagnosis first). Re-run with --yes.")  # fmt: skip
        return 1
    env = il.base_env()
    for t in SYSTEM_LOGS:
        il.ch_query(env, f"TRUNCATE TABLE IF EXISTS system.{t}")
    print("system logs emptied")
    return 0


def cmd_soak(args) -> int:
    """Hold a steady load for hours, then finish_soak. --finish-only reconciles and reports an
    existing soak run (e.g. after an interruption). The output path is resolved to an absolute
    path: the generator runs with ingest/ as its working directory."""
    env = il.base_env()
    out = Path(args.out).resolve()
    if not args.finish_only:
        il.require_load_pipeline()
        il.pin_pipeline()
        il.build_loadgen(env)
        il.eval_guard(il.active_eval_runs(env), il.engine_run_processes(), set())
        cal = json.loads((il.RESULTS / "pilot" / "pilot.json").read_text())[
            "calibration"
        ]
        spans = int(args.rate * args.hours * 3600)
        il.check_disk(il.project_disk(spans, cal["planning_ch_bytes_per_span"],
                                      cal["topic_bytes_per_span"], cal["topic_cap"]), il.free_bytes())  # fmt: skip
        out.mkdir(parents=True, exist_ok=True)
        il.truncate_load_tables(env)
        start = {"hardware": il.hardware(), "rate": args.rate, "hours": args.hours,
                 "clickhouse": il.clickhouse_state(env), "writer_restarts": writer_restarts()}  # fmt: skip
        (out / "start.json").write_text(json.dumps(start, indent=2) + "\n")
        run_id = f"soak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        il.run_loadgen(env, out / "run", args.protocol, run_id, args.rate,
                       f"{int(args.hours * 3600)}s")  # fmt: skip
    return finish_soak(env, out)


def cmd_recover(args) -> int:
    """For a soak left undrained on a degraded ClickHouse: snapshot the degraded state and
    measure its throughput with no load, check retention headroom (stop if unconsumed records
    are at risk), then restart only ClickHouse, time the writer's recovery and drain, and
    reconcile the soak."""
    env = il.base_env()
    out = Path(args.out).resolve()
    run_id = json.loads((out / "run" / "summary.json").read_text())["config"]["run_id"]
    il.eval_guard(il.active_eval_runs(env), il.engine_run_processes(), set())
    il.pin_pipeline()
    degraded = snapshot(env, run_id, args.probe_s)
    (out / "degraded-snapshot.json").write_text(json.dumps(degraded, indent=2) + "\n")
    print(
        json.dumps(
            {k: degraded[k] for k in ("server", "failed_queries", "progress_probe")},
            indent=2,
        )
    )

    retention_ms = int(il.sh(["docker", "exec", "vigil-redpanda", "rpk", "topic", "describe",
                              il.LOAD_TOPICS[0], "-c"]).split("retention.ms", 1)[1].split()[0])  # fmt: skip
    offsets = loadgen_json(env, "offsets")
    risks = retention_risks(
        offsets, oldest_pending_ms(offsets), retention_ms, int(time.time() * 1000)
    )
    if risks:
        print(
            "STOP: unconsumed soak records are at risk; not restarting ClickHouse:",
            *risks,
            sep="\n  ",
        )
        return 2
    il.sh(
        ["docker", "stop", "vigil-load-receiver"]
    )  # the only producer to the load topic
    report = {
        "degraded": degraded,
        "preflight_offsets": offsets,
        "preflight_risks": risks,
    }
    report.update(recover(env, run_id))
    il.sh(
        ["docker", "start", "vigil-load-receiver"]
    )  # reconciliation expects the pipeline up
    (out / "recovery.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "recovery.md").write_text(render_recovery(report))
    print((out / "recovery.md").read_text())
    if report["milestones"]["drained_after_s"] is None:
        print("backlog not drained within the timeout; soak not reconciled")
        return 1
    return finish_soak(env, out)


def cmd_annotate_recovery(args) -> int:
    """Record a manual intervention made during a recovery (e.g. raising a container's memory
    limit) in recovery.json, time the drain from it using the stored samples, and re-render
    recovery.md — so the report attributes the recovery to what actually caused it."""
    out = Path(args.out).resolve()
    rep = json.loads((out / "recovery.json").read_text())
    at = datetime.fromisoformat(args.at.replace("Z", "+00:00")).timestamp()
    after = [s for s in rep["samples"] if "error" not in s and s["t"] >= at - 10]
    rep.setdefault("interventions", []).append({
        "at": args.at, "at_s": at - rep["t_restart"], "action": args.action, "reason": args.reason,
        "peak_writer_mib": args.peak_writer_mib, "milestones": recovery_milestones(after, at),
    })  # fmt: skip
    (out / "recovery.json").write_text(json.dumps(rep, indent=2) + "\n")
    (out / "recovery.md").write_text(render_recovery(rep))
    print((out / "recovery.md").read_text())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("ch_uptime")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diagnose")
    d.add_argument("--from", dest="t_from", required=True)
    d.add_argument("--to", dest="t_to", required=True)
    d.add_argument(
        "--cap-gib",
        type=float,
        default=1.5,
        help="the cap the diagnosed instance ran with",
    )
    d.add_argument("--out", required=True)
    d.set_defaults(func=cmd_diagnose)
    m = sub.add_parser("merge-probe")
    m.add_argument("--out", required=True)
    m.set_defaults(func=cmd_merge_probe)
    r = sub.add_parser("reset-system-logs")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(func=cmd_reset_system_logs)
    s = sub.add_parser("soak")
    s.add_argument("--rate", type=float, default=200)
    s.add_argument("--hours", type=float, default=6)
    s.add_argument("--protocol", default="grpc")
    s.add_argument("--out", required=True)
    s.add_argument(
        "--finish-only", action="store_true", help="reconcile + report an existing soak"
    )
    s.set_defaults(func=cmd_soak)
    rc = sub.add_parser("recover")
    rc.add_argument("--out", required=True, help="the soak's output directory")
    rc.add_argument(
        "--probe-s", type=int, default=300, help="no-load progress probe length"
    )
    rc.set_defaults(func=cmd_recover)
    an = sub.add_parser("annotate-recovery")
    an.add_argument("--out", required=True)
    an.add_argument(
        "--at", required=True, help="UTC time of the intervention (ISO 8601)"
    )
    an.add_argument("--action", required=True)
    an.add_argument("--reason", required=True)
    an.add_argument("--peak-writer-mib", type=float)
    an.set_defaults(func=cmd_annotate_recovery)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
