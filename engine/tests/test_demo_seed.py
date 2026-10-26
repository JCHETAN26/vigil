"""demo/seed.py against a real (throwaway) Postgres and an in-memory ClickHouse stand-in:
idempotent re-runs, refusal of every conflicting/partial state with nothing written, existing
rows left alone, and verdicts recomputed through the production store + gate. The ClickHouse
HTTP calls themselves are checked for their fixed statements and bound parameters."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "demo"))

import seed as demo_seed
import synthetic_fixture as sf

from engine.api.clickhouse import ClickHouseError, check_read_only
from engine.db.repo import Repo


class FakeSpans:
    """ClickHouse ``spans`` as the seeder sees it: columns, read-back by identity, insert."""

    def __init__(self, rows=(), columns=None):
        self.rows = [copy.deepcopy(r) for r in rows]
        self.cols = set(demo_seed.SPAN_COLUMNS if columns is None else columns)
        self.inserts = 0

    def columns(self):
        return self.cols

    def read(self, trace_ids, run_ids, agent_id):
        return [copy.deepcopy(r) for r in self.rows if r["trace_id"] in trace_ids
                or r["eval_run_id"] in run_ids or r["agent_id"] == agent_id]

    def insert(self, rows, token):
        assert token.startswith("vigil-demo-synthetic-v1-")
        self.inserts += 1
        self.rows += [copy.deepcopy(r) for r in rows]


FX = sf.build()
COUNTS = {"eval_suites": 1, "eval_cases": 12, "version_manifests": 4, "eval_runs": 3,
          "eval_run_versions": 6, "eval_case_results": 68}


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in COUNTS}


def _refused(conn, spans, match: str) -> None:
    before_pg, before_ch = _counts(conn), (len(spans.rows), spans.inserts)
    with pytest.raises(demo_seed.SeedError, match=match):
        demo_seed.seed(conn, spans)
    assert _counts(conn) == before_pg and (len(spans.rows), spans.inserts) == before_ch


def test_seed_writes_everything_once_and_rerunning_is_a_no_op(pg_conn):
    spans = FakeSpans()
    done = demo_seed.seed(pg_conn, spans)
    assert done["postgres"].startswith("inserted") and done["clickhouse"].startswith("inserted")
    assert _counts(pg_conn) == COUNTS and len(spans.rows) == len(FX.spans)
    again = demo_seed.seed(pg_conn, spans)
    assert "nothing written" in again["postgres"] and "nothing written" in again["clickhouse"]
    assert _counts(pg_conn) == COUNTS and spans.inserts == 1


def test_seed_leaves_existing_runs_alone(pg_conn):
    repo = Repo(pg_conn)
    suite = repo.create_suite(name="real-suite", adapter="local", config={"path": "x"})
    run = repo.create_run(suite_id=suite.id, agent_id="real-agent", mode="measurement",
                          notes="a real run")
    pg_conn.commit()
    demo_seed.seed(pg_conn, FakeSpans())
    row = pg_conn.execute("SELECT agent_id, notes FROM eval_runs WHERE id = %s",
                          (run,)).fetchone()
    assert row == ("real-agent", "a real run")
    assert pg_conn.execute("SELECT count(*) FROM eval_runs").fetchone()[0] == 4


def test_a_changed_postgres_row_refuses_the_whole_seed(pg_conn):
    spans = FakeSpans()
    demo_seed.seed(pg_conn, spans)
    pg_conn.execute("UPDATE eval_runs SET notes = 'edited' WHERE id = %s", (FX.runs[0]["id"],))
    _refused(pg_conn, spans, r"eval_runs: .* differs in \['notes'\]")
    assert pg_conn.execute("SELECT notes FROM eval_runs WHERE id = %s",
                           (FX.runs[0]["id"],)).fetchone()[0] == "edited"  # not "repaired"


def test_a_partial_postgres_set_is_refused(pg_conn):
    spans = FakeSpans()
    demo_seed.seed(pg_conn, spans)
    pg_conn.execute("DELETE FROM eval_case_results WHERE trace_id = %s",
                    (FX.results[0]["trace_id"],))
    _refused(pg_conn, spans, "eval_case_results: 1 of 68 fixture rows missing")


def test_another_suite_with_the_fixture_name_is_refused(pg_conn):
    Repo(pg_conn).create_suite(name=sf.SUITE_NAME, adapter="local", config={})
    _refused(pg_conn, FakeSpans(), "eval_suites: 1 other rows")


def test_another_run_of_the_synthetic_agent_is_refused(pg_conn):
    spans = FakeSpans()
    demo_seed.seed(pg_conn, spans)
    Repo(pg_conn).create_run(suite_id=sf.SUITE_ID, agent_id=sf.AGENT_ID, mode="development")
    _refused(pg_conn, spans, "eval_runs: 1 other rows")


def test_a_partial_span_set_is_refused_before_postgres_is_written(pg_conn):
    _refused(pg_conn, FakeSpans(FX.spans[:-1]), "spans: 1 of .* fixture rows missing")


def test_a_mismatched_span_is_refused(pg_conn):
    rows = copy.deepcopy(FX.spans)
    rows[5]["span_name"] = "something else"
    _refused(pg_conn, FakeSpans(rows), r"spans: .* differs in \['span_name'\]")


def test_a_foreign_span_on_a_synthetic_run_is_refused(pg_conn):
    stray = {**copy.deepcopy(FX.spans[0]), "trace_id": "f" * 32}
    _refused(pg_conn, FakeSpans([*FX.spans, stray]), "spans: 1 other rows")


def test_an_identical_replayed_span_is_fine_but_a_different_one_is_not(pg_conn):
    demo_seed.seed(pg_conn, FakeSpans([*FX.spans, FX.spans[0]]))  # unmerged identical replay
    changed = {**copy.deepcopy(FX.spans[0]), "status_message": "replayed differently"}
    _refused(pg_conn, FakeSpans([*FX.spans, changed]), "stored twice with different content")


def test_clickhouse_missing_after_postgres_is_completed(pg_conn):
    demo_seed.seed(pg_conn, FakeSpans())
    fresh = FakeSpans()
    done = demo_seed.seed(pg_conn, fresh)
    assert "nothing written" in done["postgres"] and done["clickhouse"].startswith("inserted")
    assert fresh.inserts == 1 and _counts(pg_conn) == COUNTS


def test_unmigrated_stores_are_refused(pg_conn):
    cols = set(demo_seed.SPAN_COLUMNS) - {"cache_hit"}
    _refused(pg_conn, FakeSpans(columns=cols), "missing columns")


def test_check_and_db_verdicts_through_the_production_store(pg_conn, test_db_dsn):
    spans = FakeSpans()
    demo_seed.seed(pg_conn, spans)
    pg_conn.commit()
    assert demo_seed.check(pg_conn, spans, test_db_dsn) == []
    verdicts = asyncio.run(demo_seed.db_verdicts(test_db_dsn))
    for scn in sf.SCENARIOS:
        db = verdicts[scn.key]
        offline = demo_seed.gate.compare(demo_seed.offline_inputs(FX, scn))
        assert db.verdict == scn.expected_verdict
        # The database round trip changes nothing the gate sees.
        assert db.to_dict()["metrics"] == offline.to_dict()["metrics"]
        assert db.incomplete_reasons == offline.incomplete_reasons
        assert db.coverage == offline.coverage
    assert any("absent" in p for p in demo_seed.check(pg_conn, FakeSpans(), test_db_dsn))


# --- the real ClickHouse calls: fixed statements, bound values, no generic SQL -----------------


def test_clickhouse_statements_are_fixed_and_reads_pass_the_read_guard():
    check_read_only(demo_seed._READ_SPANS)
    check_read_only(demo_seed._COLUMNS_SQL)
    assert demo_seed._INSERT_SPANS.startswith("INSERT INTO spans (`trace_id`")
    assert demo_seed._INSERT_SPANS.endswith(") FORMAT JSONEachRow")
    with pytest.raises(demo_seed.SeedError, match="invalid ClickHouse database name"):
        demo_seed.ClickHouseSpans(user="u", password="p", database="vigil; DROP")


def test_clickhouse_insert_sends_rows_as_data_with_a_dedup_token(monkeypatch):
    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout):
        sent["url"], sent["body"], sent["auth"] = req.full_url, req.data, req.get_header(
            "Authorization")
        return Resp()

    monkeypatch.setattr(demo_seed.urllib.request, "urlopen", fake_urlopen)
    spans = demo_seed.ClickHouseSpans(user="admin", password="pw-not-in-url", host="127.0.0.1",
                                      port=8123, database="vigil")
    spans.insert(FX.spans[:2], demo_seed._dedup_token(FX))
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(sent["url"]).query)
    assert qs["query"] == [demo_seed._INSERT_SPANS] and qs["database"] == ["vigil"]
    assert qs["insert_deduplication_token"][0].startswith("vigil-demo-synthetic-v1-")
    assert "pw-not-in-url" not in sent["url"] and sent["auth"].startswith("Basic ")
    lines = sent["body"].decode().splitlines()
    assert [json.loads(x) for x in lines] == [{k: s[k] for k in demo_seed.SPAN_COLUMNS}
                                             for s in FX.spans[:2]]


class _Untouchable:
    """A connection/span store that fails the test if the seeder touches it at all."""

    def __getattr__(self, name):
        raise AssertionError(f"database touched ({name}) before the fixture was checked")


@pytest.mark.parametrize("mutate, expect", [
    (lambda f: f.runs[0].update(id="0b1e0000-0000-4000-8000-000000000101"), "reserved"),
    (lambda f: f.cases[0]["input"].update(api_key="x"), "secret-like"),
    (lambda f: f.spans[1].update(cost_usd=0.01), "invented usage"),
    (lambda f: f.suite.update(name="demo-qa"), "vigil-demo-synthetic"),
])
def test_seed_refuses_an_explicit_malformed_fixture_before_any_database_access(mutate, expect):
    fx = copy.deepcopy(FX)
    mutate(fx)
    with pytest.raises(demo_seed.SeedError, match=f"malformed.*{expect}"):
        demo_seed.seed(_Untouchable(), _Untouchable(), fx)


def test_seed_refuses_fixture_timestamps_outside_eval_span_retention():
    # Seeding spans ClickHouse's 365-day TTL has already passed would only see them deleted
    # again; the preflight says so instead of claiming a reseed helps.
    expires = demo_seed.retention_expires_at(FX)
    assert expires == datetime(2027, 10, 25, 12, 0, tzinfo=UTC)
    for now in (expires, expires - timedelta(hours=12), expires + timedelta(days=30)):
        with pytest.raises(demo_seed.SeedError, match="retention until 2027-10-25"):
            demo_seed.seed(_Untouchable(), _Untouchable(), now=now)
    # Inside retention the check passes and seeding proceeds to the stores (here: refused by
    # the untouchable stand-in, proving the fixture check came first and then let it through).
    with pytest.raises(AssertionError, match="database touched"):
        demo_seed.seed(_Untouchable(), _Untouchable(), now=expires - timedelta(days=2))


def test_columns_query_passes_the_real_read_guard_and_binds_the_database(monkeypatch):
    # Through the unchanged production ClickHouseClient (its single-read guard refuses a bare
    # `system`), with only the HTTP call replaced.
    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"name":"trace_id"}\n{"name":"cache_hit"}\n'

    def fake_urlopen(req, timeout):
        sent["url"], sent["body"] = req.full_url, req.data.decode()
        return Resp()

    monkeypatch.setattr("engine.api.clickhouse.urllib.request.urlopen", fake_urlopen)
    spans = demo_seed.ClickHouseSpans(user="ro", password="pw", host="127.0.0.1", port=8123,
                                      database="vigil")
    assert spans.columns() == {"trace_id", "cache_hit"}
    assert "FROM `system`.`columns`" in sent["body"]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(sent["url"]).query)["param_db"] == [
        "vigil"]
    assert demo_seed.ch_schema_problems(spans)  # 2 of the needed columns: refused, with reason
    # The bare form is exactly what the guard refuses (why the identifier is quoted).
    with pytest.raises(ClickHouseError, match="SYSTEM"):
        check_read_only("SELECT name FROM system.columns WHERE table = 'spans'")


def test_admin_and_readonly_clickhouse_credentials_are_explicit(monkeypatch):
    for k in ("CLICKHOUSE_PASSWORD", "CLICKHOUSE_RO_USER", "CLICKHOUSE_RO_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(demo_seed.SeedError, match="CLICKHOUSE_PASSWORD is not set"):
        demo_seed.ClickHouseSpans.admin_from_env()
    with pytest.raises(demo_seed.SeedError, match="read-only users"):
        demo_seed.ClickHouseSpans.readonly_from_env()
