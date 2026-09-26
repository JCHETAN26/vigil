"""CLI: run the hello agent against the running stack.

    python -m hello_agent "What is 23 * 19?" "Weather in Paris?"

Requires ANTHROPIC_API_KEY in the environment and the Vigil stack up (OTLP receiver on
127.0.0.1:4317). Traces are flushed on exit.
"""

from __future__ import annotations

import sys
import uuid

import vigil

from . import agent

DEFAULT_QUESTIONS = ["What is 23 * 19?", "What's the weather in Paris?"]


def main(argv: list[str]) -> int:
    questions = argv[1:] or DEFAULT_QUESTIONS
    agent.init_tracing()
    client = agent.make_client()
    run_id = "cli-" + uuid.uuid4().hex[:12]
    try:
        for i, q in enumerate(questions):
            result = agent.run(client, q, eval_run_id=run_id, eval_case_id=f"q{i}")
            print(f"Q: {q}")
            print(f"A: {result['answer']}")
            print(f"   trace_id={result['trace_id']} agent_version={result['agent_version'][:12]}…")
    finally:
        vigil.shutdown()  # flush buffered spans to the receiver before exit
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
