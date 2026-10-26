"""Unit tests for the shared regression gate (engine/engine/gate.py): policy, explicit version
selection, the comparison's denominators and completeness rules, and per-case deltas. All
through the in-memory fake store — no database."""

from __future__ import annotations

import pytest
from triage_fakes import V1, V2, V3, FakeStore, f1_rows, f1_shift_pair, rid, row, store_pair

from engine import gate

CASES = [f"q{i:03d}" for i in range(40)]


async def _cmp(store, b, c, policy=gate.DEFAULT_POLICY, bv=None, cv=None):
    return gate.compare(await gate.load_inputs(store, b, c, bv, cv), policy)


def _metric(cmp, name):
    return next(m for m in cmp.metrics if m["metric"] == name)


# --- policy ---


def test_default_policy_matches_cli_defaults():
    p = gate.DEFAULT_POLICY.to_dict()
    assert p["thresholds"] == {"passed": 0.05, "TokenF1": 0.03}
    assert (p["alpha"], p["method"], p["n_boot"], p["seed"], p["two_level"]) == (
        0.05, "t", 10000, 1234, False)
    assert p["gated"] == ["passed", "TokenF1"]
    assert p["failed_trials"] == gate.FAILED_TRIAL_POLICY


def test_policy_overrides_and_roundtrip():
    p = gate.DEFAULT_POLICY.with_overrides(threshold_f1=0.05, alpha=0.1, method="bca")
    assert p.threshold("TokenF1") == 0.05 and p.threshold("passed") == 0.05
    assert p.threshold("cost_usd") == 0.0
    assert gate.Policy.from_dict(p.to_dict()) == p


@pytest.mark.parametrize("kw", [
    {"alpha": 0.0}, {"alpha": 0.5}, {"method": "nope"}, {"n_boot": 10},
    {"thresholds": {"passed": -0.1}}, {"thresholds": {"passed": float("nan")}},
])
def test_policy_validation(kw):
    with pytest.raises(gate.ComparisonError):
        gate.Policy(**kw)


def test_policy_from_dict_rejects_other_failed_trial_rule():
    d = gate.DEFAULT_POLICY.to_dict() | {"failed_trials": "drop"}
    with pytest.raises(gate.ComparisonError):
        gate.Policy.from_dict(d)


# --- version selection ---


def test_select_version_rules():
    assert gate.select_version("r", [V1], None) == (V1, "single_version_fallback")
    assert gate.select_version("r", [V1, V2], V2) == (V2, "explicit")
    assert gate.select_version("r", [V1, V2], V2[:8]) == (V2, "explicit")  # unique prefix
    with pytest.raises(gate.ComparisonError, match="never averaged"):
        gate.select_version("r", [V1, V2], None)  # mixed run, no selection
    with pytest.raises(gate.ComparisonError) as e:
        gate.select_version("r", [V1], V3)
    assert e.value.kind == "not_found"
    with pytest.raises(gate.ComparisonError, match="ambiguous"):
        gate.select_version("r", ["abcdef12" + "0" * 56, "abcdef12" + "1" * 56], "abcdef12")
    with pytest.raises(gate.ComparisonError):
        gate.select_version("r", [V1], "aaa")  # too short to be a safe prefix
    with pytest.raises(gate.ComparisonError) as e:
        gate.select_version("r", [], None)
    assert e.value.kind == "not_found"


