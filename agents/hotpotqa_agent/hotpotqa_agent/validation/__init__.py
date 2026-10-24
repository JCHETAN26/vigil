"""HotpotQA agent variants for the controlled regression-triage validation
(bench/hotpotqa_triage.py, docs/HOTPOTQA_VALIDATION.md).

Each variant is the current agent loop (``agent.run_loop``) with one setting changed: the number
of documents a ``search`` returns (``top_k``). Everything else — system prompt, model, request
params, tool name and input schema, turn limit, retriever and corpus — comes from
``hotpotqa_agent.agent`` unchanged.

One importable module per variant (``top_k5``, ``top_k1``), because the engine spawns one worker
subprocess per ``--agent`` module path and computes the version hash from that module's
``prompts``/``tools``/``model``/``params``. To keep that hash tied to behavior, a single
``top_k`` value drives all three places it appears:

  - the search the loop actually runs (``retriever.search(query, top_k)``),
  - the tool description the model sees ("Returns the top {top_k} matching paragraphs"),
  - the manifest ``params["retrieval_top_k"]``, so the version diff names the change.

``params`` is the manifest view; the request sent to the model uses ``request_params``, which
is the default agent's ``PARAMS`` (``retrieval_top_k`` is not a model parameter and is never
sent; the Messages API's own ``top_k`` sampling parameter is not touched). Every variant holds
deep copies of its configuration, so two variants never share a mutable object with each other
or with ``hotpotqa_agent.agent``. The only shared state is the agent's cached read-only BM25
index, built from ``VIGIL_HOTPOTQA_CORPUS`` — the same corpus for every variant by design.

The default agent (``hotpotqa_agent.agent``) is not changed by this package: its defaults and
version hash stay as they were. The ``top_k5`` variant sends the model exactly the requests the
default agent sends; it has a different version hash only because its manifest also records
``retrieval_top_k``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import vigil
from vigil import RunResult

from .. import agent

VARIANT_PARAM = "retrieval_top_k"


@dataclass(frozen=True)
class Variant:
    """One fully-specified agent configuration. Build with ``Variant.build``."""

    top_k: int
    agent_id: str
    model: str
    system: str
    request_params: dict[str, Any] = field(repr=False)
    tools: list[dict[str, Any]] = field(repr=False)

    @classmethod
    def build(cls, top_k: int) -> Variant:
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
            raise ValueError(f"top_k must be an int >= 1, got {top_k!r}")
        return cls(
            top_k=top_k,
            agent_id=agent.AGENT_ID,
            model=agent.MODEL,
            system=agent.SYSTEM_PROMPT,
            request_params=copy.deepcopy(dict(agent.PARAMS)),
            tools=[agent.search_schema(top_k)],
        )

    # Manifest inputs the engine reads (contract §4). Fresh copies on every access, so a
    # caller mutating what it got back cannot change this variant's behavior or hash.
    @property
    def prompts(self) -> dict[str, str]:
        return {"system": self.system}

    @property
    def params(self) -> dict[str, Any]:
        return {**copy.deepcopy(self.request_params), VARIANT_PARAM: self.top_k}

    def manifest_tools(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.tools)

    @property
    def agent_version(self) -> str:
        return vigil.compute_agent_version(
            prompts=self.prompts, tools=self.manifest_tools(), model=self.model, params=self.params
        )

    async def run(
        self,
        client,
        case_input: Any,
        *,
        eval_run_id: str,
        eval_case_id: str,
        trial: int = 0,
    ) -> RunResult:
        return await agent.run_loop(
            client,
            case_input,
            eval_run_id=eval_run_id,
            eval_case_id=eval_case_id,
            trial=trial,
            model=self.model,
            system=self.system,
            tools=copy.deepcopy(self.tools),
            request_params=copy.deepcopy(self.request_params),
            top_k=self.top_k,
        )
