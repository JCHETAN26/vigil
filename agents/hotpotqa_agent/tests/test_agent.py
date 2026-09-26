"""Offline tests for the HotpotQA agent's multi-hop loop — no Anthropic API, no stack.

An async stub client drives two searches then a final answer; a tiny injected BM25 retriever
serves the corpus. Verifies the agent records tool calls, retrievals (query + doc_ids), tokens,
and extracts the short final_answer."""

from __future__ import annotations

import asyncio

from hotpotqa_agent import agent
from hotpotqa_agent.retrieval import BM25Retriever

_CORPUS = [
    (
        "Arthur's Magazine",
        "Arthur's Magazine was an American literary periodical first published in 1844.",
    ),
    ("First for Women", "First for Women is a women's magazine launched in 1989 in the USA."),
    ("Paris", "Paris is the capital and most populous city of France."),
]


def _tiny_retriever():
    return BM25Retriever([d[0] for d in _CORPUS], [d[1] for d in _CORPUS])


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Usage:
    input_tokens = 9
    output_tokens = 4


class _Resp:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.model = "claude-haiku-4-5"
        self.usage = _Usage()


class StubMessages:
    """Turn 1 and 2: a search each; turn 3: the final answer."""

    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        n = len(self.calls)
        if n == 1:
            return _Resp(
                [
                    _Block(
                        type="tool_use",
                        name="search",
                        input={"query": "Arthur's Magazine"},
                        id="t1",
                    )
                ],
                "tool_use",
            )
        if n == 2:
            return _Resp(
                [
                    _Block(
                        type="tool_use",
                        name="search",
                        input={"query": "First for Women magazine"},
                        id="t2",
                    )
                ],
                "tool_use",
            )
        return _Resp(
            [
                _Block(
                    type="text",
                    text="Arthur's Magazine was first (1844 vs 1989).\nFINAL: Arthur's Magazine",
                )
            ],
            "end_turn",
        )


class StubClient:
    def __init__(self):
        self.messages = StubMessages()


def test_multi_hop_run_collects_retrievals_and_final_answer():
    agent.set_retriever(_tiny_retriever())
    try:
        stub = StubClient()
        client = agent.make_client(stub)
        result = asyncio.run(
            agent.run(
                client,
                "Which was first, Arthur's Magazine or First for Women?",
                eval_run_id="off-1",
                eval_case_id="c0",
                trial=0,
            )
        )
    finally:
        agent.set_retriever(None)

    # Three model turns: two searches + the answer.
    assert len(stub.messages.calls) == 3
    assert result.final_answer == "Arthur's Magazine"
    assert result.output.startswith("Arthur's Magazine was first")

    # Two tool calls, two retrievals, each with the query and non-empty doc_ids.
    assert [tc.name for tc in result.tool_calls] == ["search", "search"]
    assert [r.query for r in result.retrievals] == ["Arthur's Magazine", "First for Women magazine"]
    assert result.retrievals[0].doc_ids  # BM25 returned something
    assert "Arthur's Magazine" in result.retrievals[0].doc_ids

    # Tokens accumulated across the three turns (9 in / 4 out each).
    assert result.input_tokens == 27 and result.output_tokens == 12


def test_dict_input_accepted():
    agent.set_retriever(_tiny_retriever())
    try:
        client = agent.make_client(StubClient())
        result = asyncio.run(
            agent.run(
                client,
                {"question": "Which was first, Arthur's Magazine or First for Women?"},
                eval_run_id="off-2",
                eval_case_id="c1",
            )
        )
        assert result.final_answer == "Arthur's Magazine"
    finally:
        agent.set_retriever(None)


def test_missing_corpus_fails_loudly(monkeypatch):
    import pytest

    monkeypatch.delenv("VIGIL_HOTPOTQA_CORPUS", raising=False)
    agent.set_retriever(None)
    with pytest.raises(RuntimeError, match="VIGIL_HOTPOTQA_CORPUS"):
        agent._get_retriever()


def test_agent_version_stable():
    from hotpotqa_agent.agent import TOOLS

    recomputed = __import__("vigil").compute_agent_version(
        prompts={"system": agent.SYSTEM_PROMPT}, tools=TOOLS, model=agent.MODEL, params=agent.PARAMS
    )
    assert recomputed == agent.AGENT_VERSION