async def test_multi_version_run_is_never_averaged():
    # One run holding two versions: V1 passes everything, V2 fails everything. Comparing the
    # run against itself version-by-version is a clean regression; asking without a version
    # is refused instead of averaging the two into one 50% series.
    s = FakeStore()
    s.add_suite("suite-1", CASES)
    r = rid()
    s.add_run(r, trials=1, versions={V1: "succeeded", V2: "succeeded"})
    s.add_results(r, V1, [row(c, 0, passed=True) for c in CASES])
    s.add_results(r, V2, [row(c, 0, passed=False) for c in CASES])
    other = rid()
    s.add_run(other, trials=1, versions={V3: "succeeded"})
    s.add_results(other, V3, [row(c, 0, passed=True) for c in CASES])

    with pytest.raises(gate.ComparisonError, match="2 agent versions"):
        await gate.load_inputs(s, other, r)
    cmp = await _cmp(s, r, r, bv=V1, cv=V2)
    assert cmp.verdict == "regression"
    assert _metric(cmp, "passed")["point_estimate"] == pytest.approx(-1.0)
    assert cmp.candidate.version_selection == "explicit"
    fallback = await _cmp(s, other, r, cv=V1)
    assert fallback.baseline.version_selection == "single_version_fallback"
    assert fallback.verdict == "no_regression"


async def test_incomparable_and_identical_and_missing():
    s = FakeStore()
    s.add_suite("suite-1", CASES)
    s.add_suite("suite-2", CASES)
    a, b = rid(), rid()
    s.add_run(a, suite_id="suite-1", versions={V1: "succeeded"})
    s.add_run(b, suite_id="suite-2", versions={V2: "succeeded"})
    with pytest.raises(gate.ComparisonError, match="different suites"):
        await gate.resolve_pair(s, a, b)
    with pytest.raises(gate.ComparisonError, match="same run and agent version"):
        await gate.resolve_pair(s, a, a)
    with pytest.raises(gate.ComparisonError) as e:
        await gate.resolve_pair(s, a, rid())
    assert e.value.kind == "not_found"
    # The diff endpoint's relaxed resolution accepts both.
    await gate.resolve_pair(s, a, b, require_same_suite=False, allow_identical=True)


# --- the specific 4-point F1 drop ---


async def test_four_point_f1_drop_flagged_under_shared_default_policy():
    base, cand = f1_shift_pair(shift=-0.04)
    s, b, c = store_pair(f1_rows(base), f1_rows(cand), cases=list(base))
    cmp = await _cmp(s, b, c)
    f1 = _metric(cmp, "TokenF1")
    assert f1["point_estimate"] == pytest.approx(-0.04, abs=0.005)
    assert f1["threshold"] == 0.03 and f1["ci_excludes_zero"] and f1["regressed"]
    assert _metric(cmp, "passed")["regressed"] is False
    assert cmp.verdict == "regression" and cmp.complete
    # The dashboard's old F1 threshold (0.05) would have called the same data a pass ("watch").
    old = await _cmp(s, b, c, gate.DEFAULT_POLICY.with_overrides(threshold_f1=0.05))
    assert _metric(old, "TokenF1")["status"] == "watch"
    assert old.verdict == "no_regression"


# --- denominators: failed / aborted / missing ---


async def test_failed_trials_stay_in_the_denominator():
    # Same agent quality, but the candidate errors on trial 0 of 20 of the 40 cases. Dropping
    # errors (the old status='ok' filter) would hide this; scoring them as failures flags it.
    base = [row(c, t, passed=True) for c in CASES for t in range(3)]
    cand = [row(c, t, status="error" if (t == 0 and i < 20) else "ok", passed=True)
            for i, c in enumerate(CASES) for t in range(3)]
    s, b, c = store_pair(base, cand, cases=CASES)
    cmp = await _cmp(s, b, c)
    assert cmp.coverage["candidate"]["failed_trials"] == 20
    assert cmp.coverage["candidate"]["statuses"] == {"error": 20, "ok": 100}
    passed = _metric(cmp, "passed")
    assert passed["point_estimate"] == pytest.approx(-20 / 3 / 40)
    assert passed["n_questions"] == 40  # every case still paired
    assert cmp.verdict == "regression"


