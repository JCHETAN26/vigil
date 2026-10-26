"""DB-free tests of the triage endpoints (/compare, /compare/cases, /versions/diff) and of the
CLI ``regress`` command, both driven by the same in-memory fake store. The TestClient is used
WITHOUT its context manager so the lifespan (Postgres pool) never runs."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from triage_fakes import (
    V1,
    V2,
    V3,
    FakeStore,
    block_scipy,
    f1_rows,
    f1_shift_pair,
    rid,
    row,
    store_pair,
)

from engine import cli, gate
from engine.api.app import app
from engine.api.clickhouse import ClickHouseError
from engine.regression import StatsDependencyError


@pytest.fixture
def use_store(monkeypatch):
    """Point both the API and the CLI at a given fake store."""

    def _use(store):
        monkeypatch.setattr(app.state, "triage", store, raising=False)

        @asynccontextmanager
        async def fake_store():
            yield store

        monkeypatch.setattr(cli, "_triage_store", fake_store)
        return TestClient(app)

    return _use


def _f1_pair(shift=-0.04):
    base, cand = f1_shift_pair(shift=shift)
    return store_pair(f1_rows(base), f1_rows(cand), cases=list(base))


# --- CLI and API reach identical decisions ---


def test_cli_and_api_identical_decision_on_four_point_f1_drop(use_store, tmp_path, capsys):
    store, b, c = _f1_pair()
    client = use_store(store)
    api = client.get("/compare", params={"baseline": b, "candidate": c}).json()

    out = tmp_path / "bundle"
    rc = cli.main(["regress", "--baseline", b, "--candidate", c, "--save", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    saved = json.loads((out / "verdict.json").read_text())

    assert api["verdict"] == saved["verdict"] == "regression"
    assert api["gated_regression"] is saved["gated_regression"] is True
    assert api["policy"] == saved["policy"]
    assert api["metrics"] == saved["metrics"]
    assert api["coverage"] == saved["coverage"]
    assert api["context"] == {"baseline": saved["baseline"], "candidate": saved["candidate"]}
    assert "VERDICT: REGRESSION on a gated metric" in printed
    # Legacy response shape preserved.
    assert api["baseline"] == b and api["candidate"] == c
    assert api["method"] == "t" and api["alpha"] == 0.05 and api["paired_cases"] == 40
    legacy = {"metric", "gated", "point_estimate", "ci_bound", "ci_excludes_zero",
              "beyond_threshold", "regressed", "direction", "tail_prob", "n_questions"}
    assert all(legacy <= set(m) for m in api["metrics"])


def test_threshold_overrides_equivalent_in_cli_and_api(use_store, tmp_path, capsys):
    store, b, c = _f1_pair()
    client = use_store(store)
    api = client.get("/compare", params={"baseline": b, "candidate": c, "threshold_f1": 0.05,
                                         "n_boot": 2000, "seed": 7}).json()
    out = tmp_path / "b2"
    cli.main(["regress", "--baseline", b, "--candidate", c, "--threshold-f1", "0.05",
              "--n-boot", "2000", "--seed", "7", "--save", str(out)])
    saved = json.loads((out / "verdict.json").read_text())
    assert api["policy"]["thresholds"]["TokenF1"] == 0.05
    assert api["policy"] == saved["policy"] and api["metrics"] == saved["metrics"]
    assert api["verdict"] == saved["verdict"] == "no_regression"
    assert "VERDICT: no gated regression" in capsys.readouterr().out


def test_cli_exit_codes_and_incomplete_verdict(use_store, capsys):
    cases = [f"q{i}" for i in range(10)]
    store, b, c = store_pair([row(x, 0) for x in cases], [row(x, 0) for x in cases[:6]],
                             cases=cases, trials=1, c_status="aborted")
    use_store(store)
    assert cli.main(["regress", "--baseline", b, "--candidate", c]) == 0  # default: always 0
    assert "VERDICT: INCOMPLETE" in capsys.readouterr().out
    assert cli.main(["regress", "--baseline", b, "--candidate", c, "--exit-code"]) == 3
    store2, b2, c2 = _f1_pair()
    use_store(store2)
    assert cli.main(["regress", "--baseline", b2, "--candidate", c2, "--exit-code"]) == 2


def test_no_gated_metric_is_incomplete_in_cli_api_and_saved_bundle(use_store, tmp_path, capsys):
    # OK trials with no pass/fail verdict and no gated score on both sides: only cost/latency
    # are comparable. API, CLI exit code and the saved bundle must all say incomplete — the CLI
    # used to exit 0 ("no gated regression") while the dashboard showed INCOMPLETE.
    cases = [f"q{i}" for i in range(20)]
    unjudged = lambda c, **kw: {**row(c, 0, **kw), "passed": None, "scores": {}}  # noqa: E731
    store, b, c = store_pair([unjudged(x) for x in cases], [unjudged(x, cost=0.5) for x in cases],
                             cases=cases, trials=1)
    client = use_store(store)
    api = client.get("/compare", params={"baseline": b, "candidate": c}).json()
    assert api["verdict"] == "incomplete" and api["complete"] is False
    assert api["gated_regression"] is False
    assert not any(m["gated"] for m in api["metrics"])
    assert any(r.startswith("no gated metric was compared") for r in api["incomplete_reasons"])

    out = tmp_path / "bundle"
    assert cli.main(["regress", "--baseline", b, "--candidate", c, "--exit-code",
                     "--save", str(out)]) == 3
    assert "VERDICT: INCOMPLETE" in capsys.readouterr().out
    saved = json.loads((out / "verdict.json").read_text())
    assert saved["verdict"] == api["verdict"] and saved["complete"] is False
    assert saved["incomplete_reasons"] == api["incomplete_reasons"]
    cases_view = client.get("/compare/cases", params={"baseline": b, "candidate": c}).json()
    assert cases_view["verdict"] == "incomplete"


def test_missing_scipy_fails_api_cli_and_gate_loudly(use_store, tmp_path, capsys, monkeypatch):
    # Without SciPy the default method 't' cannot compute its bound. Every entry point must say
    # so: no verdict computed from a different statistic, no metric quietly skipped as
    # "incomplete", no bundle written. Percentile still works, since it never needs SciPy.
    store, b, c = _f1_pair()
    client = use_store(store)
    block_scipy(monkeypatch)

    resp = client.get("/compare", params={"baseline": b, "candidate": c})
    assert resp.status_code == 503 and "needs SciPy" in resp.json()["detail"]
    cases = client.get("/compare/cases", params={"baseline": b, "candidate": c})
    assert cases.status_code == 503

    out = tmp_path / "bundle"
    assert cli.main(["regress", "--baseline", b, "--candidate", c, "--exit-code",
                     "--save", str(out)]) == 1
    captured = capsys.readouterr()
    assert "needs SciPy" in captured.err and "VERDICT" not in captured.out
    assert not out.exists()

    inputs = asyncio.run(gate.load_inputs(store, b, c))
    with pytest.raises(StatsDependencyError):
        gate.compare(inputs)
    pct = client.get("/compare", params={"baseline": b, "candidate": c, "method": "percentile"})
    assert pct.status_code == 200 and pct.json()["verdict"] == "regression"


def test_cli_refuses_mixed_version_run_and_bad_ids(use_store, capsys):
    s = FakeStore()
    s.add_suite("suite-1", ["q0"])
    r, other = rid(), rid()
    s.add_run(r, trials=1, versions={V1: "succeeded", V2: "succeeded"})
    s.add_run(other, trials=1, versions={V3: "succeeded"})
    use_store(s)
    assert cli.main(["regress", "--baseline", other, "--candidate", r]) == 1
    assert "never averaged" in capsys.readouterr().err
    assert cli.main(["regress", "--baseline", "nope", "--candidate", r]) == 1


def test_cli_alternate_uuid_spelling_cannot_compare_a_run_with_itself(use_store, capsys):
    store, b, c = _f1_pair()
    use_store(store)
    for spelling in (b.upper(), "{" + b + "}", b.replace("-", "")):
        assert cli.main(["regress", "--baseline", spelling, "--candidate", b]) == 1
        assert "same run and agent version" in capsys.readouterr().err
    assert cli.main(["regress", "--baseline", b.upper(), "--candidate", c]) == 0
    assert f"vs baseline {b[:12]}" in capsys.readouterr().out  # canonical id in the report


def test_cli_save_refuses_existing_target(use_store, tmp_path, capsys):
    store, b, c = _f1_pair()
    use_store(store)
    target = tmp_path / "exists"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    assert cli.main(["regress", "--baseline", b, "--candidate", c, "--save", str(target)]) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert [p.name for p in target.iterdir()] == ["keep.txt"]


# --- /compare validation + error mapping ---


def test_compare_error_mapping(use_store):
    s = FakeStore()
    s.add_suite("suite-1", ["q0"])
    s.add_suite("suite-2", ["q0"])
    a, b, mixed = rid(), rid(), rid()
    s.add_run(a, suite_id="suite-1", versions={V1: "succeeded"})
    s.add_run(b, suite_id="suite-2", versions={V2: "succeeded"})
    s.add_run(mixed, suite_id="suite-1", versions={V1: "succeeded", V2: "succeeded"})
    client = use_store(s)
    get = lambda **p: client.get("/compare", params=p)  # noqa: E731
    assert get(baseline=a, candidate=b).status_code == 400  # different suites
    assert get(baseline=a, candidate=rid()).status_code == 404
    r = get(baseline=a, candidate=mixed)
    assert r.status_code == 400 and "never averaged" in r.json()["detail"]
    assert get(baseline=a, candidate=mixed, candidate_version=V3).status_code == 404
    assert get(baseline=a, candidate=a).status_code == 400  # identical
    # Parameter validation happens before any store access.
    assert get(baseline=a, candidate=mixed, candidate_version="XYZ").status_code == 422
    assert get(baseline=a, candidate=mixed, candidate_version="abc").status_code == 422
    assert get(baseline="not-a-uuid", candidate=a).status_code == 422
    for bad in ({"alpha": 0.6}, {"threshold_f1": -1}, {"n_boot": 50}, {"method": "x"}):
        assert get(baseline=a, candidate=mixed, **bad).status_code == 422, bad


def test_compare_explicit_version_from_mixed_run(use_store):
    s = FakeStore()
    s.add_suite("suite-1", ["q0", "q1"])
    r = rid()
    s.add_run(r, trials=1, versions={V1: "succeeded", V2: "succeeded"})
    s.add_results(r, V1, [row("q0", 0), row("q1", 0)])
    s.add_results(r, V2, [row("q0", 0, passed=False), row("q1", 0, passed=False)])
    client = use_store(s)
    body = client.get("/compare", params={"baseline": r, "candidate": r,
                                          "baseline_version": V1[:8],
                                          "candidate_version": V2}).json()
    assert body["baseline_version"] == V1 and body["candidate_version"] == V2
    assert body["context"]["baseline"]["versions_in_run"] == [V1, V2]
    assert next(m for m in body["metrics"] if m["metric"] == "passed")["point_estimate"] == -1.0


# --- /runs/{id}/versions (version discovery for the dashboard picker) ---


def test_run_versions_lists_registered_versions_with_and_without_results(use_store):
    s = FakeStore()
    s.add_suite("suite-1", ["q0"])
    r = rid()
    # V3 is registered but has no case results yet; V1 only appears in results (the store's
    # fallback rows carry status None). Order is by hash, never by role.
    s.add_run(r, trials=1, versions={V3: "pending", V2: "succeeded"})
    s.versions[r].append({"agent_version": V1, "status": None, "cases_total": None,
                          "cases_done": None})
    s.add_results(r, V2, [row("q0", 0)])
    s.add_results(r, V1, [row("q0", 0)])
    body = use_store(s).get(f"/runs/{r}/versions").json()
    assert body["run_id"] == r and body["total"] == 3
    assert body["limit"] == 50 and body["offset"] == 0
    assert [v["agent_version"] for v in body["versions"]] == [V1, V2, V3]  # full 64-char hashes
    assert body["versions"][0] == {"agent_version": V1, "registered": False, "status": None,
                                   "cases_total": None, "cases_done": None}
    assert body["versions"][2]["registered"] is True and body["versions"][2]["status"] == "pending"


def test_run_versions_paging_is_bounded_and_stable(use_store):
    s = FakeStore()
    r = rid()
    versions = [f"{i:02x}" * 32 for i in range(7)]
    s.add_run(r, versions={v: "succeeded" for v in reversed(versions)})
    client = use_store(s)
    pages = [client.get(f"/runs/{r}/versions", params={"limit": 3, "offset": o}).json()
             for o in (0, 3, 6, 9)]
    assert [len(p["versions"]) for p in pages] == [3, 3, 1, 0]
    assert all(p["total"] == 7 for p in pages)
    assert [v["agent_version"] for p in pages for v in p["versions"]] == versions
    for bad in ({"limit": 0}, {"limit": 201}, {"offset": -1}):
        assert client.get(f"/runs/{r}/versions", params=bad).status_code == 422, bad


def test_run_versions_empty_unknown_and_malformed_runs(use_store):
    s = FakeStore()
    r = rid()
    s.add_run(r, versions={})
    client = use_store(s)
    body = client.get(f"/runs/{r}/versions").json()
    assert body["versions"] == [] and body["total"] == 0
    assert client.get(f"/runs/{rid()}/versions").status_code == 404
    assert client.get("/runs/not-a-uuid/versions").status_code == 422


def test_run_versions_is_read_only(use_store):
    """GET only (CORS and routing), and the handler touches nothing but the store's reads."""
    s = FakeStore()
    r = rid()
    s.add_run(r, versions={V1: "succeeded"})
    before = (dict(s.runs), {k: list(v) for k, v in s.versions.items()})
    client = use_store(s)
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)(f"/runs/{r}/versions").status_code == 405, method
    assert client.get(f"/runs/{r}/versions").status_code == 200
    assert (s.runs, s.versions) == before


