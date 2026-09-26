"""CLI: run the HotpotQA agent against the running stack on ad-hoc questions.

    VIGIL_HOTPOTQA_CORPUS=data/hotpotqa/corpus.v1.jsonl \\
        python -m hotpotqa_agent "Which magazine was started first, Arthur's or First for Women?"

Requires ANTHROPIC_API_KEY, the Vigil stack (OTLP receiver on 127.0.0.1:4317), and the built
corpus. Traces are flushed on exit.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import vigil

from . import agent

DEFAULT_QUESTIONS = [
    "Which magazine was started first, Arthur's Magazine or First for Women?",
]


async def _amain(questions: list[str]) -> int:
    agent.init_tracing()
    client = agent.make_client()
    run_id = "cli-" + uuid.uuid4().hex[:12]
    try:
        for i, q in enumerate(questions):
            result = await agent.run(client, q, eval_run_id=run_id, eval_case_id=f"q{i}")
            print(f"Q: {q}")
            print(f"A: {result.final_answer}")
            print(f"   searches={len(result.retrievals)} trace_id={result.trace_id}")
    finally:
        vigil.shutdown()
    return 0


def main(argv: list[str]) -> int:
    return asyncio.run(_amain(argv[1:] or DEFAULT_QUESTIONS))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
