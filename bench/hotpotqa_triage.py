"""Controlled HotpotQA regression-triage validation: the real agent, one changed parameter.

Runs the current HotpotQA agent twice in ONE eval run through the real engine path
(orchestrator → one worker subprocess per version → instrumented Anthropic client → OTLP →
ClickHouse; results → Postgres), changing only the documents returned per search:

    baseline   hotpotqa_agent.validation.top_k5   (the current default, top_k = 5)
    candidate  hotpotqa_agent.validation.top_k1   (top_k = 1)

then asks the shared regression gate for a verdict (``engine regress --save`` evidence bundle),
re-checks the bundle offline (``bench/regression_verdict.py reproduce``), and cross-checks the
run against what was planned: version hashes and manifests, unit counts, non-zero tokens and
cost, the retrieval k actually used (Postgres transcripts and ClickHouse spans), trace
coverage, and — when the read-only API is running — that ``GET /compare`` gives the CLI's
verdict. The verdict is whatever the gate computes (regression / no_regression / incomplete);
nothing here expects or forces one. See docs/HOTPOTQA_VALIDATION.md.

    python bench/hotpotqa_triage.py plan --stage pilot            # offline: no network, no DB
    python bench/hotpotqa_triage.py run --stage smoke --confirm-spend [--api-url URL]
    python bench/hotpotqa_triage.py verify bench/results/hotpotqa_triage/<dir> [--api-url URL]

Stages (predeclared; see STAGES): ``smoke`` (2 dev20 questions × 1 trial), ``pilot`` (dev20 ×
1), ``full`` (base100 × 3, measurement mode). Concurrency is fixed at 1 and the LLM cache is
off. ``--budget`` is the engine's per-run budget: a SOFT limit checked between units (in-flight
work finishes), not a billing cap. ``plan`` never touches the network, the database or a model
provider and never reads credential values (only whether ANTHROPIC_API_KEY is set). ``run``
refuses below 1 GiB free disk, with the pinned corpus missing or different, with a model
the shared price table cannot price (the engine would meter it at $0, so the soft budget could
not stop it), with ANTHROPIC_API_KEY unset, or on a dirty source tree, and writes everything
into a NEW timestamped directory (never overwritten).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# bench is tooling and may import the engine (only *agents* may not).
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import span_accounting  # noqa: E402 — bench/ is on sys.path only from here

DEFAULT_DATA_DIR = ROOT / "data" / "hotpotqa"
DEFAULT_RESULTS_ROOT = ROOT / "bench" / "results" / "hotpotqa_triage"
MIN_FREE_BYTES = 1 << 30  # 1 GiB, stricter than the engine's 1 GB default floor
# The dated snapshot, not the moving `claude-haiku-4-5` alias, so a re-run calls the same model.
# The committed base100 baseline / A/A runs used the alias; the default agent still does.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
CONCURRENCY = 1
CACHE_MODE = "off"
VARIANT_PARAM = "retrieval_top_k"

VARIANTS = {
    "baseline": {"module": "hotpotqa_agent.validation.top_k5", "top_k": 5},
    "candidate": {"module": "hotpotqa_agent.validation.top_k1", "top_k": 1},
}

# The pinned artifacts of bench/build_hotpotqa_corpus.py --n 500 (HF revision 14f0ace3…, see
# docs/plans/status.md). The builder is deterministic, so a rebuild reproduces these bytes;
# cases.v1.json itself is not pinned because its `verification` note depends on whether the
# original CMU host was reachable at build time — its source, count and case ids are.
PINS = {
    "source_revision": "14f0ace3c3fac7bd86149c616b5b05d8282e5c6a",
    "source_sha256": "c20b638ca82b21d04fe12e14ff417ad05153d4d215a65de54497fca4e972f7c6",
    "cases_version": "v1",
    "cases_count": 500,
    "corpus_docs": 4913,
    "corpus_sha256": "8672c9c62d41af09137947c674b984c29c0dba0d8d7cc951c08fa1a7df3946fd",
    "subsets": {
        "dev20": {
            "n": 20, "sha256": "0ac3fd01d971e7a7a3c94eda129b00ac30028eb52f73f9f933beb804be9df2a3",
        },
        "base100": {
            "n": 100, "sha256": "815ef9d9d05efb519de6fd941d5028917efe537a10b7fbc0f6388c3106cfc7fe",
        },
    },
}

# Predeclared stages. `take` limits the subset to its first N ids (file order). `max_budget` is
# a guard against a typo in --budget, not a billing cap.
STAGES: dict[str, dict[str, Any]] = {
    "smoke": {
        "subset": "dev20", "take": 2, "trials": 1, "mode": "development",
        "budget_usd": 0.10, "max_budget_usd": 0.25,
        "purpose": "plumbing check of the full path; no statistical meaning",
    },
    "pilot": {
        "subset": "dev20", "take": None, "trials": 1, "mode": "development",
        "budget_usd": 0.60, "max_budget_usd": 1.50,
        "purpose": "cost sizing and consistency checks on 20 dev questions; low power, "
        "uncalibrated — a verdict here is not a statistical claim about top_k",
    },
    "full": {
        "subset": "base100", "take": None, "trials": 3, "mode": "measurement",
        "budget_usd": 6.00, "max_budget_usd": 10.00,
        "purpose": "the predeclared measurement: base100 × 3 trials, the size the detector's "
        "false-positive rate and power were studied at (bench/results/regression/)",
    },
}

# Rough cost prior from the committed base100 × 3 baseline (run 785e9e33, top_k=5, Haiku 4.5):
# $1.99 for 300 units. Used only to print an estimate; top_k=1 is expected to cost less per
# unit (fewer retrieved tokens) but that is not assumed in the estimate.
PRIOR_COST = {"run": "785e9e33-0992-4d02-9a3c-ea2b4f0b108f", "usd": 1.99, "units": 300}

_MANIFEST_SNIPPET = """
import importlib, json, sys
from engine.runner.contract import manifest_of
out = {}
for label, path in json.loads(sys.argv[1]).items():
    m = manifest_of(importlib.import_module(path))
    out[label] = {"module": path, "agent_id": m.agent_id, "agent_version": m.agent_version,
                  "model": m.model, "params": m.params, "prompts": m.prompts, "tools": m.tools}