def test_discovered_versions_select_each_side_of_a_same_run_compare(use_store):
    # The picker's flow end to end on the API: list the run's versions, pass two full hashes.
    s = FakeStore()
    s.add_suite("suite-1", ["q0", "q1"])
    r = rid()
    s.add_run(r, trials=1, versions={V1: "succeeded", V2: "succeeded"})
    s.add_results(r, V1, [row("q0", 0), row("q1", 0)])
    s.add_results(r, V2, [row("q0", 0), row("q1", 0)])
    client = use_store(s)
    listed = [v["agent_version"] for v in client.get(f"/runs/{r}/versions").json()["versions"]]
    body = client.get("/compare", params={"baseline": r, "candidate": r,
                                          "baseline_version": listed[1],
                                          "candidate_version": listed[0]}).json()
    assert (body["baseline_version"], body["candidate_version"]) == (V2, V1)


# --- /compare/cases ---


def test_compare_cases_sorting_pagination_and_context(use_store):
    cases = [f"q{i}" for i in range(6)]
    base = [row(c, t, passed=True) for c in cases for t in range(2)]
    cand = [row(c, t, passed=(c not in ("q2", "q4")),
                status="error" if (c == "q4" and t == 0) else "ok")
            for c in cases[:5] for t in range(2)]
    store, b, c = store_pair(base, cand, cases=cases, trials=2, c_status="aborted")
    client = use_store(store)
    body = client.get("/compare/cases", params={"baseline": b, "candidate": c,
                                                "limit": 2}).json()
    assert body["sort_metric"] == "passed" and body["sort_direction"] == "higher_is_better"
    assert body["total"] == 6 and len(body["cases"]) == 2
    assert [x["case_id"] for x in body["cases"]] == ["q2", "q4"]
    assert body["complete"] is False  # aborted candidate, q5 missing
    summary = client.get("/compare", params={"baseline": b, "candidate": c}).json()
    for k in ("verdict", "policy", "context", "coverage", "incomplete_reasons",
              "baseline_version"):
        assert body[k] == summary[k], k
    q4 = body["cases"][1]
    assert q4["candidate"]["statuses"] == {"error": 1, "ok": 1}
    assert q4["candidate"]["trials"][0]["error"] == "boom"
    assert all(t["trace_id"] for t in q4["baseline"]["trials"])

    page3 = client.get("/compare/cases", params={"baseline": b, "candidate": c, "limit": 2,
                                                 "offset": 4}).json()["cases"]
    assert [x["case_id"] for x in page3] == ["q3", "q5"] and page3[-1]["paired"] is False
    only = lambda f: client.get("/compare/cases", params={  # noqa: E731
        "baseline": b, "candidate": c, "filter": f}).json()
    assert [x["case_id"] for x in only("unpaired")["cases"]] == ["q5"]
    assert [x["case_id"] for x in only("failed")["cases"]] == ["q4"]
    assert [x["case_id"] for x in only("adverse")["cases"]] == ["q2", "q4"]
    p = {"baseline": b, "candidate": c}
    assert client.get("/compare/cases", params=p | {"limit": 201}).status_code == 422
    assert client.get("/compare/cases", params=p | {"metric": "nope"}).status_code == 400
    assert client.get("/compare/cases", params=p | {"metric": "a b"}).status_code == 422
    assert client.get("/compare/cases", params=p | {"filter": "x"}).status_code == 422


