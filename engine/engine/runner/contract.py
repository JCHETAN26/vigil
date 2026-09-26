"""The agent-under-test contract, from the engine's side (design doc §4).

The result types (``RunResult``, ``ToolCall``) live in the SDK so agents import them without
importing the engine; the engine re-exports them here for convenience. This module adds the
engine-side view: the ``AgentModule`` protocol an agent module must satisfy, and a helper to
compute its version hash + manifest inputs for ``version_manifests``.

An agent module exposes:
  - ``AGENT_ID: str``
  - the manifest inputs ``prompts: Mapping[str, str]``, ``tools``, ``model: str``,
    ``params: Mapping[str, Any]``
  - ``async def run(client, case_input, *, eval_run_id, eval_case_id, trial) -> RunResult``

``run`` is **async** and uses the engine-provided (wrapped, maybe-cached) ``AsyncAnthropic``
client, so a per-case timeout can cancel in-flight requests instead of leaving threads
running and spending (approved change). The agent never constructs its own client in eval
mode, and it reports token counts only — the engine computes cost.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import vigil
from vigil import RunResult, ToolCall

__all__ = ["RunResult", "ToolCall", "AgentModule", "AgentManifest", "manifest_of"]


@runtime_checkable
class AgentModule(Protocol):
    AGENT_ID: str
    prompts: Mapping[str, str]
    tools: Any
    model: str
    params: Mapping[str, Any]

    async def run(
        self,
        client: Any,
        case_input: Any,
        *,
        eval_run_id: str,
        eval_case_id: str,
        trial: int,
    ) -> RunResult: ...


@dataclass
class AgentManifest:
    agent_id: str
    agent_version: str
    model: str
    params: dict
    prompts: dict
    tools: Any


def manifest_of(module: Any, *, code_paths=None) -> AgentManifest:
    """Read an agent module's manifest inputs and compute its version hash (the same
    ``compute_agent_version`` the SDK/agent uses), so the engine can upsert
    ``version_manifests`` and set the worker's version identity."""
    prompts = dict(module.prompts)
    params = dict(module.params)
    tools = module.tools
    model = module.model
    version = vigil.compute_agent_version(
        prompts=prompts, tools=tools, model=model, params=params, code_paths=code_paths
    )
    return AgentManifest(
        agent_id=module.AGENT_ID,
        agent_version=version,
        model=model,
        params=params,
        prompts=prompts,
        tools=tools,
    )