async def test_case_whose_every_trial_failed_is_paired_as_zero_not_dropped():
    base, _ = f1_shift_pair(n_cases=10, shift=0.0)
    cand_rows = f1_rows(base)
    cand_rows = [r if r["case_id"] != "q000" else row("q000", r["trial"], status="timeout")
                 for r in cand_rows]
    s, b, c = store_pair(f1_rows(base), cand_rows, cases=list(base))
    inputs = await gate.load_inputs(s, b, c)
    series = gate.build_series(inputs.candidate_rows, gate.quality_metrics(
        inputs.baseline_rows, inputs.candidate_rows))
    assert series["TokenF1"]["q000"] == [0.0, 0.0, 0.0]
    assert series["passed"]["q000"] == [0.0, 0.0, 0.0]
    assert "q000" not in series["cost_usd"]  # cost/latency: OK trials only
    cmp = gate.compare(inputs)
    assert cmp.paired_cases == 10


async def test_aborted_run_with_missing_cases_is_incomplete_not_a_pass():
    base = [row(c, t) for c in CASES for t in range(3)]
    cand = [row(c, t) for c in CASES[:35] for t in range(3)]  # stopped after 35 cases
    s, b, c = store_pair(base, cand, cases=CASES, c_status="aborted")
    cmp = await _cmp(s, b, c)
    assert cmp.verdict == "incomplete" and not cmp.complete and not cmp.gated_regression
    assert cmp.paired_cases == 35
    assert cmp.coverage["only_in_baseline"] == CASES[35:]
    assert cmp.coverage["candidate"]["missing_cases"] == CASES[35:]
    text = " ".join(cmp.incomplete_reasons)
    assert "'aborted'" in text and "missing 5 suite case" in text and "unequal case sets" in text


async def test_regression_on_incomplete_data_is_still_a_regression():
    base = [row(c, t, passed=True) for c in CASES for t in range(3)]
    cand = [row(c, t, passed=False) for c in CASES[:30] for t in range(3)]
    s, b, c = store_pair(base, cand, cases=CASES, c_status="aborted")
    cmp = await _cmp(s, b, c)
    assert cmp.verdict == "regression" and not cmp.complete


async def test_short_trials_and_succeeded_status_required():
    base = [row(c, t) for c in CASES for t in range(3)]
    cand = [row(c, t) for c in CASES for t in range(3) if not (c == "q005" and t == 2)]
    s, b, c = store_pair(base, cand, cases=CASES)
    cmp = await _cmp(s, b, c)
    assert cmp.verdict == "incomplete"
    assert cmp.coverage["candidate"]["cases_short_of_trials"] == ["q005"]
    s2, b2, c2 = store_pair(base, list(base), cases=CASES, b_status="running")
    assert (await _cmp(s2, b2, c2)).verdict == "incomplete"


async def test_gated_metric_on_one_side_only_is_incomplete():
    base, _ = f1_shift_pair(n_cases=20, shift=0.0)
    cand = [row(c, t, passed=True) for c in base for t in range(3)]  # no TokenF1 scorer
    s, b, c = store_pair(f1_rows(base), cand, cases=list(base))
    cmp = await _cmp(s, b, c)
    assert {"metric": "TokenF1", "gated": True, "reason": "only reported by baseline"} in \
        cmp.skipped_metrics
    assert cmp.verdict == "incomplete"


async def test_no_results_and_two_level_errors_are_incomplete():
    s, b, c = store_pair([row(x, 0) for x in CASES], [], cases=CASES, trials=1)
    cmp = await _cmp(s, b, c)
    assert cmp.verdict == "incomplete" and cmp.paired_cases == 0 and cmp.metrics == []
    # Two-level needs uniform trials; a ragged candidate cannot be gated.
    base = [row(x, t) for x in CASES for t in range(2)]
    cand = [row(x, t) for x in CASES for t in range(2 if x != "q001" else 1)]
    s, b, c = store_pair(base, cand, cases=CASES, trials=2)
    cmp = await _cmp(s, b, c, gate.DEFAULT_POLICY.with_overrides(two_level=True))
    assert any(m["metric"] == "passed" and "uniform" in m["reason"] for m in cmp.skipped_metrics)
    assert cmp.verdict == "incomplete"


