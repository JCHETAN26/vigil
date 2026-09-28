"""Fast, stack-free checks of the ingestion load-test orchestrator: the cleanup safety guards,
disk projection and refusal, generator-bottleneck flags, and the parsers/renderer. The live
pipeline is exercised by `python bench/ingest_load.py pilot`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ingest_load as il

GIB = 1024**3


@pytest.mark.parametrize(
    "name", ["vigil", "vigil_load2", "default", "", "vigil_load; DROP DATABASE vigil"]
)
def test_cleanup_guard_refuses_anything_but_the_load_database(name):
    with pytest.raises(ValueError):
        il.assert_load_db(name)
    assert il.assert_load_db("vigil_load") == "vigil_load"


@pytest.mark.parametrize("topic", ["otlp.spans.raw", "spans.dlq", "otlp.spans.load2"])
def test_cleanup_guard_refuses_real_topics(topic):
    with pytest.raises(ValueError):
        il.assert_load_topic(topic)
    for t in il.LOAD_TOPICS:
        assert il.assert_load_topic(t) == t


def test_loadgen_env_points_at_the_isolated_pipeline_only():
    env = il.loadgen_env(
        {
            "CLICKHOUSE_DB": "vigil",
            "VIGIL_RAW_TOPIC": "otlp.spans.raw",
            "PYTHONPATH": "/opt/ros",
            "X": "1",
        }
    )
    assert env["CLICKHOUSE_DB"] == "vigil_load"
    assert env["VIGIL_RAW_TOPIC"] == "otlp.spans.load"
    assert env["VIGIL_DLQ_TOPIC"] == "spans.dlq.load"
    assert env["VIGIL_KAFKA_GROUP"] == "vigil-writer-load"
    assert "PYTHONPATH" not in env and env["X"] == "1"


def test_project_disk_caps_topic_at_retention_and_adds_merge_headroom():
    # 1M spans: ClickHouse 600 MB x2 headroom; topic 700 MB, under the cap.
    assert il.project_disk(1_000_000, 600, 700, 2 * GIB) == 1_200_000_000 + 700_000_000
    # 10M spans: topic would be 7 GB but retention caps it at 2 GiB.
    assert il.project_disk(10_000_000, 600, 700, 2 * GIB) == 12_000_000_000 + 2 * GIB


def test_check_disk_refuses_below_reserve():
    il.check_disk(projected=10 * GIB, free=20 * GIB, reserve_gb=5)  # leaves 10 GB: fine
    with pytest.raises(RuntimeError, match="refusing"):
        il.check_disk(
            projected=16 * GIB, free=20 * GIB, reserve_gb=5
        )  # would leave 4 GB


def test_stage2_projection_covers_every_ramp_step():
    rows = il.stage2_projection(642, 700, 2 * GIB)
    assert [r["rate"] for r in rows] == list(il.RAMP_RATES)
    for r in rows:
        assert r["spans"] == r["rate"] * (il.WARMUP_S + il.STEADY_S)
        assert r["peak_bytes"] == il.project_disk(r["spans"], 642, 700, 2 * GIB)
    assert rows[-1]["peak_bytes"] > rows[0]["peak_bytes"]


@pytest.mark.parametrize(
    ("cpu_p95", "lag_p99", "n_flags"),
    [
        (0.10, 2.0, 0),
        (0.90, 2.0, 1),
        (0.10, 500.0, 1),
        (0.99, 900.0, 2),
        (0.85, 100.0, 1),
    ],
)
def test_generator_flags(cpu_p95, lag_p99, n_flags):
    s = {"cpu_fraction": {"p95": cpu_p95}, "schedule_lag_ms": {"p99": lag_p99}}
    assert len(il.generator_flags(s)) == n_flags


def test_parsers():
    lscpu = "Architecture:  aarch64\nCPU(s):  4\nVendor ID:  ARM\nModel name:  Neoverse-N1\n"
    assert il.parse_lscpu(lscpu) == {
        "model": "Neoverse-N1",
        "vendor": "ARM",
        "cpus": 4,
        "architecture": "aarch64",
    }
    assert il.parse_meminfo("MemTotal:       24556036 kB\nMemFree: 1 kB\n") == 23.4
    env = il.parse_env("# c\nA=1\nB = 'two'\n\nC=\"x=y\"\nnot a pair\n")
    assert env == {"A": "1", "B": "two", "C": "x=y"}
    logdirs = (
        "BROKER  DIR  TOPIC  PARTITION  SIZE  ERROR\n"
        "0  /d  otlp.spans.load  0  100\n"
        "0  /d  otlp.spans.load  1  250\n"
        "0  /d  otlp.spans.raw  0  999\n"
    )
    assert il.parse_logdirs(logdirs, "otlp.spans.load") == 350


def _report():
    run = {
        "summary": {
            "config": {"protocol": "grpc", "rate_spans_per_sec": 1000.0},
            "request_latency_ms": {"p99": 80.0},
            "cpu_fraction": {"p95": 0.05},
            "schedule_lag_ms": {"p99": 2.0},
            "spans_sent": 20000,
        },
        "reconcile": {
            "reconciliation": {
                "acked_spans": 20000,
                "stored_acked_unique_spans": 20000,
                "missing_spans": 0,
                "duplicate_rows": 0,
                "unacked_spans": 0,
            },
            "trace_index_span_count": 20000,
            "dlq_records": 0,
            "e2e_latency_ms": {"p50": 2100.0, "p99": 3900.0},
            "clean": True,
        },
        "flags": [],
    }
    return {
        "generated_at": "2026-09-28T00:00:00Z",
        "hardware": {
            "cpu": {"cpus": 4, "model": "Neoverse-N1", "architecture": "aarch64"},
            "memory_gib": 23.4,
            "kernel": "6.8.0",
            "docker": "29.8.1",
            "go": "go1.27.1",
            "git": {"sha": "abcdef1234567", "dirty": False},
            "shared_machine": il.SHARED_MACHINE_NOTE,
            "containers": [
                {"name": "vigil-load-writer", "cpuset": "1-3", "memory": "512 MiB"}
            ],
        },
        "templates": {
            "traces": 515,
            "spans": 3464,
            "per_agent_traces_spans": {"hello-agent": [106, 332]},
            "sha256": "afbb664d6bead607",
            "fidelity": {"mismatches": 0, "legacy_rows_upgraded": 88},
        },
        "runs": [run],
        "calibration": {
            "ch_rows": 40000,
            "ch_bytes": 20_000_000,
            "ch_bytes_per_span": 500.0,
            "spans_sent": 40000,
            "topic_bytes_delta": 27_000_000,
            "topic_bytes_per_span": 675.0,
            "topic_cap": 2 * GIB,
            "planning_ch_bytes_per_span": 642.0,
        },
        "stage2_projection": il.stage2_projection(642, 675, 2 * GIB),
        "free_bytes": 30 * GIB,
        "largest_step_fits": True,
    }


def test_render_pilot_records_hardware_setup_and_every_run():
    md = il.render_pilot(_report())
    assert "4× Neoverse-N1 (aarch64), 23.4 GiB RAM" in md
    assert "pinned to core 0" in md  # the shared-machine setup is stated
    assert (
        "| grpc | 1000 | 20000 | 20000 | 0 | 0 | 0 | 20000 | 0 | 80 | 2100 / 3900 | 0.05 | 2 | yes | — |"
        in md
    )
    assert "88 legacy rows upgraded" in md
    assert (
        md.count("| 20000 | 14,400,000 |") == 1
    )  # the 20k spans/s step of the projection


# ---------------------------------------------------------------- stage 2 (baseline ramp)


def test_eval_guard_refuses_active_runs_and_live_processes():
    il.eval_guard([], [], set())  # nothing running: fine
    with pytest.raises(RuntimeError, match="in progress"):
        il.eval_guard(["run-1"], [], set())
    with pytest.raises(RuntimeError, match="in progress"):
        il.eval_guard([], ["1234 python -m engine run --suite x"], set())
    il.eval_guard(["stale-1"], [], {"stale-1"})  # an explicitly allowed stale row
    with pytest.raises(RuntimeError):
        il.eval_guard(["stale-1", "run-2"], [], {"stale-1"})


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("512MiB", 512 * 1024**2),
        ("1.5GiB", 1.5 * 1024**3),
        ("800kB", 800_000),
        ("12B", 12),
        ("?", 0.0),
    ],
)
def test_parse_size(text, want):
    assert il.parse_size(text) == want


def test_parse_docker_stats_and_lag():
    assert il.parse_docker_stats("vigil-clickhouse|183.50%|1.2GiB / 2GiB") == (
        "vigil-clickhouse",
        1.835,
        1.2 * 1024**3,
    )
    assert il.parse_docker_stats("garbage") is None
    rpk = (
        "GROUP  vigil-writer-load\nSTATE  Stable\nTOTAL-LAG  4521\n\nTOPIC  PARTITION\n"
    )
    assert il.parse_group_lag(rpk) == 4521
    assert il.parse_group_lag("GROUP x\n") is None


def test_window_throughput_counts_only_acked_spans_scheduled_in_window():
    sec = 10**9
    recs = [
        {
            "intended_ns": str(0 * sec),
            "spans": "100",
            "rejected_spans": "0",
            "ok": "true",
        },  # warm-up
        {
            "intended_ns": str(10 * sec),
            "spans": "500",
            "rejected_spans": "0",
            "ok": "true",
        },
        {
            "intended_ns": str(15 * sec),
            "spans": "500",
            "rejected_spans": "20",
            "ok": "true",
        },
        {
            "intended_ns": str(19 * sec),
            "spans": "500",
            "rejected_spans": "0",
            "ok": "false",
        },
        {
            "intended_ns": str(20 * sec),
            "spans": "999",
            "rejected_spans": "0",
            "ok": "true",
        },  # after
    ]
    assert il.window_throughput(recs, 10 * sec, 20 * sec) == (500 + 480) / 10


def _step_inputs(**over):
    summary = {"cpu_fraction": {"p95": 0.2}, "schedule_lag_ms": {"p99": 3.0}}
    rec = {
        "reconciliation": {
            "missing_spans": 0,
            "duplicate_rows": 0,
            "unacked_spans": 0,
            "unexpected_traces": 0,
            "stored_rows": 1000,
        },
        "dlq_records": 0,
        "trace_index_span_count": 1000,
        "e2e_latency_ms": {
            "p99": 9000.0
        },  # whole run incl. warm-up: ignored when a window exists
        "e2e_window_latency_ms": {"p99": 2500.0},
    }
    for k, v in over.items():
        if k in rec["reconciliation"]:
            rec["reconciliation"][k] = v
        else:
            rec[k] = v
    return summary, rec


def test_step_verdict_pass_uses_the_steady_state_window():
    assert il.step_verdict(*_step_inputs()) == ("pass", [])


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"missing_spans": 3}, "3 missing"),
        ({"duplicate_rows": 2}, "2 duplicate rows"),
        ({"unacked_spans": 512}, "512 unacked"),
        ({"dlq_records": 1}, "1 DLQ records"),
        ({"trace_index_span_count": 999}, "trace_index inconsistent"),
        ({"e2e_window_latency_ms": {"p99": 5000.0}}, "e2e p99 5000 ms >= 5000 ms"),
    ],
)
def test_step_verdict_fail_reasons(over, reason):
    verdict, reasons = il.step_verdict(*_step_inputs(**over))
    assert verdict == "fail" and reason in reasons


def test_step_verdict_generator_bound_is_inconclusive_and_undrained_fails():
    summary, rec = _step_inputs()
    summary["cpu_fraction"]["p95"] = 0.95
    assert il.step_verdict(summary, rec)[0] == "inconclusive"
    summary["cpu_fraction"]["p95"] = 0.2
    assert il.step_verdict(summary, None)[0] == "fail"


@pytest.mark.parametrize(
    ("verdict", "reasons", "throughput", "want"),
    [
        ("pass", [], 1995.0, False),
        (
            "fail",
            ["e2e p99 5200 ms >= 5000 ms"],
            1990.0,
            False,
        ),  # latency-only: keep ramping
        ("fail", ["e2e p99 5200 ms >= 5000 ms"], 1900.0, True),  # not keeping up
        ("fail", ["3 missing"], 2000.0, True),  # any loss stops the ramp
        (
            "fail",
            ["writer did not drain within the timeout (no reconciliation)"],
            2000.0,
            True,
        ),
        ("inconclusive", ["generator CPU-bound"], 2000.0, True),
    ],
)
def test_saturated(verdict, reasons, throughput, want):
    assert il.saturated(verdict, reasons, 2000, throughput) is want


def test_sustained_is_the_highest_passing_rate_even_after_a_latency_miss():
    steps = [
        {
            "rate": 500,
            "verdict": "fail",
            "window_throughput": 499.0,
            "saturated": False,
        },  # sparse: slow flush
        {
            "rate": 2000,
            "verdict": "pass",
            "window_throughput": 1995.0,
            "saturated": False,
        },
        {
            "rate": 5000,
            "verdict": "pass",
            "window_throughput": 4990.0,
            "saturated": False,
        },
        {
            "rate": 10000,
            "verdict": "fail",
            "window_throughput": 7100.0,
            "saturated": True,
        },
    ]
    assert il.sustained(steps) == {
        "sustained_rate": 5000,
        "sustained_window_throughput": 4990.0,
        "non_pass_rates": [500, 10000],
        "saturated_at": 10000,
    }
    none = il.sustained(
        [
            {
                "rate": 500,
                "verdict": "inconclusive",
                "window_throughput": 0.0,
                "saturated": True,
            }
        ]
    )
    assert none["sustained_rate"] is None and none["saturated_at"] == 500


def test_render_baseline():
    base = _report()
    summary, rec = _step_inputs()
    summary.update({"request_latency_ms": {"p99": 90.0}})
    rec["e2e_window_latency_ms"] = {"p50": 1600.0, "p95": 2400.0, "p99": 2500.0}
    res = {
        c: {"cpu_mean": 0.5, "cpu_max": 0.9, "mem_max_bytes": 1.0}
        for c in il.PINNED_CONTAINERS
    }
    res["max_lag"] = 1200
    step = {"protocol": "grpc", "rate": 2000, "verdict": "pass", "reasons": [], "window_throughput": 1998.0,
            "summary": summary, "reconcile": rec, "resources": res}  # fmt: skip
    base["hardware"]["writer_config"] = {"VIGIL_FLUSH_INTERVAL": "2s"}
    report = {"generated_at": "2026-09-28T12:00:00Z", "label": "asis", "limits_note": "current limits", "warmup_s": 120, "steady_s": 600,
              "hardware": base["hardware"], "steps": [step],
              "sustained": {"grpc": il.sustained([step])}}  # fmt: skip
    md = il.render_baseline(report)
    assert "| grpc | 2000 | 1998 | — | — (not reached) |" in md
    assert (
        "| grpc | 2000 | 1998 | 1600 / 2400 / 2500 | 90 | 0 | 0 | 0 | 0 | 1200 |" in md
    )
    assert "pinned to core 0" in md and "e2e p99 < 5 s" in md