print(json.dumps(out))
"""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


def _failed(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.blocking and not c.ok]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_ids(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def _now() -> datetime:
    return datetime.now(UTC)


# --- preflight (offline) -------------------------------------------------------------


def check_disk(paths: list[Path], free_bytes: Callable[[Path], int] | None = None) -> Check:
    """Free space on every filesystem the run writes to must be >= 1 GiB."""
    free_bytes = free_bytes or (lambda p: shutil.disk_usage(p).free)
    lows = []
    for p in paths:
        existing = p
        while not existing.exists():
            existing = existing.parent
        free = free_bytes(existing)
        if free < MIN_FREE_BYTES:
            lows.append(f"{existing}: {free / (1 << 30):.2f} GiB")
    if lows:
        return Check("disk", False, "below the 1 GiB floor — " + "; ".join(lows)
                     + " (free space; Vigil never deletes on its own)")
    return Check("disk", True, ">= 1 GiB free on " + ", ".join(sorted({str(p) for p in paths})))


def inspect_data(data_dir: Path, stage: str) -> tuple[list[Check], dict, list[str]]:
    """Check the pinned corpus/cases/subsets and return (checks, provenance, stage case ids).
    Reads gold titles only to confirm they are in the corpus; never returns or prints answers."""
    checks: list[Check] = []
    prov: dict[str, Any] = {"data_dir": str(data_dir)}
    cases_path = data_dir / f"cases.{PINS['cases_version']}.json"
    corpus_path = data_dir / f"corpus.{PINS['cases_version']}.jsonl"
    missing = [str(p) for p in (cases_path, corpus_path) if not p.is_file()]
    spec = STAGES[stage]
    subset_path = data_dir / "subsets" / f"{spec['subset']}.txt"
    if not subset_path.is_file():
        missing.append(str(subset_path))
    if missing:
        checks.append(Check(
            "corpus", False,
            "missing " + ", ".join(missing)
            + " — build with: python bench/build_hotpotqa_corpus.py --n 500",
        ))
        return checks, prov, []

    doc = json.loads(cases_path.read_text())
    source = doc.get("source") or {}
    prov["cases"] = {
        "path": str(cases_path), "sha256": sha256_file(cases_path), "version": doc.get("version"),
        "count": doc.get("count"), "source_revision": source.get("revision"),
        "source_sha256": source.get("sha256"), "verification": doc.get("verification"),
    }
    expect = {
        "version": PINS["cases_version"], "count": PINS["cases_count"],
        "source_revision": PINS["source_revision"], "source_sha256": PINS["source_sha256"],
    }
    bad = [f"{k}={prov['cases'][k]!r} (pinned {v!r})" for k, v in expect.items()
           if prov["cases"][k] != v]
    checks.append(Check("cases_pinned", not bad, "; ".join(bad) or
                        f"v1, 500 cases, HF revision {PINS['source_revision'][:12]}"))

    corpus_ids = set()
    n_docs = 0
    with corpus_path.open() as f:
        for line in f:
            if line.strip():
                corpus_ids.add(json.loads(line)["doc_id"])
                n_docs += 1
    corpus_sha = sha256_file(corpus_path)
    prov["corpus"] = {"path": str(corpus_path), "sha256": corpus_sha, "docs": n_docs}
    ok = corpus_sha == PINS["corpus_sha256"] and n_docs == PINS["corpus_docs"]
    checks.append(Check(
        "corpus_pinned", ok,
        f"{n_docs} docs, sha256 {corpus_sha[:12]}"
        + ("" if ok else f" — pinned {PINS['corpus_docs']} docs, sha256 "
           f"{PINS['corpus_sha256'][:12]}; rebuild with bench/build_hotpotqa_corpus.py --n 500"),
    ))

    by_id = {c["case_id"]: c for c in doc.get("cases", [])}
    subsets = {}
    for name, pin in PINS["subsets"].items():
        p = data_dir / "subsets" / f"{name}.txt"
        if not p.is_file():
            continue
        ids = read_ids(p)
        subsets[name] = ids
        sha = sha256_file(p)
        prov.setdefault("subsets", {})[name] = {"path": str(p), "sha256": sha, "n": len(ids)}
        ok = sha == pin["sha256"] and len(ids) == pin["n"] and len(set(ids)) == len(ids)
        pinned = f" — pinned n={pin['n']} sha256 {pin['sha256'][:12]}"
        checks.append(Check(f"subset_{name}", ok,
                            f"{len(ids)} ids, sha256 {sha[:12]}" + ("" if ok else pinned)))
    if "dev20" in subsets and "base100" in subsets:
        overlap = set(subsets["dev20"]) & set(subsets["base100"])
        checks.append(Check("subsets_disjoint", not overlap, f"dev20 ∩ base100 = {len(overlap)}"))

    ids = subsets.get(spec["subset"], [])
    if spec["take"] is not None:
        ids = ids[: spec["take"]]
    unknown = [i for i in ids if i not in by_id]
    no_gold = [i for i in ids if i in by_id
               and not set(by_id[i].get("supporting_titles") or []) <= corpus_ids]
    checks.append(Check(
        "stage_cases", bool(ids) and not unknown and not no_gold,
        f"{len(ids)} case(s); {len(unknown)} not in cases.v1.json; "
        f"{len(no_gold)} with a gold title missing from the corpus",
    ))
    mix: dict[str, int] = {}
    for i in ids:
        if i in by_id:
            t = by_id[i].get("type", "bridge")
            mix[t] = mix.get(t, 0) + 1
    prov["stage_cases"] = {
        "subset": spec["subset"], "take": spec["take"], "n": len(ids), "type_mix": mix,
        "ids_sha256": sha256_bytes(("\n".join(ids) + "\n").encode()),
    }
    return checks, prov, ids


def compute_manifests(model: str, python: str = sys.executable) -> dict[str, dict]:
    """The two variants' manifests and version hashes exactly as the orchestrator computes
    them (``engine.runner.contract.manifest_of``), in a fresh interpreter with the run's
    model pinned and no API key in its environment. Offline: imports code only."""
    env = _child_env(model=model, corpus=None)
    env.pop("ANTHROPIC_API_KEY", None)
    spec = {label: v["module"] for label, v in VARIANTS.items()}
    proc = subprocess.run(
        [python, "-c", _MANIFEST_SNIPPET, json.dumps(spec)],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"could not compute variant manifests:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def check_manifests(manifests: dict[str, dict], model: str) -> list[Check]:
    """The variants must differ in exactly one controlled way: top_k, recorded as
    params.retrieval_top_k and in the search tool's description; same model/prompts/agent."""
    b, c = manifests["baseline"], manifests["candidate"]
    kb, kc = VARIANTS["baseline"]["top_k"], VARIANTS["candidate"]["top_k"]
    checks = [
        Check("versions_distinct", b["agent_version"] != c["agent_version"],
              f"baseline {b['agent_version'][:12]}, candidate {c['agent_version'][:12]}"),
        Check("same_agent_id", b["agent_id"] == c["agent_id"],
              f"{b['agent_id']} / {c['agent_id']}"),
        Check("model_pinned", b["model"] == c["model"] == model,
              f"{b['model']} / {c['model']} (requested {model})"),
        Check("prompts_identical", b["prompts"] == c["prompts"], "system prompt"),
    ]
    pb, pc = dict(b["params"]), dict(c["params"])
    ok = pb.pop(VARIANT_PARAM, None) == kb and pc.pop(VARIANT_PARAM, None) == kc and pb == pc
    checks.append(Check("params_differ_only_in_top_k", ok,
                        f"{VARIANT_PARAM}: {b['params'].get(VARIANT_PARAM)} → "
                        f"{c['params'].get(VARIANT_PARAM)}; other params equal: {pb == pc}"))
    tb, tc = b["tools"], c["tools"]
    ok = (
        len(tb) == len(tc) == 1
        and {k: v for k, v in tb[0].items() if k != "description"}
        == {k: v for k, v in tc[0].items() if k != "description"}
        and f"top {kb} " in tb[0]["description"] and f"top {kc} " in tc[0]["description"]
        and tb[0]["description"].replace(f"top {kb} ", f"top {kc} ") == tc[0]["description"]
    )
    checks.append(Check("tools_differ_only_in_top_k", ok,
                        "search tool: same name/schema, description states its top_k"))
    return checks


def prices_path(env: dict[str, str]) -> Path:
    """The price table the engine's workers will use: ``VIGIL_PRICES_PATH`` from the run's
    environment, else the shared ``ingest/internal/pricing/prices.json``."""
    override = env.get("VIGIL_PRICES_PATH")
    return Path(override) if override else ROOT / "ingest" / "internal" / "pricing" / "prices.json"


