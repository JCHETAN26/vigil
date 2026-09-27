"""Unit tests for the τ²-bench retail adapter and reward scorer (both τ²-free)."""

from __future__ import annotations

import json

import pytest
from vigil import RunResult

from engine.datasets.base import Case
from engine.datasets.tau2_retail import Tau2RetailAdapter
from engine.scoring import TauBenchReward, score_case, scorers_for


def _pins(tmp_path):
    doc = {
        "pinned_commit": "b7ea9074c1cba482b30687fecdb5c8425fd6f619",
        "domain": "retail",
        "dev": {"task_ids": ["0", "1", "2"]},
        "measurement": {"candidate_task_ids": ["5", "9", "12"]},
    }
    p = tmp_path / "retail.pins.json"
    p.write_text(json.dumps(doc))
    return str(p)


def test_adapter_dev_split_materializes_cases(tmp_path):
    adapter = Tau2RetailAdapter.from_config({"path": _pins(tmp_path)})  # split defaults to dev
    cases = list(adapter.load())
    assert [c.case_id for c in cases] == ["0", "1", "2"]
    c0 = cases[0]
    assert c0.input == {"task_id": "0", "domain": "retail"}
    assert c0.expected["scorers"] == ["TauBenchReward"]
    assert c0.expected["pass_scorers"] == ["TauBenchReward"]
    assert adapter.version == "b7ea9074c1cb"  # pinned commit, 12 chars


def test_adapter_measurement_split_is_disjoint_from_dev(tmp_path):
    path = _pins(tmp_path)
    dev = {c.case_id for c in Tau2RetailAdapter.from_config({"path": path, "split": "dev"}).load()}
    meas = {
        c.case_id
        for c in Tau2RetailAdapter.from_config({"path": path, "split": "measurement"}).load()
    }
    assert dev.isdisjoint(meas)


def test_adapter_unknown_split_raises(tmp_path):
    with pytest.raises(ValueError):
        Tau2RetailAdapter.from_config({"path": _pins(tmp_path), "split": "train"})


def _case():
    return Case(case_id="0", input={"task_id": "0"}, expected={"scorers": ["TauBenchReward"]})


def test_tau_bench_reward_reads_info():
    s = TauBenchReward()
    passed = s.score(_case(), RunResult(output="", info={"reward": 1.0, "db_match": True}))
    assert passed.passed and passed.score == 1.0 and passed.detail["db_match"] is True

    failed = s.score(_case(), RunResult(output="", info={"reward": 0.0, "db_match": False}))
    assert not failed.passed and failed.score == 0.0


def test_tau_bench_reward_missing_info_is_failure():
    s = TauBenchReward()
    r = s.score(_case(), RunResult(output=""))  # no info at all
    assert not r.passed and r.score == 0.0


def test_pass_gate_uses_tau_bench_reward():
    case = Case(
        case_id="0",
        input={"task_id": "0"},
        expected={"scorers": ["TauBenchReward"], "pass_scorers": ["TauBenchReward"]},
    )
    agg = score_case(case, RunResult(output="", info={"reward": 1.0}), scorers_for(case.expected))
    assert agg.passed and agg.scores["TauBenchReward"]["deciding"] is True
