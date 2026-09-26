"""Engine configuration resolved from the environment.

Week-2 stage (a) needs only the Postgres connection; later stages extend this with Redis,
OTLP, budget, concurrency, and cache settings (design doc §10). Postgres access is
``psycopg3``; the DSN mirrors the compose service (127.0.0.1:5432, db/user ``vigil``)."""

from __future__ import annotations

import os
from urllib.parse import quote


def pg_dsn() -> str:
    """Return the Postgres DSN.

    ``VIGIL_PG_DSN`` wins if set (full control). Otherwise a DSN is built from the
    ``POSTGRES_*`` env vars the compose stack uses, so a developer with the stack up and
    ``.env`` loaded needs no extra configuration. The password is required in the built
    form; a missing one fails loudly rather than silently connecting without auth.
    """
    dsn = os.getenv("VIGIL_PG_DSN")
    if dsn:
        return dsn

    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "vigil")
    db = os.getenv("POSTGRES_DB", "vigil")
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError(
            "Postgres password not set: export POSTGRES_PASSWORD (or VIGIL_PG_DSN). "
            "With the stack running, source the repo .env."
        )
    return f"postgresql://{quote(user)}:{quote(password)}@{host}:{port}/{quote(db)}"
