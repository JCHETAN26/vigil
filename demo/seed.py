"""Import the SYNTHETIC demo fixture into Postgres and ClickHouse, or check it is there.

    python demo/seed.py plan     # offline: validate the fixture, print scenarios + gate verdicts
    python demo/seed.py seed     # write it (admin credentials); idempotent; refuses conflicts
    python demo/seed.py check    # read-only (RO credentials): rows identical + verdicts recompute

This is **seeded replay data**, not an ingestion or evaluation test: no agent runs, no model is
called, no span goes through the receiver/queue/consumer. ``seed`` writes the prepared rows of
``demo/synthetic_fixture.py`` straight into the production tables (eval_* + version_manifests in
Postgres, ``spans`` in ClickHouse) so the real API, gate and dashboard can show them.

Safety rules (each enforced before anything is written):

- The fixture is shape-checked (``synthetic_fixture.validate``): reserved ``d3e00000-...`` ids,
  ``vigil-demo-synthetic-*`` names, SYNTHETIC notes, version hashes that are the hash of their
  own manifests, no usage on spans, no secret-like strings.
- Both stores are preflighted on every logical id the fixture uses (suite id and name, case ids,
  manifests of the synthetic agent, run ids and any other run of the synthetic suite, run
  versions, result keys, trace/span ids, and any span claiming a synthetic run or the synthetic
  agent). Each store is then ``absent`` (write it), ``identical`` (leave it: re-running is a
  no-op) or ``conflict`` (refuse the whole seed, write nothing). A partial or mismatched set is a
  conflict: the seeder never overwrites, deletes, merges or "repairs" rows.
- Postgres is written in one transaction that re-runs the preflight first; ClickHouse in one
  insert with a deterministic deduplication token. Both are read back and must be identical.
- Every Postgres value is a bound parameter; every ClickHouse value is a bound query parameter
  or JSONEachRow data. Table and column names are fixed constants; the ClickHouse database name
  is validated. There is no generic SQL entry point.
- ``seed`` uses the explicit admin credentials (``POSTGRES_*`` / ``VIGIL_PG_DSN`` and
  ``CLICKHOUSE_USER`` / ``CLICKHOUSE_PASSWORD``); ``check`` uses the read-only roles the API uses.
  Nothing prints a password or DSN.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
import synthetic_fixture as sf
from engine.api.clickhouse import ClickHouseClient, ClickHouseError
from engine.db.repo import CaseResultInsert, CaseRow, Repo
from psycopg.types.json import Jsonb

from engine import gate

# --- identities and comparison ---------------------------------------------------------------


class SeedError(RuntimeError):
    """The seed or check cannot proceed; nothing was written by the failing step."""


@dataclass
class StoreState:
    store: str
    status: str  # absent | identical | conflict
    details: list[str] = field(default_factory=list)

    def describe(self) -> str:
        more = f": {'; '.join(self.details[:8])}" if self.details else ""
        return f"{self.store}: {self.status}{more}"


def _canon(v):
    """Values as the stores return them, comparable with the fixture's (numbers as float,
    timestamps as UTC ISO text, UUIDs as text, jsonb recursively)."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    return v


def _compare(table: str, expected: dict, found: dict) -> list[str]:
    """Differences between expected and found ``{key: row}`` maps, as readable lines."""
    out = []
    missing = [k for k in expected if k not in found]
    extra = [k for k in found if k not in expected]
    differ = [k for k in expected if k in found and found[k] != expected[k]]
    if missing:
        out.append(f"{table}: {len(missing)} of {len(expected)} fixture rows missing")
    if extra:
        out.append(f"{table}: {len(extra)} other rows use fixture ids/names, e.g. {extra[0]}")
    for k in differ[:3]:
        cols = sorted(c for c in expected[k] if found[k].get(c) != expected[k][c])
        out.append(f"{table}: {k} differs in {cols}")
    if len(differ) > 3:
        out.append(f"{table}: {len(differ) - 3} more rows differ")
    return out


def _state(store: str, tables: dict[str, tuple[dict, dict]]) -> StoreState:
    if all(not found for _, found in tables.values()):
        return StoreState(store, "absent")
    problems = [p for t, (exp, found) in tables.items() for p in _compare(t, exp, found)]
    return StoreState(store, "conflict", problems) if problems else StoreState(store, "identical")


