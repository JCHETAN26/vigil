"""Offline tests for the validation variants (hotpotqa_agent.validation) — no API, no stack.

Checks that the one controlled change (documents per search) is consistent across what the
retriever is asked for, what the model is told, and the manifest/version hash; that the top_k5
variant sends the model exactly what the default agent sends; that variants share no mutable
state; and that the default agent's defaults and version hash are unchanged."""

from __future__ import annotations

import asyncio
import json

import pytest
import vigil

from hotpotqa_agent import agent
from hotpotqa_agent.retrieval import RetrievedDoc
from hotpotqa_agent.validation import VARIANT_PARAM, Variant, top_k1, top_k5

# The default agent's version hash before the validation refactor, for model claude-haiku-4-5.
DEFAULT_VERSION_HAIKU_45 = "0f1be87fd0a29dd03f19631d0638dedbf3d5e8d1dbcb15e8c70a137ceade102b"

QUESTION = "Which magazine was started first, Arthur's Magazine or First for Women?"


class RecordingRetriever:
    """Serves ten synthetic docs and records the k of every search."""

    def __init__(self):
        self.ks: list[int] = []

    def search(self, query: str, k: int) -> list[RetrievedDoc]:
        self.ks.append(k)
        return [RetrievedDoc(doc_id=f"doc{i}", text=f"text {i}", score=1.0) for i in range(10)][:k]


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Usage:
    input_tokens = 7
    output_tokens = 3


class _Resp:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.model = "claude-haiku-4-5"
        self.usage = _Usage()


class TwoSearchClient:
    """Two searches, then a final answer. Records every request."""

    def __init__(self):
        self.calls: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        # Snapshot: the agent keeps appending to the same messages list across turns.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        n = len(self.calls)
        if n <= 2:
            block = _Block(type="tool_use", name="search", input={"query": f"q{n}"}, id=f"t{n}")
            return _Resp([block], "tool_use")
        return _Resp([_Block(type="text", text="FINAL: something")], "end_turn")


def _run(run_fn, retriever):
    agent.set_retriever(retriever)
    try:
        client = TwoSearchClient()
        result = asyncio.run(
            run_fn(client, QUESTION, eval_run_id="off", eval_case_id="c0", trial=0)
        )
        return client, result
    finally:
        agent.set_retriever(None)


def _engine_hash(module) -> str:
    # Mirrors engine.runner.contract.manifest_of (the engine is not installed in this venv).
    return vigil.compute_agent_version(
        prompts=dict(module.prompts),
        tools=module.tools,
        model=module.model,
        params=dict(module.params),
    )


def test_default_agent_unchanged():
    assert agent.TOP_K == 5
    assert agent.PARAMS == {"max_tokens": 1024}
    assert agent.TOOLS == [agent.search_schema(5)]
    pinned = vigil.compute_agent_version(
        prompts=agent.prompts, tools=agent.TOOLS, model="claude-haiku-4-5", params=agent.PARAMS
    )
    assert pinned == DEFAULT_VERSION_HAIKU_45


@pytest.mark.parametrize("module,k", [(top_k5, 5), (top_k1, 1)])
def test_variant_manifest_hash_matches_engine_computation(module, k):
    assert module.TOP_K == k
    assert module.params[VARIANT_PARAM] == k
    assert f"top {k} matching" in module.tools[0]["description"]
    assert module.AGENT_ID == agent.AGENT_ID
    assert _engine_hash(module) == module.AGENT_VERSION


def test_variants_differ_only_in_top_k():
    assert top_k5.AGENT_VERSION != top_k1.AGENT_VERSION
    assert top_k5.AGENT_VERSION != agent.AGENT_VERSION  # manifest records retrieval_top_k
    assert top_k5.model == top_k1.model == agent.MODEL
    assert top_k5.prompts == top_k1.prompts == agent.prompts
    p5, p1 = dict(top_k5.params), dict(top_k1.params)
    assert p5.pop(VARIANT_PARAM) == 5 and p1.pop(VARIANT_PARAM) == 1
    assert p5 == p1 == agent.PARAMS
    t5, t1 = top_k5.tools[0], top_k1.tools[0]
    assert t5["name"] == t1["name"] and t5["input_schema"] == t1["input_schema"]
    assert t5["description"].replace("top 5", "top 1") == t1["description"]


@pytest.mark.parametrize("module,k", [(top_k5, 5), (top_k1, 1)])
def test_variant_searches_with_its_top_k(module, k):
    retriever = RecordingRetriever()
    client, result = _run(module.run, retriever)
    assert retriever.ks == [k, k]
    assert all(len(r.doc_ids) == k for r in result.retrievals)
    for call in client.calls:
        assert VARIANT_PARAM not in call and "top_k" not in call  # never sent to the model
        assert call["max_tokens"] == agent.PARAMS["max_tokens"]
    assert client.calls[0]["tools"] == module.tools


def test_top_k5_sends_the_default_agents_requests():
    default_client, default_result = _run(agent.run, RecordingRetriever())
    variant_client, variant_result = _run(top_k5.run, RecordingRetriever())
    dump = lambda calls: json.dumps(calls, default=lambda o: o.__dict__, sort_keys=True)  # noqa: E731
    assert dump(variant_client.calls) == dump(default_client.calls)
    assert variant_result.retrievals == default_result.retrievals


def test_variants_share_no_mutable_state_in_one_process():
    ks = []
    for module in (top_k1, top_k5, top_k1):
        r = RecordingRetriever()
        _run(module.run, r)
        ks.append(r.ks)
    assert ks == [[1, 1], [5, 5], [1, 1]]

    # Mutating what a variant exposes changes neither its behavior nor another variant.
    top_k1.params[VARIANT_PARAM] = 99
    top_k1.tools[0]["description"] = "mutated"
    try:
        r = RecordingRetriever()
        client, _ = _run(top_k1.run, r)
        assert r.ks == [1, 1]
        assert "top 1 matching" in client.calls[0]["tools"][0]["description"]
        assert top_k5.params[VARIANT_PARAM] == 5
        assert agent.TOOLS == [agent.search_schema(5)]
    finally:
        top_k1.params[VARIANT_PARAM] = 1
        top_k1.tools[:] = top_k1._VARIANT.manifest_tools()


def test_model_sees_only_question_and_retrieved_text():
    """No gold data reaches the model: the agent is given only the case input (the question),
    and everything else in the transcript is the system prompt or retrieved corpus text."""
    client, _ = _run(top_k1.run, RecordingRetriever())
    first = client.calls[0]
    assert first["system"] == agent.SYSTEM_PROMPT
    assert first["messages"] == [{"role": "user", "content": QUESTION}]
    final = client.calls[-1]["messages"]
    tool_results = [m for m in final if m["role"] == "user" and isinstance(m["content"], list)]
    for m in tool_results:
        for block in m["content"]:
            assert block["type"] == "tool_result" and block["content"].startswith("[1] doc0: ")


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "5"])
def test_variant_rejects_bad_top_k(bad):
    with pytest.raises(ValueError):
        Variant.build(bad)
