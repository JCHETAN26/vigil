"""The agent->engine result contract: RunResult / ToolCall / Retrieval shapes."""

from __future__ import annotations

import vigil


def test_runresult_defaults():
    r = vigil.RunResult(output="full text")
    assert r.output == "full text"
    assert r.final_answer is None
    assert r.tool_calls == []
    assert r.retrievals == []
    assert r.input_tokens == 0 and r.output_tokens == 0
    assert not hasattr(r, "cost_usd")  # agents report tokens only; the engine computes cost


def test_retrieval_and_tool_call():
    r = vigil.RunResult(
        output="…",
        final_answer="Paris",
        tool_calls=[vigil.ToolCall("search", {"query": "capital of France"})],
        retrievals=[
            vigil.Retrieval(query="capital of France", doc_ids=["France", "Paris"]),
            vigil.Retrieval(query="Paris", doc_ids=["Paris", "Seine"]),
        ],
    )
    assert r.final_answer == "Paris"
    assert r.tool_calls[0].name == "search"
    assert [rt.query for rt in r.retrievals] == ["capital of France", "Paris"]
    assert r.retrievals[0].doc_ids == ["France", "Paris"]
