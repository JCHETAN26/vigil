"""The orchestrator (design §3.1, §3.3): the single Postgres writer.

It loads the suite and its materialized cases, upserts each version manifest, creates the
``eval_runs`` + ``eval_run_versions`` rows, spawns one worker subprocess per agent version,
and runs the windowed ``Scheduler`` — persisting each streamed result and enforcing the
per-run cost budget across all version workers. Results stream and persist incrementally, so a
crash leaves partial ``eval_case_results`` with the run marked failed/aborted.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from dataclasses import dataclass
from typing import Any

import psycopg
import vigil

from engine.config import EngineConfig
from engine.db.repo import CaseResultInsert, Repo
from engine.runner import protocol
from engine.runner.cache import check_refusal
from engine.runner.contract import manifest_of
from engine.runner.scheduler import Scheduler, SchedulerOutcome


@dataclass
class Unit:
    agent_version: str
    case_id: str
    trial: int
    input: Any
    expected: dict


class WorkerCrashed(RuntimeError):
    """A worker subprocess exited non-zero before finishing its units."""


# --- real worker pool over subprocesses + pipes --------------------------------------


class SubprocessWorkerPool:
    """One worker subprocess per agent version. Units go to a worker over its **stdin**;
    results come back over a **dedicated pipe fd** (not stdout) and are merged into one queue
    the scheduler reads. Implements the ``WorkerPool`` protocol."""

    def __init__(self, specs: dict[str, str], *, run_id: str, mode: str, cfg: EngineConfig):
        # specs: {agent_version -> agent module path}
        self._specs = specs
        self._run_id = run_id
        self._mode = mode
        self._cfg = cfg
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._readers: list[asyncio.Task] = []
        self._monitors: list[asyncio.Task] = []
        self._queue: asyncio.Queue = asyncio.Queue()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for version, module_path in self._specs.items():
            r_fd, w_fd = os.pipe()
            os.set_inheritable(w_fd, True)
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "engine.runner.worker",
                "--agent",
                module_path,
                "--run-id",
                str(self._run_id),
                "--agent-version",
                version,
                "--mode",
                self._mode,
                "--results-fd",
                str(w_fd),
                "--cache-mode",
                self._cfg.cache_mode,
                "--concurrency",
                str(self._cfg.concurrency),
                "--timeout",
                str(self._cfg.per_case_timeout_s),
                "--max-attempts",
                str(self._cfg.max_attempts),
                stdin=asyncio.subprocess.PIPE,
                pass_fds=(w_fd,),
            )
            os.close(w_fd)  # parent drops its copy; the child holds the only write end
            self._procs[version] = proc

            reader = asyncio.StreamReader()
            await loop.connect_read_pipe(
                lambda r=reader: asyncio.StreamReaderProtocol(r),
                os.fdopen(r_fd, "rb", buffering=0),
            )
            self._readers.append(asyncio.create_task(self._pump(reader)))
            self._monitors.append(asyncio.create_task(self._monitor(version, proc)))

    async def _pump(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            await self._queue.put(protocol.decode(line))

    async def _monitor(self, version: str, proc: asyncio.subprocess.Process) -> None:
        # If a worker exits non-zero, inject a crash marker so next_result surfaces it promptly
        # instead of the scheduler blocking forever on a result that will never come.
        rc = await proc.wait()
        if rc != 0:
            await self._queue.put({"__worker_crashed__": version, "returncode": rc})

    async def submit(self, unit: Unit) -> None:
        proc = self._procs[unit.agent_version]
        msg = protocol.unit_message(
            agent_version=unit.agent_version,
            case_id=unit.case_id,
            trial=unit.trial,
            input=unit.input,
            expected=unit.expected,
        )
        proc.stdin.write(protocol.encode(msg))
        await proc.stdin.drain()

    async def next_result(self) -> dict:
        rec = await self._queue.get()
        crashed = rec.get("__worker_crashed__") if isinstance(rec, dict) else None
        if crashed is not None:
            raise WorkerCrashed(
                f"worker for version {crashed[:12]} exited with code {rec['returncode']} "
                "before finishing its units"
            )
        return rec

    async def drain(self) -> None:
        for proc in self._procs.values():
            try:
                proc.stdin.write(protocol.encode(protocol.drain_message()))
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def close(self) -> None:
        """Wait for workers to exit (they exit on drain), killing any stragglers."""
        for proc in self._procs.values():
            try:
                await asyncio.wait_for(proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
        for task in [*self._readers, *self._monitors]:
            task.cancel()


# --- orchestrator --------------------------------------------------------------------


class Orchestrator:
    def __init__(self, conn: psycopg.Connection, cfg: EngineConfig | None = None):
        self.conn = conn
        self.repo = Repo(conn)
        self.cfg = cfg or EngineConfig.from_env()

    def _resolve_versions(self, agent_modules: list[str]) -> dict[str, tuple[str, Any]]:
        """Map agent_version -> (module_path, manifest) for each agent module under test."""
        out: dict[str, tuple[str, Any]] = {}
        for path in agent_modules:
            module = importlib.import_module(path)
            manifest = manifest_of(module)
            out[manifest.agent_version] = (path, manifest)
        return out

    async def run(
        self,
        *,
        suite_name: str,
        agent_modules: list[str],
        mode: str,
        cost_budget_usd: float | None = None,
        concurrency: int | None = None,
        trials_per_case: int | None = None,
        cache_mode: str | None = None,
        notes: str | None = None,
    ) -> dict:
        cache_mode = cache_mode or self.cfg.cache_mode
        concurrency = concurrency or self.cfg.concurrency
        trials = trials_per_case or self.cfg.trials_per_case
        budget = cost_budget_usd if cost_budget_usd is not None else self.cfg.cost_budget_usd

        # Fail fast, before creating any run row: a cached measurement is refused (design §7).
        check_refusal(mode, cache_mode)

        suite = self.repo.get_suite_by_name(suite_name)
        if suite is None:
            raise KeyError(f"no suite named {suite_name!r}; create it first")
        cases = self.repo.get_cases(suite.id)
        if not cases:
            raise ValueError(f"suite {suite_name!r} has no cases")

        versions = self._resolve_versions(agent_modules)
        agent_ids = {m.agent_id for _, m in versions.values()}
        if len(agent_ids) != 1:
            raise ValueError(f"all versions must share one agent_id, got {agent_ids}")
        agent_id = agent_ids.pop()

        git = vigil.git_sha() or ""
        # This run's config, effective concurrency/trials/cache, persisted for reproducibility.
        cfg = EngineConfig(
            cache_mode=cache_mode,
            cost_budget_usd=budget,
            concurrency=concurrency,
            trials_per_case=trials,
            per_case_timeout_s=self.cfg.per_case_timeout_s,
            max_attempts=self.cfg.max_attempts,
        )
        run_id = self.repo.create_run(
            suite_id=suite.id,
            agent_id=agent_id,
            mode=mode,
            cost_budget_usd=budget,
            concurrency=concurrency,
            trials_per_case=trials,
            cache_mode=cache_mode,
            git_sha=git,
            notes=notes,
        )
        for version, (_, manifest) in versions.items():
            self.repo.upsert_manifest(
                agent_id=manifest.agent_id,
                agent_version=version,
                git_sha=git,
                model=manifest.model,
                params=manifest.params,
                prompts=manifest.prompts,
                tools=manifest.tools,
            )
            self.repo.create_run_version(
                run_id=run_id, agent_version=version, cases_total=len(cases) * trials
            )
        self.repo.set_run_status(run_id, "running")
        self.conn.commit()

        units = [
            Unit(agent_version=v, case_id=c.case_id, trial=t, input=c.input, expected=c.expected)
            for v in versions
            for c in cases
            for t in range(trials)
        ]

        def persist(record: dict) -> None:
            self.repo.insert_case_result(
                CaseResultInsert(
                    run_id=run_id,
                    agent_version=record["agent_version"],
                    case_id=record["case_id"],
                    trial=record["trial"],
                    status=record["status"],
                    trace_id=record.get("trace_id"),
                    passed=record.get("passed"),
                    score=record.get("score"),
                    scores=record.get("scores") or {},
                    error=record.get("error"),
                    attempts=record.get("attempts", 1),
                    input_tokens=record.get("input_tokens"),
                    output_tokens=record.get("output_tokens"),
                    cost_usd=record.get("cost_usd"),
                    sim_input_tokens=record.get("sim_input_tokens"),
                    sim_output_tokens=record.get("sim_output_tokens"),
                    sim_cost_usd=record.get("sim_cost_usd"),
                    latency_ms=record.get("latency_ms"),
                    output=record.get("output"),
                )
            )
            self.repo.bump_run_version_progress(
                run_id=run_id,
                agent_version=record["agent_version"],
                cost_delta=record.get("budget_cost", 0.0),
            )
            self.repo.add_run_cost(run_id, record.get("budget_cost", 0.0))
            self.conn.commit()

        pool = SubprocessWorkerPool(
            {v: path for v, (path, _) in versions.items()},
            run_id=run_id,
            mode=mode,
            cfg=cfg,
        )
        outcome: SchedulerOutcome
        try:
            await pool.start()
            scheduler = Scheduler(
                units,
                pool,
                concurrency=concurrency,
                budget=budget,
                cost_of=lambda r: r.get("budget_cost", 0.0),
                on_result=persist,
            )
            overall = max(
                60.0, len(units) * self.cfg.per_case_timeout_s * self.cfg.max_attempts + 30.0
            )
            outcome = await asyncio.wait_for(scheduler.run(), timeout=overall)
        except Exception:
            self.repo.set_run_status(run_id, "failed")
            self.conn.commit()
            await pool.close()
            raise

        await pool.close()

        final = "aborted" if outcome.aborted else "succeeded"
        for version in versions:
            self.repo.set_run_version_status(run_id=run_id, agent_version=version, status=final)
        self.repo.set_run_status(run_id, final)
        self.conn.commit()

        return {
            "run_id": run_id,
            "status": final,
            "aborted": outcome.aborted,
            "abort_reason": outcome.abort_reason,
            "cost": outcome.cost,
            "completed": outcome.completed,
        }
