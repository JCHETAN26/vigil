"""Orchestrate ingestion load tests: the isolated load pipeline, the Go load generator, and the
reproducible results under bench/results/ingest/.

The generator (ingest/cmd/loadgen) replays real recorded traces; this script manages the load
pipeline (compose profile "loadtest": database vigil_load, topics otlp.spans.load /
spans.dlq.load), CPU pinning, disk checks, hardware capture, and reporting.

    python bench/ingest_load.py up          # start the load pipeline, pin CPUs
    python bench/ingest_load.py export      # templates from real traces (read-only user)
    python bench/ingest_load.py pilot       # small gRPC + HTTP runs: correctness + disk calibration
    python bench/ingest_load.py baseline --label asis   # stage 2: sustained-throughput ramp
    python bench/ingest_load.py limits sized            # apply sized memory limits (or: asis)
    python bench/ingest_load.py hardware    # print the hardware/setup record
    python bench/ingest_load.py cleanup     # remove ALL load data and unpin CPUs

Shared-machine setup (recorded with every result): the generator and the pipeline share one
host. The generator is pinned to core 0 (taskset); the pipeline containers (load receiver/
writer, ClickHouse, Redpanda) to cores 1-3 (cpuset). Host processes (dockerd, tailscaled, ...)
are unpinned.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

ROOT = Path(__file__).resolve().parent.parent
INGEST = ROOT / "ingest"
LOADGEN = INGEST / "bin" / "loadgen"
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    str(ROOT / ".env"),
    "-f",
    str(ROOT / "deploy/docker-compose.yml"),
    "--profile",
    "loadtest",
]
SIZED_OVERRIDE = ROOT / "deploy/docker-compose.sized.yml"
TEMPLATES = ROOT / "data/loadtest/templates.v1.pb"
RESULTS = ROOT / "bench/results/ingest"

# The ONLY things this script may delete. Cleanup refuses anything else.
LOAD_DB = "vigil_load"
LOAD_TOPICS = ("otlp.spans.load", "spans.dlq.load")
LOAD_GROUP = "vigil-writer-load"
LOAD_CONTAINERS = (
    "vigil-load-receiver",
    "vigil-load-writer",
    "vigil-load-migrate",
    "vigil-load-topics",
    "vigil-load-db",
)
LOAD_TABLES = ("spans", "trace_index", "agent_version_stats_daily")

GENERATOR_CORE = "0"
PIPELINE_CPUSET = "1-3"
PINNED_CONTAINERS = (
    "vigil-load-receiver",
    "vigil-load-writer",
    "vigil-clickhouse",
    "vigil-redpanda",
)
SHARED_MACHINE_NOTE = (
    "Generator and pipeline share one host. Generator pinned to core 0 (taskset); load receiver/writer, "
    "ClickHouse and Redpanda pinned to cores 1-3 (docker cpuset). Host processes (dockerd, tailscaled, "
    "sshd, ...) are unpinned and may run on any core."
)

# A run is flagged generator-bound when the generator saturates its core or cannot send on
# schedule: its numbers then describe the generator, not the pipeline.
GEN_CPU_P95_LIMIT = 0.85  # fraction of its one core
GEN_LAG_P99_LIMIT_MS = 100.0

DISK_RESERVE_GB = 5.0  # never let a run leave less than this free
CH_MERGE_HEADROOM = 2.0  # ClickHouse merges briefly need ~2x a part's size

# Stage-2 plan (sustained throughput): per protocol, a ramp of offered rates, each held for a
# 2-minute warm-up plus the 10-minute steady-state window.
RAMP_RATES = (500, 1000, 2000, 5000, 10000, 20000)
WARMUP_S, STEADY_S = 120, 600


# ---------------------------------------------------------------- pure helpers (tested)


def assert_load_db(name: str) -> str:
    if name != LOAD_DB:
        raise ValueError(
            f"refusing to operate on database {name!r}: only {LOAD_DB!r} is load-test data"
        )
    return name


def assert_load_topic(name: str) -> str:
    if name not in LOAD_TOPICS:
        raise ValueError(
            f"refusing to operate on topic {name!r}: only {LOAD_TOPICS} are load-test topics"
        )
    return name


def parse_env(text: str) -> dict[str, str]:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def loadgen_env(base: dict[str, str]) -> dict[str, str]:
    """Environment for loadgen run/reconcile: the load database, topics, and group."""
    env = dict(base)
    env.update(
        CLICKHOUSE_DB=LOAD_DB,
        VIGIL_RAW_TOPIC=LOAD_TOPICS[0],
        VIGIL_DLQ_TOPIC=LOAD_TOPICS[1],
        VIGIL_KAFKA_GROUP=LOAD_GROUP,
    )
    env.pop("PYTHONPATH", None)
    return env


def parse_lscpu(text: str) -> dict:
    fields = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return {
        "model": fields.get("Model name", "unknown"),
        "vendor": fields.get("Vendor ID", "unknown"),
        "cpus": int(fields.get("CPU(s)", "0") or 0),
        "architecture": fields.get("Architecture", "unknown"),
    }


def parse_meminfo(text: str) -> float:
    m = re.search(r"^MemTotal:\s+(\d+) kB", text, re.MULTILINE)
    return round(int(m.group(1)) / 1024 / 1024, 1) if m else 0.0


def generator_flags(summary: dict) -> list[str]:
    """Why a run's numbers may reflect the generator rather than the pipeline (empty = none)."""
    flags = []
    cpu_p95 = summary.get("cpu_fraction", {}).get("p95", 0.0)
    lag_p99 = summary.get("schedule_lag_ms", {}).get("p99", 0.0)
    if cpu_p95 >= GEN_CPU_P95_LIMIT:
        flags.append(
            f"generator CPU-bound (p95 {cpu_p95:.2f} of its core >= {GEN_CPU_P95_LIMIT})"
        )
    if lag_p99 > GEN_LAG_P99_LIMIT_MS:
        flags.append(
            f"generator behind schedule (lag p99 {lag_p99:.0f} ms > {GEN_LAG_P99_LIMIT_MS:.0f} ms)"
        )
    return flags


