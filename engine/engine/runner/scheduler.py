"""Windowed dispatch with a per-run cost budget (design §3.3).

The scheduler keeps at most ``concurrency`` ``(case, trial)`` units in flight **across all
version workers**, reads results as they arrive, refills the window, and aggregates run-level
cost. When cumulative cost reaches ``cost_budget_usd`` it stops dispatching new units and lets
the in-flight ones drain, ending the run as ``aborted``.

**Maximum overshoot.** Because the budget is checked *between* dispatches while up to
``concurrency`` units are already running, a run can exceed its budget by at most
``concurrency × (maximum per-unit cost)`` — exactly the in-flight units that finish after the
threshold is crossed. This module is pure: it talks to an abstract ``WorkerPool`` so the
policy is unit tested with a fake pool (no subprocesses).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol


class WorkerPool(Protocol):
    async def submit(self, unit: Any) -> None:
        """Hand one unit to the worker responsible for its version."""

    async def next_result(self) -> dict:
        """Await the next completed result record from any worker."""

    async def drain(self) -> None:
        """Signal all workers to stop accepting units and finish in-flight work."""


@dataclass
class SchedulerOutcome:
    aborted: bool
    abort_reason: str | None
    cost: float
    dispatched: int
    completed: int


class Scheduler:
    def __init__(
        self,
        units: Iterable[Any],
        pool: WorkerPool,
        *,
        concurrency: int,
        budget: float | None,
        cost_of: Callable[[dict], float],
        on_result: Callable[[dict], Any],
    ):
        self._units = iter(units)
        self._pool = pool
        self._concurrency = max(1, concurrency)
        self._budget = budget
        self._cost_of = cost_of
        self._on_result = on_result
        self.cost = 0.0
        self._dispatched = 0
        self._completed = 0

    async def _try_dispatch_one(self) -> bool:
        unit = next(self._units, None)
        if unit is None:
            return False
        await self._pool.submit(unit)
        self._dispatched += 1
        return True

    async def run(self) -> SchedulerOutcome:
        aborted = False
        reason: str | None = None

        # Prime the window up to `concurrency` outstanding units.
        outstanding = 0
        for _ in range(self._concurrency):
            if await self._try_dispatch_one():
                outstanding += 1
            else:
                break

        while outstanding > 0:
            record = await self._pool.next_result()
            outstanding -= 1
            self._completed += 1
            self._on_result(record)
            self.cost += self._cost_of(record)

            if not aborted and self._budget is not None and self.cost >= self._budget:
                aborted = True
                reason = "budget"

            # Refill only while we have budget headroom; once aborted, let the window drain.
            if not aborted and await self._try_dispatch_one():
                outstanding += 1

        await self._pool.drain()
        return SchedulerOutcome(
            aborted=aborted,
            abort_reason=reason,
            cost=self.cost,
            dispatched=self._dispatched,
            completed=self._completed,
        )
