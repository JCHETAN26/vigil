"""τ²-bench RETAIL agent under test (Session 4).

A single-control customer-service agent: it follows τ²'s retail *policy* (its system prompt)
and calls τ²'s retail *tools* over the domain's mock database, conversing with an LLM **user
simulator** that role-plays the customer. Both the agent and the simulator run on Haiku via
the engine-provided (vigil-wrapped) Anthropic client; the simulator's calls are tagged
``vigil.role=user_simulator`` and its tokens are reported separately so agent and simulator
cost stay split.

τ² itself (heavy: openai/litellm/pandas) is ISOLATED in its own venv behind a subprocess
"domain server" (``domain_server.py``); this module imports no tau2 — it reads the pinned,
byte-stable prefix (policy + tool schemas) from ``data/tau2/retail.prefix.json`` and talks to
the server over JSON stdio for tool execution and the τ² DB-state reward. The reward is
reported in ``RunResult.info`` (the engine can't recompute it without tau2); the
``TauBenchReward`` scorer reads it.

The static system(policy)+tools prefix is marked with an Anthropic ``cache_control`` breakpoint
so it is written to the prompt cache once and read on every later turn (5-minute TTL).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import vigil
from vigil import RunResult, ToolCall

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PREFIX_PATH = Path(
    os.getenv("VIGIL_TAU2_PREFIX", _REPO_ROOT / "data" / "tau2" / "retail.prefix.json")
)
_SERVER_SCRIPT = Path(__file__).resolve().with_name("domain_server.py")
# The Python interpreter of τ²'s isolated venv (see agents/tau2_retail_agent/Makefile).
_TAU2_PYTHON = os.getenv(
    "VIGIL_TAU2_PYTHON", str(_REPO_ROOT / "deps" / "tau2-bench" / ".venv" / "bin" / "python")
)

AGENT_ID = "tau2-retail-agent"
MODEL = os.getenv("VIGIL_TAU2_MODEL", os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"))
SIM_MODEL = os.getenv("VIGIL_TAU2_SIM_MODEL", MODEL)  # user simulator model (also Haiku)
PARAMS: dict[str, Any] = {"max_tokens": 1024}
_MAX_AGENT_TURNS = 30  # bound the episode (agent LLM calls)
_STOP = "###STOP###"

# The pinned, byte-stable prefix (policy text + Anthropic tool schemas) from the τ² commit
# recorded in the file. Loaded at import so `prompts`/`tools` define the agent version.
_prefix = json.loads(_PREFIX_PATH.read_text())
POLICY: str = _prefix["policy"]
TOOLS: list[dict] = _prefix["tools"]

# Manifest inputs the engine reads (contract §4) + the version hash.
prompts = {"system": POLICY}
model = MODEL
params = PARAMS
tools = TOOLS
AGENT_VERSION = vigil.compute_agent_version(
    prompts=prompts, tools=TOOLS, model=MODEL, params=PARAMS
)

_SYSTEM_BLOCKS = [{"type": "text", "text": POLICY, "cache_control": {"type": "ephemeral"}}]


def _sim_system(user_scenario: dict) -> str:
    """Build the user-simulator's system prompt from τ²'s task user_scenario. The simulator
    role-plays the customer: it pursues its task, reveals known info when asked, never invents
    unknown info, and ends the chat with the stop token when its request is resolved."""
    us = user_scenario or {}
    parts = [
        "You are role-playing a CUSTOMER contacting a retail customer-service agent. Stay in "
        "character as the customer throughout. Speak naturally and briefly, one turn at a time.",
    ]
    if us.get("persona"):
        parts.append(f"Your persona: {us['persona']}")
    if us.get("reason_for_call"):
        parts.append(f"Why you are contacting support: {us['reason_for_call']}")
    if us.get("task_instructions"):
        parts.append(f"What you want to accomplish: {us['task_instructions']}")
    if us.get("known_info"):
        parts.append(f"Information you know and may share when asked: {us['known_info']}")
    if us.get("unknown_info"):
        parts.append(
            "Information you do NOT know — never make it up; say you don't know: "
            f"{us['unknown_info']}"
        )
    parts.append(
        "Do not act as the agent or call tools. Provide details only when the agent asks. When "
        f"your request has been fully resolved (or the agent transfers you to a human), reply with "
        f"exactly {_STOP} and nothing else."
    )
    return "\n\n".join(parts)


# --- domain server client (async, serialized, session-keyed) -------------------------


class _DomainServer:
    """A lazily-started, process-wide client to the τ² retail domain server. One subprocess
    serves all concurrent episodes; requests are serialized (tool calls are fast, local DB
    ops), and each episode uses its own ``session`` id so their envs don't collide."""

    def __init__(self):
        self._proc: Any = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if not Path(_TAU2_PYTHON).exists():
            raise RuntimeError(
                f"τ² venv python not found at {_TAU2_PYTHON}; run `make install` in "
                "agents/tau2_retail_agent (clones + installs tau2 at the pinned commit)."
            )
        self._proc = await asyncio.create_subprocess_exec(
            _TAU2_PYTHON,
            str(_SERVER_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

    async def request(self, req: dict) -> dict:
        async with self._lock:
            await self._ensure()
            self._proc.stdin.write((json.dumps(req) + "\n").encode())
            await self._proc.stdin.drain()
            line = await self._proc.stdout.readline()
            if not line:
                raise RuntimeError("τ² domain server closed unexpectedly")
            resp = json.loads(line.decode())
        if not resp.get("ok"):
            raise RuntimeError(f"domain server error: {resp.get('error')}")
        return resp

    async def shutdown(self) -> None:
        """Best-effort: tell the server to exit (it doesn't reply) and reap the process."""
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.stdin.write(b'{"op":"shutdown"}\n')
            await proc.stdin.drain()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, Exception):  # noqa: BLE001
            proc.kill()


_SERVER = _DomainServer()


# --- tracing / client helpers (standalone + e2e) -------------------------------------


def init_tracing():
    vigil.init(
        service_name=AGENT_ID,
        agent_id=AGENT_ID,
        agent_version=AGENT_VERSION,
        git_sha=vigil.git_sha(),
    )


def make_client(raw: Any | None = None, *, timeout: float | None = None):
    import anthropic

    return vigil.wrap(raw or anthropic.AsyncAnthropic(timeout=timeout))


# --- the episode ---------------------------------------------------------------------


async def _agent_call(client, messages: list[dict]):
    return await client.messages.create(
        model=MODEL, system=_SYSTEM_BLOCKS, tools=TOOLS, messages=messages, **PARAMS
    )


async def _sim_call(client, sim_system: str, sim_messages: list[dict]) -> tuple[str, int, int]:
    """One user-simulator turn (tagged vigil.role=user_simulator). Returns (text, in, out)."""
    with vigil.role("user_simulator"):
        resp = await client.messages.create(
            model=SIM_MODEL, system=sim_system, messages=sim_messages, max_tokens=512
        )
    text = "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
    )
    usage = getattr(resp, "usage", None)
    return text, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0