def project_disk(
    spans: int, ch_bytes_per_span: float, topic_bytes_per_span: float, topic_cap: int
) -> int:
    """Peak bytes a run of `spans` spans may occupy: ClickHouse (with merge headroom) plus the
    raw topic, which retention caps at topic_cap."""
    return int(
        spans * ch_bytes_per_span * CH_MERGE_HEADROOM
        + min(spans * topic_bytes_per_span, topic_cap)
    )


def check_disk(projected: int, free: int, reserve_gb: float = DISK_RESERVE_GB) -> None:
    reserve = int(reserve_gb * 1024**3)
    if free - projected < reserve:
        raise RuntimeError(
            f"refusing: projected {projected / 1024**3:.2f} GB would leave {(free - projected) / 1024**3:.2f} GB "
            f"free (< {reserve_gb} GB reserve)"
        )


def stage2_projection(
    ch_bytes_per_span: float, topic_bytes_per_span: float, topic_cap: int
) -> list[dict]:
    """Per-step disk for the stage-2 ramp (one protocol). Load tables are truncated between
    steps, so the peak is the largest single step, not the sum."""
    rows = []
    for rate in RAMP_RATES:
        spans = rate * (WARMUP_S + STEADY_S)
        rows.append(
            {
                "rate": rate,
                "spans": spans,
                "peak_bytes": project_disk(
                    spans, ch_bytes_per_span, topic_bytes_per_span, topic_cap
                ),
            }
        )
    return rows


def parse_logdirs(text: str, topic: str) -> int:
    """Sum the SIZE column of `rpk cluster logdirs describe` rows for topic."""
    total = 0
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 5 and cols[2] == topic:
            total += int(cols[4])
    return total


E2E_P99_LIMIT_MS = 5000.0  # sustained-throughput bar (owner decision, 2026-09-27)


def eval_guard(
    active_runs: list[str], engine_procs: list[str], allow: set[str]
) -> None:
    """Refuse to load the shared ClickHouse/Redpanda while a real eval run may be in progress:
    any eval_runs row still pending/running (unless explicitly allowed by id, for a known
    stale row left by a crash) or any live `engine run` process."""
    blocking = [r for r in active_runs if r not in allow]
    if blocking or engine_procs:
        raise RuntimeError(
            "refusing: a real eval run may be in progress "
            f"(eval_runs pending/running: {blocking or 'none'}; processes: {engine_procs or 'none'}). "
            "If a row is stale from a crash, pass --allow-stale-run <id>."
        )


