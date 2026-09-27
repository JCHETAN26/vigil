"""τ²-bench retail "domain server": a thin, long-lived process that owns the τ² retail
environment and speaks line-delimited JSON over stdio.

It exists to ISOLATE τ²'s heavy, foreign dependency stack (openai 3.x, litellm, pandas,
fastapi, tokenizers — all imported at ``import tau2``) from the Vigil engine venv that the
eval worker shares with every other agent. This script runs under τ²'s own venv
(``deps/tau2-bench/.venv``) and imports ONLY stdlib + tau2; the agent (in the engine venv)
launches it as a subprocess and calls it. Nothing here imports vigil or the engine.

Protocol: one JSON request per line on stdin, one JSON response per line on stdout.
  {"op":"prefix"}                         -> {"ok":true,"policy":str,"tools":[anthropic schema]}
  {"op":"reset","task_id":"0"}            -> {"ok":true,"user_scenario":{...},"db_hash":str}
  {"op":"use_tool","name":..,"arguments":{..}} -> {"ok":true,"result":str,"db_hash":str}
  {"op":"db_hash"}                        -> {"ok":true,"db_hash":str}
  {"op":"reward","task_id":"0"}           -> {"ok":true,"reward":0.0|1.0,"db_match":bool,
                                              "reward_basis":[...],"agent_hash":str,"gold_hash":str}
  {"op":"shutdown"}                       -> exits
Errors come back as {"ok":false,"error":str} and never crash the loop.

The static prefix (policy + tool schemas) is byte-identical across processes (τ² builds tools
in a fixed order and the policy carries no timestamps), so the agent can safely mark it as a
shared Anthropic prompt-cache prefix.
"""

from __future__ import annotations

import json
import os
import sys
from functools import cache, lru_cache
from typing import Any

# τ² (via `import tau2`) prints registry/loguru chatter that can land on stdout, which would
# corrupt our line-delimited JSON protocol. Reserve fd 1 as a clean protocol channel BEFORE
# importing tau2: dup the real stdout aside, then point fd 1 (and sys.stdout) at stderr so any
# library output goes to stderr. Protocol responses are written to `_PROTO` only.
_PROTO = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

from tau2.domains.retail.environment import get_environment, get_tasks  # noqa: E402


def _anthropic_tool(t: Any) -> dict:
    """Convert a τ² Tool to an Anthropic Messages `tools=` entry, deterministically. We keep
    τ²'s pydantic ``title`` keys verbatim (rather than stripping them) so the tool block stays
    byte-identical across processes AND large enough that the cached prefix clears Haiku's
    4096-token minimum with margin."""
    fn = t.openai_schema["function"]
    return {
        "name": fn["name"],
        "description": fn["description"],
        "input_schema": fn["parameters"],
    }


@lru_cache(maxsize=1)
def _tasks_by_id() -> dict:
    return {t.id: t for t in get_tasks(task_split_name=None)}


@cache
def _gold_hash(task_id: str) -> str:
    """The target DB hash: a fresh env with the task's reference (gold) actions replayed. Gold
    actions are replayed with ``make_tool_call`` (honoring each action's requestor), the same
    entry point τ²'s own evaluator uses — ``use_tool`` resolves/validates arguments differently
    and rejects some valid gold calls (e.g. 'Product not found' on tasks 2–4)."""
    task = _tasks_by_id()[task_id]
    env = get_environment()
    for a in task.evaluation_criteria.actions or []:
        # τ²'s own evaluator (evaluator_env.calculate_reward) replays gold actions in a
        # try/except and continues: some gold read-actions (e.g. get_product_details) don't
        # resolve standalone and raise, but reads don't mutate the DB, so the write-only DB
        # hash is unaffected. Mirror that tolerance exactly.
        try:
            env.make_tool_call(a.name, requestor=a.requestor, **(a.arguments or {}))
        except Exception:  # noqa: BLE001, S110
            pass
    return env.get_db_hash()


def _user_scenario(task) -> dict:
    """The fields our user-simulator needs to role-play the customer (τ²'s user_scenario)."""
    us = task.user_scenario
    instr = us.instructions
    return {
        "persona": getattr(us, "persona", None),
        "reason_for_call": getattr(instr, "reason_for_call", None),
        "known_info": getattr(instr, "known_info", None),
        "unknown_info": getattr(instr, "unknown_info", None),
        "task_instructions": getattr(instr, "task_instructions", None),
    }


class _Manager:
    """Owns one τ² retail env per session id, so the single shared server process can serve
    concurrent episodes (the eval worker runs several (case, trial) units at once). ``prefix``
    is session-less; ``reset``/``use_tool``/``db_hash``/``reward`` operate on the caller's
    session env."""

    def __init__(self):
        self.envs: dict[str, Any] = {}

    def handle(self, req: dict) -> dict:
        op = req.get("op")
        if op == "prefix":
            env = get_environment()
            return {
                "ok": True,
                "policy": env.get_policy(),
                "tools": [_anthropic_tool(t) for t in env.get_tools()],
            }
        sid = req.get("session")
        if op == "reset":
            task = _tasks_by_id()[req["task_id"]]
            env = get_environment()
            self.envs[sid] = env
            return {"ok": True, "user_scenario": _user_scenario(task), "db_hash": env.get_db_hash()}
        if op == "end":  # free the session's env
            self.envs.pop(sid, None)
            return {"ok": True}
        env = self.envs.get(sid)
        if env is None:
            return {"ok": False, "error": f"no session {sid!r} (call reset first)"}
        if op == "use_tool":
            # A tool that rejects the agent's arguments (bad payment method, unknown product, …)
            # is NORMAL conversational feedback in τ², not a failure: return the error as the
            # tool result so the agent can recover, exactly as τ² does. τ² validates before
            # mutating, so a rejected call leaves the DB unchanged.
            try:
                result = _as_text(env.use_tool(req["name"], **(req.get("arguments") or {})))
                is_error = False
            except Exception as exc:  # noqa: BLE001
                result = f"Error: {type(exc).__name__}: {exc}"
                is_error = True
            return {
                "ok": True,
                "result": result,
                "is_error": is_error,
                "db_hash": env.get_db_hash(),
            }
        if op == "db_hash":
            return {"ok": True, "db_hash": env.get_db_hash()}
        if op == "reward":
            task = _tasks_by_id()[req["task_id"]]
            agent_hash = env.get_db_hash()
            gold_hash = _gold_hash(req["task_id"])
            db_match = agent_hash == gold_hash
            basis = [str(b) for b in (task.evaluation_criteria.reward_basis or [])]
            return {
                "ok": True,
                "reward": 1.0 if db_match else 0.0,  # DB-state reward (final database state)
                "db_match": db_match,
                "reward_basis": basis,
                "agent_hash": agent_hash,
                "gold_hash": gold_hash,
            }
        return {"ok": False, "error": f"unknown op {op!r}"}


def _as_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    for attr in ("model_dump_json", "json"):
        fn = getattr(result, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    try:
        return json.dumps(result, default=str)
    except Exception:  # noqa: BLE001
        return str(result)


def main() -> int:
    session = _Manager()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _PROTO.write(json.dumps({"ok": False, "error": f"bad json: {exc}"}) + "\n")
            continue
        if req.get("op") == "shutdown":
            return 0
        try:
            resp = session.handle(req)
        except Exception as exc:  # noqa: BLE001 - report, never crash the loop
            resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _PROTO.write(json.dumps(resp) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
