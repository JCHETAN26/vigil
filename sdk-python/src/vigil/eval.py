"""The agent-under-test result contract (design doc §4).

``RunResult`` is what an agent's ``run()`` returns to the eval engine: the final output, the
tool calls it made (for deterministic scoring), the trace id (to link to ClickHouse), and
raw token counts. It lives in the SDK — not the engine — so an agent can depend on it
without importing the engine: the contract flows one way (the engine imports the agent).

Agents report **token counts only**; the engine computes cost from these with its cost meter
against the one shared price table. Simulator tokens (an LLM user simulator, e.g. tau-bench)
are reported separately so the engine can attribute them to the run budget while excluding
them from agent cost metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    output: Any  # final answer, for ExactMatch and judges
    tool_calls: list[ToolCall] = field(default_factory=list)
    trace_id: str = ""  # 032x hex; links to ClickHouse
    input_tokens: int = 0  # AGENT tokens (excludes user simulator)
    output_tokens: int = 0
    sim_input_tokens: int = 0  # LLM user-simulator tokens (tau-bench), if any
    sim_output_tokens: int = 0
