"""Offline tests for the agent's tools and tool-use loop — no Anthropic API, no stack.

These verify the agent logic cheaply (the end-to-end span/ClickHouse verification is in
test_e2e.py). Tracing is left uninitialized here, so the SDK spans are no-ops."""

from __future__ import annotations

from hello_agent import agent
from hello_agent.tools import calculator, get_weather


def test_calculator():
    assert calculator("23 * 19") == "437"
    assert calculator("(2 + 3) ** 2") == "25"
    assert calculator("10 / 4") == "2.5"
    assert calculator("nope(").startswith("error:")


def test_get_weather():
    assert "Paris" in get_weather("Paris")


def test_extract_final_answer():
    # The terse answer comes from the FINAL: line, not the full explanation.
    assert agent._extract_final_answer("23 * 19 = 437\nFINAL: 437") == "437"
    # The last FINAL line wins if there are several.
    assert agent._extract_final_answer("FINAL: draft\nFINAL: 42") == "42"
    # No marker -> fall back to the full (stripped) text.
    assert agent._extract_final_answer("  just this  ") == "just this"


# --- stub Anthropic client that drives one tool call then a final answer ---

# Reject kwargs the real SDK doesn't accept, so the offline test catches signature drift
# (e.g. passing the removed `temperature`) the way the real client would.
import inspect  # noqa: E402

import anthropic  # noqa: E402

_ALLOWED_CREATE_KWARGS = set(
    inspect.signature(anthropic.resources.messages.Messages.create).parameters
) - {"self"}


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.model = "claude-haiku-4-5"

        class _U:
            input_tokens = 5
            output_tokens = 7

        self.usage = _U()


class StubMessages:
    def __init__(self):
        self.calls = []

    # Async, so vigil.wrap treats the stub like a real AsyncAnthropic client (contract §4).
    async def create(self, **kwargs):
        unexpected = set(kwargs) - _ALLOWED_CREATE_KWARGS
        if unexpected:
            raise TypeError(
                f"Messages.create() got an unexpected keyword argument {sorted(unexpected)[0]!r}"
            )
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            # First turn: ask to use the calculator.
            return _Resp(
                [
                    _Block(
                        type="tool_use", name="calculator", input={"expression": "2 + 2"}, id="tu_1"
                    )
                ],
                stop_reason="tool_use",
            )
        # Second turn: final text answer.
        return _Resp([_Block(type="text", text="The answer is 4.")], stop_reason="end_turn")


class StubClient:
    def __init__(self):
        self.messages = StubMessages()


def test_tool_use_loop_executes_tool_and_returns_answer():
    import asyncio

    stub = StubClient()
    client = agent.make_client(stub)

    result = asyncio.run(
        agent.run(client, "What is 2 + 2?", eval_run_id="off-1", eval_case_id="c1", trial=0)
    )

    assert result.output == "The answer is 4."
    # The agent reports token counts (5 in + 7 out per call, two calls) — no cost.
    assert result.input_tokens == 10 and result.output_tokens == 14
    # It recorded the calculator tool call for the scorers.
    assert [tc.name for tc in result.tool_calls] == ["calculator"]
    assert result.tool_calls[0].arguments == {"expression": "2 + 2"}
    # Two model calls: initial + after the tool result.
    assert len(stub.messages.calls) == 2
    # The agent must send only SDK-supported args (max_tokens yes, no removed sampling params).
    first = stub.messages.calls[0]
    assert first["max_tokens"] == 1024
    assert "temperature" not in first and "top_p" not in first and "top_k" not in first
    # The second call must carry the tool_result with the calculator's real output ("4").
    second_messages = stub.messages.calls[1]["messages"]
    tool_results = [
        block
        for msg in second_messages
        if isinstance(msg.get("content"), list)
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["content"] == "4"


def test_agent_version_is_stable():
    # Recomputing from the same inputs yields the same version.
    import vigil

    from hello_agent.tools import TOOLS

    recomputed = vigil.compute_agent_version(
        prompts={"system": agent.SYSTEM_PROMPT},
        tools=TOOLS,
        model=agent.MODEL,
        params=agent.PARAMS,
    )
    assert recomputed == agent.AGENT_VERSION
