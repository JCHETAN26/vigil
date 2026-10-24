"""Tests for the read-only dashboard API. The endpoint tests need the live stack (Postgres +
ClickHouse) and are marked `integration`; they skip cleanly when it's down. The ClickHouse
read-guard and live-stack probe tests are unit tests."""

from __future__ import annotations

import os
import socket

import psycopg
import pytest
from psycopg import conninfo

from engine.api.clickhouse import ClickHouseClient, ClickHouseError
from engine.config import pg_ro_dsn

# --- pure unit: the ClickHouse client refuses anything but a single read statement ---


def test_clickhouse_client_rejects_non_read_statements():
    ch = ClickHouseClient()
    for sql in ["SELECT 1; DROP TABLE spans", "INSERT INTO spans VALUES (1)", "ALTER TABLE spans"]:
        with pytest.raises(ClickHouseError):
            ch.query(sql)  # guarded before any network call


# --- live-stack probe: the API's own ClickHouse/Postgres config, not fixed localhost ports ---


def _pg_endpoint(dsn: str) -> str:
    """``host:port`` of a libpq DSN/URL for skip messages — never the credentials."""
    try:
        parts = conninfo.conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:  # unparsable; the connect attempt reports it
        return "<unparsable DSN>"
    return f"{parts.get('host') or 'localhost'}:{parts.get('port') or 5432}"


def _clickhouse_problem() -> str | None:
    """Why the API's ClickHouse (same host/port/user resolution as ``ClickHouseClient``) is not
    usable, or None. An authenticated read, so some other service on the port doesn't count."""
    ch = ClickHouseClient(timeout=3.0)
    try:
        ch.query("SELECT 1 AS ok")
    except ClickHouseError as exc:  # the server's error text only, never the password
        return f"ClickHouse at {ch.host}:{ch.port} not usable: {exc}"
    return None


def _postgres_problem() -> str | None:
    """Why the API's Postgres (``pg_ro_dsn``, what the app's pool opens) is not usable, or None."""
    try:
        dsn = pg_ro_dsn()
    except RuntimeError as exc:
        return f"Postgres not configured: {exc}"
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.read_only = True
            conn.execute("SELECT 1")
    except psycopg.Error as exc:  # only the type: libpq messages can echo connection params
        return f"Postgres at {_pg_endpoint(dsn)} not usable ({type(exc).__name__})"
    return None


def _stack_problem() -> str | None:
    """None when both of the API's backends answer, else the first reason to skip."""
    return _clickhouse_problem() or _postgres_problem()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_uses_the_api_clickhouse_config(monkeypatch):
    # Pointed at a closed port, the probe reports that endpoint even if the default 8123 is up.
    port = _closed_port()
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_HTTP_PORT", str(port))
    monkeypatch.setenv("CLICKHOUSE_RO_USER", "probe_ro")
    monkeypatch.setenv("CLICKHOUSE_RO_PASSWORD", "s3cret-ch")
    problem = _clickhouse_problem()
    assert problem and f"127.0.0.1:{port}" in problem and "s3cret-ch" not in problem


def test_probe_uses_the_api_postgres_dsn(monkeypatch):
    # VIGIL_API_PG_DSN wins (as in the app); the skip reason names its host:port, not the secret.
    port = _closed_port()
    monkeypatch.setenv("VIGIL_API_PG_DSN", f"postgresql://ro:s3cret-pg@127.0.0.1:{port}/vigil")
    problem = _postgres_problem()
    assert problem and f"127.0.0.1:{port}" in problem and "s3cret-pg" not in problem


# --- integration: endpoints against the live stack ---


@pytest.fixture
def client():
    if problem := _stack_problem():
        pytest.skip(problem)
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
def test_trace_surfaces_retrieval_docs_and_text_content(client):
    # Find a HotpotQA run (its traces have `search` retrieval spans) and check the polish:
    # retrieval spans carry the retrieved doc ids, and captured content is text (not raw JSON)
    # with a truncated flag.
    runs = client.get("/runs", params={"limit": 200}).json()["runs"]
    hq = next((r for r in runs if "hotpotqa" in r["suite"]), None)
    if not hq:
        pytest.skip("no hotpotqa run to inspect")
    cases = client.get(f"/runs/{hq['id']}/cases", params={"limit": 50}).json()["cases"]
    trace_id = next((c["trace_id"] for c in cases if c.get("trace_id")), None)
    if not trace_id:
        pytest.skip("no trace on the hotpotqa run")
    spans = client.get(f"/traces/{trace_id}").json()["spans"]

    search = next((s for s in spans if s["name"] == "search"), None)
    if search is None:
        pytest.skip("no retrieval span in this trace")
    assert isinstance(search["retrieved_doc_ids"], list) and len(search["retrieved_doc_ids"]) >= 1
    assert isinstance(search["retrieval_k"], int)

    # Captured content is the recorded text, not the raw attributes JSON.
    ev = next((e for s in spans for e in s["events"] if e.get("content")), None)
    assert ev is not None
    assert isinstance(ev["content"], str)
    assert "vigil.content.truncated" not in ev["content"]  # not the raw attrs blob
    assert isinstance(ev["truncated"], bool)

    # Token/cost fields are present on LLM spans and null elsewhere.
    llm = next((s for s in spans if s["name"].startswith("gen_ai")), None)
    if llm:
        assert llm["input_tokens"] is not None and llm["model"]
    run_span = next((s for s in spans if s["name"] == "agent.run"), None)
    if run_span:
        assert run_span["input_tokens"] is None  # not an LLM call


@pytest.mark.integration
def test_api_postgres_connection_cannot_write():
    # A write attempt over the API's own Postgres connection (the read-only role from pg_ro_dsn)
    # is rejected by the grant, not just by convention.
    from engine.config import uses_ro_pg

    if problem := _stack_problem():
        pytest.skip(problem)
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

    if problem := _stack_problem():
        pytest.skip(problem)
    if not os.getenv("CLICKHOUSE_RO_USER"):
        pytest.skip("no read-only ClickHouse user configured")
    ch = ClickHouseClient()  # the API's host/port/database and its RO credentials
    token = base64.b64encode(f"{ch.user}:{ch.password}".encode()).decode()
    req = urllib.request.Request(
        f"http://{ch.host}:{ch.port}/",
        data=f"CREATE TABLE {ch.database}._ro_probe (x Int8) ENGINE=Memory".encode(),
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
