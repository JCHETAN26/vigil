"""The dataset adapter interface and a small name-based registry (design doc §5).

Adapters are registered by ``name`` and selected via ``eval_suites.adapter``. ``load()``
results are materialized into ``eval_cases`` at suite creation, with the dataset ``version``
pinned in ``eval_suites.config`` so the case set is reproducible across re-runs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Case:
    case_id: str
    input: Any  # passed to agent.run
    expected: dict  # scoring spec (§6)
    tags: list[str] = field(default_factory=list)


@runtime_checkable
class DatasetAdapter(Protocol):
    name: str
    version: str

    def load(self) -> Iterable[Case]: ...


_REGISTRY: dict[str, type] = {}


def register_adapter(cls: type) -> type:
    """Register an adapter class by its ``name`` attribute. Usable as a class decorator."""
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"{cls!r} has no non-empty 'name' to register under")
    _REGISTRY[name] = cls
    return cls


def get_adapter(name: str) -> type:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"no dataset adapter registered as {name!r}; known: {sorted(_REGISTRY)}"
        ) from None