async def test_tau2_style_suite_without_f1_is_complete():
    # A gated metric neither side reports (no TokenF1 scorer on τ²) is not applicable, not missing.
    base = [row(c, t) for c in CASES for t in range(3)]
    s, b, c = store_pair(base, list(base), cases=CASES)
    cmp = await _cmp(s, b, c)
    assert cmp.verdict == "no_regression" and cmp.skipped_metrics == []
    assert cmp.complete and cmp.incomplete_reasons == []
    assert [m["metric"] for m in cmp.metrics] == ["passed", "cost_usd", "latency_ms"]


def _unjudged(case_id, trial, **kw):
    """An OK trial with no pass/fail verdict and no scorer output (a legacy/imported row)."""
    return {**row(case_id, trial, **kw), "passed": None, "scores": {}}


async def test_no_gated_metric_compared_is_incomplete_not_a_pass():
    # Regression: both sides succeeded and every trial is OK, but no trial carries a verdict or a
    # gated score, so only cost/latency were compared. That used to be a complete
    # no_regression, even with a candidate 10x more expensive, while the dashboard said
    # INCOMPLETE for the same data.
    base = [_unjudged(c, t) for c in CASES for t in range(3)]
    cand = [_unjudged(c, t, cost=0.1) for c in CASES for t in range(3)]
    s, b, c = store_pair(base, cand, cases=CASES)
    cmp = await _cmp(s, b, c)
    assert [m["metric"] for m in cmp.metrics] == ["cost_usd", "latency_ms"]
    assert not any(m["gated"] for m in cmp.metrics)
    assert cmp.verdict == "incomplete" and not cmp.complete and not cmp.gated_regression
    assert any(r.startswith("no gated metric was compared") for r in cmp.incomplete_reasons)
    # Coverage is perfect: the only thing missing is a quality measurement.
    assert cmp.coverage["candidate"]["missing_cases"] == [] and cmp.paired_cases == 40


async def test_policy_whose_gated_metrics_nothing_reports_is_incomplete():
    # Same data shape the τ² suite produces (passed set, no TokenF1), but a policy gating only
    # on metrics no trial reports: nothing gated was compared, so no pass.
    base = [row(c, t) for c in CASES for t in range(3)]
    s, b, c = store_pair(base, list(base), cases=CASES)
    cmp = await _cmp(s, b, c, gate.Policy(gated=("ExactMatch",)))
    assert cmp.verdict == "incomplete" and cmp.skipped_metrics == []
    assert any("gated: ExactMatch" in r for r in cmp.incomplete_reasons)
    cmp_none = await _cmp(s, b, c, gate.Policy(gated=()))
    assert cmp_none.verdict == "incomplete"


async def test_unjudged_quality_drop_is_not_hidden_behind_cost_metrics():
    # ExactMatch collapses 1.0 → 0.0 on every case but is not gated, and passed is None
    # throughout: the gate must not call this a pass (it is not gated, so not a regression).
    def em(case_id, t, score):
        return {**_unjudged(case_id, t),
                "scores": {"ExactMatch": {"passed": score > 0, "score": score}}}

    base = [em(c, 0, 1.0) for c in CASES[:20]]
    cand = [em(c, 0, 0.0) for c in CASES[:20]]
    s, b, c = store_pair(base, cand, cases=CASES[:20], trials=1)
    cmp = await _cmp(s, b, c)
    assert _metric(cmp, "ExactMatch")["status"] == "regression"  # visible, but ungated
    assert cmp.verdict == "incomplete" and not cmp.gated_regression


async def test_partial_verdicts_with_a_gated_regression_still_report_regression():
    # passed is measured (and drops) on most trials; TokenF1 is absent on both sides (n/a).
    # The adverse gated evidence wins over every incompleteness reason.
    base = [row(c, t, passed=True) for c in CASES for t in range(3)]
    cand = [_unjudged(c, t) if t == 0 else row(c, t, passed=False)
            for c in CASES for t in range(3)]
    s, b, c = store_pair(base, cand, cases=CASES)
    cmp = await _cmp(s, b, c)
    assert _metric(cmp, "passed")["regressed"]
    assert cmp.verdict == "regression" and not cmp.complete
    assert not any(r.startswith("no gated metric") for r in cmp.incomplete_reasons)


