"""Read-only Postgres store behind the regression gate (``engine.gate.TriageStore``).

Wraps an async ``fetchall(sql, params) -> list[dict]`` so the dashboard API (read-only role,
connection pool) and the CLI (an AsyncConnection) run the same bound statements. Only SELECTs
live here; every value is a ``%s`` parameter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import psycopg
from psycopg.rows import dict_row

Fetchall = Callable[[str, tuple], Awaitable[list[dict]]]

_RUN = """
SELECT r.id, r.suite_id, s.name AS suite, s.adapter AS suite_adapter, s.config AS suite_config,
       r.agent_id, r.mode, r.status, r.trials_per_case, r.cache_mode, r.git_sha, r.created_at
FROM eval_runs r JOIN eval_suites s ON s.id = r.suite_id
WHERE r.id = %s
"""

# Versions registered for the run, plus any that only appear in results (defensive: the
# orchestrator registers every version, but a result row is the ground truth).
# The fallback NULLs are cast to the eval_run_versions column types: under SELECT DISTINCT an
# untyped NULL resolves to text before the UNION sees it, and text can't match int.
_RUN_VERSIONS_UNION = """
SELECT v.agent_version, v.status, v.cases_total, v.cases_done
FROM eval_run_versions v WHERE v.run_id = %s
UNION ALL
SELECT DISTINCT r.agent_version, NULL::text, NULL::int, NULL::int
FROM eval_case_results r
WHERE r.run_id = %s
  AND NOT EXISTS (SELECT 1 FROM eval_run_versions v
                  WHERE v.run_id = r.run_id AND v.agent_version = r.agent_version)
"""
_RUN_VERSIONS = _RUN_VERSIONS_UNION + "ORDER BY 1\n"
# The same set, one bounded page at a time (the dashboard's version picker). agent_version is
# unique within the set, so ordering by it alone keeps pages stable.
_RUN_VERSIONS_PAGE = _RUN_VERSIONS + "LIMIT %s OFFSET %s\n"
_RUN_VERSIONS_COUNT = f"SELECT count(*) AS n FROM ({_RUN_VERSIONS_UNION}) AS versions"

_SUITE_CASE_IDS = "SELECT case_id FROM eval_cases WHERE suite_id = %s ORDER BY case_id"

_RESULTS = """
SELECT case_id, trial, status, passed, scores, cost_usd, latency_ms, error, trace_id
FROM eval_case_results
WHERE run_id = %s AND agent_version = %s
ORDER BY case_id, trial
"""

_MANIFEST = """
SELECT agent_id, agent_version, git_sha, model, params, prompts, tools, code_ref, created_at
FROM version_manifests WHERE agent_id = %s AND agent_version = %s
"""


class PgTriageStore:
    def __init__(self, fetchall: Fetchall):
        self._fetchall = fetchall

    async def get_run(self, run_id) -> dict | None:
        rows = await self._fetchall(_RUN, (run_id,))
        return rows[0] if rows else None

    async def get_run_versions(self, run_id) -> list[dict]:
        return await self._fetchall(_RUN_VERSIONS, (run_id, run_id))

    async def get_run_versions_page(
        self, run_id, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        """One page of ``get_run_versions`` (same rows, same order) plus the total count."""
        rows = await self._fetchall(_RUN_VERSIONS_PAGE, (run_id, run_id, limit, offset))
        total = await self._fetchall(_RUN_VERSIONS_COUNT, (run_id, run_id))
        return rows, int(total[0]["n"])

    async def get_suite_case_ids(self, suite_id) -> list[str]:
        return [r["case_id"] for r in await self._fetchall(_SUITE_CASE_IDS, (suite_id,))]

    async def get_results(self, run_id, agent_version) -> list[dict]:
        return await self._fetchall(_RESULTS, (run_id, agent_version))

    async def get_manifest(self, agent_id, agent_version) -> dict | None:
        rows = await self._fetchall(_MANIFEST, (agent_id, agent_version))
        return rows[0] if rows else None


def connection_fetchall(conn: psycopg.AsyncConnection) -> Fetchall:
    """``fetchall`` over a single async connection (the CLI path)."""

    async def fetchall(sql: str, params: tuple = ()) -> list[dict]:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    return fetchall
