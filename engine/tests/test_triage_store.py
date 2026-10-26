"""PgTriageStore against a real (throwaway) Postgres: the bound SELECTs return what the gate
expects — per-version rows only, failed trials with status/error, numeric→float, manifests.
Skips cleanly when Postgres is not configured/reachable (see conftest)."""

from __future__ import annotations

import pytest

from engine import gate
from engine.db.repo import AsyncRepo, CaseResultInsert, CaseRow
from engine.db.triage import PgTriageStore, connection_fetchall

V1 = "1" * 64
V2 = "2" * 64


async def _seed(conn):
    repo = AsyncRepo(conn)
    suite = await repo.create_suite(name="triage-s", adapter="local", config={"version": "1"})
    await repo.add_cases([CaseRow(suite_id=suite.id, case_id=f"c{i}", input={}, expected={})
                          for i in range(3)])
    run = await repo.create_run(suite_id=suite.id, agent_id="agent", mode="development",
                                trials_per_case=1, git_sha="abc")
    for v in (V1, V2):
        await repo.create_run_version(run_id=run, agent_version=v, cases_total=3)
        await repo.set_run_version_status(run_id=run, agent_version=v, status="succeeded")
        await repo.upsert_manifest(agent_id="agent", agent_version=v, git_sha="abc",
                                   model="m", params={"t": 0 if v == V1 else 1},
                                   prompts={"s": "p"}, tools=[])
    await repo.set_run_status(run, "succeeded")
    for i in range(3):
        await repo.insert_case_result(CaseResultInsert(
            run_id=run, agent_version=V1, case_id=f"c{i}", trial=0, status="ok", passed=True,
            scores={"TokenF1": {"score": 0.9}}, cost_usd=0.0125, latency_ms=10,
            trace_id=f"{i:032x}"))
        await repo.insert_case_result(CaseResultInsert(
            run_id=run, agent_version=V2, case_id=f"c{i}", trial=0,
            status="error" if i == 0 else "ok", passed=i != 0, error="x" if i == 0 else None,
            scores={} if i == 0 else {"TokenF1": {"score": 0.5}}, cost_usd=0.0, latency_ms=10))
    await conn.commit()
    return run


async def test_pg_store_feeds_the_gate(async_pg_conn):
    run = await _seed(async_pg_conn)
    store = PgTriageStore(connection_fetchall(async_pg_conn))

    with pytest.raises(gate.ComparisonError, match="never averaged"):
        await gate.load_inputs(store, str(run), str(run))
    inputs = await gate.load_inputs(store, str(run), str(run), V1, V2)
    assert {r["case_id"] for r in inputs.baseline_rows} == {"c0", "c1", "c2"}
    assert len(inputs.baseline_rows) == 3 and len(inputs.candidate_rows) == 3  # one version each
    assert inputs.baseline_rows[0]["cost_usd"] == 0.0125  # numeric → float
    assert inputs.candidate_rows[0]["status"] == "error" and inputs.candidate_rows[0]["error"]
    assert inputs.suite_case_ids == ["c0", "c1", "c2"]
    assert inputs.baseline.versions_in_run == [V1, V2]

    cmp = gate.compare(inputs, gate.DEFAULT_POLICY.with_overrides(n_boot=1000))
    assert cmp.complete and cmp.coverage["candidate"]["failed_trials"] == 1
    m = await store.get_manifest("agent", V2)
    assert m["params"] == {"t": 1} and m["model"] == "m"


