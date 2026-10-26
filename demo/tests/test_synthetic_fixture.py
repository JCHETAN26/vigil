"""The SYNTHETIC demo fixture, offline: deterministic, well formed, and the production gate gives
each scenario the verdict it is meant to show. No database, no network, no model."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import seed
import synthetic_fixture as sf
from engine.manifests import diff_manifests

from engine import gate


@pytest.fixture(scope="module")
def fx() -> sf.Fixture:
    return sf.build()


def _dump(f: sf.Fixture) -> str:
    return json.dumps([f.suite, f.cases, f.manifests, f.runs, f.run_versions, f.results,
                       f.spans], sort_keys=True, default=str)


def test_build_is_deterministic_and_small(fx):
    assert _dump(sf.build()) == _dump(fx)
    assert len(fx.cases) == 12 and len(fx.runs) == 3 and len(fx.results) == 68
    assert len(_dump(fx)) < 1_000_000  # KBs, not a corpus


def test_fixture_is_well_formed(fx):
    assert sf.validate(fx) == []


def test_every_name_and_note_says_synthetic(fx):
    assert fx.suite["name"].startswith(sf.PREFIX) and fx.suite["config"]["synthetic"] is True
    assert all(r["agent_id"].startswith(sf.PREFIX) for r in fx.runs)
    assert all(r["notes"].startswith("SYNTHETIC") and "no model was called" in r["notes"]
               for r in fx.runs)
    assert {m["model"] for m in fx.manifests} == {"no-model-synthetic-fixture"}
    # No invented usage or spend anywhere: results leave tokens/cost/latency unset, spans are 0.
    assert all(r.get("cost_usd") is None and r.get("latency_ms") is None for r in fx.results)
    assert all(s["cost_usd"] == 0 and s["gen_ai_usage_input_tokens"] == 0 for s in fx.spans)


@pytest.mark.parametrize("scn", sf.SCENARIOS, ids=lambda s: s.key)
def test_production_gate_computes_each_scenario_verdict(fx, scn):
    cmp = gate.compare(seed.offline_inputs(fx, scn))
    assert cmp.verdict == scn.expected_verdict, cmp.incomplete_reasons
    gated = {m["metric"]: m for m in cmp.metrics if m["gated"]}
    assert set(gated) == {"passed", "TokenF1"}  # quality was measured on both sides
    if scn.key == "regression":
        assert cmp.complete and gated["passed"]["regressed"]
        affected = [c["case_id"] for c in gate.case_deltas(seed.offline_inputs(fx, scn), "passed")
                    if (c["adverse_delta"] or 0) > 0]
        assert affected == ["syn-q02", "syn-q04", "syn-q07", "syn-q09", "syn-q11"]
    elif scn.key == "no_regression":
        assert cmp.complete and not any(m["regressed"] for m in gated.values())
    else:  # incomplete: really incomplete under the gate, and no gated regression either
        assert not cmp.complete and not cmp.gated_regression
        reasons = " | ".join(cmp.incomplete_reasons)
        assert "run status is 'aborted'" in reasons and "missing 4 suite case(s)" in reasons
        assert cmp.coverage["candidate"]["statuses"] == {"ok": 7, "timeout": 1}


def test_version_hashes_agree_with_manifests_and_diffs_show_the_change(fx):
    by_key = {k: next(m for m in fx.manifests if m["agent_version"] == sf.version_hash(k))
              for k in sf.VERSION_CONFIGS}
    assert len({m["agent_version"] for m in by_key.values()}) == 4
    changed = {k: sorted(f for f, d in diff_manifests(by_key["base"], by_key[k]).items()
                         if d["changed"]) for k in ("top_k1", "reworded", "max_steps2")}
    assert changed == {"top_k1": ["params", "tools"], "reworded": ["prompts"],
                       "max_steps2": ["params"]}


def test_traces_are_complete_and_drillable(fx):
    by_trace: dict[str, list[dict]] = {}
    for s in fx.spans:
        assert set(s) == set(seed.SPAN_COLUMNS)
        by_trace.setdefault(s["trace_id"], []).append(s)
    for r in fx.results:
        names = [s["span_name"] for s in by_trace[r["trace_id"]]]
        if r["status"] == "ok":
            assert names == ["agent.run", f"gen_ai.chat {sf.MODEL}", "search",
                             f"gen_ai.chat {sf.MODEL}"]
            search = by_trace[r["trace_id"]][2]
            assert json.loads(search["span_attributes"]["vigil.retrieval.doc_ids"])
            assert by_trace[r["trace_id"]][3]["events.name"] == ["gen_ai.choice"]
        else:
            assert by_trace[r["trace_id"]][0]["status_code"] == "ERROR"
    # The launcher's canned trace link is a candidate trace of an affected case.
    tid = seed.first_affected_trace(fx)
    assert tid in by_trace and by_trace[tid][0]["eval_case_id"] == "syn-q02"


def test_links_json_is_offline_and_derived_from_the_fixture(fx):
    # What `make demo-smoke` reads: with no DB settings or keys in the environment, it still works.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    out = subprocess.run([sys.executable, str(Path(seed.__file__)), "links", "--json"],
                         env=env, capture_output=True, text=True, check=True)
    plan = json.loads(out.stdout)
    assert plan["synthetic"] is True and plan["run_ids"] == [r["id"] for r in fx.runs]
    for scn, got in zip(sf.SCENARIOS, plan["scenarios"], strict=True):
        assert got["compare_path"] == seed.compare_path(scn)
        assert (got["baseline_version"], got["candidate_version"]) == (
            sf.version_hash(scn.baseline), sf.version_hash(scn.candidate))
        assert got["offline_verdict"] == got["expected_verdict"] == scn.expected_verdict
    # The chart check depends on these signs: the regressions hang below zero, the pass above.
    signs = {s["key"]: s["first_chart_metric"]["point_estimate"] > 0 for s in plan["scenarios"]}
    assert signs == {"regression": False, "no_regression": True, "incomplete": False}
    assert plan["affected"]["case_id"] == "syn-q02"
    assert plan["affected"]["trace_id"] == seed.first_affected_trace(fx)
    assert len(plan["affected"]["retrieved_doc_ids"]) == 1  # top_k 1


def test_links_requires_the_json_flag():
    with pytest.raises(SystemExit):
        seed.main(["links"])


def _broken(fx, mutate) -> list[str]:
    f = copy.deepcopy(fx)
    mutate(f)
    return sf.validate(f)


@pytest.mark.parametrize("mutate, expect", [
    (lambda f: f.manifests[0]["params"].update(retrieval_top_k=9), "does not hash"),
    (lambda f: f.suite.update(name="demo-qa"), "vigil-demo-synthetic"),
    (lambda f: f.runs[0].update(notes="baseline run"), "notes must start with SYNTHETIC"),
    (lambda f: f.runs[0].update(id="0b1e0000-0000-4000-8000-000000000101"), "reserved"),
    (lambda f: f.results.append(dict(f.results[0])), "duplicate result"),
    (lambda f: f.spans[1].update(parent_span_id="f" * 16), "parent outside its trace"),
    (lambda f: f.spans[1].update(cost_usd=0.01), "invented usage"),
    (lambda f: f.spans[1].update(gen_ai_request_model="claude-haiku-4-5"), "names a model"),
    (lambda f: f.cases[0]["input"].update(api_key="x"), "secret-like"),
    (lambda f: f.spans[0]["span_attributes"].update(note="Bearer abc"), "secret-like"),
])
def test_validate_rejects_malformed_fixtures(fx, mutate, expect):
    assert any(expect in p for p in _broken(fx, mutate))
