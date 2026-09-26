"""Agent-version content hashing and git SHA capture (design doc §2.3).

The agent version is a content hash over {code, prompt templates, model id, decoding
params, tool definitions} — it changes exactly when behavior can change. Serialization is
canonical (sorted keys, stable separators, UTF-8) so the same inputs always hash the same;
prompt strings are hashed verbatim (whitespace is NOT normalized), because a whitespace
tweak in a prompt can change behavior. The git SHA is recorded separately (it answers
"which commit", not "did behavior change").
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def _canonical(obj: Any) -> bytes:
    # sort_keys gives order-independence for dict fields (params, tool schemas); lists keep
    # their order (tool order is part of the definition). ensure_ascii=False keeps prompt
    # bytes stable and exact.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _hash_code_paths(code_paths: Iterable[Any] | None) -> dict[str, str]:
    """Map each given path to the SHA-256 of its bytes. Directories are walked; the key is
    the path as provided (for directories, the file's path relative to it), so the result
    is deterministic regardless of filesystem iteration order."""
    if not code_paths:
        return {}
    out: dict[str, str] = {}
    for raw in code_paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
        elif p.is_file():
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            # Missing path is recorded as such so the hash is stable and the gap is visible.
            out[str(p)] = "missing"
    return out


def compute_agent_version(
    *,
    prompts: Mapping[str, str],
    tools: Any = None,
    model: str,
    params: Mapping[str, Any] | None = None,
    code_paths: Iterable[Any] | None = None,
) -> str:
    """Return the agent-version content hash (hex SHA-256) over the behavior-defining
    inputs. Prompts are hashed verbatim."""
    material = {
        "model": model,
        "params": dict(params) if params else {},
        "prompts": dict(prompts) if prompts else {},
        "tools": tools if tools is not None else [],
        "code": _hash_code_paths(code_paths),
    }
    return hashlib.sha256(_canonical(material)).hexdigest()


def git_sha(cwd: str | None = None) -> str | None:
    """Return the current git commit SHA, from VIGIL_AGENT_GIT_SHA if set, else `git
    rev-parse HEAD`, else None."""
    env = os.getenv("VIGIL_AGENT_GIT_SHA")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
