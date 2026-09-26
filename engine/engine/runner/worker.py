"""The eval worker (design §3.2): ``python -m engine.runner.worker``.

One worker subprocess per agent version. It fixes this process's version identity with a
single ``vigil.init``, builds the (wrapped, maybe-cached) ``AsyncAnthropic`` client the agent
will use, then runs the ``(case, trial)`` units the parent dispatches over **stdin** — up to
``concurrency`` in flight — scoring each synchronously and writing one JSON result record per
unit to a **dedicated results fd** (never stdout). stdout/stderr carry logs only. On ``drain``
(or stdin EOF) it finishes in-flight work, flushes spans with ``vigil.shutdown()``, and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import sys
import time

import vigil

from engine.config import EngineConfig, redis_url
from engine.datasets.base import Case
from engine.runner import protocol
from engine.runner.cache import RedisLLMCache, make_store
from engine.runner.cost import CostMeter, default_meter
from engine.runner.execution import execute_unit
from engine.scoring import score_case, scorers_for


def _log(msg: str) -> None:
    print(f"[worker] {msg}", file=sys.stderr, flush=True)


def _build_client(agent_module, cfg: EngineConfig, mode: str):
    """Construct the AsyncAnthropic client: request timeout below the per-case timeout as a
    backstop, then optional dev cache, then Vigil instrumentation. Engine-controlled so
    caching and tracing are guaranteed regardless of the agent."""
    import anthropic

    raw = anthropic.AsyncAnthropic(timeout=cfg.request_timeout_s)
    if cfg.cache_mode != "off":
        url = redis_url()
        if not url:
            raise RuntimeError(
                "cache requested but no Redis URL (set VIGIL_REDIS_URL / REDIS_PASSWORD)"
            )
        raw = RedisLLMCache(raw, make_store(url), mode=mode, cache_mode=cfg.cache_mode)
    return vigil.wrap(raw)


class _Worker:
    def __init__(
        self, agent_module, *, run_id, agent_version, mode, cfg, meter, results_fd, sim_model=None
    ):
        self._agent = agent_module
        self._run_id = run_id
        self._agent_version = agent_version
        self._mode = mode
        self._cfg = cfg
        self._meter = meter
        self._sim_model = sim_model or agent_module.model
        self._results = os.fdopen(results_fd, "wb", buffering=0)
        self._write_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._client = _build_client(agent_module, cfg, mode)

    async def _write(self, record: dict) -> None:
        async with self._write_lock:
            self._results.write(protocol.encode(record))

    async def _handle_unit(self, msg: dict) -> None:
        # Always emit exactly one result per unit. If scoring/serialization itself blows up
        # (not an agent error, which execute_unit already turns into a record), emit an error
        # record instead of letting the task die silently — otherwise the parent, which reads
        # one result per dispatched unit, would block forever (crash behavior, design §3.4).
        try:
            async with self._sem:
                record = await self._run_and_score(msg)
        except Exception as exc:  # noqa: BLE001
            _log(f"unit {msg.get('case_id')}#{msg.get('trial')} failed in worker: {exc!r}")
            record = {
                "type": protocol.RESULT,
                "agent_version": self._agent_version,
                "case_id": msg.get("case_id"),
                "trial": msg.get("trial"),
                "status": "error",
                "attempts": 1,
                "error": f"worker error: {exc!r}",
                "latency_ms": 0,
                "passed": False,
                "score": None,
                "scores": {},
                "trace_id": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "sim_input_tokens": 0,
                "sim_output_tokens": 0,
                "sim_cost_usd": 0.0,
                "output": None,
                "budget_cost": 0.0,
            }
        await self._write(record)

    async def _run_and_score(self, msg: dict) -> dict:
        case = Case(case_id=msg["case_id"], input=msg["input"], expected=msg["expected"], tags=[])
        trial = msg["trial"]

        async def run_call():
            return await self._agent.run(
                self._client,
                case.input,
                eval_run_id=self._run_id,
                eval_case_id=case.case_id,
                trial=trial,
            )

        start = time.monotonic()
        outcome = await execute_unit(
            run_call, timeout=self._cfg.per_case_timeout_s, max_attempts=self._cfg.max_attempts
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        base = {
            "type": protocol.RESULT,
            "agent_version": self._agent_version,
            "case_id": case.case_id,
            "trial": trial,
            "status": outcome.status,
            "attempts": outcome.attempts,
            "error": outcome.error,
            "latency_ms": latency_ms,
        }

        if outcome.status != "ok":
            # Timeout / error: a real failure. No tokens, no cost, no trace linkage.
            base.update(
                passed=False,
                score=None,
                scores={},
                trace_id=None,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                sim_input_tokens=0,
                sim_output_tokens=0,
                sim_cost_usd=0.0,
                output=None,
                budget_cost=0.0,
            )
            return base

        result = outcome.result
        agg = score_case(case, result, scorers_for(case.expected))
        cost_usd = self._meter.cost(self._agent.model, result.input_tokens, result.output_tokens)
        sim_cost_usd = self._meter.cost(
            self._sim_model, result.sim_input_tokens, result.sim_output_tokens
        )
        base.update(
            passed=agg.passed,
            score=agg.score,
            scores=agg.scores,
            trace_id=result.trace_id or None,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=cost_usd,
            sim_input_tokens=result.sim_input_tokens,
            sim_output_tokens=result.sim_output_tokens,
            sim_cost_usd=sim_cost_usd,
            # The full transcript we persist, enough to re-score a run offline without
            # re-running the agent (bench/hotpotqa_baseline.py rebuilds a RunResult from this):
            # the final answer scorers compare, the tool calls, and the per-call retrievals.
            output={
                "output": result.output,
                "final_answer": result.final_answer,
                "tool_calls": [
                    {"name": tc.name, "arguments": tc.arguments} for tc in result.tool_calls
                ],
                "retrievals": [
                    {"query": r.query, "doc_ids": list(r.doc_ids)} for r in result.retrievals
                ],
            },
            # The budget counts agent + simulator cost (design §3.3, §7).
            budget_cost=cost_usd + sim_cost_usd,
        )
        return base

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        tasks: set[asyncio.Task] = set()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:  # stdin closed
                break
            msg = protocol.decode(line)
            if msg.get("type") == protocol.DRAIN:
                break
            if msg.get("type") == protocol.UNIT:
                t = asyncio.create_task(self._handle_unit(msg))
                tasks.add(t)
                t.add_done_callback(tasks.discard)

        if tasks:
            await asyncio.gather(*tasks)
        self._results.flush()
        self._results.close()
        vigil.shutdown()  # flush buffered spans to the receiver


def _parse_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser("engine.runner.worker")
    p.add_argument("--agent", required=True, help="agent module path, e.g. hello_agent.agent")
    p.add_argument("--run-id", required=True)
    p.add_argument("--agent-version", required=True)
    p.add_argument("--mode", required=True, choices=["measurement", "development"])
    p.add_argument("--results-fd", type=int, required=True)
    p.add_argument("--cache-mode", default="off")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--sim-model", default=None)
    p.add_argument("--prices-path", default=None)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    module = importlib.import_module(args.agent)

    cfg = EngineConfig(
        cache_mode=args.cache_mode,
        concurrency=args.concurrency,
        per_case_timeout_s=args.timeout,
        max_attempts=args.max_attempts,
    )

    vigil.init(
        service_name=module.AGENT_ID,
        agent_id=module.AGENT_ID,
        agent_version=args.agent_version,
        git_sha=vigil.git_sha(),
    )

    meter = CostMeter(prices_path=args.prices_path) if args.prices_path else default_meter()
    worker = _Worker(
        module,
        run_id=args.run_id,
        agent_version=args.agent_version,
        mode=args.mode,
        cfg=cfg,
        meter=meter,
        results_fd=args.results_fd,
        sim_model=args.sim_model,
    )
    _log(f"started agent={args.agent} version={args.agent_version[:12]} mode={args.mode}")
    asyncio.run(worker.serve())
    _log("drained and exited")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