def parse_size(text: str) -> float:
    """Docker's human sizes ('512MiB', '1.5GiB', '800kB', '12B') to bytes."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKMGT]?)(i?)B\s*", text)
    if not m:
        return 0.0
    base = 1024 if m.group(3) else 1000
    power = " KMGT".index(m.group(2).upper() or " ")
    return float(m.group(1)) * base**power


def parse_docker_stats(line: str) -> tuple[str, float, float] | None:
    """One `docker stats --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}'` line to
    (name, cpu in cores, memory bytes). Docker reports CPU as % of one core."""
    parts = line.strip().split("|")
    if len(parts) != 3 or not parts[1].endswith("%"):
        return None
    return parts[0], float(parts[1][:-1]) / 100.0, parse_size(parts[2].split("/")[0])


def parse_group_lag(text: str) -> int | None:
    """TOTAL-LAG from `rpk group describe` (None if absent)."""
    m = re.search(r"^TOTAL-LAG\s+(\d+)", text, re.MULTILINE)
    return int(m.group(1)) if m else None


def window_throughput(records: list[dict], start_ns: int, end_ns: int) -> float:
    """Acked spans/sec among requests scheduled inside [start_ns, end_ns)."""
    spans = sum(
        int(r["spans"]) - int(r["rejected_spans"])
        for r in records
        if r["ok"] == "true" and start_ns <= int(r["intended_ns"]) < end_ns
    )
    return spans / ((end_ns - start_ns) / 1e9)


def step_verdict(
    summary: dict, rec: dict | None, e2e_limit_ms: float = E2E_P99_LIMIT_MS
) -> tuple[str, list[str]]:
    """Classify one ramp step. 'pass' needs zero loss (nothing missing, duplicated, unacked,
    unexpected, or DLQ'd; trace_index consistent) and steady-state e2e p99 under the limit.
    A generator-bound step is 'inconclusive': it measured the generator, not the pipeline."""
    flags = generator_flags(summary)
    if flags:
        return "inconclusive", flags
    if rec is None:
        return "fail", ["writer did not drain within the timeout (no reconciliation)"]
    reasons = []
    rc = rec["reconciliation"]
    for key, label in (
        ("missing_spans", "missing"),
        ("duplicate_rows", "duplicate rows"),
        ("unacked_spans", "unacked"),
        ("unexpected_traces", "unexpected traces"),
    ):
        if rc[key]:
            reasons.append(f"{rc[key]} {label}")
    if rec["dlq_records"]:
        reasons.append(f"{rec['dlq_records']} DLQ records")
    if rec["trace_index_span_count"] != rc["stored_rows"]:
        reasons.append("trace_index inconsistent")
    p99 = rec.get("e2e_window_latency_ms", rec["e2e_latency_ms"])["p99"]
    if p99 >= e2e_limit_ms:
        reasons.append(f"e2e p99 {p99:.0f} ms >= {e2e_limit_ms:.0f} ms")
    return ("fail" if reasons else "pass"), reasons


def saturated(
    verdict: str, reasons: list[str], rate: float, window_throughput: float
) -> bool:
    """Whether the ramp should stop after this step: the pipeline (or generator) can no longer
    keep up — loss of any kind, no drain, acked throughput under 99% of offered, or a
    generator-bound step. A latency-only miss does NOT stop the ramp: latency is not monotonic
    in rate (the writer's time-based flush waits on the poll, so sparse traffic can be slower
    than dense traffic), and the bar is the HIGHEST rate that passes."""
    if verdict == "inconclusive" or window_throughput < 0.99 * rate:
        return True
    return any(not r.startswith("e2e p99") for r in reasons)


def sustained(steps: list[dict]) -> dict:
    """Highest passing rate of a ramp, the non-passing rates, and where the ramp saturated."""
    passing = [st for st in steps if st["verdict"] == "pass"]
    best = max(passing, key=lambda st: st["rate"], default=None)
    sat = next((st for st in steps if st.get("saturated")), None)
    return {
        "sustained_rate": best["rate"] if best else None,
        "sustained_window_throughput": best["window_throughput"] if best else None,
        "non_pass_rates": sorted(st["rate"] for st in steps if st["verdict"] != "pass"),
        "saturated_at": sat["rate"] if sat else None,
    }


def gb(n: float) -> str:
    return f"{n / 1024**3:.2f} GB"


def render_pilot(report: dict) -> str:
    hw, cal = report["hardware"], report["calibration"]
    lines = [
        "# Ingestion load test — stage 1 pilot",
        "",
        (
            f"_Generated {report['generated_at']} by `bench/ingest_load.py pilot` (commit `{hw['git']['sha'][:10]}`"
            f"{', dirty' if hw['git']['dirty'] else ''})._"
        ),
        "",
        "## Hardware and setup",
        "",
        (
            f"- **Host:** {hw['cpu']['cpus']}× {hw['cpu']['model']} ({hw['cpu']['architecture']}), "
            f"{hw['memory_gib']} GiB RAM, kernel {hw['kernel']}, Docker {hw['docker']}, {hw['go']}."
        ),
        f"- **Setup:** {hw['shared_machine']}",
        (
            "- **Containers:** "
            + "; ".join(
                f"`{c['name']}` cpuset {c['cpuset'] or 'all'}, mem {c['memory'] or 'unlimited'}"
                for c in hw["containers"]
            )
        ),
        (
            f"- **Templates:** {report['templates']['traces']} real traces / {report['templates']['spans']} spans "
            f"({', '.join(f'{a} {t}/{s}' for a, (t, s) in sorted(report['templates']['per_agent_traces_spans'].items()))}), "
            f"sha256 `{report['templates']['sha256'][:12]}`, fidelity {report['templates']['fidelity']['mismatches']} "
            f"mismatches ({report['templates']['fidelity']['legacy_rows_upgraded']} legacy rows upgraded)."
        ),
        "",
        "## Pilot runs (correctness + calibration, not throughput)",
        "",
        (
            "| protocol | offered spans/s | acked | stored | missing | dup rows | unacked | trace_index | DLQ "
            "| req p99 ms | e2e p50 / p99 ms | gen CPU p95 | gen lag p99 ms | clean | flags |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["runs"]:
        s, c = r["summary"], r["reconcile"]
        rc = c["reconciliation"]
        e2e = c["e2e_latency_ms"]
        lines.append(
            f"| {s['config']['protocol']} | {s['config']['rate_spans_per_sec']:.0f} "
            f"| {rc['acked_spans']} | {rc['stored_acked_unique_spans']} | {rc['missing_spans']} "
            f"| {rc['duplicate_rows']} | {rc['unacked_spans']} | {c['trace_index_span_count']} "
            f"| {c['dlq_records']} | {s['request_latency_ms']['p99']:.0f} "
            f"| {e2e['p50']:.0f} / {e2e['p99']:.0f} | {s['cpu_fraction']['p95']:.2f} "
            f"| {s['schedule_lag_ms']['p99']:.0f} | {'yes' if c['clean'] else '**NO**'} "
            f"| {'; '.join(r['flags']) or '—'} |"
        )
    lines += [
        "",
        "## Disk calibration",
        "",
        (
            f"- ClickHouse `vigil_load.spans`: **{cal['ch_bytes_per_span']:.0f} B/span** compressed "
            f"({cal['ch_rows']} rows, {gb(cal['ch_bytes'])}); real eval data measured 642 B/span."
        ),
        (
            f"- Redpanda `{LOAD_TOPICS[0]}`: **{cal['topic_bytes_per_span']:.0f} B/span** "
            f"({gb(cal['topic_bytes_delta'])} for {cal['spans_sent']} spans); retention cap {gb(cal['topic_cap'])}."
        ),
        (
            f"- Planning figure: **{cal['planning_ch_bytes_per_span']:.0f} B/span** in ClickHouse (the larger "
            "of this replay measurement and the real-data figure; pilot parts are small and mostly unmerged), "
            "with 2× merge headroom."
        ),
        "",
        "### Stage-2 projection (per protocol; tables truncated between steps)",
        "",
        f"Each step = {WARMUP_S} s warm-up + {STEADY_S} s steady state.",
        "",
        "| offered spans/s | spans per step | peak disk per step |",
        "|---|---|---|",
    ]
    for p in report["stage2_projection"]:
        lines.append(f"| {p['rate']} | {p['spans']:,} | {gb(p['peak_bytes'])} |")
    lines += [
        "",
        (
            f"Free disk at pilot time: {gb(report['free_bytes'])}; reserve {DISK_RESERVE_GB} GB. "
            f"Largest step fits: **{'yes' if report['largest_step_fits'] else 'NO'}**."
        ),
        "",
    ]
    return "\n".join(lines)


def render_baseline(report: dict) -> str:
    hw = report["hardware"]
    lines = [
        f"# Ingestion load test — stage 2 baseline (`{report['label']}`)",
        "",
        (
            f"_Generated {report['generated_at']} by `bench/ingest_load.py baseline --label {report['label']}` "
            f"(commit `{hw['git']['sha'][:10]}`{', dirty' if hw['git']['dirty'] else ''})._"
        ),
        "",
        f"**Limits:** {report['limits_note']}",
        "",
        (
            f"- **Host:** {hw['cpu']['cpus']}× {hw['cpu']['model']} ({hw['cpu']['architecture']}), "
            f"{hw['memory_gib']} GiB RAM, kernel {hw['kernel']}, Docker {hw['docker']}."
        ),
        f"- **Setup:** {hw['shared_machine']}",
        (
            "- **Containers:** "
            + "; ".join(
                f"`{c['name']}` cpuset {c['cpuset'] or 'all'}, mem {c['memory'] or 'unlimited'}"
                for c in hw["containers"]
            )
        ),
        f"- **Writer:** {', '.join(f'{k}={v}' for k, v in hw['writer_config'].items())}",
        (
            f"- **ClickHouse** max_server_memory_usage {hw.get('clickhouse_max_server_memory_usage', 0) / 1024**3:.1f} GiB; "
            f"**Redpanda** `{hw.get('redpanda_memory_flag') or 'n/a'}`. ClickHouse at start: uptime "
            f"{hw.get('clickhouse_uptime_s', 0) / 3600:.1f} h, tracked memory "
            f"{hw.get('clickhouse_memory_tracking_bytes', 0) / 1024**3:.2f} GiB."
        ),
        (
            f"- **Method:** open-loop offered load of real replayed traces; each step = {report['warmup_s']} s "
            f"warm-up + {report['steady_s']} s steady-state window; load tables truncated between steps. "
            f"**Sustained** = highest rate with zero loss (nothing missing / duplicated / unacked / DLQ'd) "
            f"and steady-state e2e p99 < {E2E_P99_LIMIT_MS / 1000:.0f} s. A step where the generator is "
            "CPU-bound or behind schedule is *inconclusive* (it measured the generator)."
        ),
        "",
        "## Sustained throughput",
        "",
        "| protocol | sustained offered spans/s | measured acked spans/s (window) | non-passing steps | ramp saturated at |",
        "|---|---|---|---|---|",
    ]
    for proto, res in report["sustained"].items():
        thr = res["sustained_window_throughput"]
        lines.append(
            f"| {proto} | {res['sustained_rate'] or 'none'} | {f'{thr:.0f}' if thr else '—'} "
            f"| {', '.join(map(str, res['non_pass_rates'])) or '—'} | {res['saturated_at'] or '— (not reached)'} |"
        )
    lines += [
        "",
        "## Steps",
        "",
        (
            "| protocol | offered | acked/s (window) | e2e p50 / p95 / p99 ms (window) | req p99 ms | missing "
            "| dup | unacked | DLQ | max lag | CPU cores (recv / writer / CH / RP, mean) | gen CPU p95 "
            "| gen lag p99 ms | verdict |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for st in report["steps"]:
        s, rec, res = st["summary"], st["reconcile"], st["resources"]
        cpu = " / ".join(f"{res[c]['cpu_mean']:.2f}" for c in PINNED_CONTAINERS)
        if rec:
            rc = rec["reconciliation"]
            w = rec.get("e2e_window_latency_ms", rec["e2e_latency_ms"])
            e2e = f"{w['p50']:.0f} / {w['p95']:.0f} / {w['p99']:.0f}"
            cols = f"{rc['missing_spans']} | {rc['duplicate_rows']} | {rc['unacked_spans']} | {rec['dlq_records']}"
        else:
            e2e, cols = "not drained", "— | — | — | —"
        verdict = st["verdict"] + (
            f": {'; '.join(st['reasons'])}" if st["reasons"] else ""
        )
        verdict += " (ramp saturated)" if st.get("saturated") else ""
        lines.append(
            f"| {st['protocol']} | {st['rate']} | {st['window_throughput']:.0f} | {e2e} "
            f"| {s['request_latency_ms']['p99']:.0f} | {cols} | {res['max_lag']} | {cpu} "
            f"| {s['cpu_fraction']['p95']:.2f} | {s['schedule_lag_ms']['p99']:.0f} | {verdict} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- I/O


def sh(
    cmd: list[str], env: dict | None = None, check: bool = True, capture: bool = True
) -> str:
    p = subprocess.run(cmd, env=env, check=False, text=True, capture_output=capture)
    if check and p.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd[:4])}... failed ({p.returncode}): {(p.stderr or p.stdout or '').strip()[-2000:]}"
        )
    return (p.stdout or "").strip()


def base_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(parse_env((ROOT / ".env").read_text()))
    env["PATH"] = f"{Path.home()}/.local/go/bin:{env.get('PATH', '')}"
    return env


def ch_query(env: dict, sql: str) -> str:
    return sh(
        [
            "docker",
            "exec",
            "-i",
            "vigil-clickhouse",
            "clickhouse-client",
            "-u",
            env["CLICKHOUSE_USER"],
            "--password",
            env["CLICKHOUSE_PASSWORD"],
            "-q",
            sql,
        ]
    )


def container_info(name: str) -> dict:
    out = sh(
        [
            "docker",
            "inspect",
            name,
            "--format",
            "{{.HostConfig.CpusetCpus}}|{{.HostConfig.Memory}}|{{.State.Status}}",
        ],
        check=False,
    )
    if not out:
        return {"name": name, "cpuset": None, "memory": None, "status": "absent"}
    cpuset, mem, status = out.split("|")
    return {
        "name": name,
        "cpuset": cpuset,
        "memory": f"{int(mem) // 1024**2} MiB" if int(mem) else None,
        "status": status,
    }


def hardware() -> dict:
    git_sha = sh(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    dirty = bool(
        sh(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"])
    )
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cpu": parse_lscpu(sh(["lscpu"])),
        "memory_gib": parse_meminfo(Path("/proc/meminfo").read_text()),
        "kernel": platform.release(),
        "os": sh(["lsb_release", "-ds"], check=False),
        "docker": sh(["docker", "version", "--format", "{{.Server.Version}}"]),
        "go": sh([f"{Path.home()}/.local/go/bin/go", "version"]),
        "git": {"sha": git_sha, "dirty": dirty},
        "shared_machine": SHARED_MACHINE_NOTE,
        "generator_core": GENERATOR_CORE,
        "containers": [container_info(c) for c in PINNED_CONTAINERS],
        # ClickHouse state at capture: memory use creeps with uptime under a tight cap (failing
        # background merges of wide system tables), so results record both.
        "clickhouse_uptime_s": int(ch_query(base_env(), "SELECT uptime()") or 0),
        "clickhouse_memory_tracking_bytes": int(
            ch_query(
                base_env(),
                "SELECT value FROM system.metrics WHERE metric = 'MemoryTracking'",
            )
            or 0
        ),
        "clickhouse_max_server_memory_usage": int(
            ch_query(
                base_env(),
                "SELECT value FROM system.server_settings WHERE name = 'max_server_memory_usage'",
            )
            or 0
        ),
        "redpanda_memory_flag": next(
            (
                a
                for a in json.loads(
                    sh(
                        [
                            "docker",
                            "inspect",
                            "vigil-redpanda",
                            "--format",
                            "{{json .Args}}",
                        ]
                    )
                )
                if a.startswith("--memory=")
            ),
            None,
        ),
        "writer_config": {
            k: base_env().get(k)
            for k in (
                "VIGIL_BATCH_MAX_ROWS",
                "VIGIL_BATCH_MAX_BYTES",
                "VIGIL_FLUSH_INTERVAL",
            )
        },
    }


def build_loadgen(env: dict) -> None:
    subprocess.run(
        ["go", "build", "-o", str(LOADGEN), "./cmd/loadgen"],
        cwd=INGEST,
        env=env,
        check=True,
    )


def pin_pipeline() -> None:
    for c in ("vigil-clickhouse", "vigil-redpanda"):
        sh(["docker", "update", "--cpuset-cpus", PIPELINE_CPUSET, c])


def unpin_pipeline() -> None:
    ncpu = os.cpu_count() or 1
    for c in ("vigil-clickhouse", "vigil-redpanda"):
        sh(["docker", "update", "--cpuset-cpus", f"0-{ncpu - 1}", c], check=False)


def require_load_pipeline() -> None:
    for c in ("vigil-load-receiver", "vigil-load-writer"):
        if container_info(c)["status"] != "running":
            raise RuntimeError(
                f"{c} is not running — run `python bench/ingest_load.py up` first"
            )


def topic_bytes(topic: str = LOAD_TOPICS[0]) -> int:
    out = sh(
        [
            "docker",
            "exec",
            "vigil-redpanda",
            "rpk",
            "cluster",
            "logdirs",
            "describe",
            "--topics",
            assert_load_topic(topic),
        ]
    )
    return parse_logdirs(out, topic)


def free_bytes() -> int:
    return shutil.disk_usage(ROOT).free


def run_loadgen(
    env: dict, run_dir: Path, protocol: str, run_id: str, rate: float, duration: str
) -> dict:
    """Run the generator pinned to its core (GOMAXPROCS follows the affinity mask, and is
    recorded in the summary)."""
    subprocess.run(
        [
            "taskset", "-c", GENERATOR_CORE, str(LOADGEN), "run",
            "--templates", str(TEMPLATES), "--protocol", protocol, "--run-id", run_id,
            "--rate", str(rate), "--duration", duration, "--out-dir", str(run_dir),
        ],
        cwd=INGEST, env=loadgen_env(env), check=True,
    )  # fmt: skip
    return json.loads((run_dir / "summary.json").read_text())


def reconcile(
    env: dict,
    run_dir: Path,
    window: tuple[int, int] | None = None,
    drain_timeout: str = "5m",
) -> dict | None:
    """Reconcile a run (optionally with a steady-state window). Returns reconcile.json, or
    None if the writer did not drain in time. A non-clean reconciliation still returns its
    report (loadgen exits non-zero, but has written it)."""
    cmd = [
        str(LOADGEN),
        "reconcile",
        "--run-dir",
        str(run_dir),
        "--drain-timeout",
        drain_timeout,
    ]
    if window:
        cmd += ["--window-start-ns", str(window[0]), "--window-end-ns", str(window[1])]
    (run_dir / "reconcile.json").unlink(missing_ok=True)
    subprocess.run(cmd, cwd=INGEST, env=loadgen_env(env), check=False)
    path = run_dir / "reconcile.json"
    return json.loads(path.read_text()) if path.exists() else None


def read_requests(run_dir: Path) -> list[dict]:
    with gzip.open(run_dir / "requests.csv.gz", "rt") as f:
        return list(csv.DictReader(f))


def active_eval_runs(env: dict) -> list[str]:
    out = sh(
        [
            "docker", "exec", "-e", f"PGPASSWORD={env['POSTGRES_PASSWORD']}", "vigil-postgres",
            "psql", "-U", env["POSTGRES_USER"], "-d", env["POSTGRES_DB"], "-Atc",
            "SELECT id FROM eval_runs WHERE status IN ('pending', 'running')",
        ]
    )  # fmt: skip
    return [line for line in out.splitlines() if line]


def engine_run_processes() -> list[str]:
    out = sh(["pgrep", "-af", r"engine(\.__main__)? run"], check=False)
    return [line for line in out.splitlines() if line and "pgrep" not in line]


class ResourceSampler:
    """Samples pipeline container CPU/memory (docker stats) and the load writer's consumer
    lag (rpk) every `interval` seconds in a background thread."""

    def __init__(self, interval: float = 5.0):
        self.interval = interval
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()

    def _loop(self) -> None:
        while not self._stop.is_set():
            t = time.time()
            stats = sh(
                ["docker", "stats", "--no-stream", "--format",
                 "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}", *PINNED_CONTAINERS],
                check=False,
            )  # fmt: skip
            lag = parse_group_lag(
                sh(
                    [
                        "docker",
                        "exec",
                        "vigil-redpanda",
                        "rpk",
                        "group",
                        "describe",
                        LOAD_GROUP,
                    ],
                    check=False,
                )
            )
            sample = {"t": t, "lag": lag, "containers": {}}
            for line in stats.splitlines():
                parsed = parse_docker_stats(line)
                if parsed:
                    sample["containers"][parsed[0]] = {
                        "cpu_cores": parsed[1],
                        "mem_bytes": parsed[2],
                    }
            self.samples.append(sample)
            self._stop.wait(max(0.0, self.interval - (time.time() - t)))

    def summary(self, start: float, end: float) -> dict:
        """Mean/max CPU (cores) and max memory per container, and max lag, within [start, end)."""
        win = [x for x in self.samples if start <= x["t"] < end]
        out: dict = {
            "samples": len(win),
            "max_lag": max((x["lag"] or 0 for x in win), default=0),
        }
        for c in PINNED_CONTAINERS:
            cpu = [x["containers"][c]["cpu_cores"] for x in win if c in x["containers"]]
            mem = [x["containers"][c]["mem_bytes"] for x in win if c in x["containers"]]
            out[c] = {
                "cpu_mean": sum(cpu) / len(cpu) if cpu else 0.0,
                "cpu_max": max(cpu, default=0.0),
                "mem_max_bytes": max(mem, default=0.0),
            }
        return out


def truncate_load_tables(env: dict) -> None:
    db = assert_load_db(LOAD_DB)
    for t in LOAD_TABLES:
        ch_query(env, f"TRUNCATE TABLE IF EXISTS {db}.{t}")


# ---------------------------------------------------------------- commands


def cmd_up(_args) -> int:
    subprocess.run(
        COMPOSE + ["up", "-d", "--build", "load-receiver", "load-writer"], check=True
    )
    pin_pipeline()
    print(json.dumps([container_info(c) for c in PINNED_CONTAINERS], indent=2))
    return 0


def cmd_export(_args) -> int:
    env = base_env()
    build_loadgen(env)
    env.pop("PYTHONPATH", None)
    subprocess.run(
        [str(LOADGEN), "export", "--out", str(TEMPLATES)],
        cwd=INGEST,
        env=env,
        check=True,
    )
    return 0


def cmd_hardware(_args) -> int:
    print(json.dumps(hardware(), indent=2))
    return 0


def cmd_pilot(args) -> int:
    env = base_env()
    require_load_pipeline()
    pin_pipeline()
    build_loadgen(env)
    manifest = json.loads(Path(str(TEMPLATES) + ".json").read_text())
    spans_planned = (
        int(args.rate * args.seconds * 2) + 2 * 512
    )  # two protocols, plus batch rounding
    # Conservative before calibration: 1 KB/span each in ClickHouse and the topic.
    check_disk(project_disk(spans_planned, 1024, 1024, 2 * 1024**3), free_bytes())

    truncate_load_tables(env)
    out = RESULTS / "pilot"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    hw = hardware()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    topic_before = topic_bytes()
    runs = []
    for protocol in ("grpc", "http"):
        run_dir = out / protocol
        summary = run_loadgen(
            env,
            run_dir,
            protocol,
            f"pilot-{protocol}-{stamp}",
            args.rate,
            f"{args.seconds}s",
        )
        rec = reconcile(env, run_dir)
        if rec is None:
            raise RuntimeError(f"pilot {protocol}: writer did not drain")
        runs.append(
            {"summary": summary, "reconcile": rec, "flags": generator_flags(summary)}
        )

    ch_rows = int(ch_query(env, f"SELECT count() FROM {LOAD_DB}.spans"))
    ch_bytes = int(
        ch_query(
            env,
            f"SELECT sum(data_compressed_bytes) FROM system.parts "
            f"WHERE active AND database = '{LOAD_DB}' AND table = 'spans'",
        )
    )
    spans_sent = sum(r["summary"]["spans_sent"] for r in runs)
    topic_delta = topic_bytes() - topic_before
    topic_cap = int(env.get("VIGIL_LOAD_RETENTION_BYTES", 2 * 1024**3))
    cal = {
        "ch_rows": ch_rows,
        "ch_bytes": ch_bytes,
        "ch_bytes_per_span": ch_bytes / max(ch_rows, 1),
        "spans_sent": spans_sent,
        "topic_bytes_delta": topic_delta,
        "topic_bytes_per_span": topic_delta / max(spans_sent, 1),
        "topic_cap": topic_cap,
    }
    cal["planning_ch_bytes_per_span"] = max(cal["ch_bytes_per_span"], 642.0)
    proj = stage2_projection(
        cal["planning_ch_bytes_per_span"], cal["topic_bytes_per_span"], topic_cap
    )
    free = free_bytes()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hardware": hw,
        "templates": manifest,
        "runs": runs,
        "calibration": cal,
        "stage2_projection": proj,
        "free_bytes": free,
    }
    try:
        check_disk(max(p["peak_bytes"] for p in proj), free)
        report["largest_step_fits"] = True
    except RuntimeError:
        report["largest_step_fits"] = False
    (out / "pilot.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "pilot.md").write_text(render_pilot(report))
    print((out / "pilot.md").read_text())
    return 0 if all(r["reconcile"]["clean"] for r in runs) else 1


def cmd_baseline(args) -> int:
    env = base_env()
    require_load_pipeline()
    pin_pipeline()
    build_loadgen(env)
    cal = json.loads((RESULTS / "pilot" / "pilot.json").read_text())["calibration"]
    allow = set(args.allow_stale_run or [])
    out = RESULTS / f"baseline-{args.label}"
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "label": args.label,
        "limits_note": args.limits_note,
        "warmup_s": args.warmup,
        "steady_s": args.steady,
        "hardware": hardware(),
        "steps": [],
        "sustained": {},
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for protocol in args.protocols.split(","):
        for rate in (int(r) for r in args.rates.split(",")):
            eval_guard(active_eval_runs(env), engine_run_processes(), allow)
            spans = rate * (args.warmup + args.steady)
            check_disk(
                project_disk(
                    spans,
                    cal["planning_ch_bytes_per_span"],
                    cal["topic_bytes_per_span"],
                    cal["topic_cap"],
                ),
                free_bytes(),
            )
            truncate_load_tables(env)
            run_dir = out / protocol / str(rate)
            if run_dir.exists():
                shutil.rmtree(run_dir)
            run_id = f"{args.label}-{protocol}-{rate}-{stamp}"
            print(
                f"==> {args.label} {protocol} {rate} spans/s ({spans:,} spans)",
                flush=True,
            )
            with ResourceSampler() as sampler:
                summary = run_loadgen(
                    env,
                    run_dir,
                    protocol,
                    run_id,
                    rate,
                    f"{args.warmup + args.steady}s",
                )
                start = summary["start_ns"] + args.warmup * 10**9
                end = summary["start_ns"] + (args.warmup + args.steady) * 10**9
                rec = reconcile(
                    env, run_dir, (start, end), drain_timeout=args.drain_timeout
                )
            resources = sampler.summary(start / 1e9, end / 1e9)
            (run_dir / "resources.json").write_text(
                json.dumps(sampler.samples, indent=1) + "\n"
            )
            verdict, reasons = step_verdict(summary, rec)
            throughput = window_throughput(read_requests(run_dir), start, end)
            step = {
                "protocol": protocol,
                "rate": rate,
                "run_id": run_id,
                "verdict": verdict,
                "reasons": reasons,
                "window_throughput": throughput,
                "saturated": saturated(verdict, reasons, rate, throughput),
                "summary": summary,
                "reconcile": rec,
                "resources": resources,
            }
            report["steps"].append(step)
            print(
                f"    {verdict}{': ' + '; '.join(reasons) if reasons else ''}",
                flush=True,
            )
            (out / "baseline.json").write_text(json.dumps(report, indent=2) + "\n")
            if step["saturated"]:
                print(
                    "    pipeline saturated: stopping this protocol's ramp", flush=True
                )
                break
        report["sustained"][protocol] = sustained(
            [st for st in report["steps"] if st["protocol"] == protocol]
        )
    truncate_load_tables(env)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    (out / "baseline.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "baseline.md").write_text(render_baseline(report))
    print((out / "baseline.md").read_text())
    return 0


def cmd_limits(args) -> int:
    """Recreate ClickHouse, Redpanda and the load receiver/writer with the as-is or sized
    memory limits. ClickHouse/Redpanda are shared with the real stack (data volumes persist;
    the real receiver/writer reconnect), so this refuses while a real eval run is active."""
    env = base_env()
    eval_guard(
        active_eval_runs(env), engine_run_processes(), set(args.allow_stale_run or [])
    )
    files = (
        COMPOSE[:6]
        + (["-f", str(SIZED_OVERRIDE)] if args.which == "sized" else [])
        + COMPOSE[6:]
    )
    subprocess.run(
        files
        + [
            "up",
            "-d",
            "--wait",
            "clickhouse",
            "redpanda",
            "load-receiver",
            "load-writer",
        ],
        check=True,
    )
    pin_pipeline()
    hw = hardware()
    print(json.dumps({k: hw[k] for k in ("containers", "clickhouse_max_server_memory_usage",
                                         "redpanda_memory_flag")}, indent=2))  # fmt: skip
    return 0


def cmd_cleanup(args) -> int:
    env = base_env()
    if not args.yes:
        print(
            f"Would remove containers {LOAD_CONTAINERS}, DROP DATABASE {LOAD_DB}, delete topics {LOAD_TOPICS} "
            f"and group {LOAD_GROUP}, and unpin ClickHouse/Redpanda. Re-run with --yes."
        )
        return 1
    subprocess.run(
        COMPOSE + ["rm", "-sf", *[c.removeprefix("vigil-") for c in LOAD_CONTAINERS]],
        check=False,
    )
    ch_query(env, f"DROP DATABASE IF EXISTS {assert_load_db(LOAD_DB)}")
    for t in LOAD_TOPICS:
        sh(
            [
                "docker",
                "exec",
                "vigil-redpanda",
                "rpk",
                "topic",
                "delete",
                assert_load_topic(t),
            ],
            check=False,
        )
    sh(
        ["docker", "exec", "vigil-redpanda", "rpk", "group", "delete", LOAD_GROUP],
        check=False,
    )
    unpin_pipeline()
    print("load-test data removed; ClickHouse/Redpanda unpinned")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("ingest_load")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up").set_defaults(func=cmd_up)
    sub.add_parser("export").set_defaults(func=cmd_export)
    sub.add_parser("hardware").set_defaults(func=cmd_hardware)
    p = sub.add_parser("pilot")
    p.add_argument("--rate", type=float, default=1000)
    p.add_argument("--seconds", type=int, default=20)
    p.set_defaults(func=cmd_pilot)
    b = sub.add_parser("baseline")
    b.add_argument(
        "--label",
        required=True,
        help="e.g. asis or sized (results go to baseline-<label>/)",
    )
    b.add_argument(
        "--limits-note",
        required=True,
        help="one line describing the container limits in force",
    )
    b.add_argument("--protocols", default="grpc,http")
    b.add_argument("--rates", default=",".join(str(r) for r in RAMP_RATES))
    b.add_argument("--warmup", type=int, default=WARMUP_S)
    b.add_argument("--steady", type=int, default=STEADY_S)
    b.add_argument("--drain-timeout", default="15m")
    b.add_argument(
        "--allow-stale-run",
        action="append",
        help="eval_runs id known to be stale (repeatable)",
    )
    b.set_defaults(func=cmd_baseline)
    lim = sub.add_parser("limits")
    lim.add_argument("which", choices=("asis", "sized"))
    lim.add_argument("--allow-stale-run", action="append")
    lim.set_defaults(func=cmd_limits)
    c = sub.add_parser("cleanup")
    c.add_argument("--yes", action="store_true")
    c.set_defaults(func=cmd_cleanup)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