def test_compare_cases_defaults_to_first_gated_metric_present(use_store):
    store, b, c = _f1_pair()
    client = use_store(store)
    body = client.get("/compare/cases", params={"baseline": b, "candidate": c,
                                                "metric": "TokenF1"}).json()
    assert body["sort_metric"] == "TokenF1"
    assert body["metrics_available"][:2] == ["passed", "TokenF1"]
    deltas = [x["adverse_delta"] for x in body["cases"]]
    assert deltas == sorted(deltas, reverse=True) and deltas[0] > 0


def test_compare_cases_with_no_results_is_empty_and_incomplete(use_store):
    store, b, c = store_pair([], [], cases=["q0", "q1"], trials=1)
    client = use_store(store)
    resp = client.get("/compare/cases", params={"baseline": b, "candidate": c})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cases"] == [] and body["total"] == 0
    assert body["metrics_available"] == [] and body["sort_metric"] == "passed"
    assert body["verdict"] == "incomplete" and body["complete"] is False
    summary = client.get("/compare", params={"baseline": b, "candidate": c}).json()
    assert summary["verdict"] == "incomplete"


# --- /versions/diff ---


def _manifest(version, **over):
    m = {"agent_id": "agent", "agent_version": version, "git_sha": "1111",
         "model": "claude-haiku-5-5", "params": {"max_tokens": 512, "temperature": 0.0},
         "prompts": {"system": "be brief"}, "tools": [{"name": "search"}], "code_ref": None,
         "created_at": None}
    m.update(over)
    return m


