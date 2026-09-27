"""Offline test for the τ² retail agent: a stub domain server + stub Anthropic client drive a
full episode (open → tool call → resolve → user stops), with no tau2 and no network. Asserts
the tool calls, the agent/simulator token split, cache-token accumulation, and the reward in
RunResult.info."""

from __future__ import annotations

import asyncio

from tau2_retail_agent import agent


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Usage:
    def __init__(self, i, o, cw=0, cr=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_creation_input_tokens = cw
        self.cache_read_input_tokens = cr


class Resp:
    def __init__(self, content, usage):
        self.content = content
        self.usage = usage
        self.stop_reason = "end_turn"


class StubClient:
    """messages.create branches on `tools`: with tools -> agent turn, without -> simulator."""

    def __init__(self):
        self.messages = self
        self.agent_calls = 0
        self.sim_calls = 0

    async def create(self, **kwargs):
        if "tools" in kwargs:
            self.agent_calls += 1
            if self.agent_calls == 1:
                # First agent turn: one tool call (cancel_pending_order). Cache WRITE on turn 1.
                block = Obj(
                    type="tool_use", name="cancel_pending_order",
                    input={"order_id": "#W1", "reason": "no longer needed"}, id="tu1",
                )
                return Resp([block], Usage(5000, 20, cw=4800, cr=0))
            # Second agent turn: a text reply to the customer. Cache READ on later turns.
            return Resp([Obj(type="text", text="Your order is cancelled. Anything else?")],
                        Usage(200, 15, cw=0, cr=4800))
        # Simulator turns.
        self.sim_calls += 1
        if self.sim_calls == 1:
            return Resp([Obj(type="text", text="Please cancel my order #W1.")], Usage(80, 12))
        return Resp([Obj(type="text", text=agent._STOP)], Usage(90, 3))


class FakeServer:
    def __init__(self):
        self.ops = []

    async def request(self, req):
        self.ops.append(req["op"])
        op = req["op"]
        if op == "reset":
            return {
                "ok": True,
                "user_scenario": {"reason_for_call": "cancel an order"},
                "db_hash": "h0",
            }
        if op == "use_tool":
            return {"ok": True, "result": '{"status":"cancelled"}', "db_hash": "h1"}
        if op == "reward":
            return {"ok": True, "reward": 1.0, "db_match": True,
                    "reward_basis": ["RewardType.DB", "RewardType.NL_ASSERTION"],
                    "gold_hash": "h1", "agent_hash": "h1"}
        return {"ok": True}


def test_episode_tool_calls_reward_and_cost_split(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(agent, "_SERVER", server)
    client = StubClient()
    result = asyncio.run(
        agent.run(client, {"task_id": "0"}, eval_run_id="r", eval_case_id="0", trial=0)
    )

    # The tool call was recorded with its arguments.
    assert [tc.name for tc in result.tool_calls] == ["cancel_pending_order"]
    assert result.tool_calls[0].arguments["order_id"] == "#W1"

    # Agent tokens exclude the simulator; simulator tokens are separate.
    assert result.input_tokens == 5200  # 5000 + 200
    assert result.output_tokens == 35  # 20 + 15
    assert result.sim_input_tokens == 170  # 80 + 90
    assert result.sim_output_tokens == 15  # 12 + 3

    # Cache tokens accumulated across agent calls (write on turn 1, read on turn 2).
    assert result.cache_creation_input_tokens == 4800
    assert result.cache_read_input_tokens == 4800

    # τ²'s reward is surfaced in info.
    assert result.info["reward"] == 1.0
    assert result.info["db_match"] is True

    # The server session was reset, the reward requested, and the session ended.
    assert server.ops[0] == "reset"
    assert "reward" in server.ops and server.ops[-1] == "end"
    assert result.trace_id is not None


def test_agent_version_is_stable():
    # The version is a pure function of the pinned prefix + model/params.
    v = agent.vigil.compute_agent_version(
        prompts=agent.prompts, tools=agent.tools, model=agent.model, params=agent.params
    )
    assert v == agent.AGENT_VERSION
