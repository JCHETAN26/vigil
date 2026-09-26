"""Per-unit execution policy (design §3.4): retries for **infrastructure** failures only,
timeouts as a **scored failure** (never retried), and assertion failures as real
``ok``/``passed=false`` results (never retried).

This module is deliberately free of subprocess/DB/agent concerns so the policy can be unit
tested directly: ``execute_unit`` drives any zero-arg async callable.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

try:
    import anthropic
except Exception:  # pragma: no cover - anthropic is a hard dep, but stay import-safe
    anthropic = None  # type: ignore


def _is_infra_status(code: Any) -> bool:
    return isinstance(code, int) and (code == 429 or 500 <= code < 600)


def is_infra_error(exc: BaseException) -> bool:
    """True for *transient infrastructure* errors that a retry might clear: HTTP 429, HTTP
    5xx, and connection/timeout errors from the client. An assertion that didn't hold or a
    bug in the agent is **not** infra — it is a genuine failure and must not be retried."""
    # Connection / socket timeouts raised outside the anthropic layer.
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if anthropic is not None:
        for name in (
            "APIConnectionError",
            "APITimeoutError",
            "RateLimitError",
            "InternalServerError",
        ):
            cls = getattr(anthropic, name, None)
            if cls is not None and isinstance(exc, cls):
                return True
    # Any error carrying a 429/5xx status code (e.g. anthropic.APIStatusError).
    if _is_infra_status(getattr(exc, "status_code", None)):
        return True
    if _is_infra_status(getattr(getattr(exc, "response", None), "status_code", None)):
        return True
    return False


def default_backoff(attempt: int, *, base: float = 0.5, cap: float = 8.0) -> float:
    """Exponential backoff with full jitter, in seconds. ``attempt`` is 1-based."""
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0, ceiling)


@dataclass
class UnitOutcome:
    status: str  # "ok" | "timeout" | "error"
    attempts: int
    result: Any = None  # a RunResult on "ok"
    error: str | None = None


async def execute_unit(
    run_call: Callable[[], Awaitable[Any]],
    *,
    timeout: float,
    max_attempts: int,
    backoff: Callable[[int], float] = default_backoff,
) -> UnitOutcome:
    """Run one ``(case, trial)`` unit with the retry/timeout policy.

    ``run_call`` is a zero-arg coroutine factory producing a ``RunResult``. Each attempt is
    bounded by ``timeout`` via ``asyncio.wait_for``, which **cancels the in-flight coroutine**
    on expiry — so a slow agent's HTTP request is actually cancelled, not left running and
    spending. A timeout is terminal (``status='timeout'``, not retried). Only infra errors are
    retried, up to ``max_attempts``, with ``backoff`` between attempts.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            result = await asyncio.wait_for(run_call(), timeout=timeout)
            return UnitOutcome(status="ok", attempts=attempts, result=result)
        except asyncio.TimeoutError:
            return UnitOutcome(
                status="timeout",
                attempts=attempts,
                error=f"exceeded per-case timeout of {timeout}s",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify, then retry or record
            if is_infra_error(exc) and attempts < max_attempts:
                await asyncio.sleep(backoff(attempts))
                continue
            return UnitOutcome(status="error", attempts=attempts, error=repr(exc))