# --- Postgres -------------------------------------------------------------------------------

_PG_TABLES = ("eval_suites", "eval_cases", "version_manifests", "eval_runs",
              "eval_run_versions", "eval_case_results")
_RUN_COLS = ("suite_id", "agent_id", "mode", "status", "cost_budget_usd", "cost_spent_usd",
             "concurrency", "trials_per_case", "cache_mode", "git_sha", "notes", "created_at",
             "started_at", "finished_at")
_VERSION_COLS = ("status", "cost_spent_usd", "cases_total", "cases_done", "started_at",
                 "finished_at")
_RESULT_COLS = ("trace_id", "passed", "score", "scores", "status", "error", "output", "attempts",
                "input_tokens", "output_tokens", "cost_usd", "latency_ms")
_MANIFEST_COLS = ("git_sha", "model", "params", "prompts", "tools", "code_ref")

_SEL_SUITES = "SELECT id, name, adapter, config FROM eval_suites WHERE id = %s OR name = %s"
_SEL_CASES = "SELECT case_id, input, expected, tags FROM eval_cases WHERE suite_id = %s"
_SEL_MANIFESTS = (
    "SELECT agent_version, git_sha, model, params, prompts, tools, code_ref "
    "FROM version_manifests WHERE agent_id = %s"
)
_SEL_RUNS = (
    "SELECT id, " + ", ".join(_RUN_COLS) + " FROM eval_runs "
    "WHERE id = ANY(%s::uuid[]) OR suite_id = %s OR agent_id = %s"
)
_SEL_VERSIONS = (
    "SELECT run_id, agent_version, " + ", ".join(_VERSION_COLS) + " FROM eval_run_versions "
    "WHERE run_id = ANY(%s::uuid[])"
)
_SEL_RESULTS = (
    "SELECT run_id, agent_version, case_id, trial, " + ", ".join(_RESULT_COLS)
    + " FROM eval_case_results WHERE run_id = ANY(%s::uuid[])"
)
_INS_SUITE = "INSERT INTO eval_suites (id, name, adapter, config) VALUES (%s, %s, %s, %s)"
_INS_RUN = (
    "INSERT INTO eval_runs (id, " + ", ".join(_RUN_COLS) + ") VALUES ("
    + ", ".join(["%s"] * (len(_RUN_COLS) + 1)) + ")"
)
_INS_VERSION = (
    "INSERT INTO eval_run_versions (run_id, agent_version, " + ", ".join(_VERSION_COLS)
    + ") VALUES (" + ", ".join(["%s"] * (len(_VERSION_COLS) + 2)) + ")"
)


def _pick(row: dict, cols) -> dict:
    return {c: _canon(row.get(c)) for c in cols}


def pg_expected(fx: sf.Fixture) -> dict[str, dict]:
    s = fx.suite
    results = {}
    for r in fx.results:
        full = {"attempts": 1, "input_tokens": None, "output_tokens": None, "cost_usd": None,
                "latency_ms": None, **r}
        key = (r["run_id"], r["agent_version"], r["case_id"], r["trial"])
        results[key] = _pick(full, _RESULT_COLS)
    return {
        "eval_suites": {s["id"]: _pick(s, ("name", "adapter", "config"))},
        "eval_cases": {c["case_id"]: _pick(c, ("input", "expected", "tags")) for c in fx.cases},
        "version_manifests": {m["agent_version"]: _pick(m, _MANIFEST_COLS)
                              for m in fx.manifests},
        "eval_runs": {r["id"]: _pick(r, _RUN_COLS) for r in fx.runs},
        "eval_run_versions": {(v["run_id"], v["agent_version"]): _pick(v, _VERSION_COLS)
                              for v in fx.run_versions},
        "eval_case_results": results,
    }