def check_pricing(model: str, manifests: dict[str, dict], path: Path) -> tuple[Check, dict]:
    """The engine's CostMeter prices an unknown model at $0, so the soft budget would never
    stop such a run. Every model the run will call must be in the price table with positive
    input and output rates. Reads only the price file."""
    from engine.runner.cost import CostMeter

    prov: dict[str, Any] = {"path": str(path)}
    try:
        meter = CostMeter(path)
        prov["sha256"] = sha256_file(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return Check("model_priced", False, f"price table {path} unreadable: {exc}"), prov
    prov["prices_as_of"] = meter.prices_as_of
    models = sorted({model, *(m.get("model") for m in manifests.values())} - {None})
    rates, bad = {}, []
    for m in models:
        if not meter.has_model(m):
            bad.append(f"{m!r} not in the price table")
            continue
        rate = {"input_usd_per_mtok": meter.cost(m, 1_000_000, 0),
                "output_usd_per_mtok": meter.cost(m, 0, 1_000_000)}
        rates[m] = rate
        if not all(math.isfinite(r) and r > 0 for r in rate.values()):
            bad.append(f"{m!r} has non-positive rates {rate}")
    prov["rates"] = rates
    if bad:
        detail = ("; ".join(bad) + " — its cost would be $0 and the soft budget could not "
                  "stop spend")
    else:
        detail = ", ".join(f"{m}: ${r['input_usd_per_mtok']:g} in / "
                           f"${r['output_usd_per_mtok']:g} out per MTok"
                           for m, r in rates.items())
    detail += f" (prices_as_of {meter.prices_as_of or '?'}, sha256 {prov['sha256'][:12]})"
    return Check("model_priced", not bad, detail), prov


def check_api_key_present(env: dict[str, str]) -> Check:
    # Presence only: the value is never read into the plan, logged, or printed.
    present = bool(env.get("ANTHROPIC_API_KEY"))
    return Check("anthropic_api_key", present,
                 "set" if present else "ANTHROPIC_API_KEY is not set — no model calls possible")


def check_db_env(env: dict[str, str]) -> Check:
    missing = []
    if not (env.get("VIGIL_PG_DSN") or env.get("POSTGRES_PASSWORD")):
        missing.append("POSTGRES_PASSWORD/VIGIL_PG_DSN")
    if not env.get("CLICKHOUSE_PASSWORD"):
        missing.append("CLICKHOUSE_PASSWORD")
    return Check("stack_credentials_set", not missing,
                 "set (values not read)" if not missing else "unset: " + ", ".join(missing)
                 + " (load the repo .env into the environment)")


def git_state(results_root: Path) -> dict:
    """HEAD and whether tracked/untracked source differs from it (ignoring this tool's own
    result directories, so an earlier stage's output does not block the next)."""
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(ROOT),
                              timeout=30)
    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    try:
        rel = results_root.resolve().relative_to(ROOT).as_posix().rstrip("/") + "/"
    except ValueError:
        rel = None
    dirty = [
        line for line in status.stdout.splitlines()
        if line.strip() and not (rel and line[3:].strip().strip('"').startswith(rel))
    ] if status.returncode == 0 else ["<git status failed>"]
    return {"head": head.stdout.strip() or None, "dirty_paths": dirty[:50],
            "dirty": bool(dirty)}


