"""SYNTHETIC demo fixture: a small, deterministic, invented dataset for the interactive demo.

Nothing here was measured. No agent ran and no model was called: every score, answer, trace
span and timestamp below is invented correctness-fixture data, written so the real stack (the
production Postgres schema, ClickHouse ``spans``, ``engine.gate``, the read-only API and the
dashboard) has one example of each verdict to show. It is not an AI-quality, latency or cost
measurement of anything. ``demo/seed.py`` imports it as seeded replay data.

Shape (12 cases, one agent, four version configs, three runs of two versions each):

  scenario       run      status, units           baseline -> candidate      gate verdict
  regression     ...0101  succeeded, 12 + 12      base -> retrieval_top_k 1  regression
  no_regression  ...0102  succeeded, 12 + 12      base -> reworded prompt    no_regression
  incomplete     ...0103  aborted,   12 + 8       base -> max_steps 2        incomplete

The verdicts are not stored anywhere: they are what ``engine.gate.compare`` computes from these
rows under the default policy (demo/tests check it offline; ``make demo-check`` re-checks it
against the seeded database). Version hashes are ``vigil.compute_agent_version`` of each
version's manifest, so the hash, the manifest and the version diff always agree.

Identity: every name starts with ``vigil-demo-synthetic-``; ids are fixed UUIDs under the
``d3e00000-0000-4000-8000-`` prefix; trace/span ids are sha256-derived from those. The model is
``no-model-synthetic-fixture``; token counts and cost are 0 on spans and unset on results.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from vigil.identity import compute_agent_version

FIXTURE_VERSION = 1
PREFIX = "vigil-demo-synthetic-"
NOTE = (
    "SYNTHETIC demo fixture (prepared, invented data): no agent ran and no model was called. "
    "Scores, answers and traces are invented correctness-fixture data, not an AI-quality, "
    "latency or cost measurement."
)
SUITE_ID = "d3e00000-0000-4000-8000-000000000001"
SUITE_NAME = PREFIX + "qa"
AGENT_ID = PREFIX + "agent"
SERVICE = PREFIX + "fixture"
MODEL = "no-model-synthetic-fixture"
FIXTURE_SOURCE = "demo/synthetic_fixture.py"
_T0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

# --- the invented suite -------------------------------------------------------------------
# (case_id, question, gold answer, wrong answer, the two "gold" passages, kind)
CASES = [
    ("syn-q01", "Which river runs through the fictional town of Arnel?", "the Sorrel",
     "the Wend", ("Arnel (synthetic atlas)", "Sorrel River (synthetic atlas)"), "bridge"),
    ("syn-q02", "Who founded the fictional Brightwater Library?", "Ada Quill",
     "Tomas Reed", ("Brightwater Library (synthetic atlas)", "Ada Quill (synthetic atlas)"),
     "bridge"),
    ("syn-q03", "Is Mount Ferrin taller than Mount Olo in the synthetic atlas?", "yes", "no",
     ("Mount Ferrin (synthetic atlas)", "Mount Olo (synthetic atlas)"), "comparison"),
    ("syn-q04", "In which year was the fictional Calder Bridge opened?", "1911", "1923",
     ("Calder Bridge (synthetic atlas)", "Calder county (synthetic atlas)"), "bridge"),
    ("syn-q05", "Which invented festival is held in the town where Ada Quill was born?",
     "the Lantern Fair", "the Harvest Moot",
     ("Ada Quill (synthetic atlas)", "Lantern Fair (synthetic atlas)"), "bridge"),
    ("syn-q06", "Are Velmor and Ostrava Point in the same fictional province?", "no", "yes",
     ("Velmor (synthetic atlas)", "Ostrava Point (synthetic atlas)"), "comparison"),
    ("syn-q07", "What instrument did the fictional composer Ilse Marrow play?", "the cello",
     "the oboe", ("Ilse Marrow (synthetic atlas)", "Marrow Quartet (synthetic atlas)"),
     "bridge"),
    ("syn-q08", "Which fictional ship first reached the Gannet Isles?", "the Kestrel",
     "the Plover", ("Gannet Isles (synthetic atlas)", "Kestrel (synthetic atlas)"), "bridge"),
    ("syn-q09", "Was the Sorrel River bridge built before Calder Bridge?", "no", "yes",
     ("Sorrel River (synthetic atlas)", "Calder Bridge (synthetic atlas)"), "comparison"),
    ("syn-q10", "Which invented guild maintains the Brightwater Library archive?",
     "the Binders' Guild", "the Scribes' Union",
     ("Brightwater Library (synthetic atlas)", "Binders' Guild (synthetic atlas)"), "bridge"),
    ("syn-q11", "How many towers does the fictional Ostrava Point lighthouse have?", "two",
     "three", ("Ostrava Point (synthetic atlas)", "Ostrava lighthouse (synthetic atlas)"),
     "bridge"),
    ("syn-q12", "Did Ilse Marrow and Ada Quill live in the same fictional century?", "yes", "no",
     ("Ilse Marrow (synthetic atlas)", "Ada Quill (synthetic atlas)"), "comparison"),
]
DISTRACTORS = [f"Unrelated entry {i} (synthetic atlas)" for i in range(1, 6)]

# --- the four invented version configs ------------------------------------------------------
_SYSTEM = (
    "SYNTHETIC DEMO AGENT (fixture only; no model is called). Answer the question from the "
    "passages the search tool returns. End with a line 'FINAL: <answer>'."
)
_SYSTEM_REWORDED = (
    "SYNTHETIC DEMO AGENT (fixture only; no model is called). Use the search tool, answer only "
    "from what it returns, and keep the answer short. End with a line 'FINAL: <answer>'."
)


def _search_tool(k: int) -> list[dict]:
    return [{
        "name": "search",
        "description": f"Search the synthetic atlas. Returns the top {k} matching passages.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                         "required": ["query"]},
    }]


VERSION_CONFIGS = {
    "base": {"model": MODEL, "params": {"max_steps": 4, "retrieval_top_k": 5},
             "prompts": {"system": _SYSTEM}, "tools": _search_tool(5)},
    "top_k1": {"model": MODEL, "params": {"max_steps": 4, "retrieval_top_k": 1},
               "prompts": {"system": _SYSTEM}, "tools": _search_tool(1)},
    "reworded": {"model": MODEL, "params": {"max_steps": 4, "retrieval_top_k": 5},
                 "prompts": {"system": _SYSTEM_REWORDED}, "tools": _search_tool(5)},
    "max_steps2": {"model": MODEL, "params": {"max_steps": 2, "retrieval_top_k": 5},
                   "prompts": {"system": _SYSTEM}, "tools": _search_tool(5)},
}


def version_hash(key: str) -> str:
    cfg = VERSION_CONFIGS[key]
    return compute_agent_version(prompts=cfg["prompts"], tools=cfg["tools"], model=cfg["model"],
                                 params=cfg["params"])


# --- invented per-case outcomes -------------------------------------------------------------
# Per version: passed flags and TokenF1 per case (index = case order above), plus retrieval.
_BASE_PASS = [1, 1, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1]
_BASE_F1 = [1.0, 0.86, 1.0, 1.0, 0.33, 0.9, 1.0, 0.8, 1.0, 0.25, 0.86, 1.0]
_BASE_GOLD = [2, 2, 2, 2, 1, 2, 2, 2, 2, 1, 2, 2]  # gold passages retrieved (of 2)
OUTCOMES = {
    "base": {"passed": _BASE_PASS, "f1": _BASE_F1, "gold": _BASE_GOLD},
    # Controlled change 1: one passage per search. Five previously passing cases now fail.
    "top_k1": {"passed": [1, 0, 1, 0, 0, 1, 0, 1, 0, 0, 0, 1],
               "f1": [1.0, 0.2, 1.0, 0.0, 0.33, 0.9, 0.25, 0.8, 0.0, 0.25, 0.4, 1.0],
               "gold": [1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1, 1]},
    # Controlled change 2: a reworded prompt. One case improves, a few F1 values move slightly.
    "reworded": {"passed": [1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1, 1],
                 "f1": [1.0, 0.88, 1.0, 0.98, 0.9, 0.9, 1.0, 0.82, 1.0, 0.25, 0.86, 1.0],
                 "gold": _BASE_GOLD},
    # Controlled change 3: a step cap. The invented run stops after 8 of 12 cases (aborted),
    # with one timeout; the cases it finished match the baseline.
    "max_steps2": {"passed": _BASE_PASS, "f1": _BASE_F1, "gold": _BASE_GOLD},
}


@dataclass(frozen=True)
class Scenario:
    key: str
    run_id: str
    title: str
    baseline: str  # version config key
    candidate: str
    expected_verdict: str
    run_status: str
    candidate_status: str
    candidate_cases: int  # how many of the 12 cases the candidate has results for
    candidate_timeouts: tuple[str, ...] = ()
    story: str = ""


SCENARIOS = (
    Scenario("regression", "d3e00000-0000-4000-8000-000000000101",
             "retrieval_top_k 5 -> 1", "base", "top_k1", "regression", "succeeded",
             "succeeded", 12,
             story="One passage per search instead of five: five cases that passed now fail."),
    Scenario("no_regression", "d3e00000-0000-4000-8000-000000000102",
             "reworded system prompt", "base", "reworded", "no_regression", "succeeded",
             "succeeded", 12,
             story="Prompt wording only: one case improves, nothing gated gets worse."),
    Scenario("incomplete", "d3e00000-0000-4000-8000-000000000103",
             "max_steps 4 -> 2 (run aborted)", "base", "max_steps2", "incomplete", "aborted",
             "aborted", 8, candidate_timeouts=("syn-q04",),
             story="The candidate stopped after 8 of 12 cases with one timeout: not a full "
                   "paired measurement, so the gate reports incomplete, never a pass."),
)


@dataclass
class Fixture:
    suite: dict
    cases: list[dict]
    manifests: list[dict]
    runs: list[dict]
    run_versions: list[dict]
    results: list[dict]
    spans: list[dict]
    scenarios: tuple[Scenario, ...] = field(default_factory=tuple)


def stable_hex(*parts: object, n: int) -> str:
    material = "|".join(map(str, ("vigil-demo-synthetic", *parts)))
    return hashlib.sha256(material.encode()).hexdigest()[:n]


def trace_id_for(run_id: str, version: str, case_id: str, trial: int = 0) -> str:
    return stable_hex(run_id, version, case_id, trial, n=32)


def ch_time(t: datetime) -> str:
    """DateTime64(9) text as ClickHouse prints it (the seeder inserts and reads back this form)."""
    return f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond * 1000:09d}"


def _event(name: str, content: object, tool: str | None = None) -> tuple[str, str]:
    attrs = {"content": content if isinstance(content, str) else json.dumps(content),
             "vigil.content.truncated": False}
    if tool:
        attrs["gen_ai.tool.name"] = tool
    return name, json.dumps(attrs, sort_keys=True)


def _spans(scn: Scenario, vkey: str, version: str, idx: int, start: datetime,
           failed: bool) -> list[dict]:
    case_id, question, gold, wrong, gold_docs, _ = CASES[idx]
    out = OUTCOMES[vkey]
    cfg = VERSION_CONFIGS[vkey]
    k = cfg["params"]["retrieval_top_k"]
    tid = trace_id_for(scn.run_id, version, case_id)
    answer = gold if out["passed"][idx] else wrong
    retrieved = (list(gold_docs[: out["gold"][idx]]) + DISTRACTORS)[:k]
    sid = {name: stable_hex(tid, name, n=16) for name in ("root", "plan", "search", "answer")}
    # Every column the seeder writes (demo/seed.py SPAN_COLUMNS); usage is zero by design.
    common = {
        "trace_id": tid, "trace_state": "", "agent_id": AGENT_ID, "agent_version": version,
        "git_sha": "", "run_id": tid, "run_kind": "eval", "eval_run_id": scn.run_id,
        "eval_case_id": case_id, "eval_trial": 0, "session_id": "", "role": "",
        "service_name": SERVICE, "service_version": str(FIXTURE_VERSION),
        "span_kind": "INTERNAL", "status_code": "UNSET", "status_message": "",
        "gen_ai_system": "", "gen_ai_operation_name": "", "gen_ai_request_model": "",
        "gen_ai_response_model": "", "gen_ai_usage_input_tokens": 0,
        "gen_ai_usage_output_tokens": 0, "gen_ai_usage_cache_creation_input_tokens": 0,
        "gen_ai_usage_cache_read_input_tokens": 0, "cost_usd": 0.0, "cache_hit": 0,
        "resource_attributes": {"service.name": SERVICE, "vigil.synthetic": "true"},
    }

    def span(name, key, parent, t0_ms, t1_ms, events=(), attrs=None, **over):
        ev = list(events)
        return {**common, "span_id": sid[key], "parent_span_id": sid[parent] if parent else "",
                "span_name": name,
                "start_time": ch_time(start + timedelta(milliseconds=t0_ms)),
                "end_time": ch_time(start + timedelta(milliseconds=t1_ms)),
                "span_attributes": {"vigil.synthetic": "true", **(attrs or {})},
                "events.timestamp": [ch_time(start + timedelta(milliseconds=t0_ms))] * len(ev),
                "events.name": [e[0] for e in ev], "events.attributes": [e[1] for e in ev],
                **over}

    llm = {"span_kind": "CLIENT", "gen_ai_system": "synthetic", "gen_ai_operation_name": "chat",
           "gen_ai_request_model": MODEL, "gen_ai_response_model": MODEL}
    plan = span(f"gen_ai.chat {MODEL}", "plan", "root", 10, 60, [
        _event("gen_ai.system.message", cfg["prompts"]["system"]),
        _event("gen_ai.user.message", question),
        _event("gen_ai.tool_use", {"query": question}, tool="search"),
    ], **llm)
    if failed:
        root = span("agent.run", "root", None, 0, 30000, attrs={"vigil.demo.note": NOTE},
                    status_code="ERROR",
                    status_message="SYNTHETIC: invented timeout (no model was called)")
        return [root, plan]
    root = span("agent.run", "root", None, 0, 400, attrs={"vigil.demo.note": NOTE})
    search = span("search", "search", "root", 70, 120,
                  [_event("gen_ai.retrieval.query", question)],
                  attrs={"vigil.retrieval.doc_ids": json.dumps(retrieved),
                         "vigil.retrieval.k": str(len(retrieved))},
                  span_kind="CLIENT", gen_ai_system="synthetic-atlas",
                  gen_ai_operation_name="retrieval")
    final = span(f"gen_ai.chat {MODEL}", "answer", "root", 130, 390,
                 [_event("gen_ai.choice", f"FINAL: {answer}")], **llm)
    return [root, plan, search, final]


def _scores(vkey: str, idx: int) -> dict:
    out = OUTCOMES[vkey]
    f1 = out["f1"][idx]
    gold = out["gold"][idx]
    passed = bool(out["passed"][idx])
    detail = "SYNTHETIC invented score"

    def entry(score: float, ok: bool) -> dict:
        return {"passed": ok, "score": score, "detail": detail}

    return {
        "ExactMatch": entry(1.0 if passed else 0.0, passed),
        "TokenF1": entry(f1, f1 >= 0.5),
        "RetrievalRecall@5": entry(gold / 2, gold == 2),
        "AllGoldRetrieved": entry(1.0 if gold == 2 else 0.0, gold == 2),
    }


def build() -> Fixture:
    """The whole fixture, deterministically (same output on every call and machine)."""
    suite = {"id": SUITE_ID, "name": SUITE_NAME, "adapter": "synthetic-fixture",
             "config": {"synthetic": True, "fixture": FIXTURE_SOURCE,
                        "fixture_version": FIXTURE_VERSION, "note": NOTE}}
    cases = [{"suite_id": SUITE_ID, "case_id": c[0], "input": {"question": c[1]},
              "expected": {"answer": c[2], "gold_passages": list(c[4]), "synthetic": True},
              "tags": ["synthetic", c[5]]} for c in CASES]
    manifests = [{"agent_id": AGENT_ID, "agent_version": version_hash(k), "git_sha": "none",
                  "model": cfg["model"], "params": cfg["params"], "prompts": cfg["prompts"],
                  "tools": cfg["tools"], "code_ref": None}
                 for k, cfg in VERSION_CONFIGS.items()]
    runs, run_versions, results, spans = [], [], [], []
    for n, scn in enumerate(SCENARIOS):
        created = _T0 + timedelta(minutes=10 * n)
        runs.append({"id": scn.run_id, "suite_id": SUITE_ID, "agent_id": AGENT_ID,
                     "mode": "development", "status": scn.run_status, "cost_budget_usd": None,
                     "cost_spent_usd": 0, "concurrency": 1, "trials_per_case": 1,
                     "cache_mode": "off", "git_sha": None,
                     "notes": f"{NOTE} Scenario: {scn.key} ({scn.title}). {scn.story}",
                     "created_at": created, "started_at": created,
                     "finished_at": created + timedelta(minutes=5)})
        for side, vkey in (("baseline", scn.baseline), ("candidate", scn.candidate)):
            version = version_hash(vkey)
            n_cases = len(CASES) if side == "baseline" else scn.candidate_cases
            status = "succeeded" if side == "baseline" else scn.candidate_status
            run_versions.append({"run_id": scn.run_id, "agent_version": version,
                                 "status": status, "cost_spent_usd": 0,
                                 "cases_total": len(CASES), "cases_done": n_cases,
                                 "started_at": created,
                                 "finished_at": created + timedelta(minutes=5)})
            side_start = created + timedelta(seconds=150 if side == "candidate" else 0)
            for idx in range(n_cases):
                case_id = CASES[idx][0]
                failed = side == "candidate" and case_id in scn.candidate_timeouts
                start = side_start + timedelta(seconds=10 * idx)
                tid = trace_id_for(scn.run_id, version, case_id)
                spans += _spans(scn, vkey, version, idx, start, failed)
                if failed:
                    results.append({
                        "run_id": scn.run_id, "agent_version": version, "case_id": case_id,
                        "trial": 0, "trace_id": tid, "passed": False, "score": None,
                        "scores": {}, "status": "timeout",
                        "error": "SYNTHETIC: invented timeout to show failed-trial handling",
                        "output": None})
                    continue
                scores = _scores(vkey, idx)
                answer = CASES[idx][2] if OUTCOMES[vkey]["passed"][idx] else CASES[idx][3]
                results.append({
                    "run_id": scn.run_id, "agent_version": version, "case_id": case_id,
                    "trial": 0, "trace_id": tid, "passed": bool(OUTCOMES[vkey]["passed"][idx]),
                    "score": scores["TokenF1"]["score"], "scores": scores, "status": "ok",
                    "error": None, "output": {"final": answer, "synthetic": True}})
    return Fixture(suite=suite, cases=cases, manifests=manifests, runs=runs,
                   run_versions=run_versions, results=results, spans=spans,
                   scenarios=SCENARIOS)


# --- shape validation (run before any database access) --------------------------------------

_UUID = re.compile(r"^d3e00000-0000-4000-8000-[0-9a-f]{12}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# Secret-looking keys or values have no place in a fixture (checked on every string).
_SECRETISH = re.compile(
    r"(api[_-]?key|secret|passw|bearer|authorization|private[_-]?key|credential|"
    r"access[_-]?token|auth[_-]?token|sk-ant|sk-[A-Za-z0-9]{16})", re.IGNORECASE)


def _strings(obj, path="$"):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}(key)", str(k)
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def validate(fx: Fixture) -> list[str]:
    """Every invariant the seeder relies on; [] when the fixture is well formed."""
    p: list[str] = []
    if not fx.suite["name"].startswith(PREFIX) or not _UUID.match(fx.suite["id"]):
        p.append("suite must be named vigil-demo-synthetic-* with a reserved d3e00000 id")
    case_ids = [c["case_id"] for c in fx.cases]
    if len(set(case_ids)) != len(case_ids):
        p.append("duplicate case ids")
    hashes = set()
    for m in fx.manifests:
        want = compute_agent_version(prompts=m["prompts"], tools=m["tools"], model=m["model"],
                                     params=m["params"])
        if m["agent_version"] != want or not _HEX64.match(want):
            p.append(f"manifest {m['agent_version'][:12]} does not hash to its own config")
        if m["agent_id"] != AGENT_ID or "synthetic" not in m["model"]:
            p.append("manifests must belong to the synthetic agent and name a no-model model")
        hashes.add(m["agent_version"])
    run_ids = {r["id"] for r in fx.runs}
    for r in fx.runs:
        if not _UUID.match(r["id"]) or r["agent_id"] != AGENT_ID:
            p.append(f"run {r['id']} is not a reserved synthetic run")
        if not str(r["notes"]).startswith("SYNTHETIC"):
            p.append(f"run {r['id']} notes must start with SYNTHETIC")
    keys = set()
    for v in fx.run_versions:
        if v["run_id"] not in run_ids or v["agent_version"] not in hashes:
            p.append(f"run version {v['agent_version'][:12]} has no run or manifest")
        keys.add((v["run_id"], v["agent_version"]))
    unit_keys = set()
    traces = set()
    for r in fx.results:
        k = (r["run_id"], r["agent_version"], r["case_id"], r["trial"])
        if k in unit_keys:
            p.append(f"duplicate result {k}")
        unit_keys.add(k)
        if (r["run_id"], r["agent_version"]) not in keys or r["case_id"] not in case_ids:
            p.append(f"result {k} has no registered version or suite case")
        if not _HEX32.match(r["trace_id"] or "") or r["trace_id"] in traces:
            p.append(f"result {k} needs its own 32-hex trace id")
        traces.add(r["trace_id"])
    by_trace: dict[str, set[str]] = {}
    for s in fx.spans:
        by_trace.setdefault(s["trace_id"], set()).add(s["span_id"])
    if set(by_trace) != traces:
        p.append("every result must have a trace and every trace a result")
    seen = set()
    for s in fx.spans:
        ident = (s["trace_id"], s["span_id"])
        if not _HEX16.match(s["span_id"]) or ident in seen:
            p.append(f"span {ident} needs a unique 16-hex id")
        seen.add(ident)
        if s["parent_span_id"] and s["parent_span_id"] not in by_trace[s["trace_id"]]:
            p.append(f"span {ident} has a parent outside its trace")
        if s["agent_id"] != AGENT_ID or s["eval_run_id"] not in run_ids:
            p.append(f"span {ident} is not a synthetic span")
        if s["gen_ai_request_model"] not in ("", MODEL):
            p.append(f"span {ident} names a model other than {MODEL}")
        usage = ("cost_usd", "gen_ai_usage_input_tokens", "gen_ai_usage_output_tokens",
                 "gen_ai_usage_cache_creation_input_tokens", "gen_ai_usage_cache_read_input_tokens")
        if any(s[f] != 0 for f in usage):
            p.append(f"span {ident} carries invented usage")
    for path, text in _strings([fx.suite, fx.cases, fx.manifests, fx.runs, fx.results,
                                fx.spans]):
        if _SECRETISH.search(text):
            p.append(f"secret-like text at {path}")
    return p
