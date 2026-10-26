"""In-memory ``engine.gate.TriageStore`` + synthetic row builders for the triage tests.

The data these build are correctness fixtures (known shifts, known failures), not quality or
performance evidence."""

from __future__ import annotations

import sys
import uuid

import numpy as np

V1 = "a" * 64
V2 = "b" * 64
V3 = "c" * 64


def block_scipy(monkeypatch) -> None:
    """Make ``import scipy`` / ``from scipy.stats import t`` fail as on an install without SciPy."""
    for name in [m for m in sys.modules if m == "scipy" or m.startswith("scipy.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "scipy", None)
    monkeypatch.setitem(sys.modules, "scipy.stats", None)


def rid() -> str:
    return str(uuid.uuid4())


class FakeStore:
    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.versions: dict[str, list[dict]] = {}
        self.results: dict[tuple[str, str], list[dict]] = {}
        self.suites: dict[str, list[str]] = {}
        self.manifests: dict[tuple[str, str], dict] = {}

    def add_suite(self, suite_id: str, case_ids: list[str]) -> None:
        self.suites[suite_id] = list(case_ids)

    def add_run(self, run_id, *, suite_id="suite-1", status="succeeded", trials=3,
                agent_id="agent", versions=None, git_sha="deadbeef"):
        self.runs[run_id] = {
            "id": run_id, "suite_id": suite_id, "suite": f"name-{suite_id}",
            "suite_adapter": "local", "suite_config": {"path": "x.json", "version": "1"},
            "agent_id": agent_id, "mode": "measurement", "status": status,
            "trials_per_case": trials, "cache_mode": "off", "git_sha": git_sha,
        }
        self.versions[run_id] = [
            {"agent_version": v, "status": s, "cases_total": 0, "cases_done": 0}
            for v, s in (versions or {}).items()
        ]

    def add_results(self, run_id, version, rows):
        self.results.setdefault((run_id, version), []).extend(rows)

    async def get_run(self, run_id):
        return self.runs.get(str(run_id))

    async def get_run_versions(self, run_id):
        # Same order as PgTriageStore: by hash.
        return sorted(self.versions.get(str(run_id), []), key=lambda v: v["agent_version"])

    async def get_run_versions_page(self, run_id, limit, offset):
        rows = await self.get_run_versions(run_id)
        return rows[offset: offset + limit], len(rows)

    async def get_suite_case_ids(self, suite_id):
        return list(self.suites.get(str(suite_id), []))

    async def get_results(self, run_id, agent_version):
        return list(self.results.get((str(run_id), agent_version), []))

    async def get_manifest(self, agent_id, agent_version):
        return self.manifests.get((agent_id, agent_version))


def row(case_id, trial, *, passed=True, f1=None, status="ok", cost=0.01, latency=100,
        error=None, trace_id=None):
    scores = {} if f1 is None else {"TokenF1": {"passed": f1 > 0.5, "score": f1, "detail": {}}}
    if status != "ok":  # what the worker records for an error/timeout unit
        return {"case_id": case_id, "trial": trial, "status": status, "passed": False,
                "scores": {}, "cost_usd": 0.0, "latency_ms": latency,
                "error": error or "boom", "trace_id": None}
    return {"case_id": case_id, "trial": trial, "status": "ok", "passed": passed,
            "scores": scores, "cost_usd": cost, "latency_ms": latency, "error": None,
            "trace_id": trace_id or uuid.uuid4().hex}


def f1_rows(f1_by_case: dict[str, list[float]], **kw) -> list[dict]:
    return [row(c, t, f1=v, passed=v > 0.5, **kw)
            for c, vals in f1_by_case.items() for t, v in enumerate(vals)]


def f1_shift_pair(n_cases=40, trials=3, shift=-0.04, seed=0):
    """Baseline F1 per (case, trial) and a candidate shifted by ``shift`` with small paired
    noise; pass/fail is unchanged (F1 stays above 0.5) so only TokenF1 moves."""
    rng = np.random.default_rng(seed)
    base, cand = {}, {}
    for i in range(n_cases):
        b = [float(x) for x in rng.uniform(0.65, 0.95, trials)]
        base[f"q{i:03d}"] = b
        cand[f"q{i:03d}"] = [x + shift + float(rng.normal(0, 0.005)) for x in b]
    return base, cand


def store_pair(base_rows, cand_rows, *, cases, trials=3, b_status="succeeded",
               c_status="succeeded"):
    """Two single-version runs on one suite."""
    s = FakeStore()
    s.add_suite("suite-1", cases)
    b, c = rid(), rid()
    s.add_run(b, trials=trials, status=b_status, versions={V1: b_status})
    s.add_run(c, trials=trials, status=c_status, versions={V2: c_status})
    s.add_results(b, V1, base_rows)
    s.add_results(c, V2, cand_rows)
    return s, b, c