def probe_tcp(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_stack(env: dict[str, str], probe: Callable[[str, int], bool] = probe_tcp) -> list[Check]:
    """Local TCP probes only (no data sent): Postgres, ClickHouse HTTP, the OTLP receiver."""
    otlp = env.get("VIGIL_OTLP_GRPC_ADDR", "127.0.0.1:4317")
    targets = {
        "postgres": (env.get("POSTGRES_HOST", "127.0.0.1"), int(env.get("POSTGRES_PORT", "5432"))),
        "clickhouse_http": (env.get("CLICKHOUSE_HOST", "127.0.0.1"),
                            int(env.get("CLICKHOUSE_HTTP_PORT", "8123"))),
        "otlp_receiver": (otlp.rsplit(":", 1)[0], int(otlp.rsplit(":", 1)[1])),
    }
    return [Check(f"stack_{name}", probe(h, p), f"{h}:{p}") for name, (h, p) in targets.items()]


# --- plan ----------------------------------------------------------------------------


def estimate_cost(units: int) -> float:
    return units * PRIOR_COST["usd"] / PRIOR_COST["units"]


def suite_name(stage: str, prov: dict) -> str:
    """Deterministic over the exact cases (cases file + ordered ids), so a re-run reuses the
    suite only when its content is the same."""
    key = prov["cases"]["sha256"] + prov["stage_cases"]["ids_sha256"]
    return f"hotpotqa-triage-{stage}-{sha256_bytes(key.encode())[:12]}"


def build_plan(
    *, stage: str, model: str, budget: float | None, data_dir: Path, results_root: Path,
    env: dict[str, str], manifests: dict[str, dict] | None = None,
    free_bytes: Callable[[Path], int] | None = None,
) -> dict:
    """Everything a run will do, decided before it starts. Offline."""
    from engine import gate

    spec = STAGES[stage]
    budget = spec["budget_usd"] if budget is None else budget
    checks = [check_disk([ROOT, results_root], free_bytes)]
    data_checks, prov, ids = inspect_data(data_dir, stage)
    checks += data_checks
    if not (isinstance(budget, (int, float)) and math.isfinite(budget)
            and 0 < budget <= spec["max_budget_usd"]):
        checks.append(Check("budget", False, f"--budget {budget!r} must be in (0, "
                            f"{spec['max_budget_usd']}] for stage {stage}"))
    if manifests is None:
        try:
            manifests = compute_manifests(model)
        except RuntimeError as exc:
            checks.append(Check("variant_manifests", False, str(exc)))
            manifests = {}
    if manifests:
        checks += check_manifests(manifests, model)
    priced, pricing = check_pricing(model, manifests, prices_path(env))
    checks.append(priced)
    checks.append(check_api_key_present(env))
    checks.append(check_db_env(env))

    units_per_version = len(ids) * spec["trials"]
    estimate = estimate_cost(2 * units_per_version)
    checks.append(Check(
        "budget_headroom", budget >= 1.5 * estimate,
        f"budget ${budget:.2f} vs rough estimate ${estimate:.2f} (prior: $"
        f"{PRIOR_COST['usd']}/{PRIOR_COST['units']} units, run {PRIOR_COST['run'][:8]}); "
        "below 1.5× the run may stop early and the verdict will be incomplete",
        blocking=False,
    ))
    return {
        "tool": "bench/hotpotqa_triage.py",
        "stage": stage,
        "purpose": spec["purpose"],
        "created_at": _now().isoformat(),
        "predeclared": {
            "subset": spec["subset"], "take": spec["take"],
            "case_ids": ids, "case_ids_sha256": prov.get("stage_cases", {}).get("ids_sha256"),
            "trials": spec["trials"], "mode": spec["mode"],
            "comparison": "candidate top_k=1 vs baseline top_k=5, same run, explicit versions",
            "policy": gate.DEFAULT_POLICY.to_dict(),
            "expected_verdict": None,  # deliberately not predicted; the gate decides
        },
        "execution": {
            "concurrency": CONCURRENCY, "cache": CACHE_MODE, "budget_usd": budget,
            "budget_note": "engine soft budget: checked between units, in-flight work "
            "finishes; NOT a billing cap",
            "units_per_version": units_per_version, "units_total": 2 * units_per_version,
            "estimated_cost_usd": round(estimate, 4),
            "model": model,
            "pricing": pricing,
            "sampling": "Anthropic Messages defaults (temperature not set; the API has no "
            "seed) — identical for both versions; trials capture sampling noise",
            "python": sys.executable,
            "suite": suite_name(stage, prov) if "stage_cases" in prov and "cases" in prov else None,
        },
        "variants": {
            label: {**VARIANTS[label], **({k: manifests[label][k] for k in
                    ("agent_id", "agent_version", "model", "params")} if manifests else {})}
            for label in VARIANTS
        },
        "manifests": manifests,
        "data": prov,
        "git": git_state(results_root),
        "checks": [asdict(c) for c in checks],
        "network": "plan made no network, database or model-provider calls",
    }


def plan_failures(plan: dict, *, allow_dirty: bool = False) -> list[str]:
    out = [f"{c['name']}: {c['detail']}" for c in plan["checks"] if c["blocking"] and not c["ok"]]
    if plan["git"]["dirty"] and not allow_dirty:
        out.append("source tree has uncommitted changes (commit first, or --allow-dirty to "
                   "record them): " + ", ".join(plan["git"]["dirty_paths"][:10]))
    return out


def render_plan(plan: dict, commands: list[list[str]] | None = None) -> str:
    ex, pre = plan["execution"], plan["predeclared"]
    lines = [
        f"HotpotQA triage validation — stage {plan['stage']}: {plan['purpose']}",
        f"  cases     {len(pre['case_ids'])} from {pre['subset']}"
        + (f" (first {pre['take']})" if pre["take"] else "")
        + f", ids sha256 {str(pre['case_ids_sha256'])[:12]}; trials {pre['trials']}; "
        f"mode {pre['mode']}",
        f"  execution concurrency {ex['concurrency']}, cache {ex['cache']}, model {ex['model']}, "
        f"{ex['units_total']} units, est. ${ex['estimated_cost_usd']:.2f}, "
        f"soft budget ${ex['budget_usd']:.2f} (not a billing cap)",
        f"  suite     {ex['suite']}",
    ]
    for label, v in plan["variants"].items():
        lines.append(f"  {label:9s} {v['module']} top_k={v['top_k']} "
                     f"version {str(v.get('agent_version', '?'))[:12]}")
    g = plan["git"]
    lines.append(f"  git       {g['head']}" + (" (DIRTY)" if g["dirty"] else " (clean)"))
    lines.append("  checks:")
    for c in plan["checks"]:
        mark = "ok  " if c["ok"] else ("FAIL" if c["blocking"] else "warn")
        lines.append(f"    [{mark}] {c['name']}: {c['detail']}")
    if commands:
        lines.append("  commands (run in order by `run`; <RUN_ID> is parsed from step 2):")
        for i, cmd in enumerate(commands, 1):
            lines.append(f"    {i}. " + " ".join(_shell_quote(a) for a in cmd))
    lines.append(f"  {plan['network']}")
    return "\n".join(lines)


def _shell_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


def _child_env(*, model: str, corpus: Path | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["VIGIL_HOTPOT_MODEL"] = model
    if corpus is not None:
        env["VIGIL_HOTPOTQA_CORPUS"] = str(corpus)
    # The engine's own disk floor is 1 GB (decimal); never let the environment lower it below
    # this tool's 1 GiB floor (or disable it with 0).
    try:
        floor = float(env.get("VIGIL_MIN_FREE_DISK_GB", "1.0"))
    except ValueError:
        floor = 0.0
    env["VIGIL_MIN_FREE_DISK_GB"] = str(max(floor, MIN_FREE_BYTES / 1e9))
    return env


def build_commands(plan: dict, result_dir: Path, subset_file: Path,
                   run_id: str = "<RUN_ID>") -> dict[str, list[str]]:
    py = plan["execution"]["python"]
    v = plan["variants"]
    return {
        "suite_create": [
            py, "-m", "engine", "suite", "create", "--name", plan["execution"]["suite"],
            "--adapter", "hotpotqa", "--path", plan["data"]["cases"]["path"],
            "--subset-path", str(subset_file),
        ],
        "engine_run": [
            py, "-m", "engine", "run", "--suite", plan["execution"]["suite"],
            "--agent", v["baseline"]["module"], "--agent", v["candidate"]["module"],
            "--mode", plan["predeclared"]["mode"], "--trials", str(plan["predeclared"]["trials"]),
            "--concurrency", str(CONCURRENCY), "--cache", CACHE_MODE,
            "--budget", f"{plan['execution']['budget_usd']:.2f}",
            "--notes", f"hotpotqa-triage {plan['stage']} {result_dir.name}",
        ],
        "regress": [
            py, "-m", "engine", "regress",
            "--baseline", run_id, "--baseline-version", v["baseline"].get("agent_version", "?"),
            "--candidate", run_id, "--candidate-version", v["candidate"].get("agent_version", "?"),
            "--save", str(result_dir / "bundle"), "--exit-code",
        ],
        "reproduce": [py, str(ROOT / "bench" / "regression_verdict.py"), "reproduce",
                      str(result_dir / "bundle")],
    }


def make_result_dir(results_root: Path, stage: str, now: datetime | None = None) -> Path:
    """A NEW directory named by UTC time and stage; refuses to reuse an existing one."""
    now = now or _now()
    d = results_root / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{stage}"
    results_root.mkdir(parents=True, exist_ok=True)
    d.mkdir(exist_ok=False)  # FileExistsError: never overwrite earlier raw outputs
    return d


# --- post-run checks (pure; data fetched by a Backend) -------------------------------

REGRESS_EXIT = {0: "no_regression", 2: "regression", 3: "incomplete"}


def check_run_versions(plan: dict, run: dict | None, versions: list[dict]) -> list[Check]:
    if run is None:
        return [Check("run_exists", False, "run not found in Postgres")]
    planned = {v["agent_version"] for v in plan["variants"].values()}
    found = {v["agent_version"] for v in versions}
    pre = plan["predeclared"]
    checks = [
        Check("run_versions_match_plan", found == planned,
              f"run has {sorted(x[:12] for x in found)}, "
              f"planned {sorted(x[:12] for x in planned)}"),
        Check("run_settings_match_plan",
              (run.get("mode"), run.get("trials_per_case"), run.get("concurrency"),
               run.get("cache_mode")) == (pre["mode"], pre["trials"], CONCURRENCY, CACHE_MODE),
              f"mode={run.get('mode')} trials={run.get('trials_per_case')} "
              f"concurrency={run.get('concurrency')} cache={run.get('cache_mode')}"),
        Check("run_status", run.get("status") == "succeeded",
              f"run status {run.get('status')!r} (aborted = soft budget hit or stopped; the "
              "verdict is then incomplete)", blocking=False),
    ]
    head = plan["git"]["head"]
    checks.append(Check("run_git_sha_matches_plan", run.get("git_sha") == head,
                        f"run {str(run.get('git_sha'))[:12]}, plan {str(head)[:12]}"))
    return checks


def check_db_manifests(plan: dict, db_manifests: dict[str, dict | None]) -> list[Check]:
    out = []
    for label, planned in plan["manifests"].items():
        row = db_manifests.get(label)
        same = row is not None and all(row.get(k) == planned[k]
                                       for k in ("model", "params", "prompts", "tools"))
        out.append(Check(f"manifest_{label}_matches_plan", same,
                         "missing" if row is None else ("identical" if same else "differs")))
    return out


def check_units(
    label: str, top_k: int, n_expected: int, rows: list[dict]
) -> tuple[list[Check], dict]:
    """Unit coverage, real (non-zero) tokens and cost on OK units, and the retrieval k the
    agent actually used, from the persisted transcripts."""
    ok = [r for r in rows if r["status"] == "ok"]
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    zero = [f"{r['case_id']}#{r['trial']}" for r in ok
            if not (r.get("input_tokens") or 0) > 0 or not (r.get("output_tokens") or 0) > 0
            or not (r.get("cost_usd") or 0) > 0]
    sizes = [len(ret.get("doc_ids") or []) for r in ok
             for ret in ((r.get("output") or {}).get("retrievals") or [])]
    over = [s for s in sizes if s > top_k]
    stats = {
        "units": len(rows), "expected_units": n_expected, "statuses": statuses,
        "ok_units": len(ok),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in ok),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in ok),
        "cost_usd": sum(float(r.get("cost_usd") or 0) for r in ok),
        "searches": len(sizes),
        "searches_returning_top_k": sum(1 for s in sizes if s == top_k),
        "max_docs_per_search": max(sizes) if sizes else None,
        "with_trace_id": sum(1 for r in ok if r.get("trace_id")),
    }
    checks = [
        Check(f"{label}_units_complete", len(rows) == n_expected,
              f"{len(rows)}/{n_expected} units, statuses {statuses}"),
        Check(f"{label}_has_ok_units", bool(ok), f"{len(ok)} OK"),
        Check(f"{label}_nonzero_tokens_cost", bool(ok) and not zero,
              f"tokens {stats['input_tokens']} in / {stats['output_tokens']} out, "
              f"${stats['cost_usd']:.4f} on OK units"
              + (f"; zero on {len(zero)}: {zero[:5]}" if zero else "")),
        Check(f"{label}_retrieval_k",
              bool(sizes) and not over and stats["searches_returning_top_k"] > 0,
              f"{len(sizes)} searches, max {stats['max_docs_per_search']} docs (top_k {top_k}), "
              f"{stats['searches_returning_top_k']} returned exactly top_k"),
        Check(f"{label}_trace_ids", stats["with_trace_id"] == len(ok),
              f"{stats['with_trace_id']}/{len(ok)} OK units link a trace"),
    ]
    return checks, stats


