"""Ambient run/eval identity, propagated to child spans within a run.

Identity that varies per execution (run_id, run_kind, eval ids, session) lives in a
contextvar so that every span opened inside an ``agent_run`` inherits it (contextvars
propagate across ``await`` and nested calls in the same task). A span processor stamps
these onto each span at start (see processor.py)."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_kind: str = "live"  # "live" | "eval" | "unknown"
    eval_run_id: str | None = None
    eval_case_id: str | None = None
    trial: int | None = None  # 0-based repeat index within an eval run (design doc §2.3)
    session_id: str | None = None
    dataset: str | None = None


_current: ContextVar[RunContext | None] = ContextVar("vigil_run", default=None)

# The active role within a run (e.g. "user_simulator"), stamped as vigil.role onto every
# span opened while it is set. Orthogonal to run identity: a role is a sub-scope inside an
# agent_run, so it lives in its own contextvar and nests independently.
_role: ContextVar[str | None] = ContextVar("vigil_role", default=None)


def current_run() -> RunContext | None:
    return _current.get()


def set_run(rc: RunContext) -> Token:
    return _current.set(rc)


def reset_run(token: Token) -> None:
    _current.reset(token)


def current_run_kind() -> str:
    rc = _current.get()
    return rc.run_kind if rc else "unknown"


def current_role() -> str | None:
    return _role.get()


def set_role(role: str) -> Token:
    return _role.set(role)


def reset_role(token: Token) -> None:
    _role.reset(token)
