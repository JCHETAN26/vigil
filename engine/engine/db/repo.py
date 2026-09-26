"""Typed data-access for the eval metadata + results store.

SQL lives in module-level constants so the sync ``Repo`` (used by the orchestrator, the
single Postgres writer — design §3.1) and the ``AsyncRepo`` (for async callers) run exactly
the same statements; only the ``execute``/``await execute`` differs. There is no ORM: SQL is
the source of truth (design decision §1).

jsonb columns are passed through ``psycopg.types.json.Jsonb`` so dicts/lists round-trip as
JSON; ``text[]`` (tags) is adapted from a Python list automatically.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

# --- row / input types ---------------------------------------------------------------


@dataclass
class Suite:
    id: uuid.UUID
    name: str
    adapter: str
    config: dict
    created_at: datetime | None = None


@dataclass
class CaseRow:
    suite_id: uuid.UUID
    case_id: str
    input: Any
    expected: dict
    tags: list[str] = field(default_factory=list)


@dataclass
class CaseResultInsert:
    run_id: uuid.UUID
    agent_version: str
    case_id: str
    trial: int
    status: str  # ok | error | timeout
    trace_id: str | None = None
    passed: bool | None = None
    score: float | None = None
    scores: dict = field(default_factory=dict)
    error: str | None = None
    attempts: int = 1
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    sim_input_tokens: int | None = None
    sim_output_tokens: int | None = None
    sim_cost_usd: float | None = None
    latency_ms: int | None = None
    output: Any = None


# --- SQL -----------------------------------------------------------------------------

_UPSERT_MANIFEST = """
INSERT INTO version_manifests
    (agent_id, agent_version, git_sha, model, params, prompts, tools, code_ref)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (agent_id, agent_version) DO NOTHING
"""

_INSERT_SUITE = """
INSERT INTO eval_suites (name, adapter, config)
VALUES (%s, %s, %s)
RETURNING id, name, adapter, config, created_at
"""

_SELECT_SUITE_BY_NAME = """
SELECT id, name, adapter, config, created_at FROM eval_suites WHERE name = %s
"""

_INSERT_CASE = """
INSERT INTO eval_cases (suite_id, case_id, input, expected, tags)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (suite_id, case_id) DO NOTHING
"""

_SELECT_CASES = """
SELECT suite_id, case_id, input, expected, tags
FROM eval_cases WHERE suite_id = %s ORDER BY case_id
"""

_INSERT_RUN = """
INSERT INTO eval_runs
    (suite_id, agent_id, mode, status, cost_budget_usd, concurrency,
     trials_per_case, cache_mode, git_sha, notes)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING id
"""

_SET_RUN_STATUS = """
UPDATE eval_runs
SET status = %s,
    started_at = COALESCE(started_at, CASE WHEN %s = 'running' THEN now() END),
    finished_at = CASE WHEN %s IN ('succeeded', 'failed', 'aborted') THEN now() ELSE finished_at END
WHERE id = %s
"""

_ADD_RUN_COST = "UPDATE eval_runs SET cost_spent_usd = cost_spent_usd + %s WHERE id = %s"

_INSERT_RUN_VERSION = """
INSERT INTO eval_run_versions (run_id, agent_version, status, cases_total)
VALUES (%s, %s, %s, %s)
ON CONFLICT (run_id, agent_version) DO NOTHING
"""

_UPDATE_RUN_VERSION_PROGRESS = """
UPDATE eval_run_versions
SET cases_done = cases_done + %s, cost_spent_usd = cost_spent_usd + %s
WHERE run_id = %s AND agent_version = %s
"""

_SET_RUN_VERSION_STATUS = """
UPDATE eval_run_versions
SET status = %s,
    started_at = COALESCE(started_at, CASE WHEN %s = 'running' THEN now() END),
    finished_at = CASE WHEN %s IN ('succeeded', 'failed', 'aborted') THEN now() ELSE finished_at END
WHERE run_id = %s AND agent_version = %s
"""

_INSERT_CASE_RESULT = """
INSERT INTO eval_case_results
    (run_id, agent_version, case_id, trial, trace_id, passed, score, scores, status, error,
     attempts, input_tokens, output_tokens, cost_usd, sim_input_tokens, sim_output_tokens,
     sim_cost_usd, latency_ms, output)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING id
