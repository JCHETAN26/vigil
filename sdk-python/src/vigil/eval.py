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
class Retrieval:
    """One retrieval/search call the agent made: the query and the ids of the documents it
    got back, in rank order. Retrieval scorers (recall@k, nDCG) read these; the SDK also
    records a retrieval span per call so the same is visible in ClickHouse."""

    query: str
    doc_ids: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    output: Any  # the agent's full output (may include reasoning/explanation)
    # A short, canonical final answer for scoring, separate from the full output. Scorers like
    # ExactMatch and TokenF1 compare this when present, falling back to ``output`` when it is
    # None — so an agent that explains its work can still be scored on just the answer.
    final_answer: Any = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    retrievals: list[Retrieval] = field(default_factory=list)  # one per search call (RAG agents)
    trace_id: str = ""  # 032x hex; links to ClickHouse
    input_tokens: int = 0  # AGENT tokens (excludes user simulator)
    output_tokens: int = 0
    # AGENT prompt-cache tokens (Anthropic): cache_creation = written to cache, cache_read =
    # served from cache. Priced at the model's cache_write_5m / cache_read rates by the engine;
    # 0 when the agent doesn't use caching.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    sim_input_tokens: int = 0  # LLM user-simulator tokens (tau-bench), if any
    sim_output_tokens: int = 0
    # Free-form agent-reported metadata a scorer can read, e.g. a benchmark's own reward that
    # the engine can't recompute (τ²-bench: {"reward", "db_match", "gold_hash", ...}). Kept out
    # of the token/cost fields; persisted with the transcript.
    info: dict = field(default_factory=dict)
