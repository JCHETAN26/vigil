"""Unit tests for the per-unit execution policy (design §3.4): infra-only retries, timeout as
a terminal scored failure, assertion/other errors not retried."""

from __future__ import annotations

import asyncio

from engine.runner.execution import execute_unit, is_infra_error

_NO_BACKOFF = lambda _n: 0.0  # noqa: E731


class _StatusError(Exception):
    def __init__(self, status_code=None, response=None):
        self.status_code = status_code
        self.response = response


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def test_is_infra_error_classification():
    assert is_infra_error(_StatusError(status_code=429))
    assert is_infra_error(_StatusError(status_code=503))
    assert is_infra_error(_StatusError(response=_Resp(500)))
    assert is_infra_error(ConnectionError())
    assert is_infra_error(TimeoutError())
    # Not infra: client errors and plain bugs are genuine failures, never retried.
    assert not is_infra_error(_StatusError(status_code=400))
    assert not is_infra_error(ValueError("assertion failed"))


async def test_success_first_try():
    async def run_call():
        return "ok"

    out = await execute_unit(run_call, timeout=1, max_attempts=3, backoff=_NO_BACKOFF)
    assert out.status == "ok" and out.attempts == 1 and out.result == "ok"


async def test_retries_infra_then_succeeds():
    calls = {"n": 0}

    async def run_call():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _StatusError(status_code=503)
        return "recovered"

    out = await execute_unit(run_call, timeout=1, max_attempts=5, backoff=_NO_BACKOFF)
    assert out.status == "ok" and out.attempts == 3 and out.result == "recovered"


async def test_infra_error_exhausts_attempts():
    async def run_call():
        raise _StatusError(status_code=429)

    out = await execute_unit(run_call, timeout=1, max_attempts=3, backoff=_NO_BACKOFF)
    assert out.status == "error" and out.attempts == 3


async def test_non_infra_error_not_retried():
    calls = {"n": 0}

    async def run_call():
        calls["n"] += 1
        raise ValueError("bad output")

    out = await execute_unit(run_call, timeout=1, max_attempts=3, backoff=_NO_BACKOFF)
    assert out.status == "error" and out.attempts == 1
    assert calls["n"] == 1  # exactly one attempt, no retry


async def test_timeout_is_terminal_and_cancels_inflight():
    finished = {"done": False}

    async def run_call():
        try:
            await asyncio.sleep(5)  # far longer than the timeout
        except asyncio.CancelledError:
            raise
        finished["done"] = True
        return "should not happen"

    out = await execute_unit(run_call, timeout=0.02, max_attempts=3, backoff=_NO_BACKOFF)
    assert out.status == "timeout"
    assert out.attempts == 1  # a timeout is not retried
    # wait_for cancelled the in-flight coroutine: it never reached the end.
    await asyncio.sleep(0)
    assert finished["done"] is False
