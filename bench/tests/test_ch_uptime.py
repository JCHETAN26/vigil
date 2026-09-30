"""Stack-free checks of the ClickHouse uptime diagnosis helpers (trend, threshold, report)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ch_uptime as cu


def test_growth_per_hour_is_the_least_squares_slope():
    rows = [{"x": 1.0}, {"x": 1.5}, {"x": 2.0}, {"x": 2.5}]
    assert cu.growth_per_hour(rows, "x") == pytest.approx(0.5)
    noisy = [{"x": 1.0}, {"x": 3.0}, {"x": 2.0}, {"x": 4.0}]
    assert cu.growth_per_hour(noisy, "x") == pytest.approx(0.8)
    assert cu.growth_per_hour([{"x": 1.0}], "x") == 0.0


def test_first_hour_over():
    rows = [{"h": "a", "m": 1.0}, {"h": "b", "m": 1.6}, {"h": "c", "m": 1.2}]
    assert cu.first_hour_over(rows, "m", 1.5) == "b"
    assert cu.first_hour_over(rows, "m", 2.0) is None


def _diagnosis():
    hourly = [
        {"h": "2026-09-28 00:00:00", "tracked_avg": 0.74, "tracked_max": 0.84, "rss": 0.83,
         "inserts": 0, "merges": 1270, "mark_mib": 0.0, "parts": 46.0},
        {"h": "2026-09-28 01:00:00", "tracked_avg": 0.79, "tracked_max": 1.53, "rss": 0.88,
         "inserts": 0, "merges": 1471, "mark_mib": 0.0, "parts": 45.0},
    ]  # fmt: skip
    return {
        "generated_at": "2026-09-29T10:00:00Z",
        "window": {
            "from": "2026-09-27 22:13:05",
            "to": "2026-09-28 09:30:40",
            "cap_gib": 1.5,
        },
        "hourly": hourly,
        "memory_errors_hourly": [
            {"h": "2026-09-28 01:00:00", "merge_failures": 388, "other_failures": 0}
        ],
        "memory_errors_by_source": [
            {
                "source": "background merge",
                "table": "system.metric_log",
                "n": 20271,
                "first": "2026-09-28 01:39:00",
                "last": "2026-09-28 09:30:00",
            },
        ],
        "trend": {
            "tracked_avg_gib_per_h": 0.05,
            "rss_gib_per_h": 0.05,
            "first_hour_over_cap": "2026-09-28 01:00:00",
        },
        "parts": [
            {
                "database": "system",
                "table": "metric_log",
                "partition": "202609",
                "active_parts": 2,
                "total_rows": 129495,
                "max_level": 385,
            }
        ],
        "merges": [],
        "unfinished_mutations": 0,
        "caches": {"MarkCacheBytes": 1048576.0},
        "metric_log_columns": 991,
        "settings": {"max_server_memory_usage": 1.5 * cu.GIB},
        "merge_probe": {
            "rows": 129442,
            "columns": 991,
            "parts": 3,
            "results": [
                {"algorithm": "Horizontal", "peak_bytes": 4.1 * cu.GIB, "seconds": 8.7},
                {"algorithm": "Vertical", "peak_bytes": 0.02 * cu.GIB, "seconds": 9.1},
            ],
        },
    }


def test_render_diagnosis_shows_trend_culprit_and_probe():
    md = cu.render_diagnosis(_diagnosis())
    assert "| 09-28 01:00 | 0.79 / 1.53 | 0.88 | 0 | 1471 | 388 | 0 | 0 | 45 |" in md
    assert "| background merge | system.metric_log | 20271 |" in md
    assert "first exceeded the cap in hour **2026-09-28 01:00:00**" in md
    assert "**991**" in md
    assert (
        "| Horizontal | 4.10 GiB | 8.7 |" in md
        and "| Vertical | 0.02 GiB | 9.1 |" in md
    )


def test_render_soak_reports_delivery_reconciliation_and_restarts():
    report = {
        "start": {"writer_restarts": 0},
        "summary": {
            "config": {
                "protocol": "grpc",
                "rate_spans_per_sec": 200.0,
                "duration_ns": 21_600 * 10**9,
            },
            "spans_acked": 4_320_097,
            "spans_unacked": 0,
            "request_latency_ms": {"p99": 148.0},
            "cpu_fraction": {"p95": 0.01},
        },
        "reconcile": {
            "reconciliation": {
                "stored_acked_unique_spans": 4_320_097,
                "missing_spans": 0,
                "duplicate_rows": 0,
            },
            "trace_index_span_count": 4_320_097,
            "dlq_records": 0,
            "clean": True,
        },
        "writer_restarts_at_end": 312,
        "clickhouse_at_end": {
            "uptime_s": 21_700,
            "memory_tracking_bytes": 1.5 * cu.GIB,
        },
        "history": _diagnosis(),
    }
    md = cu.render_soak(report)
    assert "grpc at 200 spans/s for 6.0 h" in md
    assert "acked 4,320,097, unacked 0" in md
    assert "missing 0, duplicate rows 0" in md and "clean = True" in md
    assert "312 during the soak" in md
    assert "| background merge | system.metric_log | 20271 |" in md  # history included
    report["start"] = {}
    assert "312 since the container was created" in cu.render_soak(report)


H = 3_600_000  # ms


@pytest.mark.parametrize(
    ("deleted", "oldest_age_h", "n_risks"),
    [(0, 10, 0), (5, 10, 1), (0, 44, 1), (3, 45, 2)],
)
def test_retention_risks(deleted, oldest_age_h, n_risks):
    now = 1_000 * H
    offsets = {"deleted_unconsumed": deleted}
    risks = cu.retention_risks(offsets, [now - oldest_age_h * H, now - H], 48 * H, now)
    assert len(risks) == n_risks
    assert cu.retention_risks({"deleted_unconsumed": 0}, [], 48 * H, now) == []


def _sample(t, stored, drained=False, deleted=0):
    return {"t": t, "stored": stored, "drained": drained, "deleted_unconsumed": deleted}


def test_recovery_milestones():
    samples = [
        _sample(110, 1000), _sample(120, 1000), _sample(130, 5000),
        _sample(140, 9000, drained=True), _sample(150, 9000, drained=True, deleted=0),
    ]  # fmt: skip
    m = cu.recovery_milestones(samples, t_restart=100)
    assert m == {
        "writer_recovered_after_s": 30,
        "drained_after_s": 50,
        "max_deleted_unconsumed": 0,
    }
    stuck = [_sample(110, 1000), _sample(120, 1000, deleted=7)]
    assert cu.recovery_milestones(stuck, 100) == {
        "writer_recovered_after_s": None,
        "drained_after_s": None,
        "max_deleted_unconsumed": 7,
    }


def test_render_recovery():
    report = {
        "degraded": {
            "server": {
                "uptime_h": 11.4,
                "tracked_gib": 1.17,
                "memory_limit_errors": 156197,
            },
            "cap_gib": 1.5,
            "failed_queries": ["recent_errors"],
            "progress_probe": {
                "seconds": 300,
                "stored_delta": 0,
                "writer_restarts_delta": 6,
                "pending_records": 425913,
            },
        },
        "preflight_risks": [],
        "preflight_offsets": {"min_consumed_retained": 9742},
        "milestones": {
            "writer_recovered_after_s": 45,
            "drained_after_s": 1260,
            "max_deleted_unconsumed": 0,
        },
    }
    md = cu.render_recovery(report)
    assert "0 spans stored in 5 min" in md and "restarted 6 times" in md
    assert "425,913 records" in md and "(recent_errors)" in md
    assert "no risk" in md and "9,742" in md
    assert "**0.8 min**" in md and "**21.0 min**" in md
    assert "at any point: **0**" in md


def test_render_recovery_attributes_recovery_to_the_intervention():
    base = {
        "degraded": {
            "server": {"uptime_h": 12.2, "tracked_gib": 1.16, "memory_limit_errors": 161395},
            "cap_gib": 1.5,
            "failed_queries": [],
            "progress_probe": {"seconds": 300, "stored_delta": 0, "writer_restarts_delta": 6,
                               "pending_records": 425913},
        },
        "preflight_risks": [],
        "preflight_offsets": {"min_consumed_retained": 9742},
        "milestones": {"writer_recovered_after_s": 2250, "drained_after_s": 2994,
                       "max_deleted_unconsumed": 0},
        "interventions": [{
            "at_s": 2225, "action": "raised the load writer's memory to 2 GiB",
            "reason": "OOM-killed at 512 MiB while draining", "peak_writer_mib": 1012,
            "milestones": {"writer_recovered_after_s": 25, "drained_after_s": 769,
                           "max_deleted_unconsumed": 0},
        }],
    }  # fmt: skip
    md = cu.render_recovery(base)
    assert "Restarting ClickHouse alone: **no progress** in the 37.1 min" in md
    assert (
        "**Intervention at +37.1 min:** raised the load writer's memory to 2 GiB" in md
    )
    assert "first stored rows in **0.4 min**" in md and "drained in **12.8 min**" in md
    assert "peak writer memory **1012 MiB**" in md
    assert "after the restart; backlog drained" not in md


def test_probe_only_touches_the_load_database():
    assert all(not t.startswith("system") for t in cu.PROBE_TABLES)
    with pytest.raises(ValueError):
        cu.il.assert_load_db("system")