"""

_COUNT_CASE_RESULTS = "SELECT count(*) FROM eval_case_results WHERE run_id = %s"

_SELECT_CASE_RESULTS = """
SELECT agent_version, case_id, trial, status, passed, score, trace_id
FROM eval_case_results WHERE run_id = %s
ORDER BY agent_version, case_id, trial
"""

_SELECT_RUN_VERSIONS = """
SELECT agent_version, status, cost_spent_usd, cases_total, cases_done
FROM eval_run_versions WHERE run_id = %s ORDER BY agent_version
"""


# --- param builders (shared by sync + async) -----------------------------------------


def _manifest_params(agent_id, agent_version, git_sha, model, params, prompts, tools, code_ref):
    return (
        agent_id,
        agent_version,
        git_sha,
        model,
        Jsonb(params),
        Jsonb(prompts),
        Jsonb(tools),
        Jsonb(code_ref) if code_ref is not None else None,
    )


def _case_params(c: CaseRow):
    return (c.suite_id, c.case_id, Jsonb(c.input), Jsonb(c.expected), list(c.tags))


def _run_params(
    suite_id,
    agent_id,
    mode,
    cost_budget_usd,
    concurrency,
    trials_per_case,
    cache_mode,
    git_sha,
    notes,
):
    return (
        suite_id,
        agent_id,
        mode,
        "pending",
        cost_budget_usd,
        concurrency,
        trials_per_case,
        cache_mode,
        git_sha,
        notes,
    )


def _case_result_params(r: CaseResultInsert):
    return (
        r.run_id,
        r.agent_version,
        r.case_id,
        r.trial,
        r.trace_id,
        r.passed,
        r.score,
        Jsonb(r.scores),
        r.status,
        r.error,
        r.attempts,
        r.input_tokens,
        r.output_tokens,
        r.cost_usd,
        r.sim_input_tokens,
        r.sim_output_tokens,
        r.sim_cost_usd,
        r.latency_ms,
        Jsonb(r.output) if r.output is not None else None,
    )


def _suite_from_row(row) -> Suite:
    return Suite(id=row[0], name=row[1], adapter=row[2], config=row[3], created_at=row[4])


def _case_from_row(row) -> CaseRow:
    return CaseRow(
        suite_id=row[0], case_id=row[1], input=row[2], expected=row[3], tags=list(row[4])
    )


# --- sync repo -----------------------------------------------------------------------


class Repo:
    """Synchronous data-access over a psycopg connection. The caller owns the connection
    and its transactions (the orchestrator commits after streaming a batch of results)."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def upsert_manifest(
        self, *, agent_id, agent_version, git_sha, model, params, prompts, tools, code_ref=None
    ) -> None:
        self.conn.execute(
            _UPSERT_MANIFEST,
            _manifest_params(
                agent_id, agent_version, git_sha, model, params, prompts, tools, code_ref
            ),
        )

    def create_suite(self, *, name, adapter, config) -> Suite:
        row = self.conn.execute(_INSERT_SUITE, (name, adapter, Jsonb(config))).fetchone()
        return _suite_from_row(row)

    def get_suite_by_name(self, name: str) -> Suite | None:
        row = self.conn.execute(_SELECT_SUITE_BY_NAME, (name,)).fetchone()
        return _suite_from_row(row) if row else None

    def add_cases(self, cases: list[CaseRow]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(_INSERT_CASE, [_case_params(c) for c in cases])

    def get_cases(self, suite_id) -> list[CaseRow]:
        rows = self.conn.execute(_SELECT_CASES, (suite_id,)).fetchall()
        return [_case_from_row(r) for r in rows]

    def create_run(
        self,
        *,
        suite_id,
        agent_id,
        mode,
        cost_budget_usd=None,
        concurrency=4,
        trials_per_case=1,
        cache_mode="off",
        git_sha=None,
        notes=None,
    ) -> uuid.UUID:
        row = self.conn.execute(
            _INSERT_RUN,
            _run_params(
                suite_id,
                agent_id,
                mode,
                cost_budget_usd,
                concurrency,
                trials_per_case,
                cache_mode,
                git_sha,
                notes,
            ),
        ).fetchone()
        return row[0]

    def set_run_status(self, run_id, status: str) -> None:
        self.conn.execute(_SET_RUN_STATUS, (status, status, status, run_id))

    def add_run_cost(self, run_id, delta: float) -> None:
        self.conn.execute(_ADD_RUN_COST, (delta, run_id))

    def create_run_version(self, *, run_id, agent_version, cases_total=0, status="pending") -> None:
        self.conn.execute(_INSERT_RUN_VERSION, (run_id, agent_version, status, cases_total))

    def bump_run_version_progress(
        self, *, run_id, agent_version, cases_done=1, cost_delta=0.0
    ) -> None:
        self.conn.execute(
            _UPDATE_RUN_VERSION_PROGRESS, (cases_done, cost_delta, run_id, agent_version)
        )

    def set_run_version_status(self, *, run_id, agent_version, status: str) -> None:
        self.conn.execute(_SET_RUN_VERSION_STATUS, (status, status, status, run_id, agent_version))

    def insert_case_result(self, r: CaseResultInsert) -> int:
        row = self.conn.execute(_INSERT_CASE_RESULT, _case_result_params(r)).fetchone()
        return row[0]

    def count_case_results(self, run_id) -> int:
        return self.conn.execute(_COUNT_CASE_RESULTS, (run_id,)).fetchone()[0]

    def get_case_results(self, run_id) -> list[dict]:
        rows = self.conn.execute(_SELECT_CASE_RESULTS, (run_id,)).fetchall()
        keys = ("agent_version", "case_id", "trial", "status", "passed", "score", "trace_id")
        return [dict(zip(keys, r)) for r in rows]

    def get_run_versions(self, run_id) -> list[dict]:
        rows = self.conn.execute(_SELECT_RUN_VERSIONS, (run_id,)).fetchall()
        keys = ("agent_version", "status", "cost_spent_usd", "cases_total", "cases_done")
        return [dict(zip(keys, r)) for r in rows]


# --- async repo ----------------------------------------------------------------------


class AsyncRepo:
    """Asynchronous mirror of ``Repo`` over a psycopg AsyncConnection. Same SQL, awaited."""

    def __init__(self, conn: psycopg.AsyncConnection):
        self.conn = conn

    async def upsert_manifest(
        self, *, agent_id, agent_version, git_sha, model, params, prompts, tools, code_ref=None
    ) -> None:
        await self.conn.execute(
            _UPSERT_MANIFEST,
            _manifest_params(
                agent_id, agent_version, git_sha, model, params, prompts, tools, code_ref
            ),
        )

    async def create_suite(self, *, name, adapter, config) -> Suite:
        cur = await self.conn.execute(_INSERT_SUITE, (name, adapter, Jsonb(config)))
        return _suite_from_row(await cur.fetchone())

    async def get_suite_by_name(self, name: str) -> Suite | None:
        cur = await self.conn.execute(_SELECT_SUITE_BY_NAME, (name,))
        row = await cur.fetchone()
        return _suite_from_row(row) if row else None

    async def add_cases(self, cases: list[CaseRow]) -> None:
        async with self.conn.cursor() as cur:
            await cur.executemany(_INSERT_CASE, [_case_params(c) for c in cases])

    async def get_cases(self, suite_id) -> list[CaseRow]:
        cur = await self.conn.execute(_SELECT_CASES, (suite_id,))
        return [_case_from_row(r) for r in await cur.fetchall()]

    async def create_run(
        self,
        *,
        suite_id,
        agent_id,
        mode,
        cost_budget_usd=None,
        concurrency=4,
        trials_per_case=1,
        cache_mode="off",
        git_sha=None,
        notes=None,
    ) -> uuid.UUID:
        cur = await self.conn.execute(
            _INSERT_RUN,
            _run_params(
                suite_id,
                agent_id,
                mode,
                cost_budget_usd,
                concurrency,
                trials_per_case,
                cache_mode,
                git_sha,
                notes,
            ),
        )
        return (await cur.fetchone())[0]

    async def set_run_status(self, run_id, status: str) -> None:
        await self.conn.execute(_SET_RUN_STATUS, (status, status, status, run_id))

    async def add_run_cost(self, run_id, delta: float) -> None:
        await self.conn.execute(_ADD_RUN_COST, (delta, run_id))

    async def create_run_version(
        self, *, run_id, agent_version, cases_total=0, status="pending"
    ) -> None:
        await self.conn.execute(_INSERT_RUN_VERSION, (run_id, agent_version, status, cases_total))

    async def bump_run_version_progress(
        self, *, run_id, agent_version, cases_done=1, cost_delta=0.0
    ) -> None:
        await self.conn.execute(
            _UPDATE_RUN_VERSION_PROGRESS, (cases_done, cost_delta, run_id, agent_version)
        )

    async def set_run_version_status(self, *, run_id, agent_version, status: str) -> None:
        await self.conn.execute(
            _SET_RUN_VERSION_STATUS, (status, status, status, run_id, agent_version)
        )

    async def insert_case_result(self, r: CaseResultInsert) -> int:
        cur = await self.conn.execute(_INSERT_CASE_RESULT, _case_result_params(r))
        return (await cur.fetchone())[0]

    async def count_case_results(self, run_id) -> int:
        cur = await self.conn.execute(_COUNT_CASE_RESULTS, (run_id,))
        return (await cur.fetchone())[0]

    async def get_case_results(self, run_id) -> list[dict]:
        cur = await self.conn.execute(_SELECT_CASE_RESULTS, (run_id,))
        keys = ("agent_version", "case_id", "trial", "status", "passed", "score", "trace_id")
        return [dict(zip(keys, r)) for r in await cur.fetchall()]

    async def get_run_versions(self, run_id) -> list[dict]:
        cur = await self.conn.execute(_SELECT_RUN_VERSIONS, (run_id,))
        keys = ("agent_version", "status", "cost_spent_usd", "cases_total", "cases_done")
        return [dict(zip(keys, r)) for r in await cur.fetchall()]