# --- side availability: measured vs failure zeros vs partial ---


def _drop_f1(r):
    return {**r, "scores": {}}


async def test_gated_metric_missing_on_some_ok_trials_is_incomplete():
    # Every case still has TokenF1 on some trial (n_questions unchanged), but 5 OK baseline
    # trials have no TokenF1 score — partial coverage of a gated metric is not complete.
    base, _ = f1_shift_pair(n_cases=20, shift=0.0)
    base_rows = [_drop_f1(r) if r["trial"] == 2 and r["case_id"] < "q005" else r
                 for r in f1_rows(base)]
    s, b, c = store_pair(base_rows, f1_rows(base), cases=list(base))
    cmp = await _cmp(s, b, c)
    f1 = _metric(cmp, "TokenF1")
    assert f1["n_questions"] == 20
    assert f1["ok_trials_missing"] == {"baseline": 5, "candidate": 0}
    assert f1["failure_zeros_only"] == []
    assert cmp.verdict == "incomplete" and not cmp.complete
    assert "gated metric TokenF1 is missing on 5 OK trial(s) of baseline" in \
        cmp.incomplete_reasons


async def test_ungated_metric_missing_on_some_ok_trials_stays_complete():
    base = [row(c, t) for c in CASES for t in range(3)]
    cand = [row(c, t, cost=None if t == 0 else 0.01) for c in CASES for t in range(3)]
    s, b, c = store_pair(base, cand, cases=CASES)
    cmp = await _cmp(s, b, c)
    assert _metric(cmp, "cost_usd")["ok_trials_missing"] == {"baseline": 0, "candidate": 40}
    assert cmp.verdict == "no_regression" and cmp.complete


async def test_scorer_measured_on_one_side_is_not_made_two_sided_by_failure_zeros():
    # The baseline measures TokenF1; the candidate's OK trials never report it, and two of its
    # trials failed. Zero-filling those failures must not invent a candidate TokenF1 series.
    base, _ = f1_shift_pair(n_cases=20, shift=0.0)
    cand = [row(c, t, status="error" if (t == 0 and c in ("q000", "q001")) else "ok")
            for c in base for t in range(3)]
    s, b, c = store_pair(f1_rows(base), cand, cases=list(base))
    inputs = await gate.load_inputs(s, b, c)
    cmp = gate.compare(inputs)
    assert "TokenF1" not in [m["metric"] for m in cmp.metrics]
    assert {"metric": "TokenF1", "gated": True, "reason": "only reported by baseline"} in \
        cmp.skipped_metrics
    assert cmp.verdict == "incomplete" and not cmp.gated_regression
    q000 = next(r for r in gate.case_deltas(inputs, "passed") if r["case_id"] == "q000")
    assert q000["candidate"]["mean"]["TokenF1"] is None  # not a failure-zero "measurement"
    assert q000["baseline"]["mean"]["TokenF1"] is not None
    assert q000["delta"]["TokenF1"] is None


async def test_all_failed_candidate_is_a_regression_on_failure_zeros_but_incomplete():
    base, _ = f1_shift_pair(n_cases=20, shift=0.0)
    cand = [row(c, t, status="timeout") for c in base for t in range(3)]
    s, b, c = store_pair(f1_rows(base), cand, cases=list(base))
    cmp = await _cmp(s, b, c)
    f1 = _metric(cmp, "TokenF1")
    assert f1["failure_zeros_only"] == ["candidate"] and f1["regressed"]
    assert _metric(cmp, "passed")["regressed"]
    assert cmp.verdict == "regression" and not cmp.complete  # adverse evidence stands
    assert any("candidate has no OK trials" in r for r in cmp.incomplete_reasons)
    assert "cost_usd" in [s_["metric"] for s_ in cmp.skipped_metrics]  # OK-only: one-sided


