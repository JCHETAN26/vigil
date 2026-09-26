"""Data-access tests against a live but throwaway Postgres. Exercises the sync ``Repo`` end
to end and confirms the ``AsyncRepo`` runs the same SQL, so both sync and async callers work
(design §3.1)."""

from __future__ import annotations

import pytest

from engine.db.repo import AsyncRepo, CaseResultInsert, CaseRow, Repo


def _seed_suite_and_run(repo: Repo):
    suite = repo.create_suite(name="s1", adapter="local", config={"version": "v1"})
    repo.add_cases(
        [
            CaseRow(
                suite_id=suite.id,
                case_id="c0",
                input={"q": "2+2"},
                expected={"answer": "4"},
                tags=["math"],
            ),
            CaseRow(
                suite_id=suite.id, case_id="c1", input={"q": "hi"}, expected={"answer": "hello"}
            ),
        ]
    )
    run_id = repo.create_run(
        suite_id=suite.id,
        agent_id="hello-agent",
        mode="measurement",
        cost_budget_usd=1.0,
        concurrency=2,
        trials_per_case=2,
        cache_mode="off",
    )
    repo.create_run_version(run_id=run_id, agent_version="ver-a", cases_total=4)
    return suite, run_id


def test_suite_and_cases_roundtrip(pg_conn):
    repo = Repo(pg_conn)
    suite, _ = _seed_suite_and_run(repo)
    pg_conn.commit()

    fetched = repo.get_suite_by_name("s1")
    assert fetched is not None
    assert fetched.id == suite.id
    assert fetched.adapter == "local"
    assert fetched.config == {"version": "v1"}

    cases = repo.get_cases(suite.id)
    assert [c.case_id for c in cases] == ["c0", "c1"]
    assert cases[0].input == {"q": "2+2"}
    assert cases[0].expected == {"answer": "4"}
    assert cases[0].tags == ["math"]
    assert cases[1].tags == []


def test_upsert_manifest_is_idempotent(pg_conn):
    repo = Repo(pg_conn)
    kw = dict(
        agent_id="a",
        agent_version="v",
        git_sha="sha",
        model="claude-haiku-4-5",
        params={"max_tokens": 1024},
        prompts={"system": "hi"},
        tools=[{"name": "t"}],
    )
    repo.upsert_manifest(**kw)
    repo.upsert_manifest(**kw)  # second upsert is a no-op, not a duplicate/error
    pg_conn.commit()
    n = pg_conn.execute("SELECT count(*) FROM version_manifests").fetchone()[0]
    assert n == 1
    row = pg_conn.execute("SELECT params, prompts, tools FROM version_manifests").fetchone()
    assert row == ({"max_tokens": 1024}, {"system": "hi"}, [{"name": "t"}])


def test_case_results_and_progress(pg_conn):
    repo = Repo(pg_conn)
    _, run_id = _seed_suite_and_run(repo)

    for trial in (0, 1):
        repo.insert_case_result(
            CaseResultInsert(
                run_id=run_id,
                agent_version="ver-a",
                case_id="c0",
                trial=trial,
                status="ok",
                trace_id=f"trace{trial}",
                passed=True,
                score=1.0,
                scores={"ExactMatch": {"passed": True, "score": 1.0, "detail": {}}},
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.001,
                latency_ms=42,
                output={"output": "4", "tool_calls": []},
            )
        )
        repo.bump_run_version_progress(run_id=run_id, agent_version="ver-a", cost_delta=0.001)
        repo.add_run_cost(run_id, 0.001)
    pg_conn.commit()

    assert repo.count_case_results(run_id) == 2
    results = repo.get_case_results(run_id)
    assert [r["trial"] for r in results] == [0, 1]
    assert all(r["passed"] for r in results)

    versions = repo.get_run_versions(run_id)
    assert versions[0]["cases_done"] == 2
    assert float(versions[0]["cost_spent_usd"]) == pytest.approx(0.002)

    spent = pg_conn.execute(
        "SELECT cost_spent_usd FROM eval_runs WHERE id = %s", (run_id,)
    ).fetchone()[0]
    assert float(spent) == pytest.approx(0.002)


def test_unique_case_result_constraint(pg_conn):
    import psycopg

    repo = Repo(pg_conn)
    _, run_id = _seed_suite_and_run(repo)
    r = CaseResultInsert(run_id=run_id, agent_version="ver-a", case_id="c0", trial=0, status="ok")
    repo.insert_case_result(r)
    with pytest.raises(psycopg.errors.UniqueViolation):
        repo.insert_case_result(r)  # same (run, version, case, trial) is rejected


def test_run_status_transitions(pg_conn):
    repo = Repo(pg_conn)
    _, run_id = _seed_suite_and_run(repo)
    repo.set_run_status(run_id, "running")
    pg_conn.commit()
    started = pg_conn.execute(
        "SELECT started_at, finished_at FROM eval_runs WHERE id=%s", (run_id,)
    ).fetchone()
    assert started[0] is not None and started[1] is None

    repo.set_run_status(run_id, "aborted")
    pg_conn.commit()
    done = pg_conn.execute(
        "SELECT started_at, finished_at, status FROM eval_runs WHERE id=%s", (run_id,)
    ).fetchone()
    assert done[0] is not None and done[1] is not None and done[2] == "aborted"


async def test_async_repo_roundtrip(async_pg_conn):
    repo = AsyncRepo(async_pg_conn)
    suite = await repo.create_suite(name="s_async", adapter="local", config={"v": 1})
    await repo.add_cases(
        [
            CaseRow(suite_id=suite.id, case_id="c0", input={"q": "x"}, expected={"answer": "y"}),
        ]
    )
    run_id = await repo.create_run(
        suite_id=suite.id, agent_id="a", mode="development", trials_per_case=1
    )
    await repo.create_run_version(run_id=run_id, agent_version="ver-a", cases_total=1)
    await repo.insert_case_result(
        CaseResultInsert(
            run_id=run_id,
            agent_version="ver-a",
            case_id="c0",
            trial=0,
            status="ok",
            passed=True,
            score=1.0,
        )
    )
    await async_pg_conn.commit()

    assert await repo.count_case_results(run_id) == 1
    cases = await repo.get_cases(suite.id)
    assert [c.case_id for c in cases] == ["c0"]
