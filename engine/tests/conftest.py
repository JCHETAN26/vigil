"""Test fixtures. The repo/migrator tests run against the *live* Postgres from the compose
stack, but in a throwaway database created and dropped per session, so they never touch the
real ``vigil`` database and leave nothing behind.

Requires the stack up and Postgres creds in the environment (source the repo .env). If
Postgres is not configured, unreachable, or refuses ``CREATE DATABASE``, the DB-backed tests
are skipped with the reason (``-rs`` prints it), so the suite still runs on a machine without
the stack. Set ``REQUIRE_DB`` (or ``REQUIRE_LIVE``) to any non-empty value to make that a test
failure instead: CI sets ``REQUIRE_DB=1`` because it provides Postgres and the SQL must run.
Reasons never include the password or the raw DSN.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import NoReturn
from urllib.parse import quote

import psycopg
import pytest
from psycopg import conninfo

from engine.config import pg_dsn
from engine.db.migrate import migrate

# Data tables to wipe between tests for isolation (schema_migrations is left intact).
_DATA_TABLES = (
    "eval_case_results",
    "eval_run_versions",
    "eval_runs",
    "eval_cases",
    "eval_suites",
    "version_manifests",
)

# Any non-empty value counts, matching how the root Makefile reads REQUIRE_LIVE.
_REQUIRE_VARS = ("REQUIRE_DB", "REQUIRE_LIVE")


def _required_by() -> str | None:
    """Name of the first set variable that makes the DB fixtures mandatory, else None."""
    return next((v for v in _REQUIRE_VARS if os.environ.get(v)), None)


def _redact(text: str, dsn: str | None) -> str:
    """Strip the DSN and its password from an error message (libpq can echo parts of it)."""
    if not dsn:
        return text
    secrets = [dsn]
    try:
        password = conninfo.conninfo_to_dict(dsn).get("password")
    except psycopg.Error:
        password = None
    if password:
        # Also the percent-encoded forms (pg_dsn() quotes it, keeping "/"; others encode all).
        pw = str(password)
        secrets += [pw, quote(pw), quote(pw, safe="")]
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def _unavailable(what: str, exc: BaseException, dsn: str | None = None) -> NoReturn:
    """Skip (default) or fail (REQUIRE_DB/REQUIRE_LIVE set) because Postgres is unusable."""
    detail = " ".join(_redact(str(exc), dsn).split())
    reason = f"Postgres {what}: {type(exc).__name__}: {detail}"
    required_by = _required_by()
    # Only the redacted reason may be printed: pytrace=False drops the traceback (whose frame
    # locals hold the raw DSN) and `from None` drops the chained, unredacted original error.
    if required_by:
        msg = f"{reason} ({required_by} is set, so this fails instead of skipping)"
        raise pytest.fail.Exception(msg, pytrace=False) from None
    msg = f"{reason}; is the stack up? Set REQUIRE_DB=1 to fail instead of skipping"
    raise pytest.skip.Exception(msg) from None


@contextmanager
def _throwaway_db(prefix: str) -> Iterator[str]:
    """Create a fresh, empty database, yield its DSN, and drop it on exit."""
    try:
        admin_dsn = pg_dsn()
    except RuntimeError as exc:
        _unavailable("not configured", exc)

    db_name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5)
    except psycopg.Error as exc:
        _unavailable("unreachable", exc, admin_dsn)

    with admin:
        try:
            admin.execute(f'CREATE DATABASE "{db_name}"')
        except psycopg.Error as exc:
            _unavailable("refused CREATE DATABASE", exc, admin_dsn)
        try:
            yield conninfo.make_conninfo(admin_dsn, dbname=db_name)
        finally:
            # FORCE (PG13+) terminates any lingering connections so the drop can't hang.
            admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def test_db_dsn():
    """Create a throwaway database, migrate it, yield its DSN, and drop it at session end."""
    with _throwaway_db("vigil_test") as dsn:
        with psycopg.connect(dsn) as conn:
            migrate(conn)
        yield dsn


@pytest.fixture
def empty_db_dsn():
    """A brand-new, un-migrated throwaway database, dropped after the test. For migrator
    tests that need to control exactly which migrations run."""
    with _throwaway_db("vigil_mig") as dsn:
        yield dsn


@pytest.fixture
def pg_conn(test_db_dsn):
    """A fresh sync connection to the throwaway DB, with data tables truncated first."""
    with psycopg.connect(test_db_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(_DATA_TABLES)} CASCADE")
        conn.commit()
        yield conn


@pytest.fixture
async def async_pg_conn(test_db_dsn):
    """A fresh async connection to the throwaway DB, with data tables truncated first."""
    async with await psycopg.AsyncConnection.connect(test_db_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"TRUNCATE {', '.join(_DATA_TABLES)} CASCADE")
        await conn.commit()
        yield conn