def _rows(conn: psycopg.Connection, sql: str, params: tuple) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        names = [d.name for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def pg_found(conn: psycopg.Connection, fx: sf.Fixture) -> dict[str, dict]:
    run_ids = [r["id"] for r in fx.runs]
    suites = _rows(conn, _SEL_SUITES, (fx.suite["id"], fx.suite["name"]))
    cases = _rows(conn, _SEL_CASES, (fx.suite["id"],))
    manifests = _rows(conn, _SEL_MANIFESTS, (sf.AGENT_ID,))
    runs = _rows(conn, _SEL_RUNS, (run_ids, fx.suite["id"], sf.AGENT_ID))
    versions = _rows(conn, _SEL_VERSIONS, (run_ids,))
    results = _rows(conn, _SEL_RESULTS, (run_ids,))
    return {
        "eval_suites": {str(r["id"]): _pick(r, ("name", "adapter", "config")) for r in suites},
        "eval_cases": {r["case_id"]: _pick(r, ("input", "expected", "tags")) for r in cases},
        "version_manifests": {r["agent_version"]: _pick(r, _MANIFEST_COLS) for r in manifests},
        "eval_runs": {str(r["id"]): _pick(r, _RUN_COLS) for r in runs},
        "eval_run_versions": {(str(r["run_id"]), r["agent_version"]): _pick(r, _VERSION_COLS)
                              for r in versions},
        "eval_case_results": {(str(r["run_id"]), r["agent_version"], r["case_id"], r["trial"]):
                              _pick(r, _RESULT_COLS) for r in results},
    }


def pg_state(conn: psycopg.Connection, fx: sf.Fixture) -> StoreState:
    exp, found = pg_expected(fx), pg_found(conn, fx)
    return _state("postgres", {t: (exp[t], found[t]) for t in _PG_TABLES})


def pg_schema_problems(conn: psycopg.Connection) -> list[str]:
    missing = [t for t in _PG_TABLES
               if _rows(conn, "SELECT to_regclass(%s) AS t", (f"public.{t}",))[0]["t"] is None]
    return [f"Postgres table {t} is missing; run `python -m engine migrate` first"
            for t in missing]


def pg_write(conn: psycopg.Connection, fx: sf.Fixture) -> None:
    """Insert the whole fixture in one transaction, re-checking that nothing is there first."""
    repo = Repo(conn)
    with conn.transaction():
        state = pg_state(conn, fx)
        if state.status != "absent":
            raise SeedError(f"refusing to write: {state.describe()}")
        s = fx.suite
        conn.execute(_INS_SUITE, (s["id"], s["name"], s["adapter"], Jsonb(s["config"])))
        repo.add_cases([CaseRow(suite_id=c["suite_id"], case_id=c["case_id"], input=c["input"],
                                expected=c["expected"], tags=c["tags"]) for c in fx.cases])
        for m in fx.manifests:
            repo.upsert_manifest(**m)
        for r in fx.runs:
            conn.execute(_INS_RUN, (r["id"], *(r[c] for c in _RUN_COLS)))
        for v in fx.run_versions:
            conn.execute(_INS_VERSION, (v["run_id"], v["agent_version"],
                                        *(v[c] for c in _VERSION_COLS)))
        for r in fx.results:
            repo.insert_case_result(CaseResultInsert(
                run_id=r["run_id"], agent_version=r["agent_version"], case_id=r["case_id"],
                trial=r["trial"], status=r["status"], trace_id=r["trace_id"],
                passed=r["passed"], score=r["score"], scores=r["scores"], error=r["error"],
                output=r["output"]))


# --- ClickHouse -----------------------------------------------------------------------------

SPAN_COLUMNS = (
    "trace_id", "span_id", "parent_span_id", "trace_state", "start_time", "end_time",
    "span_name", "span_kind", "status_code", "status_message", "service_name",
    "service_version", "agent_id", "agent_version", "git_sha", "run_id", "run_kind",
    "eval_run_id", "eval_case_id", "eval_trial", "session_id", "role", "gen_ai_system",
    "gen_ai_operation_name", "gen_ai_request_model", "gen_ai_response_model",
    "gen_ai_usage_input_tokens", "gen_ai_usage_output_tokens",
    "gen_ai_usage_cache_creation_input_tokens", "gen_ai_usage_cache_read_input_tokens",
    "cost_usd", "cache_hit", "resource_attributes", "span_attributes", "events.timestamp",
    "events.name", "events.attributes",
)
_INT_COLS = {"eval_trial", "gen_ai_usage_input_tokens", "gen_ai_usage_output_tokens",
             "gen_ai_usage_cache_creation_input_tokens", "gen_ai_usage_cache_read_input_tokens",
             "cache_hit"}
# Read-back aliases differ from the column names (ClickHouse rejects `f(x) AS x` as cyclic).
_CH_READ_EXPR = {
    "start_time": "toString(start_time)", "end_time": "toString(end_time)",
    "span_kind": "toString(span_kind)", "status_code": "toString(status_code)",
    "run_kind": "toString(run_kind)",
    "events.timestamp": "arrayMap(t -> toString(t), `events.timestamp`)",
    "events.name": "`events.name`", "events.attributes": "`events.attributes`",
}
_CH_ALIAS = {c: f"c{i}" for i, c in enumerate(SPAN_COLUMNS)}
_READ_SPANS = (
    "SELECT " + ", ".join(f"{_CH_READ_EXPR.get(c, c)} AS {_CH_ALIAS[c]}" for c in SPAN_COLUMNS)
    + " FROM spans WHERE has({tids:Array(String)}, trace_id) "
    "OR has({runs:Array(String)}, eval_run_id) OR agent_id = {agent:String}"
)
_INSERT_SPANS = (
    "INSERT INTO spans (" + ", ".join(f"`{c}`" for c in SPAN_COLUMNS) + ") FORMAT JSONEachRow"
)
# `system` is quoted: the read guard refuses the bare word (it is also a statement keyword).
_COLUMNS_SQL = (
    "SELECT name FROM `system`.`columns` WHERE database = {db:String} AND table = 'spans'"
)
_DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _span_canon(row: dict) -> dict:
    out = {}
    for c in SPAN_COLUMNS:
        v = row[c]
        if c in _INT_COLS:
            v = int(v)
        elif c == "cost_usd":
            v = float(v)
        elif isinstance(v, dict):
            v = {str(k): str(x) for k, x in v.items()}
        elif isinstance(v, tuple):
            v = list(v)
        out[c] = v
    return out


class ClickHouseSpans:
    """The seeder's view of ClickHouse ``spans``: column list, read-back by fixture identity,
    and one fixed-statement insert. Reads go through ``ClickHouseClient`` (single-SELECT guard,
    bound parameters); the insert statement is the constant ``_INSERT_SPANS``."""

    def __init__(self, *, user: str, password: str, host: str | None = None,
                 port: int | str | None = None, database: str | None = None,
                 timeout: float = 15.0):
        self.database = database or os.getenv("CLICKHOUSE_DB", "vigil")
        if not _DB_NAME.match(self.database):
            raise SeedError(f"invalid ClickHouse database name {self.database!r}")
        self._client = ClickHouseClient(host=host, port=port, database=self.database, user=user,
                                        password=password, timeout=timeout)

    @classmethod
    def admin_from_env(cls) -> ClickHouseSpans:
        password = os.getenv("CLICKHOUSE_PASSWORD")
        if not password:
            raise SeedError("CLICKHOUSE_PASSWORD is not set: seeding ClickHouse needs the admin "
                            "user (CLICKHOUSE_USER, default vigil) and its password")
        return cls(user=os.getenv("CLICKHOUSE_USER", "vigil"), password=password)

    @classmethod
    def readonly_from_env(cls) -> ClickHouseSpans:
        user, password = os.getenv("CLICKHOUSE_RO_USER"), os.getenv("CLICKHOUSE_RO_PASSWORD")
        if not (user and password):
            raise SeedError("CLICKHOUSE_RO_USER / CLICKHOUSE_RO_PASSWORD are not set: create "
                            "the read-only users first (deploy/create_readonly_users.sh)")
        return cls(user=user, password=password)

    def columns(self) -> set[str]:
        return {r["name"] for r in self._client.query(_COLUMNS_SQL, {"db": self.database})}

    def read(self, trace_ids: list[str], run_ids: list[str], agent_id: str) -> list[dict]:
        rows = self._client.query(_READ_SPANS, {"tids": trace_ids, "runs": run_ids,
                                                "agent": agent_id})
        return [{c: r[_CH_ALIAS[c]] for c in SPAN_COLUMNS} for r in rows]

    def insert(self, rows: list[dict], token: str) -> None:
        c = self._client
        qs = urllib.parse.urlencode({"database": self.database, "query": _INSERT_SPANS,
                                     "insert_deduplicate": 1,
                                     "insert_deduplication_token": token})
        body = "".join(json.dumps({k: r[k] for k in SPAN_COLUMNS}) + "\n" for r in rows)
        req = urllib.request.Request(f"http://{c.host}:{c.port}/?{qs}", data=body.encode(),
                                     method="POST")
        auth = base64.b64encode(f"{c.user}:{c.password}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        try:
            with urllib.request.urlopen(req, timeout=c.timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise SeedError(f"ClickHouse insert failed: HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SeedError(f"ClickHouse unreachable: {getattr(exc, 'reason', exc)}") from None


def ch_state(spans: ClickHouseSpans, fx: sf.Fixture) -> StoreState:
    trace_ids = sorted({s["trace_id"] for s in fx.spans})
    rows = spans.read(trace_ids, [r["id"] for r in fx.runs], sf.AGENT_ID)
    expected = {(s["trace_id"], s["span_id"]): _span_canon(s) for s in fx.spans}
    found: dict[tuple[str, str], dict] = {}
    dup_conflicts = []
    for r in rows:  # an unmerged replay of the same span is fine only if it is identical
        k, canon = (r["trace_id"], r["span_id"]), _span_canon(r)
        if k in found and found[k] != canon:
            dup_conflicts.append(f"spans: {k} is stored twice with different content")
        found.setdefault(k, canon)
    state = _state("clickhouse", {"spans": (expected, found)})
    if dup_conflicts:
        return StoreState("clickhouse", "conflict", dup_conflicts + state.details)
    return state


def ch_schema_problems(spans: ClickHouseSpans) -> list[str]:
    missing = sorted(set(SPAN_COLUMNS) - spans.columns())
    if missing:
        return [f"ClickHouse spans is missing columns {missing[:5]}: run the ingest migrations "
                "(start the stack: cd ingest && make up)"]
    return []


def _dedup_token(fx: sf.Fixture) -> str:
    return f"vigil-demo-synthetic-v{sf.FIXTURE_VERSION}-" + sf.stable_hex(
        *sorted((s["trace_id"], s["span_id"]) for s in fx.spans), n=32)


# --- seed / check ---------------------------------------------------------------------------


# ClickHouse keeps eval spans 365 days after their start_time (ingest/internal/schema/
# 001_spans.sql: `TTL toDateTime(start_time) + INTERVAL 365 DAY DELETE WHERE run_kind = 'eval'`).
EVAL_SPAN_RETENTION = timedelta(days=365)
# The TTL is evaluated in the server's time zone on a timezone-less column: stay a day clear.
_RETENTION_MARGIN = timedelta(days=1)


def retention_expires_at(fx: sf.Fixture) -> datetime:
    """When ClickHouse starts deleting the fixture's spans (its earliest span + the TTL)."""
    earliest = min(datetime.strptime(s["start_time"][:19], "%Y-%m-%d %H:%M:%S")
                   for s in fx.spans)
    return earliest.replace(tzinfo=UTC) + EVAL_SPAN_RETENTION


def _checked(fx: sf.Fixture | None = None, now: datetime | None = None) -> sf.Fixture:
    """The fixture to seed or check, refused unless it is well formed and its fixed span
    timestamps are still inside the eval-span retention (otherwise ClickHouse would delete
    the seeded spans again, so reseeding the same timestamps cannot fix it)."""
    fx = sf.build() if fx is None else fx
    problems = sf.validate(fx)
    if problems:
        raise SeedError("the fixture is malformed: " + "; ".join(problems[:5]))
    expires = retention_expires_at(fx)
    if (now or datetime.now(UTC)) >= expires - _RETENTION_MARGIN:
        raise SeedError(
            f"the fixture's spans are dated for retention until {expires:%Y-%m-%d} "
            f"({EVAL_SPAN_RETENTION.days}-day eval-span TTL): ClickHouse would delete seeded "
            "spans again. Move the fixture's date anchor (_T0) forward and bump "
            "FIXTURE_VERSION in demo/synthetic_fixture.py; retention is not changed for the demo")
    return fx


def seed(conn: psycopg.Connection, spans: ClickHouseSpans, fx: sf.Fixture | None = None,
         now: datetime | None = None) -> dict[str, str]:
    """Write whatever is absent, after both stores pass preflight; return what happened. The
    fixture (built here, or the one passed in) is shape- and retention-checked before any
    database read or write."""
    fx = _checked(fx, now)
    problems = pg_schema_problems(conn) + ch_schema_problems(spans)
    if problems:
        raise SeedError("; ".join(problems))
    pg, ch = pg_state(conn, fx), ch_state(spans, fx)
    conflicts = [s.describe() for s in (pg, ch) if s.status == "conflict"]
    if conflicts:
        raise SeedError("refusing to seed, existing data conflicts with the fixture (nothing "
                        "was written): " + " | ".join(conflicts))
    done = {}
    if pg.status == "absent":
        pg_write(conn, fx)
        done["postgres"] = (f"inserted 1 suite, {len(fx.cases)} cases, {len(fx.manifests)} "
                            f"manifests, {len(fx.runs)} runs, {len(fx.run_versions)} run "
                            f"versions, {len(fx.results)} results")
    else:
        done["postgres"] = "already present and identical (nothing written)"
    if ch.status == "absent":
        spans.insert(fx.spans, _dedup_token(fx))
        done["clickhouse"] = f"inserted {len(fx.spans)} spans in {len(fx.results)} traces"
    else:
        done["clickhouse"] = "already present and identical (nothing written)"
    after = [pg_state(conn, fx), ch_state(spans, fx)]
    bad = [s.describe() for s in after if s.status != "identical"]
    if bad:
        raise SeedError("read-back after seeding is not identical: " + " | ".join(bad))
    return done


def offline_inputs(fx: sf.Fixture, scn: sf.Scenario) -> gate.ComparisonInputs:
    """The gate's inputs for one scenario, built from the fixture rows (no database)."""
    run = next(r for r in fx.runs if r["id"] == scn.run_id)
    versions = {v["agent_version"]: v for v in fx.run_versions if v["run_id"] == scn.run_id}

    def side(vkey: str) -> gate.RunSide:
        ver = sf.version_hash(vkey)
        return gate.RunSide(
            run_id=scn.run_id, agent_version=ver, agent_id=run["agent_id"],
            suite_id=run["suite_id"], suite_name=fx.suite["name"], run_status=run["status"],
            version_status=versions[ver]["status"], mode=run["mode"],
            trials_per_case=run["trials_per_case"], cache_mode=run["cache_mode"],
            git_sha=run["git_sha"], versions_in_run=sorted(versions))

    def rows(vkey: str) -> list[dict]:
        ver = sf.version_hash(vkey)
        return sorted((gate.normalize_row(r) for r in fx.results
                       if r["run_id"] == scn.run_id and r["agent_version"] == ver),
                      key=lambda r: (r["case_id"], r["trial"]))

    return gate.ComparisonInputs(
        baseline=side(scn.baseline), candidate=side(scn.candidate),
        suite_case_ids=sorted(c["case_id"] for c in fx.cases),
        baseline_rows=rows(scn.baseline), candidate_rows=rows(scn.candidate))


async def db_verdicts(dsn: str) -> dict[str, gate.Comparison]:
    """Each scenario's comparison as the API computes it: the production store and gate."""
    from engine.db.triage import PgTriageStore, connection_fetchall

    out = {}
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        store = PgTriageStore(connection_fetchall(conn))
        for scn in sf.SCENARIOS:
            inputs = await gate.load_inputs(store, scn.run_id, scn.run_id,
                                            sf.version_hash(scn.baseline),
                                            sf.version_hash(scn.candidate))
            out[scn.key] = gate.compare(inputs)
    return out


def check(conn: psycopg.Connection, spans: ClickHouseSpans, dsn: str) -> list[str]:
    """Read-only: [] when the seeded data is present, identical, and every scenario's verdict
    recomputes as expected through the production store + gate."""
    fx = _checked()
    problems = [s.describe() for s in (pg_state(conn, fx), ch_state(spans, fx))
                if s.status != "identical"]
    if problems:
        return problems + ["run `make demo-seed` (or `python demo/seed.py seed`) first"]
    for key, cmp in asyncio.run(db_verdicts(dsn)).items():
        want = next(s.expected_verdict for s in sf.SCENARIOS if s.key == key)
        if cmp.verdict != want:
            problems.append(f"{key}: the gate computes {cmp.verdict}, expected {want}")
    return problems


# --- URLs and CLI ----------------------------------------------------------------------------


def compare_path(scn: sf.Scenario) -> str:
    q = urllib.parse.urlencode({
        "baseline": scn.run_id, "baseline_version": sf.version_hash(scn.baseline),
        "candidate": scn.run_id, "candidate_version": sf.version_hash(scn.candidate)})
    return f"/compare?{q}"


def first_affected_trace(fx: sf.Fixture) -> str:
    """The regression scenario's first case that passed on the baseline and fails on the
    candidate: its candidate trace."""
    scn = next(s for s in fx.scenarios if s.key == "regression")
    base, cand = sf.OUTCOMES[scn.baseline]["passed"], sf.OUTCOMES[scn.candidate]["passed"]
    idx = next(i for i in range(len(sf.CASES)) if base[i] and not cand[i])
    return sf.trace_id_for(scn.run_id, sf.version_hash(scn.candidate), sf.CASES[idx][0])


def redact(text: str) -> str:
    for var in ("POSTGRES_PASSWORD", "POSTGRES_RO_PASSWORD", "CLICKHOUSE_PASSWORD",
                "CLICKHOUSE_RO_PASSWORD", "VIGIL_PG_DSN", "VIGIL_API_PG_DSN"):
        secret = os.getenv(var)
        if secret:  # raw, and percent-encoded as engine.config builds DSNs
            for form in (secret, urllib.parse.quote(secret), urllib.parse.quote(secret, safe="")):
                text = text.replace(form, "***")
    return text


def _plan() -> int:
    fx = _checked()
    print(f"SYNTHETIC fixture v{sf.FIXTURE_VERSION} ({sf.FIXTURE_SOURCE}): suite {sf.SUITE_NAME}, "
          f"{len(fx.cases)} cases, {len(fx.runs)} runs, {len(fx.results)} results, "
          f"{len(fx.spans)} spans. No model calls; invented data.")
    for scn in fx.scenarios:
        cmp = gate.compare(offline_inputs(fx, scn))
        print(f"  {scn.key:14s} run {scn.run_id}: {scn.title} -> gate {cmp.verdict} "
              f"(expected {scn.expected_verdict})")
        for reason in cmp.incomplete_reasons:
            print(f"                 incomplete: {reason}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("demo/seed.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("plan", "seed", "check"))
    args = ap.parse_args(argv)
    if args.command == "plan":
        return _plan()
    from engine.config import pg_dsn, pg_ro_dsn, uses_ro_pg

    try:
        if args.command == "seed":
            with psycopg.connect(pg_dsn(), connect_timeout=5) as conn:
                done = seed(conn, ClickHouseSpans.admin_from_env())
            for store, what in done.items():
                print(f"{store}: {what}")
            print("SEEDED: SYNTHETIC demo fixture (prepared replay data; no model calls).")
            return 0
        if not uses_ro_pg():
            raise SeedError("no read-only Postgres role configured (POSTGRES_RO_USER/"
                            "POSTGRES_RO_PASSWORD or VIGIL_API_PG_DSN); see "
                            "deploy/create_readonly_users.sh")
        dsn = pg_ro_dsn()
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            problems = check(conn, ClickHouseSpans.readonly_from_env(), dsn)
    except (SeedError, RuntimeError, psycopg.Error, ClickHouseError, gate.ComparisonError) as exc:
        print(f"FAILED: {redact(str(exc))}", file=sys.stderr)
        return 1
    if problems:
        print("CHECK FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {redact(p)}", file=sys.stderr)
        return 1
    print("CHECKED: SYNTHETIC fixture present and identical in Postgres and ClickHouse; "
          "verdicts recomputed by engine.gate:")
    for scn in sf.SCENARIOS:
        print(f"  {scn.key:14s} {scn.expected_verdict:14s} {compare_path(scn)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
