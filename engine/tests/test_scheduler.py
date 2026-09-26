"""Unit tests for windowed dispatch + the per-run cost budget and its overshoot bound
(design §3.3), using a fake worker pool (no subprocesses)."""

from __future__ import annotations

import asyncio


class FakePool:
    """Completes each submitted unit instantly with a fixed per-unit cost."""

    def __init__(self, cost_per_unit: float):
        self._q: asyncio.Queue = asyncio.Queue()
        self._cost = cost_per_unit
        self.submitted: list = []
        self.drained = False

    async def submit(self, unit):
        self.submitted.append(unit)
        await self._q.put({"unit": unit, "budget_cost": self._cost})

    async def next_result(self):
        return await self._q.get()

    async def drain(self):
        self.drained = True


def _cost_of(record):
    return record["budget_cost"]


async def _run(units, pool, *, concurrency, budget):
    from engine.runner.scheduler import Scheduler

    seen = []
    sched = Scheduler(
        units,
        pool,
        concurrency=concurrency,
        budget=budget,
        cost_of=_cost_of,
        on_result=seen.append,
    )
    outcome = await sched.run()
    return outcome, seen


async def test_no_budget_runs_all():
    pool = FakePool(1.0)
    outcome, seen = await _run(list(range(10)), pool, concurrency=4, budget=None)
    assert not outcome.aborted
    assert outcome.completed == 10
    assert len(seen) == 10
    assert pool.drained


async def test_budget_aborts_and_stops_dispatching():
    pool = FakePool(1.0)
    outcome, seen = await _run(list(range(100)), pool, concurrency=2, budget=3.0)
    assert outcome.aborted
    assert outcome.abort_reason == "budget"
    assert outcome.cost >= 3.0
    # It stopped early: nowhere near all 100 units ran.
    assert outcome.completed < 100
    assert len(pool.submitted) < 100


async def test_overshoot_bounded_by_concurrency_times_max_cost():
    concurrency = 4
    cost = 1.0
    budget = 1.0
    pool = FakePool(cost)
    outcome, _ = await _run(list(range(100)), pool, concurrency=concurrency, budget=budget)
    assert outcome.aborted
    # Overshoot <= concurrency x max per-unit cost: the in-flight units that finish after the
    # threshold is crossed (design §3.3).
    assert outcome.cost <= budget + concurrency * cost
    assert outcome.cost >= budget


async def test_window_never_exceeds_concurrency():
    # Instrument submit/next_result to track outstanding; it must stay <= concurrency.
    concurrency = 3
    max_outstanding = {"n": 0}

    class TrackingPool(FakePool):
        def __init__(self, cost):
            super().__init__(cost)
            self.outstanding = 0

        async def submit(self, unit):
            self.outstanding += 1
            max_outstanding["n"] = max(max_outstanding["n"], self.outstanding)
            await super().submit(unit)

        async def next_result(self):
            rec = await super().next_result()
            self.outstanding -= 1
            return rec

    pool = TrackingPool(0.0)
    outcome, _ = await _run(list(range(20)), pool, concurrency=concurrency, budget=None)
    assert outcome.completed == 20
    assert max_outstanding["n"] <= concurrency