async def run(
    client,
    case_input: Any,
    *,
    eval_run_id: str = "",
    eval_case_id: str = "",
    trial: int = 0,
) -> RunResult:
    task_id = case_input["task_id"] if isinstance(case_input, dict) else str(case_input)
    session = f"{eval_run_id}:{eval_case_id}:{trial}"

    tool_calls: list[ToolCall] = []
    in_tok = out_tok = cache_write = cache_read = 0
    sim_in = sim_out = 0
    last_agent_text = ""

    with vigil.agent_run(
        run_kind="eval",
        eval_run_id=eval_run_id,
        eval_case_id=eval_case_id,
        trial=trial,
        attributes={"vigil.tau2.task_id": task_id, "vigil.tau2.domain": "retail"},
    ) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        reset = await _SERVER.request({"op": "reset", "session": session, "task_id": task_id})
        sim_system = _sim_system(reset.get("user_scenario") or {})

        # The customer opens: elicit their request with a synthetic agent greeting.
        sim_messages = [{"role": "user", "content": "Hello! How can I help you today?"}]
        opening, si, so = await _sim_call(client, sim_system, sim_messages)
        sim_in += si
        sim_out += so
        sim_messages.append({"role": "assistant", "content": opening})
        messages: list[dict] = [{"role": "user", "content": opening}]

        stop = _STOP in opening
        turns = 0
        while not stop and turns < _MAX_AGENT_TURNS:
            turns += 1
            resp = await _agent_call(client, messages)
            usage = getattr(resp, "usage", None)
            if usage:
                in_tok += getattr(usage, "input_tokens", 0) or 0
                out_tok += getattr(usage, "output_tokens", 0) or 0
                cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
                cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if tool_uses:
                results = []
                for block in tool_uses:
                    args = dict(getattr(block, "input", {}) or {})
                    tool_calls.append(ToolCall(name=block.name, arguments=args))
                    with vigil.tool_call(tool_name=block.name, arguments=args):
                        r = await _SERVER.request(
                            {
                                "op": "use_tool",
                                "session": session,
                                "name": block.name,
                                "arguments": args,
                            }
                        )
                    tr = {"type": "tool_result", "tool_use_id": block.id, "content": r["result"]}
                    if r.get("is_error"):
                        tr["is_error"] = True  # let the model see the failure and recover
                    results.append(tr)
                    if block.name == "transfer_to_human_agents":
                        stop = True
                messages.append({"role": "user", "content": results})
                continue

            # No tool use → the agent addressed the customer. Forward to the simulator.
            text = "".join(
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
            )
            last_agent_text = text
            sim_messages.append({"role": "user", "content": text})
            reply, si, so = await _sim_call(client, sim_system, sim_messages)
            sim_in += si
            sim_out += so
            if _STOP in reply:
                break
            sim_messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": reply})

        reward = await _SERVER.request({"op": "reward", "session": session, "task_id": task_id})
        await _SERVER.request({"op": "end", "session": session})

    return RunResult(
        output=last_agent_text,
        final_answer=None,
        tool_calls=tool_calls,
        trace_id=trace_id,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
        sim_input_tokens=sim_in,
        sim_output_tokens=sim_out,
        info={
            "reward": reward["reward"],
            "db_match": reward["db_match"],
            "reward_basis": reward["reward_basis"],
            "gold_hash": reward["gold_hash"],
            "agent_hash": reward["agent_hash"],
            "turns": turns,
        },
    )
