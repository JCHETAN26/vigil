"""DB-free tests of the triage endpoints (/compare, /compare/cases, /versions/diff) and of the
CLI ``regress`` command, both driven by the same in-memory fake store. The TestClient is used
WITHOUT its context manager so the lifespan (Postgres pool) never runs."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from triage_fakes import V1, V2, V3, FakeStore, f1_rows, f1_shift_pair, rid, row, store_pair

from engine import cli
from engine.api.app import app
from engine.api.clickhouse import ClickHouseError


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
