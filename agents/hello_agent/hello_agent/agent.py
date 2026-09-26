"""Week 1 test agent: a small question-answering agent that runs a real Anthropic
tool-use loop over two tools (calculator, mock weather), instrumented with the Vigil SDK.

Runs natively on the host (see the infrastructure note in docs/design/data-model.md):
it calls the Anthropic API over the host network and exports OTLP traces to the ingest
receiver on 127.0.0.1. Runs are tagged run_kind="eval", so content is captured.
"""

from __future__ import annotations

import os
from typing import Any

import vigil
from vigil import RunResult, ToolCall

from .tools import TOOLS, dispatch

AGENT_ID = "hello-agent"

SYSTEM_PROMPT = (
    "You are a concise assistant. When a question needs arithmetic, call the calculator "
    "tool. When it asks about weather, call the get_weather tool. Prefer the tools over "
    "guessing, and give a short final answer."
)

MODEL = os.getenv("VIGIL_HELLO_MODEL", os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"))
# Decoding params passed to messages.create AND folded into the agent-version hash. The
# installed Anthropic SDK (1.8.x) removed the sampling params (temperature/top_p/top_k) —
# they raise TypeError — so only supported keys go here; add e.g. `thinking`/`output_config`
# if a future model needs them.
PARAMS: dict[str, Any] = {"max_tokens": 1024}

# The manifest inputs the eval engine reads (contract §4): the engine computes the version
# hash from these and upserts version_manifests. Named to satisfy the AgentModule protocol.
prompts = {"system": SYSTEM_PROMPT}
model = MODEL
params = PARAMS
tools = TOOLS

# The agent version is a content hash over the behavior-defining inputs (§2.3): the
# prompt, the tool schemas, the model, and the decoding params. It changes whenever any
# of these change.
AGENT_VERSION = vigil.compute_agent_version(
    prompts=prompts,
    tools=TOOLS,
    model=MODEL,
    params=PARAMS,
)

_MAX_TURNS = 6


def init_tracing():
    """Initialize Vigil tracing with this agent's identity. Call once before running."""
    return vigil.init(
        service_name=AGENT_ID,
        agent_id=AGENT_ID,
        agent_version=AGENT_VERSION,
        git_sha=vigil.git_sha(),
    )


def make_client(raw: Any | None = None, *, timeout: float | None = None):
    """Return a Vigil-instrumented **async** Anthropic client. Pass ``raw`` (e.g. a stub) in
    tests; otherwise a real ``anthropic.AsyncAnthropic`` is created (key from env). ``timeout``
    sets the client's per-request timeout — the eval worker sets it below the per-case timeout
    as a backstop so a hung request can't outlive the case's cancellation."""
    if raw is None:
        import anthropic

        raw = anthropic.AsyncAnthropic(timeout=timeout) if timeout else anthropic.AsyncAnthropic()
    return vigil.wrap(raw)


async def run(
    client,
    case_input: Any,
    *,
    eval_run_id: str,
    eval_case_id: str,
    trial: int = 0,
) -> RunResult:
    """Answer one question with a tool-use loop, as one eval agent run (contract §4). ``run``
    is async so a per-case timeout can cancel an in-flight request. Returns a ``RunResult``
    with the final answer, the tool calls made, the trace id, and raw token counts (the engine
    computes cost)."""
    question = case_input.get("question", "") if isinstance(case_input, dict) else str(case_input)
    with vigil.agent_run(
        run_kind="eval",
        eval_run_id=eval_run_id,
        eval_case_id=eval_case_id,
        trial=trial,
        attributes={"vigil.question": question},
    ) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        answer = ""
        tool_calls: list[ToolCall] = []
        input_tokens = 0
        output_tokens = 0

        for _ in range(_MAX_TURNS):
            resp = await client.messages.create(
                model=MODEL,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
                **PARAMS,
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                input_tokens += getattr(usage, "input_tokens", 0) or 0
                output_tokens += getattr(usage, "output_tokens", 0) or 0
            messages.append({"role": "assistant", "content": resp.content})

            if getattr(resp, "stop_reason", None) == "tool_use":
                tool_results = []
                for block in resp.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    tool_calls.append(ToolCall(name=block.name, arguments=dict(block.input or {})))
                    with vigil.tool_call(tool_name=block.name, arguments=block.input) as tspan:
                        result = dispatch(block.name, block.input)
                        tspan.set_attribute("gen_ai.tool.call.id", block.id)
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result}
                    )
                messages.append({"role": "user", "content": tool_results})
                continue

            # No tool requested — collect the text answer and stop.
            answer = "".join(
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
            )
            break

        span.set_attribute("vigil.answer_chars", len(answer))
        return RunResult(
            output=answer,
            tool_calls=tool_calls,
            trace_id=trace_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