def test_versions_diff_fields_and_redaction(use_store):
    s = FakeStore()
    s.add_suite("suite-1", ["q0"])
    r = rid()
    s.add_run(r, trials=1, versions={V1: "succeeded", V2: "succeeded"})
    s.manifests[("agent", V1)] = _manifest(V1, params={"max_tokens": 512, "temperature": 0.0,
                                                       "api_key": "sk-old"})
    s.manifests[("agent", V2)] = _manifest(
        V2, git_sha="2222", params={"max_tokens": 1024, "temperature": 0.0, "top_k": 5,
                                    "api_key": "sk-new"},
        prompts={"system": "be thorough"})
    client = use_store(s)
    body = client.get("/versions/diff", params={"baseline": r, "candidate": r,
                                                "baseline_version": V1,
                                                "candidate_version": V2}).json()
    f = body["fields"]
    assert f["model"] == {"changed": False}
    assert f["git_sha"] == {"changed": True}
    assert f["params"] == {"changed": True, "added": ["top_k"], "removed": [],
                           "modified": ["api_key", "max_tokens"]}
    assert f["prompts"]["modified"] == ["system"] and f["tools"] == {"changed": False}
    assert body["identical"] is False and body["missing_manifests"] == []
    assert body["baseline_manifest"]["params"]["api_key"] == "[redacted]"
    assert "sk-" not in json.dumps(body)
    assert body["redaction"]["redacted_paths"]["candidate"] == ["params.api_key"]
    assert body["baseline_manifest"]["params"]["max_tokens"] == 512  # not a credential
    assert set(body["baseline_manifest"]) == {"model", "git_sha", "params", "prompts", "tools",
                                              "code_ref", "created_at"}
    # Same selection rules as /compare: a mixed run needs an explicit version.
    assert client.get("/versions/diff", params={"baseline": r, "candidate": r}).status_code == 400