def check_traces(label: str, top_k: int, model: str, pg_rows: list[dict], ch: dict,
                 pg_stats: dict, cache_mode: str | None = CACHE_MODE) -> list[Check]:
    """``ch``: {"trace_ids": [...], "max_k": int|None, "k_spans": int, "models": [...],
    "input_tokens": int, "output_tokens": int, "cost_usd": float, "accounting": {...}} for
    this run+version; the token/cost figures are the canonical ones of
    ``span_accounting.reconcile``, which ``accounting`` is."""
    want = {r["trace_id"] for r in pg_rows if r["status"] == "ok" and r.get("trace_id")}
    have = set(ch.get("trace_ids") or [])
    missing = sorted(want - have)
    checks = [
        Check(f"{label}_traces_in_clickhouse", bool(want) and not missing,
              f"{len(want) - len(missing)}/{len(want)} OK-unit traces found"
              + (f"; missing e.g. {missing[:3]} (the writer may still be flushing — re-run "
                 "verify)" if missing else "")),
        Check(f"{label}_span_retrieval_k", ch.get("max_k") is not None and ch["max_k"] <= top_k,
              f"max vigil.retrieval.k {ch.get('max_k')} over {ch.get('k_spans', 0)} search spans "
              f"(top_k {top_k}; Postgres has {pg_stats['searches']} searches)"),
        Check(f"{label}_span_model", set(ch.get("models") or []) == {model},
              f"gen_ai.request.model {sorted(ch.get('models') or [])}"),
        Check(f"{label}_span_tokens_nonzero",
              (ch.get("input_tokens") or 0) > 0 and (ch.get("cost_usd") or 0) > 0,
              f"spans: {ch.get('input_tokens')} in / {ch.get('output_tokens')} out tokens, "
              f"${float(ch.get('cost_usd') or 0):.4f} (Postgres OK units: "
              f"{pg_stats['input_tokens']} / {pg_stats['output_tokens']}, "
              f"${pg_stats['cost_usd']:.4f}; spans also include retried/failed attempts)"),
        check_single_owner(label, ch.get("accounting")),
        check_spend_reconciles(label, pg_rows, ch, pg_stats, cache_mode),
    ]
    return checks


def check_single_owner(label: str, acct: dict | None) -> Check:
    """Every LLM span with usage is a Vigil ``gen_ai.*`` span: no second instrumentation
    (e.g. the anthropic SDK's native call spans) recording the same calls again."""
    name = f"{label}_span_single_owner"
    if acct is None:
        return Check(name, False, "span accounting not reported")
    detail = (f"{acct['owner_spans']} gen_ai spans; {acct['shadow_spans']} proven duplicate "
              f"native spans (excluded), {acct['unresolved_spans']} unresolved nested LLM "
              f"spans {acct['unresolved_reasons'] or ''} and {acct['unowned_spans']} unowned "
              f"ones (both still counted); {acct['duplicate_identity_rows']} replayed rows "
              "collapsed")
    if not acct["single_owner"]:
        s, c = acct["as_stored"], acct["canonical"]
        ex = acct["examples"]
        detail += (f". Stored rows sum to {s['input_tokens']} in / {s['output_tokens']} out, "
                   f"${s['cost_usd']:.6f}; reconciled {c['input_tokens']} / "
                   f"{c['output_tokens']}, ${c['cost_usd']:.6f}"
                   + ("" if not (acct["unresolved_spans"] or acct["unowned_spans"]) else
                      " (an upper bound: unresolved/unowned spans may be duplicates)")
                   + f"; e.g. {(ex['unresolved'] or ex['unowned'] or ex['shadow'])[:2]}")
    return Check(name, acct["single_owner"], detail)


def check_spend_reconciles(label: str, pg_rows: list[dict], ch: dict, pg_stats: dict,
                           cache_mode: str | None) -> Check:
    """The canonical span spend against the Postgres OK units.

    Exact when the two must agree: every unit OK on its first attempt, no LLM cache and no
    span of another role (e.g. a user simulator). Otherwise the spans may legitimately hold
    more — failed units and retried attempts spent tokens that no OK unit reports — so they
    must be at least the Postgres totals. With the cache on, a hit's tokens are in Postgres
    but not spend, so the comparison is informational only."""
    name = f"{label}_span_spend_reconciles"
    acct = ch.get("accounting") or {}
    have = (int(ch.get("input_tokens") or 0), int(ch.get("output_tokens") or 0),
            float(ch.get("cost_usd") or 0))
    want = (pg_stats["input_tokens"], pg_stats["output_tokens"], pg_stats["cost_usd"])
    figures = (f"spans {have[0]} in / {have[1]} out, ${have[2]:.6f}; Postgres OK units "
               f"{want[0]} / {want[1]}, ${want[2]:.6f}")
    reasons = []
    if any(r["status"] != "ok" for r in pg_rows):
        reasons.append("non-OK units")
    if any((r.get("attempts") or 1) > 1 for r in pg_rows):
        reasons.append("retried units")
    if acct.get("roles"):
        reasons.append(f"other roles {acct['roles']}")
    if cache_mode != "off":
        return Check(name, True, f"not comparable (cache {cache_mode}): {figures}",
                     blocking=False)
    if not reasons:
        same = (have[0] == want[0] and have[1] == want[1]
                and math.isclose(have[2], want[2], rel_tol=1e-6, abs_tol=1e-9))
        return Check(name, same, f"exact (every unit OK on its first attempt, cache off): "
                     f"{figures}" + ("" if same else
                                     f"; ratio {have[0] / want[0] if want[0] else 0:.3f}×"))
    at_least = (have[0] >= want[0] and have[1] >= want[1]
                and have[2] >= want[2] * (1 - 1e-6) - 1e-9)
    return Check(name, at_least, f"lower bound ({', '.join(reasons)}): {figures}")


def _close(a, b, tol=1e-9) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=1e-12)


def check_cli_api(verdict: dict, cli_exit: int | None, api: dict | None) -> list[Check]:
    """CLI exit code, the saved verdict and the API's /compare must agree."""
    checks = [Check("cli_exit_matches_bundle", REGRESS_EXIT.get(cli_exit) == verdict["verdict"],
                    f"engine regress exit {cli_exit} → {REGRESS_EXIT.get(cli_exit)}, "
                    f"bundle verdict {verdict['verdict']}")]
    if api is None:
        checks.append(Check("api_matches_cli", False, "not checked (no --api-url)", blocking=False))
        return checks
    diffs = []
    for k in ("verdict", "paired_cases"):
        if api.get(k) != verdict.get(k):
            diffs.append(f"{k}: api {api.get(k)!r} vs cli {verdict.get(k)!r}")
    am = {m["metric"]: m for m in api.get("metrics", [])}
    for m in verdict.get("metrics", []):
        a = am.get(m["metric"])
        if a is None:
            diffs.append(f"{m['metric']}: missing in api")
            continue
        for f in ("point_estimate", "ci_bound", "tail_prob"):
            if not _close(a.get(f), m.get(f)):
                diffs.append(f"{m['metric']}.{f}: {a.get(f)} vs {m.get(f)}")
        for f in ("regressed", "status", "gated"):
            if a.get(f) != m.get(f):
                diffs.append(f"{m['metric']}.{f}: {a.get(f)} vs {m.get(f)}")
    checks.append(Check("api_matches_cli", not diffs,
                        "; ".join(diffs[:10]) or f"verdict {verdict['verdict']}, "
                        f"{len(am)} metrics identical"))
    return checks


def check_version_diff(diff: dict | None) -> Check:
    if diff is None:
        return Check("api_version_diff", False, "not checked (no --api-url)", blocking=False)
    f = diff.get("fields", {})
    p = f.get("params", {})
    ok = (
        p.get("changed") is True and p.get("modified") == [VARIANT_PARAM]
        and not p.get("added") and not p.get("removed")
        and f.get("tools", {}).get("changed") is True
        and f.get("model", {}).get("changed") is False
        and f.get("prompts", {}).get("changed") is False
    )
    changed = sorted(k for k, v in f.items() if v.get("changed"))
    return Check("api_version_diff", ok, f"changed fields {changed}; params modified "
                 f"{p.get('modified')}")


# --- backend: Postgres, ClickHouse, API (real I/O; tests substitute a fake) ----------


