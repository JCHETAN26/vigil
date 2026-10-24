"""No-network tests of the ClickHouse client: the read guard runs before any I/O, values are
bound as query parameters (never spliced into the SQL), and transport failures surface as
ClickHouseError. ``urllib.request.urlopen`` is replaced by a fake in every test."""

from __future__ import annotations

import io
import urllib.error
import urllib.parse

import pytest

from engine.api import clickhouse
from engine.api.clickhouse import ClickHouseClient, ClickHouseError, check_read_only

TRACE_SQL = (  # the shape of the API's real trace query
    "SELECT span_id, gen_ai_usage_input_tokens AS in_tok, "
    "span_attributes['vigil.retrieval.doc_ids'] AS doc_ids, `events.name` AS event_names "
    "FROM spans WHERE trace_id = {tid:String} ORDER BY start_time LIMIT {lim:UInt32}"
)


class _Resp:
    def __init__(self, body: str):
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(clickhouse.urllib.request, "urlopen", refuse)


@pytest.fixture
def capture(monkeypatch):
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        return _Resp('{"n": "0"}\n')

    monkeypatch.setattr(clickhouse.urllib.request, "urlopen", fake)
    return calls


@pytest.mark.parametrize("sql", [
    "INSERT INTO spans VALUES (1)",
    "insert into spans values (1)",
    "ALTER TABLE spans DELETE WHERE 1",
    "  \n\tINSERT INTO spans SELECT * FROM spans",
    "DROP TABLE spans",
    "SELECT 1; DROP TABLE spans",
    "SELECT 1;DROP TABLE spans",
    "SELECT 1 ;",
    "SELECT 1\n;\nTRUNCATE TABLE spans",
    "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "SELECT * FROM spans\tINTO\tOUTFILE 'x.csv'",
    "SELECT 1 -- comment",
    "/* hidden */ INSERT INTO spans VALUES (1)",
    "SELECT 1 # comment",
    "SYSTEM FLUSH LOGS",
    "OPTIMIZE TABLE spans FINAL",
    "SET readonly = 0",
    "SHOW TABLES",
    "DESCRIBE spans",
    "",
    "SELECT 'unterminated",
    "SELECTX 1",
])
def test_guard_rejects_before_network(sql, no_network):
    with pytest.raises(ClickHouseError):
        ClickHouseClient().query(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "  select count() AS n FROM spans",
    "WITH 1 AS x SELECT x",
    "SELECT * FROM spans WHERE span_name = 'a; DROP TABLE spans -- /* #'",
    "SELECT `insert`, \"delete\" FROM t",
    "SELECT 'it''s', 'esc\\'aped' FROM t",
    "SELECT * FROM spans ORDER BY start_time LIMIT 10 OFFSET 5 SETTINGS max_threads = 1",
    TRACE_SQL,
])
def test_guard_allows_single_reads(sql):
    check_read_only(sql)


def test_values_are_bound_not_spliced(capture):
    evil = "x' OR '1'='1"
    rows = ClickHouseClient(host="h", port=1, database="vigil").query(TRACE_SQL,
                                                                      {"tid": evil, "lim": 5})
    assert rows == [{"n": "0"}]
    (req,) = capture
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
    assert qs["param_tid"] == [evil] and qs["param_lim"] == ["5"] and qs["database"] == ["vigil"]
    body = req.data.decode()
    assert evil not in body and "{tid:String}" in body and body.endswith("FORMAT JSONEachRow")


def test_parameter_names_are_validated(no_network):
    with pytest.raises(ClickHouseError, match="parameter name"):
        ClickHouseClient().query("SELECT {a:String}", {"a&database=system": "x"})


def _raising(exc):
    def fake(req, timeout=None):
        raise exc

    return fake


def test_http_error_becomes_clickhouse_error(monkeypatch):
    err = urllib.error.HTTPError("http://h/", 500, "err", {}, io.BytesIO(b"Code: 497. ACCESS"))
    monkeypatch.setattr(clickhouse.urllib.request, "urlopen", _raising(err))
    with pytest.raises(ClickHouseError, match=r"HTTP 500: Code: 497"):
        ClickHouseClient().query("SELECT 1")


@pytest.mark.parametrize("exc", [
    urllib.error.URLError(ConnectionRefusedError(111, "refused")),
    TimeoutError("timed out"),
    ConnectionResetError(104, "reset"),
])
def test_transport_errors_become_clickhouse_error(monkeypatch, exc):
    monkeypatch.setattr(clickhouse.urllib.request, "urlopen", _raising(exc))
    with pytest.raises(ClickHouseError, match="unreachable"):
        ClickHouseClient().query("SELECT 1")


def test_malformed_response_becomes_clickhouse_error(monkeypatch):
    monkeypatch.setattr(clickhouse.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp("not json\n"))
    with pytest.raises(ClickHouseError, match="malformed"):
        ClickHouseClient().query("SELECT 1")
