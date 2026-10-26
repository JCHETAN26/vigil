"""DB-free tests of the run-detail endpoints (/runs/{id}, /runs/{id}/cases) on a multi-version run,
where the same (case_id, trial) exists once per agent_version. The TestClient is used WITHOUT its
context manager so the lifespan (Postgres pool) never runs; the module's query helpers are
replaced by a fake that honours the SQL's ORDER BY / LIMIT / OFFSET and, like Postgres, returns
ORDER BY ties in arbitrary order."""

from __future__ import annotations

import random
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from engine.api import app as app_module
from engine.api.app import app

RUN = uuid.uuid4()
V1, V2 = "a1" * 32, "b2" * 32


def _result(version: str, case_id: str, trial: int, *, passed: bool) -> dict:
    return {
        "agent_version": version, "case_id": case_id, "trial": trial, "status": "ok",
        "passed": passed, "score": 1.0 if passed else 0.0, "scores": None, "cost_usd": 0.01,
        "input_tokens": 10, "output_tokens": 5, "latency_ms": 100,
        "trace_id": f"{version[:2]}{case_id}{trial}".ljust(32, "0"), "tags": None,
    }


# Two versions × two cases × two trials: every (case_id, trial) appears twice.
RESULTS = [
    _result(v, c, t, passed=(v == V1))
    for v in (V1, V2) for c in ("case-a", "case-b") for t in (0, 1)
]


class FakeDb:
    """Answers the three queries the run-detail endpoints issue against ``RESULTS``."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)

    async def fetchone(self, sql: str, params: tuple = ()):
        if sql.startswith("SELECT 1 FROM eval_runs"):
            return {"?column?": 1} if params[0] == RUN else None
        if "FROM eval_runs r JOIN eval_suites" in sql:
            return {"id": RUN, "suite": "s", "agent_id": "agent", "mode": "full",
                    "trials_per_case": 2, "status": "succeeded", "cost_spent_usd": 0.08,
                    "created_at": None, "notes": "SYNTHETIC fixture run"}
        ok = [r for r in RESULTS if r["status"] == "ok"]
        return {"n": len(RESULTS), "n_ok": len(ok),
                "pass_rate": sum(r["passed"] for r in ok) / len(ok),
                "mean_cost": 0.01, "mean_latency": 100.0,
                "n_versions": len({r["agent_version"] for r in RESULTS})}

    async def fetchall(self, sql: str, params: tuple = ()):
        assert "FROM eval_case_results" in sql
        keys = [k.strip().removeprefix("r.") for k in
                re.search(r"ORDER BY (.+?) LIMIT", sql).group(1).split(",")]
        _, limit, offset = params
        rows = list(RESULTS)
        self.rng.shuffle(rows)  # ties come back in arbitrary order, as in Postgres
        rows.sort(key=lambda r: tuple(r[k] for k in keys))
        return rows[offset: offset + limit]


@pytest.fixture(params=range(5))
def client(request, monkeypatch):
    db = FakeDb(seed=request.param)
    monkeypatch.setattr(app_module, "_fetchone", db.fetchone)
    monkeypatch.setattr(app_module, "_fetchall", db.fetchall)
    return TestClient(app)


def _identity(c: dict) -> tuple:
    return (c["agent_version"], c["case_id"], c["trial"])


def test_cases_carry_agent_version_so_repeated_case_trials_are_distinguishable(client):
    cases = client.get(f"/runs/{RUN}/cases", params={"limit": 100}).json()["cases"]
    assert len(cases) == len(RESULTS)
    assert len({_identity(c) for c in cases}) == len(cases)
    same_case_trial = [c for c in cases if (c["case_id"], c["trial"]) == ("case-a", 0)]
    assert {c["agent_version"] for c in same_case_trial} == {V1, V2}
    # The versions' rows differ, and each keeps its own outcome and trace.
    by_version = {c["agent_version"]: c for c in same_case_trial}
    assert by_version[V1]["passed"] is True and by_version[V2]["passed"] is False
    assert by_version[V1]["trace_id"] != by_version[V2]["trace_id"]


def test_cases_order_is_total_and_paging_returns_each_row_exactly_once(client):
    paged = []
    for offset in range(len(RESULTS)):  # one row per page: any tie would shuffle across pages
        page = client.get(f"/runs/{RUN}/cases", params={"limit": 1, "offset": offset}).json()
        paged += page["cases"]
    assert sorted(map(_identity, paged)) == sorted(map(_identity, RESULTS))
    # Grouped by case, then version, then trial.
    assert [_identity(c) for c in paged] == sorted(
        (_identity(c) for c in paged), key=lambda k: (k[1], k[0], k[2]))


def test_cases_response_keeps_legacy_fields(client):
    case = client.get(f"/runs/{RUN}/cases").json()["cases"][0]
    legacy = {"case_id", "trial", "status", "passed", "score", "scores", "cost_usd",
              "input_tokens", "output_tokens", "latency_ms", "trace_id", "tags"}
    assert set(case) == legacy | {"agent_version"}


def test_run_detail_reports_how_many_versions_its_pooled_aggregates_span(client):
    body = client.get(f"/runs/{RUN}").json()
    assert body["n_versions"] == 2
    assert body["n_cases"] == len(RESULTS)
    assert body["pass_rate"] == 0.5  # pooled across both versions, deliberately unchanged


def test_run_detail_returns_the_run_notes(client):
    # The demo fixture's SYNTHETIC label travels in eval_runs.notes; the API passes it through.
    assert client.get(f"/runs/{RUN}").json()["notes"] == "SYNTHETIC fixture run"


def test_unknown_run_cases_is_404(client):
    assert client.get(f"/runs/{uuid.uuid4()}/cases").status_code == 404
