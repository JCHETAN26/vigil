"""Engine configuration resolved from the environment.

Week-2 stage (a) needs only the Postgres connection; later stages extend this with Redis,
OTLP, budget, concurrency, and cache settings (design doc §10). Postgres access is
``psycopg3``; the DSN mirrors the compose service (127.0.0.1:5432, db/user ``vigil``)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote


def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_float(key: str, default: float | None) -> float | None:
    v = os.getenv(key)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


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


def pg_ro_dsn() -> str:
    """Postgres DSN for the **read-only** dashboard API. ``VIGIL_API_PG_DSN`` wins; else, when
    ``POSTGRES_RO_USER``/``POSTGRES_RO_PASSWORD`` are set, a DSN for that role is built (this is
    what the API should use so a bug can't write). If no RO credentials are configured it falls
    back to :func:`pg_dsn` so the API still runs — but then it is only read-only by discipline,
    not by grant, so set up the RO role (deploy/create_readonly_users.sh)."""
    dsn = os.getenv("VIGIL_API_PG_DSN")
    if dsn:
        return dsn
    ro_user = os.getenv("POSTGRES_RO_USER")
    ro_pass = os.getenv("POSTGRES_RO_PASSWORD")
    if not (ro_user and ro_pass):
        return pg_dsn()
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "vigil")
    return f"postgresql://{quote(ro_user)}:{quote(ro_pass)}@{host}:{port}/{quote(db)}"


def uses_ro_pg() -> bool:
    """True when a dedicated read-only Postgres role is configured for the API."""
    if os.getenv("VIGIL_API_PG_DSN"):
        return True
    return bool(os.getenv("POSTGRES_RO_USER") and os.getenv("POSTGRES_RO_PASSWORD"))


def redis_url() -> str | None:
    """Redis URL for the dev LLM cache. ``VIGIL_REDIS_URL`` wins; otherwise built from
    ``REDIS_PASSWORD`` against the compose service. Returns None if neither is set (the
    cache is off by default, so a missing URL is not an error)."""
    url = os.getenv("VIGIL_REDIS_URL")
    if url:
        return url
    password = os.getenv("REDIS_PASSWORD")
    if not password:
        return None
    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = os.getenv("REDIS_PORT", "6379")
    return f"redis://:{quote(password)}@{host}:{port}/0"


@dataclass
class EngineConfig:
    """Run-time engine settings resolved from the environment (design §10). The CLI uses
    these as defaults; the values that define a run (concurrency, trials, cache, budget) are
    persisted on the ``eval_runs`` row so a run is reproducible from the database alone."""

    cache_mode: str = "off"  # off | read | read_write
    cost_budget_usd: float | None = None
    concurrency: int = 4
    trials_per_case: int = 1
    per_case_timeout_s: float = 120.0
    max_attempts: int = 3

    @property
    def request_timeout_s(self) -> float:
        """The Anthropic client's per-request timeout — a backstop set *below* the per-case
        timeout so a hung HTTP request errors out before (and cannot outlive) the per-case
        cancellation (approved change)."""
        return max(1.0, self.per_case_timeout_s * 0.8)

    @classmethod
    def from_env(cls) -> EngineConfig:
        return cls(
            cache_mode=os.getenv("VIGIL_EVAL_CACHE", "off").lower(),
            cost_budget_usd=_env_float("VIGIL_EVAL_COST_BUDGET_USD", None),
            concurrency=_env_int("VIGIL_EVAL_CONCURRENCY", 4),
            trials_per_case=_env_int("VIGIL_EVAL_TRIALS_PER_CASE", 1),
            per_case_timeout_s=_env_float("VIGIL_EVAL_CASE_TIMEOUT_S", 120.0),
            max_attempts=_env_int("VIGIL_EVAL_MAX_ATTEMPTS", 3),
        )