@pytest.mark.parametrize("which", ["both", "baseline"])
async def test_no_ok_trials_never_completes(which):
    good = [row(c, t, passed=True) for c in CASES for t in range(3)]
    failed = [row(c, t, status="error") for c in CASES for t in range(3)]
    base = failed
    cand = failed if which == "both" else good
    s, b, c = store_pair(base, cand, cases=CASES)
    cmp = await _cmp(s, b, c)
    assert cmp.verdict == "incomplete" and not cmp.complete and not cmp.gated_regression
    assert any("baseline has no OK trials" in r for r in cmp.incomplete_reasons)
    assert ("candidate has no OK trials" in " ".join(cmp.incomplete_reasons)) == (which == "both")


async def test_side_availability_definition():
    rows = [gate.normalize_row(r) for r in (
        row("q0", 0, f1=0.8), row("q0", 1, passed=True), row("q0", 2, status="error"))]
    series = gate.build_series(rows, ["passed", "TokenF1", "Other"])
    av = gate.side_availability(rows, series)
    assert av["TokenF1"] == {"measured": True, "failure_zeros_only": False,
                             "ok_trials_missing": 1}
    assert av["passed"]["ok_trials_missing"] == 0
    # Zero-filled on the failed trial only: present in the series but not measured here.
    assert av["Other"] == {"measured": False, "failure_zeros_only": False,
                           "ok_trials_missing": 0}
    failed = [gate.normalize_row(row("q0", 0, status="error"))]
    av_failed = gate.side_availability(failed, gate.build_series(failed, ["passed", "TokenF1"]))
    assert av_failed["TokenF1"]["failure_zeros_only"] is True


# --- run id spelling ---


async def test_alternate_uuid_spellings_of_one_run_are_the_same_run():
    s = FakeStore()
    s.add_suite("suite-1", CASES)
    a = rid()
    s.add_run(a, versions={V1: "succeeded"})
    for spelling in (a.upper(), "{" + a + "}", a.replace("-", ""), f"urn:uuid:{a}"):
        with pytest.raises(gate.ComparisonError, match="same run and agent version"):
            await gate.resolve_pair(s, spelling, a)
    b, _ = await gate.resolve_pair(s, a.upper(), a, allow_identical=True)
    assert b.run_id == a
    with pytest.raises(gate.ComparisonError, match="not a UUID"):
        await gate.resolve_pair(s, "nope", a)


# --- per-case deltas ---


async def test_case_deltas_sorted_most_adverse_first_with_traces():
    base = [row(c, 0, passed=True, trace_id=f"{i:032x}") for i, c in enumerate(CASES[:4])]
    cand = [
        row("q000", 0, passed=True),
        row("q001", 0, status="error", error="tool crashed"),
        row("q002", 0, passed=False),
    ]  # q003 missing from candidate
    s, b, c = store_pair(base, cand, cases=CASES[:4], trials=1)
    rows = gate.case_deltas(await gate.load_inputs(s, b, c), "passed")
    assert [r["case_id"] for r in rows] == ["q001", "q002", "q000", "q003"]
    q001 = rows[0]
    assert q001["adverse_delta"] == 1.0 and q001["paired"]
    assert q001["candidate"]["trials"][0] == {
        "trial": 0, "status": "error", "passed": False, "trace_id": None, "error": "tool crashed"}
    assert q001["baseline"]["trials"][0]["trace_id"] == f"{1:032x}"
    assert rows[-1]["paired"] is False and rows[-1]["adverse_delta"] is None
    # Lower-is-better metrics sort a rise first.
    lat = gate.case_deltas(await gate.load_inputs(s, b, c), "latency_ms")
    assert all(r["adverse_delta"] in (0.0, None) for r in lat)
    with pytest.raises(gate.ComparisonError):
        gate.case_deltas(await gate.load_inputs(s, b, c), "nope")


async def test_case_deltas_with_no_results_is_empty_not_an_error():
    s, b, c = store_pair([], [], cases=CASES)
    inputs = await gate.load_inputs(s, b, c)
    assert gate.available_metrics(inputs) == []
    assert gate.case_deltas(inputs, "passed") == []
