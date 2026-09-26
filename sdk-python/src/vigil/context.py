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
    session_id: str | None = None
    dataset: str | None = None


_current: ContextVar[RunContext | None] = ContextVar("vigil_run", default=None)


def current_run() -> RunContext | None:
    return _current.get()


def set_run(rc: RunContext) -> Token:
    return _current.set(rc)


def reset_run(token: Token) -> None:
    _current.reset(token)


def current_run_kind() -> str:
    rc = _current.get()
    return rc.run_kind if rc else "unknown"
