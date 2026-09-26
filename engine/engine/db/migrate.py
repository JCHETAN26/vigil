"""A small psycopg3 migration runner, mirroring the Go ingest migrator's role for
ClickHouse (design decision §1): raw-SQL migrations, SQL as the source of truth, no ORM.

Unlike the Go migrator's plain ``IF NOT EXISTS`` idempotency, this one records every applied
migration's filename and content checksum in a ``schema_migrations`` table and **fails
loudly** if a file that was already applied has since changed. Editing an applied migration
in place is almost always a mistake — the change silently would not re-run — so we surface it
instead of drifting the schema. Fixes go in a new numbered file.

Usage:
    python -m engine.db.migrate           # apply against VIGIL_PG_DSN / POSTGRES_*
Or programmatically:
    from engine.db.migrate import migrate
    migrate(conn)                          # apply to an open psycopg connection
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import psycopg

from engine.config import pg_dsn

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text        PRIMARY KEY,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationDriftError(RuntimeError):
    """Raised when an already-applied migration file's checksum no longer matches what was
    recorded — i.e. the file was edited after being applied."""


def _checksum(sql: bytes) -> str:
    return hashlib.sha256(sql).hexdigest()


def _migration_files(migrations_dir: Path) -> list[Path]:
    # Numbered filenames (001_*, 002_*, ...) sort lexicographically into apply order.
    return sorted(p for p in migrations_dir.glob("*.sql") if p.is_file())


def migrate(conn: psycopg.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Apply pending migrations on ``conn`` in filename order. Returns the list of files
    newly applied by this call (already-applied files are skipped). Raises
    ``MigrationDriftError`` if an applied file's content changed.

    Each pending file is applied and recorded in a single transaction, so a crash leaves no
    applied-but-unrecorded migration. The caller's connection is used as-is; the function
    manages transactions via ``conn.transaction()``.
    """
    migrations_dir = migrations_dir or MIGRATIONS_DIR

    with conn.transaction():
        conn.execute(_SCHEMA_MIGRATIONS_DDL)

    with conn.cursor() as cur:
        cur.execute("SELECT filename, checksum FROM schema_migrations")
        applied = dict(cur.fetchall())

    newly_applied: list[str] = []
    for path in _migration_files(migrations_dir):
        name = path.name
        sql_bytes = path.read_bytes()
        checksum = _checksum(sql_bytes)

        recorded = applied.get(name)
        if recorded is not None:
            if recorded != checksum:
                raise MigrationDriftError(
                    f"migration {name!r} was already applied but its content has changed "
                    f"(recorded checksum {recorded[:12]}…, current {checksum[:12]}…). "
                    "Migrations are immutable once applied; put the fix in a new numbered file."
                )
            continue  # already applied, unchanged

        # Apply the file and record it atomically.
        with conn.transaction():
            conn.execute(sql_bytes.decode("utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                (name, checksum),
            )
        newly_applied.append(name)

    return newly_applied


def main(argv: list[str] | None = None) -> int:
    dsn = pg_dsn()
    with psycopg.connect(dsn) as conn:
        applied = migrate(conn)
    if applied:
        print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("schema up to date; no migrations to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
