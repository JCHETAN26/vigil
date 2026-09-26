"""The parent↔worker line protocol (design §3.2, §3.4).

Two independent channels keep the result stream immune to stray output:

- **control** (parent → worker): unit-dispatch and drain messages, written as JSONL to the
  worker's **stdin**.
- **results** (worker → parent): one JSON result record per line, written to a **dedicated
  results file descriptor** — *not* stdout — so a stray ``print`` or a library's logging can
  never corrupt the protocol. stdout/stderr carry human-readable logs only.

Messages are newline-delimited JSON. Each message has a ``type``:
- ``unit``   — control: run one ``(case, trial)`` unit.
- ``drain``  — control: stop accepting units, finish in-flight work, flush, exit.
- ``result`` — results: one completed unit's outcome.
"""

from __future__ import annotations

import json
from typing import Any

UNIT = "unit"
DRAIN = "drain"
RESULT = "result"


def encode(message: dict) -> bytes:
    """Serialize a message to one length-free JSON line (trailing newline included)."""
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: str | bytes) -> dict:
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)


def unit_message(
    *, agent_version: str, case_id: str, trial: int, input: Any, expected: dict
) -> dict:
    return {
        "type": UNIT,
        "agent_version": agent_version,
        "case_id": case_id,
        "trial": trial,
        "input": input,
        "expected": expected,
    }


def drain_message() -> dict:
    return {"type": DRAIN}


def iter_lines(text: str):
    """Yield decoded messages from a blob of newline-delimited JSON, ignoring blank lines.
    Tolerates a partial trailing line (no newline) by skipping it — callers that need every
    record ensure a trailing newline (``encode`` always writes one)."""
    for raw in text.splitlines():
        raw = raw.strip()
        if raw:
            yield decode(raw)
