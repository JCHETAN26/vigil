"""τ²-bench retail dataset adapter.

Materializes eval cases for the τ² retail domain from the pinned selection in
``data/tau2/retail.pins.json`` (the τ² commit + the dev/measurement task-id split). It is
deliberately **τ²-free**: it only needs the task ids (the agent fetches the task's scenario
from the isolated τ² domain server at run time, and reports τ²'s DB-state reward in
``RunResult.info``, which the ``TauBenchReward`` scorer reads). So this adapter — and the
engine venv it runs in — never import the heavy tau2 package.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from .base import Case, register_adapter

TAU2_SCORERS = ["TauBenchReward"]


@register_adapter
class Tau2RetailAdapter:
    name = "tau2_retail"

    def __init__(
        self,
        path: str,
        version: str | None = None,
        split: str = "dev",
        subset_ids: list[str] | None = None,
    ):
        doc = json.loads(Path(path).read_text())
        self.version = version or (doc.get("pinned_commit") or "unversioned")[:12]
        self._domain = doc.get("domain", "retail")
        if split == "dev":
            self._task_ids = list(doc["dev"]["task_ids"])
        elif split == "measurement":
            self._task_ids = list(doc["measurement"]["candidate_task_ids"])
        else:
            raise ValueError(f"unknown split {split!r}; use 'dev' or 'measurement'")
        self._subset = set(subset_ids) if subset_ids else None

    @classmethod
    def from_config(cls, config: dict) -> Tau2RetailAdapter:
        if "path" not in config:
            raise ValueError("tau2_retail adapter config requires a 'path' (retail.pins.json)")
        subset_ids = config.get("subset_ids")
        if subset_ids is None and config.get("subset_path"):
            subset_ids = [
                line.strip()
                for line in Path(config["subset_path"]).read_text().splitlines()
                if line.strip()
            ]
        return cls(
            path=config["path"],
            version=config.get("version"),
            split=config.get("split", "dev"),
            subset_ids=subset_ids,
        )

    def load(self) -> Iterator[Case]:
        for tid in self._task_ids:
            if self._subset is not None and tid not in self._subset:
                continue
            yield Case(
                case_id=tid,
                input={"task_id": tid, "domain": self._domain},
                expected={
                    "domain": self._domain,
                    # The agent computes τ²'s reward (needs tau2); TauBenchReward reads it from
                    # RunResult.info. It is the only scorer and it gates pass/fail.
                    "scorers": TAU2_SCORERS,
                    "pass_scorers": TAU2_SCORERS,
                },
                tags=[self._domain],
            )
