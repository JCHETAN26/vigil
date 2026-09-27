"""Tests for the read-only dashboard API. The endpoint tests need the live stack (Postgres +
ClickHouse) and are marked `integration`; they skip cleanly when it's down. The ClickHouse
read-guard test is a pure unit test."""

from __future__ import annotations

import os
import socket

import pytest

from engine.api.clickhouse import ClickHouseClient, ClickHouseError

# --- pure unit: the ClickHouse client refuses anything but a single read statement ---


def test_clickhouse_client_rejects_non_read_statements():
    ch = ClickHouseClient()
    for sql in ["SELECT 1; DROP TABLE spans", "INSERT INTO spans VALUES (1)", "ALTER TABLE spans"]:
        with pytest.raises(ClickHouseError):
            ch.query(sql)  # guarded before any network call


# --- integration: endpoints against the live stack ---


def _stack_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8123), timeout=2):
            pass
        # Postgres reachable via the pool at import of the app fixture; probe its port too.
        with socket.create_connection(("127.0.0.1", 5432), timeout=2):
            pass
        return True
    except OSError:
        return False


@pytest.fixture
def client():
    if not _stack_up():
        pytest.skip("stack (ClickHouse/Postgres) not reachable")
    from fastapi.testclient import TestClient

    from engine.api.app import app

    with TestClient(app) as c:  # runs the lifespan (opens the async pool)
        yield c


pytestmark_note = "integration tests use the `client` fixture, which skips when the stack is down"


@pytest.mark.integration
def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


@pytest.mark.integration
def test_runs_list_and_pagination_bounds(client):
    r = client.get("/runs")
    assert r.status_code == 200 and isinstance(r.json()["runs"], list)
    assert client.get("/runs", params={"limit": 999}).status_code == 422  # over the cap
    assert client.get("/runs", params={"offset": -1}).status_code == 422


@pytest.mark.integration
def test_run_detail_id_validation(client):
    runs = client.get("/runs").json()["runs"]
    if not runs:
        pytest.skip("no runs in the database")
    assert client.get(f"/runs/{runs[0]['id']}").status_code == 200
    assert client.get("/runs/not-a-uuid").status_code == 422  # bad id → validation, not a query
    assert client.get("/runs/00000000-0000-0000-0000-000000000000").status_code == 404


@pytest.mark.integration
def test_malicious_trace_id_cannot_alter_the_query(client):
    # SQL-injection-shaped ids are rejected by the 32-hex format validation before any query.
    for evil in [
        "abc' OR '1'='1",
        "'; DROP TABLE spans;--",
        "0" * 31,  # too short
        "z" * 32,  # not hex
    ]:
        assert client.get(f"/traces/{evil}").status_code == 422, evil
    # A well-formed but nonexistent id is a clean 404, not an error.
    assert client.get(f"/traces/{'0' * 32}").status_code == 404

    # And even a raw quoted value handed straight to the ClickHouse client is BOUND, not spliced:
    # it matches nothing and the table is untouched (no error, count 0).
    from engine.api.app import app as _app

    rows = _app.state.ch.query(
        "SELECT count() AS n FROM spans WHERE trace_id = {tid:String}",
        {"tid": "x' OR '1'='1"},
    )
    assert rows == [{"n": "0"}] or rows == [{"n": 0}]


@pytest.mark.integration
def test_cases_pagination_and_trace_roundtrip(client):
    runs = client.get("/runs").json()["runs"]
    if not runs:
        pytest.skip("no runs")
    run_id = runs[0]["id"]
    cases = client.get(f"/runs/{run_id}/cases", params={"limit": 5}).json()["cases"]
    assert len(cases) <= 5
    assert client.get(f"/runs/{run_id}/cases", params={"limit": 9999}).status_code == 422
    # Follow a case's trace into the ClickHouse span tree.
    with_trace = next((c for c in cases if c.get("trace_id")), None)
    if with_trace:
        t = client.get(f"/traces/{with_trace['trace_id']}")
        assert t.status_code == 200
        body = t.json()
        assert body["n_spans"] >= 1
        assert any(s["name"] == "agent.run" for s in body["spans"])


@pytest.mark.integration
def test_api_postgres_connection_cannot_write():
    # A write attempt over the API's own Postgres connection (the read-only role from pg_ro_dsn)
    # is rejected by the grant, not just by convention.
    import psycopg

    from engine.config import pg_ro_dsn, uses_ro_pg

    if not _stack_up():
        pytest.skip("stack not reachable")
    if not uses_ro_pg():
        pytest.skip("no read-only Postgres role configured (deploy/create_readonly_users.sh)")
    with psycopg.connect(pg_ro_dsn()) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE _ro_probe (x int)")
        conn.rollback()


@pytest.mark.integration
def test_api_clickhouse_connection_cannot_write():
    # A write sent with the API's ClickHouse credentials (readonly=1 user) is rejected server
    # side — proven by bypassing the client's own read-guard and posting a DDL directly.
    import base64
    import urllib.error
    import urllib.request

    if not _stack_up():
        pytest.skip("stack not reachable")
    ro_user = os.getenv("CLICKHOUSE_RO_USER")
    if not ro_user:
        pytest.skip("no read-only ClickHouse user configured")
    ro_pass = os.getenv("CLICKHOUSE_RO_PASSWORD", "")
    db = os.getenv("CLICKHOUSE_DB", "vigil")
    token = base64.b64encode(f"{ro_user}:{ro_pass}".encode()).decode()
    req = urllib.request.Request(
        "http://127.0.0.1:8123/",
        data=f"CREATE TABLE {db}._ro_probe (x Int8) ENGINE=Memory".encode(),
        method="POST",
        headers={"Authorization": f"Basic {token}"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code in (403, 500)  # ClickHouse ACCESS_DENIED


@pytest.mark.integration
def test_compare_same_suite_and_mismatch(client):
    runs = client.get("/runs", params={"limit": 200}).json()["runs"]
    by_suite: dict[str, list[str]] = {}
    for r in runs:
        by_suite.setdefault(r["suite"], []).append(r["id"])
    # Same-suite comparison: expect 200 with a positive paired-case count.
    same = next((ids for ids in by_suite.values() if len(ids) >= 2), None)
    if same:
        resp = client.get("/compare", params={"baseline": same[0], "candidate": same[1]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["paired_cases"] >= 1
        assert isinstance(body["metrics"], list)
    # Different-suite comparison: refused with a clear 400.
    suites = [ids for ids in by_suite.values() if ids]
    if len(suites) >= 2:
        resp = client.get("/compare", params={"baseline": suites[0][0], "candidate": suites[1][0]})
        assert resp.status_code == 400
