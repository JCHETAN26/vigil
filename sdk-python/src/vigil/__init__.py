"""Vigil tracing SDK — a thin layer over OpenTelemetry that exports OTLP traces of agent
runs, LLM calls, tool calls, and retrieval steps to the Vigil ingest service."""

from .anthropic import wrap
from .config import Config
from .context import RunContext, current_run
from .identity import compute_agent_version, git_sha
from .spans import (
    agent_run,
    llm_call,
    retrieval,
    set_gen_ai_request,
    set_gen_ai_response,
    tool_call,
    traced_agent_run,
    traced_retrieval,
    traced_tool_call,
)
from .tracer import get_config, get_tracer, init, shutdown

__all__ = [
    "Config",
    "RunContext",
    "agent_run",
    "compute_agent_version",
    "current_run",
    "get_config",
    "get_tracer",
    "git_sha",
    "init",
    "llm_call",
    "retrieval",
    "set_gen_ai_request",
    "set_gen_ai_response",
    "shutdown",
    "tool_call",
    "traced_agent_run",
    "traced_retrieval",
    "traced_tool_call",
    "wrap",
]