class Backend:
    def suite_case_ids(self, name: str) -> list[str] | None:
        import psycopg
        from engine.config import pg_dsn

        with psycopg.connect(pg_dsn()) as conn:
            row = conn.execute("SELECT id FROM eval_suites WHERE name = %s", (name,)).fetchone()
            if row is None:
                return None
            return [r[0] for r in conn.execute(
                "SELECT case_id FROM eval_cases WHERE suite_id = %s ORDER BY case_id", (row[0],))]

    def run_ids_by_notes(self, notes: str) -> list[str]:
        import psycopg
        from engine.config import pg_dsn

        with psycopg.connect(pg_dsn()) as conn:
            return [str(r[0]) for r in conn.execute(
                "SELECT id FROM eval_runs WHERE notes = %s ORDER BY created_at", (notes,))]

    def run_data(self, run_id: str) -> dict:
        import psycopg
        from engine.config import pg_dsn

        with psycopg.connect(pg_dsn()) as conn:
            r = conn.execute(
                "SELECT status, mode, trials_per_case, concurrency, cache_mode, cost_budget_usd, "
                "cost_spent_usd, git_sha, notes, agent_id, suite_id FROM eval_runs WHERE id = %s",
                (run_id,)).fetchone()
            if r is None:
                return {"run": None, "versions": [], "manifests": {}, "rows": {}}
            run = dict(zip(("status", "mode", "trials_per_case", "concurrency", "cache_mode",
                            "cost_budget_usd", "cost_spent_usd", "git_sha", "notes", "agent_id",
                            "suite_id"), r))
            for k in ("cost_budget_usd", "cost_spent_usd"):
                run[k] = None if run[k] is None else float(run[k])
            run["suite_id"] = str(run["suite_id"])
            versions = [dict(zip(("agent_version", "status", "cases_done", "cases_total",
                                  "cost_spent_usd"), v)) for v in conn.execute(
                "SELECT agent_version, status, cases_done, cases_total, cost_spent_usd "
                "FROM eval_run_versions WHERE run_id = %s", (run_id,))]
            for v in versions:
                v["cost_spent_usd"] = float(v["cost_spent_usd"])
            manifests, rows = {}, {}
            for v in versions:
                m = conn.execute(
                    "SELECT model, params, prompts, tools FROM version_manifests "
                    "WHERE agent_id = %s AND agent_version = %s",
                    (run["agent_id"], v["agent_version"])).fetchone()
                manifests[v["agent_version"]] = (
                    None if m is None else dict(zip(("model", "params", "prompts", "tools"), m)))
                rows[v["agent_version"]] = [dict(zip(
                    ("case_id", "trial", "status", "passed", "scores", "output", "input_tokens",
                     "output_tokens", "cost_usd", "latency_ms", "trace_id", "attempts",
                     "expected", "tags"), x)) for x in conn.execute(
                    "SELECT r.case_id, r.trial, r.status, r.passed, r.scores, r.output, "
                    "r.input_tokens, r.output_tokens, r.cost_usd, r.latency_ms, r.trace_id, "
                    "r.attempts, ec.expected, ec.tags FROM eval_case_results r "
                    "JOIN eval_cases ec ON ec.suite_id = %s AND ec.case_id = r.case_id "
                    "WHERE r.run_id = %s AND r.agent_version = %s ORDER BY r.case_id, r.trial",
                    (run["suite_id"], run_id, v["agent_version"]))]
                for x in rows[v["agent_version"]]:
                    x["cost_usd"] = None if x["cost_usd"] is None else float(x["cost_usd"])
        return {"run": run, "versions": versions, "manifests": manifests, "rows": rows}

    def clickhouse(self, run_id: str, version: str) -> dict:
        from engine.api.clickhouse import ClickHouseClient

        ch = ClickHouseClient()
        p = {"run": run_id, "ver": version}
        where = "eval_run_id = {run:String} AND agent_version = {ver:String}"
        tids = [r["trace_id"] for r in ch.query(
            f"SELECT DISTINCT trace_id FROM spans WHERE {where}", p)]
        k = ch.query(
            "SELECT max(toUInt32OrZero(span_attributes['vigil.retrieval.k'])) AS max_k, "
            f"count() AS n FROM spans WHERE {where} "
            "AND mapContains(span_attributes, 'vigil.retrieval.k')", p)[0]
        # Not a plain sum over the rows: one accounting owner per call, one row per span
        # identity, cache hits excluded (bench/span_accounting.py).
        acct = span_accounting.reconcile(span_accounting.fetch_llm_spans(ch, run_id, version))
        canonical = acct["canonical"]
        return {
            "trace_ids": tids, "max_k": int(k["max_k"]) if int(k["n"]) else None,
            "k_spans": int(k["n"]), "models": acct["models"],
            "input_tokens": canonical["input_tokens"],
            "output_tokens": canonical["output_tokens"], "cost_usd": canonical["cost_usd"],
            "accounting": acct,
        }

    def api_get(self, base: str, path: str, params: dict) -> dict:
        url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.loads(resp.read())


def check_api_url(url: str | None) -> str | None:
    """Only a loopback API: this tool never sends run data off the machine."""
    if url is None:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"--api-url must be http on a loopback host, got {url!r}")
    return url


# --- run / verify --------------------------------------------------------------------


