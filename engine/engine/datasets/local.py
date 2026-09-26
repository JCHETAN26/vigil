"""A local JSON dataset adapter for hand-written cases (design doc §5).

The file format is a small JSON document:

    {
      "version": "2026-09-25",              # pinned into eval_suites.config for reproducibility
      "cases": [
        {
          "case_id": "calc-1",
          "input": "What is 23 * 19?",       # passed to agent.run
          "expected": {"answer": "437", "tool_calls": ["calculator"]},
          "tags": ["math"]                    # optional
        }
      ]
    }

This is the adapter Week-2 uses to exercise the whole pipeline against ``hello_agent`` before
the real dataset adapters (tau-bench/HotpotQA/BFCL) are wired in.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from .base import Case, register_adapter


@register_adapter
class LocalJSONAdapter:
    name = "local"

    def __init__(self, path: str, version: str | None = None):
        self.path = Path(path)
        self._doc = json.loads(self.path.read_text())
        # The file's version wins; the caller may override (e.g. from suite config).
        self.version = version or self._doc.get("version") or "unversioned"

    @classmethod
    def from_config(cls, config: dict) -> LocalJSONAdapter:
        """Build from an ``eval_suites.config`` dict: requires ``path``; ``version`` optional."""
        if "path" not in config:
            raise ValueError("local adapter config requires a 'path'")
        return cls(path=config["path"], version=config.get("version"))

    def load(self) -> Iterator[Case]:
        cases = self._doc.get("cases", [])
        if not isinstance(cases, list):
            raise ValueError(f"{self.path}: 'cases' must be a list")
        seen: set[str] = set()
        for i, raw in enumerate(cases):
            case_id = raw.get("case_id")
            if not case_id:
                raise ValueError(f"{self.path}: case at index {i} has no 'case_id'")
            if case_id in seen:
                raise ValueError(f"{self.path}: duplicate case_id {case_id!r}")
            seen.add(case_id)
            if "expected" not in raw:
                raise ValueError(f"{self.path}: case {case_id!r} has no 'expected' spec")
            yield Case(
                case_id=case_id,
                input=raw.get("input"),
                expected=raw["expected"],
                tags=list(raw.get("tags", [])),
            )