def test_versions_diff_missing_manifest_is_unknown_not_changed(use_store):
    s = FakeStore()
    s.add_suite("suite-1", ["q0"])
    s.add_suite("suite-2", ["q0"])
    a, b = rid(), rid()
    s.add_run(a, suite_id="suite-1", versions={V1: "succeeded"})
    s.add_run(b, suite_id="suite-2", versions={V2: "succeeded"})
    s.manifests[("agent", V1)] = _manifest(V1, prompts={"system": "x" * 30_000})
    body = use_store(s).get("/versions/diff", params={"baseline": a, "candidate": b}).json()
    assert body["missing_manifests"] == ["candidate"] and body["identical"] is None
    assert body["same_suite"] is False
    assert all(v == {"changed": None} for v in body["fields"].values())
    assert len(body["baseline_manifest"]["prompts"]["system"]) == 20_000
    assert body["redaction"]["truncated_paths"]["baseline"] == ["prompts.system"]


# --- /traces maps ClickHouse failures to 502 (no network) ---


class _FailingCH:
    def query(self, sql, params=None):
        raise ClickHouseError("clickhouse unreachable: refused")


def test_trace_endpoint_maps_clickhouse_error_to_502(monkeypatch):
    monkeypatch.setattr(app.state, "ch", _FailingCH(), raising=False)
    r = TestClient(app).get(f"/traces/{'0' * 32}")
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


class _BlockingCH:
    """Blocks inside query() (like urllib waiting out its timeout) until released."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread: int | None = None

    def query(self, sql, params=None):
        self.thread = threading.get_ident()
        self.entered.set()
        assert self.release.wait(10), "test never released the blocked ClickHouse query"
        raise ClickHouseError("clickhouse unreachable: timed out")


async def test_slow_clickhouse_does_not_block_other_requests(monkeypatch):
    # Regression: get_trace called the synchronous ClickHouse client on the event loop, so one
    # stuck trace request (up to the 15 s timeout) stalled every API request.
    ch = _BlockingCH()
    monkeypatch.setattr(app.state, "ch", ch, raising=False)
    loop_thread = threading.get_ident()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        trace = asyncio.create_task(client.get(f"/traces/{'0' * 32}"))
        assert await asyncio.to_thread(ch.entered.wait, 5)
        health = await asyncio.wait_for(client.get("/healthz"), timeout=2)
        assert health.status_code == 200 and not trace.done()
        ch.release.set()
        resp = await asyncio.wait_for(trace, timeout=5)
    assert resp.status_code == 502 and "timed out" in resp.json()["detail"]
    assert ch.thread is not None and ch.thread != loop_thread  # ran in the threadpool
