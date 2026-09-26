"""Migrator tests against a live but throwaway Postgres: application, idempotency, ordering,
and the loud failure on drift (an applied migration file edited in place)."""

from __future__ import annotations

import psycopg
import pytest

from engine.db.migrate import MigrationDriftError, migrate


def _table_exists(conn, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()
    return row[0] is not None


def test_applies_all_and_is_idempotent(empty_db_dsn):
    with psycopg.connect(empty_db_dsn) as conn:
        applied = migrate(conn)
        assert applied == ["001_version_manifests.sql", "002_eval_schema.sql"]
        assert _table_exists(conn, "version_manifests")
        assert _table_exists(conn, "eval_case_results")

        # Recorded in schema_migrations, and a second run is a no-op.
        count = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        assert count == 2
        assert migrate(conn) == []


def test_applies_in_filename_order(empty_db_dsn):
    with psycopg.connect(empty_db_dsn) as conn:
        applied = migrate(conn)
        assert applied == sorted(applied)


def test_drift_raises(empty_db_dsn, tmp_path):
    m1 = tmp_path / "001_init.sql"
    m1.write_text("CREATE TABLE t (id int);\n")
    with psycopg.connect(empty_db_dsn) as conn:
        assert migrate(conn, migrations_dir=tmp_path) == ["001_init.sql"]

        # Edit the already-applied file: the checksum no longer matches.
        m1.write_text("CREATE TABLE t (id int, name text);\n")
        with pytest.raises(
            MigrationDriftError, match="already applied but its content has changed"
        ):
            migrate(conn, migrations_dir=tmp_path)


def test_new_migration_applies_over_existing(empty_db_dsn, tmp_path):
    (tmp_path / "001_init.sql").write_text("CREATE TABLE t (id int);\n")
    with psycopg.connect(empty_db_dsn) as conn:
        assert migrate(conn, migrations_dir=tmp_path) == ["001_init.sql"]
        # Adding a new numbered file applies only that file.
        (tmp_path / "002_more.sql").write_text("CREATE TABLE u (id int);\n")
        assert migrate(conn, migrations_dir=tmp_path) == ["002_more.sql"]
        assert _table_exists(conn, "u")