async def test_run_versions_results_only_fallback(async_pg_conn):
    """Regression: the results-only branch of the version UNION used untyped NULLs, which
    Postgres resolved to text under SELECT DISTINCT and then refused to UNION with the int
    columns (DatatypeMismatch on every GET /compare). The fakes can't see SQL types, so this
    forces that branch on real Postgres with a version present only in eval_case_results."""
    repo = AsyncRepo(async_pg_conn)
    suite = await repo.create_suite(name="triage-fb", adapter="local", config={})
    await repo.add_cases([CaseRow(suite_id=suite.id, case_id="c0", input={}, expected={})])
    run = await repo.create_run(suite_id=suite.id, agent_id="agent", mode="development",
                                trials_per_case=2)
    await repo.create_run_version(run_id=run, agent_version=V2, cases_total=2)
    for v in (V1, V2):  # V1 has results but was never registered
        for trial in (0, 1):  # two trials: DISTINCT must still yield a single V1 row
            await repo.insert_case_result(CaseResultInsert(
                run_id=run, agent_version=v, case_id="c0", trial=trial, status="ok",
                passed=True, scores={}, cost_usd=0.0, latency_ms=1))
    await async_pg_conn.commit()
    store = PgTriageStore(connection_fetchall(async_pg_conn))

    assert await store.get_run_versions(str(run)) == [
        {"agent_version": V1, "status": None, "cases_total": None, "cases_done": None},
        {"agent_version": V2, "status": "pending", "cases_total": 2, "cases_done": 0},
    ]
    inputs = await gate.load_inputs(store, str(run), str(run), V1, V2)
    assert inputs.baseline.versions_in_run == [V1, V2]
    assert inputs.baseline.version_status is None
    assert inputs.candidate.version_status == "pending"


async def test_run_versions_empty_run(async_pg_conn):
    """Both UNION branches empty: the statement still plans and returns no rows."""
    repo = AsyncRepo(async_pg_conn)
    suite = await repo.create_suite(name="triage-empty", adapter="local", config={})
    run = await repo.create_run(suite_id=suite.id, agent_id="agent", mode="development")
    await async_pg_conn.commit()
    store = PgTriageStore(connection_fetchall(async_pg_conn))
    assert await store.get_run_versions(str(run)) == []
    assert await store.get_run_versions_page(str(run), 50, 0) == ([], 0)


async def test_run_versions_page_matches_full_list(async_pg_conn):
    """The paged query (GET /runs/{id}/versions) returns exactly the gate's version set: a
    registered version with no results, a results-only version (once, despite several trials),
    in hash order, bounded by LIMIT/OFFSET, with the total of the whole set."""
    repo = AsyncRepo(async_pg_conn)
    suite = await repo.create_suite(name="triage-page", adapter="local", config={})
    await repo.add_cases([CaseRow(suite_id=suite.id, case_id="c0", input={}, expected={})])
    run = await repo.create_run(suite_id=suite.id, agent_id="agent", mode="development",
                                trials_per_case=2)
    v3, v4 = "3" * 64, "4" * 64
    for v in (v4, V2, v3):  # registered; v3 never gets a result
        await repo.create_run_version(run_id=run, agent_version=v, cases_total=2)
    for v in (V1, V2, v4):  # V1 has results only
        for trial in (0, 1):
            await repo.insert_case_result(CaseResultInsert(
                run_id=run, agent_version=v, case_id="c0", trial=trial, status="ok",
                passed=True, scores={}, cost_usd=0.0, latency_ms=1))
    await async_pg_conn.commit()
    store = PgTriageStore(connection_fetchall(async_pg_conn))

    full = await store.get_run_versions(str(run))
    assert [r["agent_version"] for r in full] == [V1, V2, v3, v4]
    pages = [await store.get_run_versions_page(str(run), 3, o) for o in (0, 3, 6)]
    assert [rows for rows, _ in pages] == [full[:3], full[3:], []]
    assert {total for _, total in pages} == {4}
    assert full[0]["status"] is None and full[2]["status"] == "pending"


async def test_pg_store_unknown_run_is_not_found(async_pg_conn):
    store = PgTriageStore(connection_fetchall(async_pg_conn))
    with pytest.raises(gate.ComparisonError) as e:
        await gate.resolve_pair(store, "00000000-0000-0000-0000-000000000000",
                                "00000000-0000-0000-0000-000000000001")
    assert e.value.kind == "not_found"
