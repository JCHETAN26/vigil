"""Validation candidate: the current HotpotQA agent with ``top_k = 1`` — the one controlled
change against ``top_k5``. Use as ``--agent hotpotqa_agent.validation.top_k1``."""

from __future__ import annotations

from . import Variant

_VARIANT = Variant.build(top_k=1)

AGENT_ID = _VARIANT.agent_id
TOP_K = _VARIANT.top_k
prompts = _VARIANT.prompts
tools = _VARIANT.manifest_tools()
model = _VARIANT.model
params = _VARIANT.params
AGENT_VERSION = _VARIANT.agent_version
run = _VARIANT.run
