"""Orchestrate ingestion load tests: the isolated load pipeline, the Go load generator, and the
reproducible results under bench/results/ingest/.

The generator (ingest/cmd/loadgen) replays real recorded traces; this script manages the load
pipeline (compose profile "loadtest": database vigil_load, topics otlp.spans.load /
spans.dlq.load), CPU pinning, disk checks, hardware capture, and reporting.

    python bench/ingest_load.py up          # start the load pipeline, pin CPUs
    python bench/ingest_load.py export      # templates from real traces (read-only user)
    python bench/ingest_load.py pilot       # small gRPC + HTTP runs: correctness + disk calibration
    python bench/ingest_load.py hardware    # print the hardware/setup record
    python bench/ingest_load.py cleanup     # remove ALL load data and unpin CPUs

Shared-machine setup (recorded with every result): the generator and the pipeline share one
host. The generator is pinned to core 0 (taskset); the pipeline containers (load receiver/
writer, ClickHouse, Redpanda) to cores 1-3 (cpuset). Host processes (dockerd, tailscaled, ...)
are unpinned.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

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
) -> tuple[dict, dict]:
    lenv = loadgen_env(env)
    # Pin the generator to its core; GOMAXPROCS follows the affinity mask (recorded in the summary).
    subprocess.run(
        [
            "taskset",
            "-c",
            GENERATOR_CORE,
            str(LOADGEN),
            "run",
            "--templates",
            str(TEMPLATES),
            "--protocol",
            protocol,
            "--run-id",
            run_id,
            "--rate",
            str(rate),
            "--duration",
            duration,
            "--out-dir",
            str(run_dir),
        ],
        cwd=INGEST,
        env=lenv,
        check=True,
    )
    subprocess.run(
        [str(LOADGEN), "reconcile", "--run-dir", str(run_dir)],
        cwd=INGEST,
        env=lenv,
        check=True,
    )
    return (
        json.loads((run_dir / "summary.json").read_text()),
        json.loads((run_dir / "reconcile.json").read_text()),
    )


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
        summary, rec = run_loadgen(
            env,
            run_dir,
            protocol,
            f"pilot-{protocol}-{stamp}",
            args.rate,
            f"{args.seconds}s",
        )
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
    c = sub.add_parser("cleanup")
    c.add_argument("--yes", action="store_true")
    c.set_defaults(func=cmd_cleanup)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
