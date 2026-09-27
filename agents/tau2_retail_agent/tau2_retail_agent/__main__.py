"""CLI: run the τ² retail agent against the running stack on one or more task ids.

    VIGIL_TAU2_PYTHON=deps/tau2-bench/.venv/bin/python \\
        python -m tau2_retail_agent 0 1 2

Requires ANTHROPIC_API_KEY, the Vigil stack (OTLP receiver), the τ² venv + prefix artifact.
Traces are flushed on exit.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import vigil

from . import agent


async def _amain(task_ids: list[str]) -> int:
    agent.init_tracing()
    client = agent.make_client()
    run_id = "cli-" + uuid.uuid4().hex[:12]
    try:
        for tid in task_ids:
            r = await agent.run(client, {"task_id": tid}, eval_run_id=run_id, eval_case_id=tid)
            info = r.info
            print(
                f"task {tid}: reward={info['reward']} db_match={info['db_match']} "
                f"turns={info['turns']}"
            )
            print(f"   tools={len(r.tool_calls)} cache_write={r.cache_creation_input_tokens} "
                  f"cache_read={r.cache_read_input_tokens} trace_id={r.trace_id}")
    finally:
        await agent._SERVER.shutdown()  # noqa: SLF001
        vigil.shutdown()
    return 0


def main(argv: list[str]) -> int:
    return asyncio.run(_amain(argv[1:] or ["0"]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
