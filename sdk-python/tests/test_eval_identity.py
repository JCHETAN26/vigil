"""Tests for the eval-run identity additions: vigil.eval.trial (0-based repeat index) and
vigil.role (e.g. user_simulator), both stamped onto every span of a run."""

from __future__ import annotations

import vigil


def _by_name(spans):
    return {s.name: s for s in spans}


def test_trial_stamped_on_root_and_child_spans(spans):
    with vigil.agent_run(run_kind="eval", eval_run_id="run-1", eval_case_id="c0", trial=2):
        with vigil.llm_call(request_model="claude-haiku-4-5"):
            pass
        with vigil.tool_call(tool_name="calculator"):
            pass

    found = spans()
    assert found, "expected spans"
    for s in found:
        assert s.attributes["vigil.eval.trial"] == 2
        assert s.attributes["vigil.eval.run_id"] == "run-1"


def test_trial_zero_is_stamped(spans):
    # trial 0 is a real trial index, not "absent" — presence must be tested, not truthiness.
    with vigil.agent_run(run_kind="eval", eval_run_id="run-1", eval_case_id="c0", trial=0):
        with vigil.tool_call(tool_name="calculator"):
            pass

    for s in spans():
        assert s.attributes["vigil.eval.trial"] == 0


def test_trial_absent_when_not_set(spans):
    with vigil.agent_run(run_kind="eval", eval_run_id="run-1", eval_case_id="c0"):
        pass
    for s in spans():
        assert "vigil.eval.trial" not in s.attributes


def test_role_tags_spans_within_block_only(spans):
    with vigil.agent_run(run_kind="eval", eval_run_id="run-1", eval_case_id="c0"):
        with vigil.role("user_simulator"):
            with vigil.llm_call(request_model="sim-model", name="sim.call"):
                pass
        with vigil.llm_call(request_model="agent-model", name="agent.call"):
            pass

    by_name = _by_name(spans())
    assert by_name["sim.call"].attributes["vigil.role"] == "user_simulator"
    assert "vigil.role" not in by_name["agent.call"].attributes
    # The root agent span is outside the role block, so it is not tagged.
    assert "vigil.role" not in by_name["agent.run"].attributes


def test_role_resets_after_block(spans):
    with vigil.agent_run(run_kind="eval", eval_run_id="run-1", eval_case_id="c0"):
        with vigil.role("user_simulator"):
            pass
        with vigil.tool_call(tool_name="after"):
            pass
    by_name = _by_name(spans())
    assert "vigil.role" not in by_name["tool.after"].attributes
