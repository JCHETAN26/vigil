"""Unit tests for the engine-side agent contract: an agent module satisfies the protocol, and
manifest_of computes the same version the agent computes (design §4)."""

from __future__ import annotations

from engine.runner.contract import AgentModule, RunResult, ToolCall, manifest_of


def test_hello_agent_satisfies_protocol_and_manifest():
    from hello_agent import agent

    # The module exposes the manifest inputs + an async run(): it is an AgentModule.
    assert isinstance(agent, AgentModule)

    manifest = manifest_of(agent)
    assert manifest.agent_id == "hello-agent"
    # The engine computes the identical version the agent computed for itself.
    assert manifest.agent_version == agent.AGENT_VERSION
    assert manifest.model == agent.MODEL
    assert manifest.params == agent.PARAMS
    assert manifest.prompts == {"system": agent.SYSTEM_PROMPT}


def test_run_result_defaults_are_token_only():
    # RunResult carries token counts only; there is no cost field (the engine computes cost).
    r = RunResult(output="x", tool_calls=[ToolCall("t", {"a": 1})], trace_id="tid")
    assert r.input_tokens == 0 and r.output_tokens == 0
    assert r.sim_input_tokens == 0 and r.sim_output_tokens == 0
    assert not hasattr(r, "cost_usd")
