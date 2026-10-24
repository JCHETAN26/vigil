"""Unit tests for bench/hotpotqa_triage.py — no API key, no stack, no network.

The preflight/plan logic runs against a tiny synthetic data dir (with the module's pins pointed
at it); the run → regress → reproduce → verify pipeline runs with a fake step runner and a fake
Postgres/ClickHouse/API backend, so the tests check sequencing, refusals and that the reported
verdict is exactly the gate's, without making any model call. The variant manifests are
computed for real (offline: a subprocess that only imports code)."""

from __future__ import annotations

import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hotpotqa_triage as ht

GIB = 1 << 30
SECRET_ANSWER = "Zanzibar-Gold-Answer-7731"
FAKE_ENV = {  # placeholders only: presence is all the tool checks
    "ANTHROPIC_API_KEY": "placeholder", "POSTGRES_PASSWORD": "placeholder",
    "CLICKHOUSE_PASSWORD": "placeholder",
}


# --- fixtures ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def manifests():
    return ht.compute_manifests(ht.DEFAULT_MODEL)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A 3-case dataset shaped like the builder's output, with PINS pointed at it."""
    d = tmp_path / "hotpotqa"
    (d / "subsets").mkdir(parents=True)
    docs = [{"doc_id": t, "text": f"text about {t}"} for t in ("A", "B", "C", "D")]
    (d / "corpus.v1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in docs))
    cases = [
        {"case_id": "q1", "question": "Q1?", "answer": SECRET_ANSWER,
         "supporting_titles": ["A", "B"], "type": "bridge", "level": "hard"},
        {"case_id": "q2", "question": "Q2?", "answer": SECRET_ANSWER,
         "supporting_titles": ["C"], "type": "comparison", "level": "hard"},
        {"case_id": "q3", "question": "Q3?", "answer": SECRET_ANSWER,
         "supporting_titles": ["D"], "type": "bridge", "level": "hard"},
    ]
    (d / "cases.v1.json").write_text(json.dumps({
        "version": "v1", "source": {"revision": "rev1", "sha256": "src1"}, "count": 3,
        "verification": "disabled", "cases": cases}))
    (d / "subsets" / "dev20.txt").write_text("q1\nq2\n")
    (d / "subsets" / "base100.txt").write_text("q3\n")
    pins = {
        "source_revision": "rev1", "source_sha256": "src1", "cases_version": "v1",
        "cases_count": 3, "corpus_docs": 4, "corpus_sha256": ht.sha256_file(d / "corpus.v1.jsonl"),
        "subsets": {
            "dev20": {"n": 2, "sha256": ht.sha256_file(d / "subsets" / "dev20.txt")},
            "base100": {"n": 1, "sha256": ht.sha256_file(d / "subsets" / "base100.txt")},
        },
    }
    monkeypatch.setattr(ht, "PINS", pins)
    return d


def _plan(data_dir, tmp_path, manifests, *, stage="pilot", env=None, budget=None, free=10 * GIB,
          model=None):
    return ht.build_plan(
        stage=stage, model=model or ht.DEFAULT_MODEL, budget=budget, data_dir=data_dir,
        results_root=tmp_path / "results", env=FAKE_ENV if env is None else env,
        manifests=manifests, free_bytes=lambda p: free,
    )


# --- preflight -----------------------------------------------------------------------


def test_disk_floor_is_one_gib(tmp_path):
    assert not ht.check_disk([tmp_path], lambda p: GIB - 1).ok
    assert ht.check_disk([tmp_path], lambda p: GIB).ok
    # A not-yet-created results dir is measured on its nearest existing parent.
    assert ht.check_disk([tmp_path / "a" / "b"], lambda p: 5 * GIB).ok


def test_missing_corpus_refuses_with_build_hint(tmp_path):
    checks, _, ids = ht.inspect_data(tmp_path / "nope", "pilot")
    assert ids == [] and not checks[0].ok
    assert "build_hotpotqa_corpus.py" in checks[0].detail


def test_pinned_data_passes_and_drift_fails(data_dir):
    checks, prov, ids = ht.inspect_data(data_dir, "pilot")
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert ids == ["q1", "q2"]
    assert prov["stage_cases"]["type_mix"] == {"bridge": 1, "comparison": 1}

    with (data_dir / "corpus.v1.jsonl").open("a") as f:
        f.write(json.dumps({"doc_id": "E", "text": "extra"}) + "\n")
    checks, _, _ = ht.inspect_data(data_dir, "pilot")
    assert not next(c for c in checks if c.name == "corpus_pinned").ok


def test_smoke_takes_the_first_ids_and_gold_titles_must_be_in_corpus(data_dir, monkeypatch):
    monkeypatch.setitem(ht.STAGES, "smoke", {**ht.STAGES["smoke"], "take": 1})
    _, _, ids = ht.inspect_data(data_dir, "smoke")
    assert ids == ["q1"]
    doc = json.loads((data_dir / "cases.v1.json").read_text())
    doc["cases"][0]["supporting_titles"] = ["not-in-corpus"]
    (data_dir / "cases.v1.json").write_text(json.dumps(doc))
    checks, _, _ = ht.inspect_data(data_dir, "smoke")
    assert not next(c for c in checks if c.name == "stage_cases").ok


def test_real_variant_manifests_differ_only_in_top_k(manifests):
    checks = ht.check_manifests(manifests, ht.DEFAULT_MODEL)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert manifests["baseline"]["params"][ht.VARIANT_PARAM] == 5
    assert manifests["candidate"]["params"][ht.VARIANT_PARAM] == 1


def test_manifests_match_the_orchestrators_hash_in_process():
    import importlib

    from engine.runner.contract import manifest_of

    for v in ht.VARIANTS.values():
        module = importlib.import_module(v["module"])
        assert manifest_of(module).agent_version == module.AGENT_VERSION
        assert module.TOP_K == v["top_k"]


@pytest.mark.parametrize("tamper,failing", [
    (lambda m: m["candidate"]["params"].update(temperature=0.0), "params_differ_only_in_top_k"),
    (lambda m: m["candidate"].update(model="other-model"), "model_pinned"),
    (lambda m: m["candidate"]["prompts"].update(system="changed"), "prompts_identical"),
    (lambda m: m["candidate"]["tools"][0].update(name="lookup"), "tools_differ_only_in_top_k"),
    (lambda m: m["candidate"].update(agent_version=m["baseline"]["agent_version"]),
     "versions_distinct"),
])
def test_manifest_checks_catch_a_second_change(manifests, tamper, failing):
    m = copy.deepcopy(manifests)
    tamper(m)
    bad = {c.name for c in ht.check_manifests(m, ht.DEFAULT_MODEL) if not c.ok}
    assert failing in bad


def test_manifest_subprocess_never_gets_the_api_key(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw["env"])
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setattr(ht.subprocess, "run", fake_run)
    ht.compute_manifests("m-1")
    assert "ANTHROPIC_API_KEY" not in seen and "PYTHONPATH" not in seen
    assert seen["VIGIL_HOTPOT_MODEL"] == "m-1"


def test_child_env_never_lowers_the_engine_disk_floor(monkeypatch):
    monkeypatch.setenv("VIGIL_MIN_FREE_DISK_GB", "0")
    env = ht._child_env(model="m", corpus=Path("/c.jsonl"))
    assert float(env["VIGIL_MIN_FREE_DISK_GB"]) >= GIB / 1e9
    assert env["VIGIL_HOTPOTQA_CORPUS"] == "/c.jsonl"


# --- plan ----------------------------------------------------------------------------


def test_plan_is_ready_with_prereqs_and_refuses_without(data_dir, tmp_path, manifests):
    plan = _plan(data_dir, tmp_path, manifests)
    assert ht.plan_failures(plan, allow_dirty=True) == []
    assert plan["predeclared"]["expected_verdict"] is None
    assert plan["execution"]["concurrency"] == 1 and plan["execution"]["cache"] == "off"
    assert "NOT a billing cap" in plan["execution"]["budget_note"]

    bare = _plan(data_dir, tmp_path, manifests, env={}, free=GIB // 2)
    fails = " ".join(ht.plan_failures(bare, allow_dirty=True))
    assert "ANTHROPIC_API_KEY" in fails and "disk" in fails and "POSTGRES_PASSWORD" in fails


@pytest.mark.parametrize("budget", [0, -1, float("nan"), 999.0])
def test_budget_outside_stage_guard_refused(data_dir, tmp_path, manifests, budget):
    plan = _plan(data_dir, tmp_path, manifests, budget=budget)
    assert any(f.startswith("budget:") for f in ht.plan_failures(plan, allow_dirty=True))


def test_default_model_is_a_priced_dated_snapshot(data_dir, tmp_path, manifests):
    assert ht.DEFAULT_MODEL == "claude-haiku-4-5-20251001"
    assert {m["model"] for m in manifests.values()} == {ht.DEFAULT_MODEL}
    plan = _plan(data_dir, tmp_path, manifests)
    priced = next(c for c in plan["checks"] if c["name"] == "model_priced")
    assert priced["ok"] and priced["blocking"]
    pricing = plan["execution"]["pricing"]
    # Real shared table: the snapshot resolves to the claude-haiku-4-5 entry ($1 in / $5 out).
    assert pricing["rates"] == {ht.DEFAULT_MODEL: {"input_usd_per_mtok": 1.0,
                                                   "output_usd_per_mtok": 5.0}}
    table = ht.ROOT / "ingest" / "internal" / "pricing" / "prices.json"
    assert pricing["path"] == str(table) and pricing["sha256"] == ht.sha256_file(table)
    assert pricing["prices_as_of"] == json.loads(table.read_text())["prices_as_of"]


def _with_model(manifests, model):
    m = copy.deepcopy(manifests)
    for v in m.values():
        v["model"] = model
    return m


def test_unpriced_model_blocks_the_plan(data_dir, tmp_path, manifests):
    plan = _plan(data_dir, tmp_path, _with_model(manifests, "gpt-9-ultra"), model="gpt-9-ultra")
    fails = [f for f in ht.plan_failures(plan, allow_dirty=True) if f.startswith("model_priced:")]
    assert len(fails) == 1 and "'gpt-9-ultra' not in the price table" in fails[0]
    assert plan["execution"]["pricing"]["rates"] == {}


def test_unpriced_model_in_either_version_blocks(data_dir, tmp_path, manifests):
    m = copy.deepcopy(manifests)
    m["candidate"]["model"] = "gpt-9-ultra"
    plan = _plan(data_dir, tmp_path, m)
    priced = next(c for c in plan["checks"] if c["name"] == "model_priced")
    assert not priced["ok"] and "gpt-9-ultra" in priced["detail"]


@pytest.mark.parametrize("rates", [{"input": 0.0, "output": 5.0}, {"input": 1.0, "output": 0.0}])
def test_zero_rate_model_blocks_the_plan(data_dir, tmp_path, manifests, rates):
    table = tmp_path / "prices.json"
    table.write_text(json.dumps({"prices_as_of": "test", "models": {"claude-haiku-4-5": rates}}))
    env = {**FAKE_ENV, "VIGIL_PRICES_PATH": str(table)}
    plan = _plan(data_dir, tmp_path, manifests, env=env)
    priced = next(c for c in plan["checks"] if c["name"] == "model_priced")
    assert not priced["ok"] and "non-positive" in priced["detail"]
    assert plan["execution"]["pricing"]["sha256"] == ht.sha256_file(table)


def test_unreadable_price_table_blocks_the_plan(data_dir, tmp_path, manifests):
    env = {**FAKE_ENV, "VIGIL_PRICES_PATH": str(tmp_path / "missing.json")}
    plan = _plan(data_dir, tmp_path, manifests, env=env)
    assert any(f.startswith("model_priced:") for f in ht.plan_failures(plan, allow_dirty=True))


def test_run_refuses_an_unpriced_model_even_with_confirm_spend(data_dir, tmp_path, manifests,
                                                               capsys):
    def runner(*a):
        raise AssertionError("no step may run")

    args = _run_args(tmp_path, data_dir, model="gpt-9-ultra", confirm_spend=True)
    rc = ht.cmd_run(args, backend=None, runner=runner, env=FAKE_ENV,
                    manifests=_with_model(manifests, "gpt-9-ultra"),
                    free_bytes=lambda p: 10 * GIB)
    assert rc == 1 and "model_priced" in capsys.readouterr().err
    assert not (tmp_path / "results").exists() or not any((tmp_path / "results").iterdir())


def test_plan_holds_no_gold_answers_or_credentials(data_dir, tmp_path, manifests):
    env = {**FAKE_ENV, "ANTHROPIC_API_KEY": "sk-ant-never-print-me"}
    plan = _plan(data_dir, tmp_path, manifests, env=env)
    text = json.dumps(plan) + ht.render_plan(plan)
    assert SECRET_ANSWER not in text
    assert "sk-ant-never-print-me" not in text and "placeholder" not in text


def test_commands_run_both_versions_in_one_run_with_fixed_settings(data_dir, tmp_path, manifests):
    plan = _plan(data_dir, tmp_path, manifests)
    rd = tmp_path / "rd"
    cmds = ht.build_commands(plan, rd, Path("/s.txt"), run_id="R")
    run = cmds["engine_run"]
    assert run[run.index("--concurrency") + 1] == "1"
    assert run[run.index("--cache") + 1] == "off"
    assert [run[i + 1] for i, a in enumerate(run) if a == "--agent"] == [
        ht.VARIANTS["baseline"]["module"], ht.VARIANTS["candidate"]["module"]]
    reg = cmds["regress"]
    assert reg[reg.index("--baseline-version") + 1] == manifests["baseline"]["agent_version"]
    assert reg[reg.index("--candidate-version") + 1] == manifests["candidate"]["agent_version"]
    assert reg[reg.index("--save") + 1] == str(rd / "bundle") and "--exit-code" in reg


def test_result_dir_is_new_and_never_reused(tmp_path):
    now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
    d = ht.make_result_dir(tmp_path, "pilot", now)
    assert d.name == "20261023T120000Z-pilot"
    with pytest.raises(FileExistsError):
        ht.make_result_dir(tmp_path, "pilot", now)


def test_api_url_must_be_loopback():
    assert ht.check_api_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    for bad in ("http://example.com", "https://127.0.0.1:8080", "http://10.0.0.5:8080"):
        with pytest.raises(SystemExit):
            ht.check_api_url(bad)


def test_parse_run_id():
    rid = "785e9e33-0992-4d02-9a3c-ea2b4f0b108f"
    assert ht.parse_run_id(f"disk check: ok\nrun {rid} succeeded — 4 unit(s), $0.02\n") == rid
    assert ht.parse_run_id("error: worker crashed\n") is None


# --- post-run checks -----------------------------------------------------------------


def _row(case_id, k, *, status="ok", trace="t", tokens=(100, 10), cost=0.001):
    return {
        "case_id": case_id, "trial": 0, "status": status, "passed": True,
        "scores": {"TokenF1": {"score": 1.0}}, "attempts": 1, "latency_ms": 900,
        "input_tokens": tokens[0], "output_tokens": tokens[1], "cost_usd": cost,
        "trace_id": f"{trace}-{case_id}" if status == "ok" else None,
        "output": {"retrievals": [{"query": "x", "doc_ids": [f"d{i}" for i in range(k)]}]},
        "expected": {"type": "bridge"}, "tags": ["bridge"],
    }


def test_unit_checks_detect_wrong_k_zero_cost_and_missing_units():
    good = [_row("q1", 1), _row("q2", 1)]
    checks, stats = ht.check_units("candidate", 1, 2, good)
    assert all(c.ok for c in checks) and stats["searches_returning_top_k"] == 2

    bad = {c.name for c in ht.check_units("candidate", 1, 2, [_row("q1", 5), _row("q2", 1)])[0]
           if not c.ok}
    assert bad == {"candidate_retrieval_k"}
    bad = {c.name for c in ht.check_units("candidate", 1, 2, [_row("q1", 1, cost=0.0),
                                                               _row("q2", 1)])[0] if not c.ok}
    assert bad == {"candidate_nonzero_tokens_cost"}
    bad = {c.name for c in ht.check_units("candidate", 1, 3, good)[0] if not c.ok}
    assert bad == {"candidate_units_complete"}


def test_trace_checks():
    rows = [_row("q1", 5), _row("q2", 5)]
    _, stats = ht.check_units("baseline", 5, 2, rows)
    ch = {"trace_ids": ["t-q1", "t-q2"], "max_k": 5, "k_spans": 2, "models": ["m"],
          "input_tokens": 200, "output_tokens": 20, "cost_usd": 0.002}
    assert all(c.ok for c in ht.check_traces("baseline", 5, "m", rows, ch, stats))
    ch2 = {**ch, "trace_ids": ["t-q1"], "max_k": 6, "models": ["m", "other"]}
    bad = {c.name for c in ht.check_traces("baseline", 5, "m", rows, ch2, stats) if not c.ok}
    assert bad == {
        "baseline_traces_in_clickhouse", "baseline_span_retrieval_k", "baseline_span_model"}


def _verdict(v="regression"):
    return {"verdict": v, "paired_cases": 2, "incomplete_reasons": [], "metrics": [
        {"metric": "passed", "gated": True, "point_estimate": -0.5, "ci_bound": -0.1,
         "tail_prob": 0.01, "regressed": v == "regression", "status": "ok"}]}


def test_cli_api_agreement():
    v = _verdict()
    assert all(c.ok for c in ht.check_cli_api(v, 2, copy.deepcopy(v)))
    api = copy.deepcopy(v)
    api["metrics"][0]["point_estimate"] = -0.4
    api["verdict"] = "no_regression"
    c = next(c for c in ht.check_cli_api(v, 2, api) if c.name == "api_matches_cli")
    assert not c.ok and "point_estimate" in c.detail and "verdict" in c.detail
    assert not ht.check_cli_api(v, 0, None)[0].ok  # exit 0 means no_regression, bundle says not
    skipped = ht.check_cli_api(v, 2, None)[1]
    assert not skipped.ok and not skipped.blocking


def _diff(**over):
    fields = {"model": {"changed": False}, "prompts": {"changed": False},
              "tools": {"changed": True}, "git_sha": {"changed": False},
              "params": {"changed": True, "added": [], "removed": [],
                         "modified": [ht.VARIANT_PARAM]}, "code_ref": {"changed": False}}
    fields.update(over)
    return {"fields": fields}


def test_version_diff_check():
    assert ht.check_version_diff(_diff()).ok
    assert not ht.check_version_diff(_diff(model={"changed": True})).ok
    assert not ht.check_version_diff(_diff(params={"changed": True, "added": ["x"],
                                                   "removed": [], "modified": []})).ok


# --- the run pipeline with fakes -----------------------------------------------------


class FakeBackend:
    def __init__(self, plan_getter, *, verdict, ch_missing=False, api=True):
        self.suites: dict[str, list[str]] = {}
        self.plan_getter = plan_getter
        self.verdict = verdict
        self.ch_missing = ch_missing
        self.api = api
        self.run_id = "11111111-2222-3333-4444-555555555555"

    def suite_case_ids(self, name):
        return self.suites.get(name)

    def run_ids_by_notes(self, notes):
        return [self.run_id]

    def run_data(self, run_id):
        plan = self.plan_getter()
        ids = plan["predeclared"]["case_ids"]
        versions, rows, manifests = [], {}, {}
        for label, v in plan["variants"].items():
            ver = v["agent_version"]
            versions.append({"agent_version": ver, "status": "succeeded", "cases_done": len(ids),
                             "cases_total": len(ids), "cost_spent_usd": 0.01})
            rows[ver] = [_row(i, v["top_k"], trace=label) for i in ids]
            manifests[ver] = {k: plan["manifests"][label][k]
                              for k in ("model", "params", "prompts", "tools")}
        run = {"status": "succeeded", "mode": plan["predeclared"]["mode"],
               "trials_per_case": plan["predeclared"]["trials"], "concurrency": 1,
               "cache_mode": "off", "git_sha": plan["git"]["head"], "agent_id": "hotpotqa-agent",
               "suite_id": "s"}
        return {"run": run, "versions": versions, "manifests": manifests, "rows": rows}

    def clickhouse(self, run_id, version):
        plan = self.plan_getter()
        label, v = next((lbl, v) for lbl, v in plan["variants"].items()
                        if v["agent_version"] == version)
        ids = plan["predeclared"]["case_ids"]
        tids = [f"{label}-{i}" for i in ids][: (1 if self.ch_missing else None)]
        return {"trace_ids": tids, "max_k": v["top_k"], "k_spans": len(ids),
                "models": [plan["execution"]["model"]], "input_tokens": 200,
                "output_tokens": 20, "cost_usd": 0.002}

    def api_get(self, base, path, params):
        assert base.startswith("http://127.0.0.1")
        return copy.deepcopy(self.verdict) if path == "/compare" else _diff()


def _fake_runner(backend, verdict, calls):
    exit_for = {"regression": 2, "no_regression": 0, "incomplete": 3}

    def run(cmd, log_path, env):
        calls.append(cmd)
        assert "PYTHONPATH" not in env and env["VIGIL_HOTPOT_MODEL"] == ht.DEFAULT_MODEL
        assert env["VIGIL_HOTPOTQA_CORPUS"].endswith("corpus.v1.jsonl")
        log_path.write_text("log\n")
        if cmd[3:5] == ["suite", "create"]:
            subset = Path(cmd[cmd.index("--subset-path") + 1])
            backend.suites[cmd[cmd.index("--name") + 1]] = ht.read_ids(subset)
            return 0, "created\n"
        if cmd[3] == "run":
            return 0, f"disk check: ok\nrun {backend.run_id} succeeded — 4 unit(s), $0.01\n"
        if cmd[3] == "regress":
            bundle = Path(cmd[cmd.index("--save") + 1])
            bundle.mkdir()
            (bundle / "verdict.json").write_text(json.dumps(verdict))
            return exit_for[verdict["verdict"]], "VERDICT: ...\n"
        if cmd[2] == "reproduce":
            return 0, "REPRODUCED\n"
        raise AssertionError(cmd)

    return run


def _run_args(tmp_path, data_dir, **over):
    base = dict(stage="pilot", model=ht.DEFAULT_MODEL, budget=None, data_dir=str(data_dir),
                results_root=str(tmp_path / "results"), confirm_spend=True, allow_dirty=True,
                skip_stack_probe=True, api_url="http://127.0.0.1:8080")
    base.update(over)
    return SimpleNamespace(**base)


def _only_result_dir(tmp_path) -> Path:
    (d,) = list((tmp_path / "results").iterdir())
    return d


@pytest.mark.parametrize("verdict", ["regression", "no_regression", "incomplete"])
def test_pipeline_reports_the_gates_verdict_unchanged(data_dir, tmp_path, manifests, verdict):
    v = _verdict(verdict)
    holder = {}
    backend = FakeBackend(lambda: holder["plan"], verdict=v)
    calls: list = []
    runner = _fake_runner(backend, v, calls)

    def capturing_runner(cmd, log_path, env):
        holder.setdefault("plan", json.loads((log_path.parent / "plan.json").read_text()))
        return runner(cmd, log_path, env)

    rc = ht.cmd_run(_run_args(tmp_path, data_dir), backend=backend, runner=capturing_runner,
                    env=FAKE_ENV, manifests=manifests, free_bytes=lambda p: 10 * GIB)
    d = _only_result_dir(tmp_path)
    summary = json.loads((d / "summary.json").read_text())
    assert rc == 0, summary
    assert summary["verdict"] == verdict and summary["status"] == "verified"
    assert [c[3] if c[2] != "reproduce" else "reproduce" for c in calls] == [
        "suite", "run", "regress", "reproduce"]
    assert (d / "units_baseline.json").is_file() and (d / "table_candidate.md").is_file()
    assert f"Gate verdict: `{verdict}`" in (d / "summary.md").read_text()
    for f in d.iterdir():  # no gold answer and no credential placeholder in any output
        if f.is_file():
            text = f.read_text()
            assert SECRET_ANSWER not in text and "placeholder" not in text

    # verify is re-runnable: raw units are kept, and confirmed unchanged.
    before = (d / "units_baseline.json").read_text()
    assert ht.verify(d, api_url="http://127.0.0.1:8080", backend=backend) == 0
    assert (d / "units_baseline.json").read_text() == before
    reports = sorted(d.glob("verify-*.json"))
    last = json.loads(reports[-1].read_text())
    assert any(c["name"] == "baseline_units_unchanged" and c["ok"] for c in last["checks"])


def test_pipeline_flags_missing_traces_but_still_reports_verdict(data_dir, tmp_path, manifests):
    v = _verdict("no_regression")
    holder = {}
    backend = FakeBackend(lambda: holder["plan"], verdict=v, ch_missing=True)
    runner = _fake_runner(backend, v, [])

    def capturing_runner(cmd, log_path, env):
        holder.setdefault("plan", json.loads((log_path.parent / "plan.json").read_text()))
        return runner(cmd, log_path, env)

    rc = ht.cmd_run(_run_args(tmp_path, data_dir), backend=backend, runner=capturing_runner,
                    env=FAKE_ENV, manifests=manifests, free_bytes=lambda p: 10 * GIB)
    summary = json.loads((_only_result_dir(tmp_path) / "summary.json").read_text())
    assert rc == 1 and summary["verdict"] == "no_regression"
    assert "baseline_traces_in_clickhouse" in summary["checks_failed"]


def test_tampered_plan_fails_verify(data_dir, tmp_path, manifests):
    v = _verdict("regression")
    holder = {}
    backend = FakeBackend(lambda: holder["plan"], verdict=v)
    runner = _fake_runner(backend, v, [])

    def capturing_runner(cmd, log_path, env):
        holder.setdefault("plan", json.loads((log_path.parent / "plan.json").read_text()))
        return runner(cmd, log_path, env)

    ht.cmd_run(_run_args(tmp_path, data_dir), backend=backend, runner=capturing_runner,
               env=FAKE_ENV, manifests=manifests, free_bytes=lambda p: 10 * GIB)
    d = _only_result_dir(tmp_path)
    plan = json.loads((d / "plan.json").read_text())
    plan["predeclared"]["trials"] = 3
    (d / "plan.json").write_text(json.dumps(plan))
    assert ht.verify(d, api_url=None, backend=backend) == 1
    assert "plan_unchanged" in json.loads((d / "summary.json").read_text())["checks_failed"]


@pytest.mark.parametrize("over,env,needle", [
    ({"confirm_spend": False}, FAKE_ENV, "--confirm-spend"),
    ({}, {}, "ANTHROPIC_API_KEY"),
])
def test_run_refuses_and_writes_nothing(data_dir, tmp_path, manifests, capsys, over, env, needle):
    def runner(*a):
        raise AssertionError("no step may run")

    rc = ht.cmd_run(_run_args(tmp_path, data_dir, **over), backend=None, runner=runner, env=env,
                    manifests=manifests, free_bytes=lambda p: 10 * GIB)
    assert rc == 1
    assert needle in capsys.readouterr().err
    assert not (tmp_path / "results").exists() or not any((tmp_path / "results").iterdir())


def test_run_refuses_below_disk_floor(data_dir, tmp_path, manifests, capsys):
    rc = ht.cmd_run(_run_args(tmp_path, data_dir), backend=None, runner=None, env=FAKE_ENV,
                    manifests=manifests, free_bytes=lambda p: GIB - 1)
    assert rc == 1 and "1 GiB" in capsys.readouterr().err


def test_existing_suite_with_different_cases_stops_before_spending(data_dir, tmp_path, manifests):
    holder = {}
    backend = FakeBackend(lambda: holder["plan"], verdict=_verdict())
    calls: list = []

    def runner(cmd, log_path, env):
        calls.append(cmd)
        raise AssertionError("must not run")

    plan = _plan(data_dir, tmp_path, manifests)
    backend.suites[plan["execution"]["suite"]] = ["q1"]  # same name, different cases
    rc = ht.cmd_run(_run_args(tmp_path, data_dir), backend=backend, runner=runner, env=FAKE_ENV,
                    manifests=manifests, free_bytes=lambda p: 10 * GIB)
    assert rc == 1 and calls == []
    summary = json.loads((_only_result_dir(tmp_path) / "summary.json").read_text())
    assert summary["status"] == "suite_mismatch" and summary["verdict"] is None


def test_crashed_engine_run_is_found_by_notes_and_still_gated(data_dir, tmp_path, manifests):
    v = _verdict("incomplete")
    holder = {}
    backend = FakeBackend(lambda: holder["plan"], verdict=v)
    inner = _fake_runner(backend, v, [])

    def runner(cmd, log_path, env):
        holder.setdefault("plan", json.loads((log_path.parent / "plan.json").read_text()))
        if cmd[3] == "run":
            return 1, "error: worker for version 512191d1b2a4 exited with code 1\n"
        return inner(cmd, log_path, env)

    ht.cmd_run(_run_args(tmp_path, data_dir), backend=backend, runner=runner, env=FAKE_ENV,
               manifests=manifests, free_bytes=lambda p: 10 * GIB)
    summary = json.loads((_only_result_dir(tmp_path) / "summary.json").read_text())
    assert summary["run_id"] == backend.run_id and summary["verdict"] == "incomplete"
