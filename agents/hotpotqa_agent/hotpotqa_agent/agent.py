"""HotpotQA multi-hop RAG agent under test (Session 3).

A tool-use loop with a single ``search`` tool over the pooled BM25 corpus. The model searches
several times to connect facts across paragraphs (bridge/comparison questions), then gives a
short ``FINAL:`` answer. Every search is recorded as a Vigil retrieval span and collected into
``RunResult.retrievals`` so the engine's retrieval scorers (recall@5/@10, nDCG, all-gold) can
grade what was retrieved.

Runs natively on the host and exports OTLP traces to the ingest receiver. The corpus path comes
from ``VIGIL_HOTPOTQA_CORPUS`` (the corpus.jsonl built by bench/build_hotpotqa_corpus.py); the
retriever is built once and cached. Agents do not import the engine.
"""

from __future__ import annotations

import json
import os
from typing import Any

import vigil
from vigil import RunResult, ToolCall

from .retrieval import Retriever, bm25_from_corpus

AGENT_ID = "hotpotqa-agent"

SYSTEM_PROMPT = (
    "You answer multi-hop questions using a Wikipedia `search` tool over a large paragraph "
    "corpus. You will usually need MORE THAN ONE search: first find a bridge entity or the two "
    "entities being compared, then search again for the detail that answers the question. Base "
    "your answer only on retrieved text; do not rely on prior knowledge. For yes/no comparison "
    "questions, answer exactly 'yes' or 'no'. Keep the answer as short as possible — a name, "
    "entity, date, or number, with no explanation. End your reply with a line 'FINAL: <answer>' "
    "containing just that answer."
)

MODEL = os.getenv("VIGIL_HOTPOT_MODEL", os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"))
PARAMS: dict[str, Any] = {"max_tokens": 1024}
TOP_K = 5  # documents returned per search call (first hop → recall@5; two hops → recall@10)
_MAX_TURNS = 8
_SNIPPET_CHARS = 600  # per-doc text shown to the model

SEARCH_SCHEMA = {
    "name": "search",
    "description": (
        "Search the Wikipedia paragraph corpus for text relevant to a query. Returns the top "
        f"{TOP_K} matching paragraphs (title + text). Call it multiple times, refining the "
        "query, to gather the evidence a multi-hop question needs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A focused search query (keywords or a phrase).",
            }
        },
        "required": ["query"],
    },
}
TOOLS = [SEARCH_SCHEMA]

# Manifest inputs the engine reads (contract §4) + the version hash.
prompts = {"system": SYSTEM_PROMPT}
model = MODEL
params = PARAMS
tools = TOOLS
AGENT_VERSION = vigil.compute_agent_version(
    prompts=prompts, tools=TOOLS, model=MODEL, params=PARAMS
)

_RETRIEVER: Retriever | None = None


def _get_retriever() -> Retriever:
    """Lazily build (and cache) the BM25 retriever from VIGIL_HOTPOTQA_CORPUS."""
    global _RETRIEVER
    if _RETRIEVER is None:
        path = os.getenv("VIGIL_HOTPOTQA_CORPUS")
        if not path:
            raise RuntimeError(
                "VIGIL_HOTPOTQA_CORPUS is not set — point it at the corpus.jsonl built by "
                "bench/build_hotpotqa_corpus.py"
            )
        _RETRIEVER = bm25_from_corpus(path)
    return _RETRIEVER


def set_retriever(retriever: Retriever | None) -> None:
    """Inject/reset the retriever (tests, or a dense retriever swap)."""
    global _RETRIEVER
    _RETRIEVER = retriever


def init_tracing():
    return vigil.init(
        service_name=AGENT_ID,
        agent_id=AGENT_ID,
        agent_version=AGENT_VERSION,
        git_sha=vigil.git_sha(),
    )


def make_client(raw: Any | None = None, *, timeout: float | None = None):
    """Return a Vigil-instrumented async Anthropic client (see hello_agent.make_client)."""
    if raw is None:
        import anthropic

        raw = anthropic.AsyncAnthropic(timeout=timeout) if timeout else anthropic.AsyncAnthropic()
    return vigil.wrap(raw)


def _extract_final_answer(text: str) -> str:
    """Terse answer from the last ``FINAL:`` line, else the full stripped text."""
    final = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("FINAL:"):
            final = stripped.split(":", 1)[1].strip()
    return final if final is not None else text.strip()


def _format_results(docs) -> str:
    if not docs:
        return "No results."
    return "\n\n".join(
        f"[{i}] {d.doc_id}: {d.text[:_SNIPPET_CHARS]}" for i, d in enumerate(docs, 1)
    )


def _run_search(retriever: Retriever, query: str) -> tuple[list[str], str]:
    """Execute one search, recording a retrieval span, and return (doc_ids, formatted results)."""
    with vigil.retrieval(name="search", query=query, system="bm25") as span:
        docs = retriever.search(query, TOP_K)
        doc_ids = [d.doc_id for d in docs]
        span.set_attribute("vigil.retrieval.doc_ids", json.dumps(doc_ids))
        span.set_attribute("vigil.retrieval.k", len(doc_ids))
    return doc_ids, _format_results(docs)


async def run(
    client,
    case_input: Any,
    *,
    eval_run_id: str,
    eval_case_id: str,
    trial: int = 0,
) -> RunResult:
    question = case_input.get("question", "") if isinstance(case_input, dict) else str(case_input)
    retriever = _get_retriever()

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
        retrievals: list[vigil.Retrieval] = []
        input_tokens = 0
        output_tokens = 0

        for turn in range(_MAX_TURNS):
            # On the last turn, drop the tools so the model must produce an answer instead of
            # searching again — this bounds cost and guarantees a non-empty final answer even
            # when retrieval never surfaces enough evidence.
            offer_tools = turn < _MAX_TURNS - 1
            request = dict(model=MODEL, system=SYSTEM_PROMPT, messages=messages, **PARAMS)
            if offer_tools:
                request["tools"] = TOOLS
            resp = await client.messages.create(**request)
            usage = getattr(resp, "usage", None)
            if usage is not None:
                input_tokens += getattr(usage, "input_tokens", 0) or 0
                output_tokens += getattr(usage, "output_tokens", 0) or 0
            messages.append({"role": "assistant", "content": resp.content})

            if offer_tools and getattr(resp, "stop_reason", None) == "tool_use":
                tool_results = []
                for block in resp.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    args = dict(block.input or {})
                    tool_calls.append(ToolCall(name=block.name, arguments=args))
                    if block.name == "search":
                        query = str(args.get("query", ""))
                        doc_ids, result_text = _run_search(retriever, query)
                        retrievals.append(vigil.Retrieval(query=query, doc_ids=doc_ids))
                    else:
                        result_text = f"error: unknown tool {block.name!r}"
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result_text}
                    )
                messages.append({"role": "user", "content": tool_results})
                continue

            answer = "".join(
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
            )
            break

        final_answer = _extract_final_answer(answer)
        span.set_attribute("vigil.answer_chars", len(answer))
        span.set_attribute("vigil.search_count", len(retrievals))
        return RunResult(
            output=answer,
            final_answer=final_answer,
            tool_calls=tool_calls,
            retrievals=retrievals,
            trace_id=trace_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