def execute(cmd: list[str], log_path: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run one step, streaming its output to the console and to ``log_path``."""
    out: list[str] = []
    with log_path.open("w") as log:
        log.write("$ " + " ".join(_shell_quote(a) for a in cmd) + "\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, cwd=str(ROOT))
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
            out.append(line)
        rc = proc.wait()
        log.write(f"# exit {rc}\n")
    return rc, "".join(out)


def parse_run_id(stdout: str) -> str | None:
    import re

    m = re.search(r"^run ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) ",
                  stdout, re.MULTILINE)
    return m.group(1) if m else None


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def _log_step(result_dir: Path, step: str, **fields) -> None:
    with (result_dir / "steps.jsonl").open("a") as f:
        f.write(json.dumps({"step": step, "at": _now().isoformat(), **fields}, default=str) + "\n")


def cmd_run(args, *, backend: Backend | None = None, runner=execute,
            env: dict[str, str] | None = None, manifests: dict | None = None,
            free_bytes=None, now: datetime | None = None) -> int:
    backend = backend or Backend()
    env = dict(os.environ) if env is None else env
    results_root = Path(args.results_root)
    plan = build_plan(stage=args.stage, model=args.model, budget=args.budget,
                      data_dir=Path(args.data_dir), results_root=results_root, env=env,
                      manifests=manifests, free_bytes=free_bytes)
    stack = check_stack(env) if not args.skip_stack_probe else []
    plan["checks"] += [asdict(c) for c in stack]
    problems = plan_failures(plan, allow_dirty=args.allow_dirty)
    if not args.confirm_spend:
        problems.append("--confirm-spend not given: `run` makes paid model calls "
                        f"(est. ${plan['execution']['estimated_cost_usd']:.2f}, soft budget "
                        f"${plan['execution']['budget_usd']:.2f})")
    if problems:
        print(render_plan(plan))
        print("\nREFUSED — nothing was run, nothing was written:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    result_dir = make_result_dir(results_root, args.stage, now)
    plan["result_dir"] = str(result_dir)
    plan["api_url"] = args.api_url
    if args.stage == "smoke":
        subset_file = result_dir / "smoke_subset.txt"
        subset_file.write_text("\n".join(plan["predeclared"]["case_ids"]) + "\n")
    else:
        subset_file = Path(plan["data"]["subsets"][plan["predeclared"]["subset"]]["path"])
    cmds = build_commands(plan, result_dir, subset_file)
    plan["commands"] = cmds
    _write_json(result_dir / "plan.json", plan)
    (result_dir / "plan.json.sha256").write_text(
        sha256_file(result_dir / "plan.json") + "  plan.json\n")
    print(render_plan(plan, list(cmds.values())))
    child_env = _child_env(model=args.model, corpus=Path(plan["data"]["corpus"]["path"]))

    # 1. Suite: reuse only when its cases are exactly the predeclared ones.
    name = plan["execution"]["suite"]
    existing = backend.suite_case_ids(name)
    want = sorted(plan["predeclared"]["case_ids"])
    if existing is None:
        rc, _ = runner(cmds["suite_create"], result_dir / "01_suite_create.log", child_env)
        _log_step(result_dir, "suite_create", exit=rc)
        if rc != 0:
            return _finish(result_dir, plan, status="suite_create_failed")
        existing = backend.suite_case_ids(name)
    if sorted(existing or []) != want:
        _log_step(result_dir, "suite_verify", ok=False, n=len(existing or []))
        return _finish(result_dir, plan, status="suite_mismatch")
    _log_step(result_dir, "suite_verify", ok=True, n=len(want))

    # 2. The eval run: both versions in one run, one worker each.
    rc, out = runner(cmds["engine_run"], result_dir / "02_engine_run.log", child_env)
    run_id = parse_run_id(out)
    if run_id is None:
        # A crash after the run row was created (e.g. a worker died) prints no run line; the
        # notes carry this result dir's unique name, so the failed run is still found and goes
        # through the gate (which reports it as incomplete or a regression, never a pass).
        found = backend.run_ids_by_notes(cmds["engine_run"][-1])
        run_id = found[0] if len(found) == 1 else None
    _log_step(result_dir, "engine_run", exit=rc, run_id=run_id)
    _write_json(result_dir / "run.json", {"run_id": run_id, "exit": rc})
    if run_id is None:
        return _finish(result_dir, plan, status="engine_run_failed")

    # 3. Verdict + evidence bundle via the shared gate; 4. offline reproduction.
    cmds = build_commands(plan, result_dir, subset_file, run_id=run_id)
    rc_regress, _ = runner(cmds["regress"], result_dir / "03_regress.log", child_env)
    _log_step(result_dir, "regress", exit=rc_regress)
    _write_json(result_dir / "run.json", {"run_id": run_id, "exit": rc, "regress_exit": rc_regress})
    if rc_regress not in REGRESS_EXIT:
        return _finish(result_dir, plan, status="regress_failed", run_id=run_id)
    rc_repro, _ = runner(cmds["reproduce"], result_dir / "04_reproduce.log", child_env)
    _log_step(result_dir, "reproduce", exit=rc_repro)
    _write_json(result_dir / "run.json", {"run_id": run_id, "exit": rc, "regress_exit": rc_regress,
                                         "reproduce_exit": rc_repro})
    return verify(result_dir, api_url=args.api_url, backend=backend)


def _finish(result_dir: Path, plan: dict, *, status: str, run_id: str | None = None) -> int:
    _write_json(result_dir / "summary.json", {"status": status, "run_id": run_id,
                                               "stage": plan["stage"], "verdict": None})
    print(f"\nSTOPPED: {status} — see {result_dir}", file=sys.stderr)
    return 1


def units_for_report(rows: list[dict]) -> list[dict]:
    """Rows → the unit shape bench/hotpotqa_baseline.py aggregates, using the scores stored at
    run time (the same scores the gate compared)."""
    units = []
    for r in rows:
        if r["status"] != "ok":
            continue
        expected = r.get("expected") or {}
        metrics = {"passed": 1.0 if r.get("passed") else 0.0}
        for name, entry in (r.get("scores") or {}).items():
            if isinstance(entry, dict) and isinstance(entry.get("score"), (int, float)):
                metrics[name] = float(entry["score"])
        units.append({
            "case_id": r["case_id"], "trial": r["trial"],
            "type": expected.get("type") or "bridge", "metrics": metrics,
            "cost_usd": float(r.get("cost_usd") or 0.0),
            "input_tokens": r.get("input_tokens") or 0,
            "output_tokens": r.get("output_tokens") or 0, "latency_ms": r.get("latency_ms"),
        })
    return units


def verify(result_dir: Path, *, api_url: str | None, backend: Backend | None = None) -> int:
    """Post-run consistency checks + reports. Re-runnable; writes verify-<UTC>.json and
    (re)writes summary.json / summary.md / per-version tables. Never re-runs the eval."""
    import hotpotqa_baseline as hb

    backend = backend or Backend()
    api_url = check_api_url(api_url)
    plan = json.loads((result_dir / "plan.json").read_text())
    recorded = (result_dir / "plan.json.sha256").read_text().split()[0]
    run_info = json.loads((result_dir / "run.json").read_text())
    run_id = run_info["run_id"]
    checks = [Check("plan_unchanged", sha256_file(result_dir / "plan.json") == recorded,
                    "plan.json matches the sha256 recorded before the run")]

    data = backend.run_data(run_id)
    checks += check_run_versions(plan, data["run"], data["versions"])
    by_label = {label: v["agent_version"] for label, v in plan["variants"].items()}
    checks += check_db_manifests(plan, {lbl: data["manifests"].get(ver)
                                        for lbl, ver in by_label.items()})

    n_expected = len(plan["predeclared"]["case_ids"]) * plan["predeclared"]["trials"]
    stats: dict[str, dict] = {}
    for label, ver in by_label.items():
        top_k = plan["variants"][label]["top_k"]
        rows = data["rows"].get(ver, [])
        c, s = check_units(label, top_k, n_expected, rows)
        checks += c
        stats[label] = s
        try:
            ch = backend.clickhouse(run_id, ver)
            checks += check_traces(label, top_k, plan["execution"]["model"], rows, ch, s,
                                   (data["run"] or {}).get("cache_mode"))
            stats[label]["clickhouse"] = {k: v for k, v in ch.items() if k != "trace_ids"}
            stats[label]["clickhouse"]["traces"] = len(ch["trace_ids"])
        except Exception as exc:  # noqa: BLE001 — reported as a failed check, not a crash
            checks.append(Check(f"{label}_clickhouse", False, f"query failed: {exc}"))
        units = units_for_report(rows)
        if units:
            metrics = [m for m in hb.METRICS if any(m in u["metrics"] for u in units)]
            agg = hb.aggregate(units, metrics, n_boot=10000, seed=1234)
            meta = {"run_id": run_id, "suite": plan["execution"]["suite"],
                    "agent_id": data["run"]["agent_id"] if data["run"] else "?",
                    "mode": plan["predeclared"]["mode"],
                    "trials_per_case": plan["predeclared"]["trials"],
                    "title": f"{plan['stage']} · {label} top_k={top_k} · {ver[:12]}"}
            raw = {"meta": meta, "agent_version": ver, "metrics": metrics, "units": units,
                   "aggregate": agg}
            raw_path = result_dir / f"units_{label}.json"
            if raw_path.exists():
                # Raw data is written once; a later verify only confirms it is unchanged.
                same = json.loads(raw_path.read_text()) == json.loads(
                    json.dumps(raw, sort_keys=True, default=str))
                checks.append(Check(f"{label}_units_unchanged", same,
                                    "stored units match the database" if same else
                                    f"database rows differ from {raw_path.name}"))
            else:
                _write_json(raw_path, raw)
                (result_dir / f"table_{label}.md").write_text(
                    hb.render_markdown(meta, agg, metrics) + "\n")

    bundle = result_dir / "bundle"
    verdict = None
    if (bundle / "verdict.json").is_file():
        verdict = json.loads((bundle / "verdict.json").read_text())
        rc_repro = run_info.get("reproduce_exit")
        checks.append(Check("bundle_reproduces", rc_repro == 0,
                            f"regression_verdict.py reproduce exit {rc_repro}"))
        api = diff = None
        if api_url:
            q = {"baseline": run_id, "candidate": run_id,
                 "baseline_version": by_label["baseline"],
                 "candidate_version": by_label["candidate"]}
            try:
                api = backend.api_get(api_url, "/compare", q)
                diff = backend.api_get(api_url, "/versions/diff", q)
            except Exception as exc:  # noqa: BLE001
                checks.append(Check("api_reachable", False, f"{api_url}: {exc}"))
        checks += check_cli_api(verdict, run_info.get("regress_exit"), api)
        checks.append(check_version_diff(diff))
    else:
        checks.append(Check("bundle_present", False, "no evidence bundle (regress failed?)"))

    failed = _failed(checks)
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_id": run_id, "stage": plan["stage"], "api_url": api_url,
        "verdict": verdict["verdict"] if verdict else None,
        "checks": [asdict(c) for c in checks], "checks_failed": len(failed), "stats": stats,
    }
    _write_json(result_dir / f"verify-{stamp}.json", report)
    # A re-verify replaces the summary; the one it replaces is evidence too, so it is kept.
    for name in ("summary.json", "summary.md"):
        old = result_dir / name
        n = 0
        while old.exists():
            kept = result_dir / f"{old.stem}.before-{stamp}{f'-{n}' if n else ''}{old.suffix}"
            if not kept.exists():
                old.rename(kept)
            n += 1
    summary = {
        "status": "verified" if not failed else "checks_failed",
        "stage": plan["stage"], "run_id": run_id, "verdict": report["verdict"],
        "versions": by_label, "checks_failed": [c.name for c in failed],
        "plan_sha256": recorded, "verify_report": f"verify-{stamp}.json",
    }
    _write_json(result_dir / "summary.json", summary)
    md = render_summary(plan, run_id, verdict, checks, stats, api_url)
    (result_dir / "summary.md").write_text(md + "\n")
    print("\n" + md)
    return 0 if not failed else 1


def render_summary(plan: dict, run_id: str, verdict: dict | None, checks: list[Check],
                   stats: dict, api_url: str | None) -> str:
    v = plan["variants"]
    lines = [
        f"# HotpotQA triage validation — {plan['stage']}",
        "",
        f"_{plan['purpose']}._",
        "",
        f"- Run `{run_id}`; suite `{plan['execution']['suite']}` "
        f"({len(plan['predeclared']['case_ids'])} cases × {plan['predeclared']['trials']} "
        f"trial(s), {plan['predeclared']['mode']}); model `{plan['execution']['model']}`; "
        f"concurrency {CONCURRENCY}; cache {CACHE_MODE}; soft budget "
        f"${plan['execution']['budget_usd']:.2f} (not a billing cap).",
        f"- Baseline `{v['baseline']['module']}` (top_k {v['baseline']['top_k']}) "
        f"`{v['baseline']['agent_version'][:12]}`; candidate `{v['candidate']['module']}` "
        f"(top_k {v['candidate']['top_k']}) `{v['candidate']['agent_version'][:12]}`.",
        f"- Source `{plan['git']['head']}`" + (" (dirty tree)" if plan["git"]["dirty"] else "")
        + f"; corpus sha256 `{plan['data']['corpus']['sha256'][:12]}`; case ids sha256 "
        f"`{plan['predeclared']['case_ids_sha256'][:12]}`.",
        "",
    ]
    if verdict:
        lines += [f"**Gate verdict: `{verdict['verdict']}`** (paired cases "
                  f"{verdict.get('paired_cases')}; computed by engine.gate with the default "
                  "policy; not predicted in advance)", ""]
        for r in verdict.get("incomplete_reasons") or []:
            lines.append(f"- incomplete: {r}")
        lines += ["", "| metric | gated | Δ (cand − base) | 1−α bound | p | status |",
                  "|---|---|---|---|---|---|"]
        for m in verdict.get("metrics", []):
            lines.append(
                f"| {m['metric']} | {'yes' if m['gated'] else 'no'} | "
                f"{_num(m.get('point_estimate'))} | {_num(m.get('ci_bound'))} | "
                f"{_num(m.get('tail_prob'), 3)} | {m.get('status')} |")
        lines.append("")
    else:
        lines += ["**No verdict** (the run or the regress step did not complete).", ""]
    for label, s in stats.items():
        lines.append(
            f"- {label}: {s['ok_units']}/{s['expected_units']} OK units, "
            f"${s['cost_usd']:.4f}, {s['input_tokens']} in / {s['output_tokens']} out tokens, "
            f"{s['searches']} searches (max {s['max_docs_per_search']} docs each)")
        acct = (s.get("clickhouse") or {}).get("accounting")
        if acct:
            c, st = acct["canonical"], acct["as_stored"]
            lines.append(
                f"  - spans (one owner per call): {c['spans']} LLM spans, "
                f"{c['input_tokens']} in / {c['output_tokens']} out, ${c['cost_usd']:.4f}"
                + ("" if acct["single_owner"] else
                   f" — reconciled; the stored rows sum to {st['input_tokens']} / "
                   f"{st['output_tokens']}, ${st['cost_usd']:.4f} ({acct['shadow_spans']} "
                   f"proven duplicate spans excluded; {acct['unresolved_spans']} unresolved "
                   f"and {acct['unowned_spans']} unowned spans still counted)"))
    lines += ["", "Checks:", ""]
    for c in checks:
        mark = "ok" if c.ok else ("FAIL" if c.blocking else "not run/warn")
        lines.append(f"- [{mark}] {c.name}: {c.detail}")
    b, c = v["baseline"]["agent_version"], v["candidate"]["agent_version"]
    lines += [
        "",
        "Reproduce / inspect:",
        "",
        "- `engine/.venv/bin/python bench/regression_verdict.py reproduce "
        f"{plan.get('result_dir', '<dir>')}/bundle`",
        f"- dashboard: `/compare?baseline={run_id}&baseline_version={b}&candidate={run_id}"
        f"&candidate_version={c}`",
        "",
        "Limits: one run of one controlled change on one dataset. Smoke and pilot stages are "
        "not calibrated statistical claims. The budget is the engine's soft budget, not a "
        "billing cap. The version hash covers prompts/tools/model/params, not source code; the "
        "commit is recorded separately.",
    ]
    if not api_url:
        lines.append("API/CLI agreement was not checked (no --api-url).")
    return "\n".join(lines)


def _num(x, digits: int = 4) -> str:
    return "—" if x is None else f"{x:+.{digits}f}"


# --- CLI -----------------------------------------------------------------------------


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stage", choices=sorted(STAGES), required=True)
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="pinned for both versions; must be priced in "
                   "ingest/internal/pricing/prices.json (or VIGIL_PRICES_PATH)")
    p.add_argument("--budget", type=float, default=None,
                   help="engine soft budget in USD (default: the stage's; not a billing cap)")
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    p.add_argument("--results-root", default=str(DEFAULT_RESULTS_ROOT))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("hotpotqa_triage", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    pp = sub.add_parser("plan", help="offline plan + preflight; no network, DB or model calls")
    _common(pp)
    pp.add_argument("--json", action="store_true", help="print the plan as JSON")
    rp = sub.add_parser("run", help="preflight, then the real eval run, verdict and checks")
    _common(rp)
    rp.add_argument("--confirm-spend", action="store_true",
                    help="required: acknowledges paid model calls")
    rp.add_argument("--allow-dirty", action="store_true",
                    help="run from an uncommitted tree (recorded in plan.json)")
    rp.add_argument("--skip-stack-probe", action="store_true",
                    help="skip the local TCP probes of Postgres/ClickHouse/OTLP")
    rp.add_argument("--api-url", default=None,
                    help="loopback URL of the running read-only API, e.g. http://127.0.0.1:8080")
    vp = sub.add_parser("verify", help="re-run the post-run checks on a result directory")
    vp.add_argument("result_dir")
    vp.add_argument("--api-url", default=None)
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if args.command == "plan":
        plan = build_plan(stage=args.stage, model=args.model, budget=args.budget,
                          data_dir=Path(args.data_dir), results_root=Path(args.results_root),
                          env=dict(os.environ))
        placeholder = Path(args.results_root) / f"<UTC>-{args.stage}"
        subset = (placeholder / "smoke_subset.txt" if args.stage == "smoke"
                  else Path(plan["data"].get("subsets", {}).get(STAGES[args.stage]["subset"],
                                                                 {}).get("path", "?")))
        cmds = (build_commands(plan, placeholder, subset)
                if plan["execution"]["suite"] and plan["manifests"] else {})
        if args.json:
            print(json.dumps({**plan, "commands": cmds}, indent=2, default=str))
        else:
            print(render_plan(plan, list(cmds.values())))
            fails = plan_failures(plan)
            print("\nready to run" if not fails else
                  "\nNOT ready — `run` would refuse:\n  - " + "\n  - ".join(fails))
        return 0
    if args.command == "run":
        check_api_url(args.api_url)
        return cmd_run(args)
    return verify(Path(args.result_dir), api_url=args.api_url)


if __name__ == "__main__":
    raise SystemExit(main())
