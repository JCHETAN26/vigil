"""Test fixtures. The repo/migrator tests run against the *live* Postgres from the compose
stack, but in a throwaway database created and dropped per session, so they never touch the
real ``vigil`` database and leave nothing behind.

Requires the stack up and Postgres creds in the environment (source the repo .env). If the
server is unreachable, the DB-backed tests are skipped with a clear reason rather than
failing the whole suite on a machine without the stack.
"""

from __future__ import annotations

import uuid

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


@pytest.fixture(scope="session")
def test_db_dsn():
    """Create a throwaway database, migrate it, yield its DSN, and drop it at session end."""
    try:
        admin_dsn = pg_dsn()
    except RuntimeError as exc:
        pytest.skip(f"Postgres not configured: {exc}")

    db_name = f"vigil_test_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres unreachable ({exc}); is the stack up?")

    with admin:
        admin.execute(f'CREATE DATABASE "{db_name}"')
        try:
            dsn = conninfo.make_conninfo(admin_dsn, dbname=db_name)
            with psycopg.connect(dsn) as conn:
                migrate(conn)
            yield dsn
        finally:
            # FORCE (PG13+) terminates any lingering connections so the drop can't hang.
            admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


@pytest.fixture
def empty_db_dsn():
    """A brand-new, un-migrated throwaway database, dropped after the test. For migrator
    tests that need to control exactly which migrations run."""
    try:
        admin_dsn = pg_dsn()
        admin = psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5)
    except (RuntimeError, psycopg.OperationalError) as exc:
        pytest.skip(f"Postgres unavailable: {exc}")

    db_name = f"vigil_mig_{uuid.uuid4().hex[:12]}"
    with admin:
        admin.execute(f'CREATE DATABASE "{db_name}"')
        try:
            yield conninfo.make_conninfo(admin_dsn, dbname=db_name)
        finally:
            admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


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
