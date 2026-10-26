"""Offline reproduction of `engine regress --save` bundles (bench/regression_verdict.py).

Bundles are produced by the real CLI command against an in-memory fake store, then verified;
tampering with the verdict, the raw rows, or the policy must fail reproduction — even when the
tamperer also rewrites SHA256SUMS. The synthetic rows are correctness fixtures, not quality or
performance evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "engine" / "tests"))
sys.path.insert(0, str(ROOT / "engine"))

# These resolve only after the sys.path setup above (bench/ and engine/ are not installed).
import regression_verdict as rv  # noqa: E402
from triage_fakes import V1, block_scipy, f1_rows, f1_shift_pair, row, store_pair  # noqa: E402

from engine import bundle, cli, gate  # noqa: E402


@pytest.fixture
def saved(tmp_path, monkeypatch, capsys):
    """A bundle for a 4-point F1 drop where the candidate also errors on some trials."""
    base, cand = f1_shift_pair(n_cases=30, shift=-0.04)
    cand_rows = f1_rows(cand)
    cand_rows[0] = row(cand_rows[0]["case_id"], 0, status="error", error="tool crashed")
    store, b, c = store_pair(f1_rows(base), cand_rows, cases=list(base))
    store.manifests[("agent", V1)] = {
        "agent_id": "agent", "agent_version": V1, "git_sha": "abc", "model": "m",
        "params": {"api_key": "sk-secret", "max_tokens": 64}, "prompts": {"s": "p"},
        "tools": [], "code_ref": None, "created_at": None,
    }

    @asynccontextmanager
    async def fake_store():
        yield store

    monkeypatch.setattr(cli, "_triage_store", fake_store)
    out = tmp_path / "bundle"
    assert cli.main(["regress", "--baseline", b, "--candidate", c, "--n-boot", "2000",
                     "--save", str(out)]) == 0
    capsys.readouterr()
    return out


def _edit(path: Path, fn, *, forge_sums: bool) -> None:
    doc = json.loads(path.read_text())
    fn(doc)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    if forge_sums:
        d = path.parent
        (d / bundle.SUMS).write_text("".join(
            f"{hashlib.sha256((d / n).read_bytes()).hexdigest()}  {n}\n" for n in bundle.FILES))


def _reproduce(d, capsys):
    rc = rv.main(["reproduce", str(d)])
    return rc, capsys.readouterr().out


def test_saved_bundle_reproduces(saved, capsys):
    rc, out = _reproduce(saved, capsys)
    assert rc == 0, out
    assert "REPRODUCED: verdict regression" in out
    verdict = json.loads((saved / "verdict.json").read_text())
    inputs = json.loads((saved / "inputs.json").read_text())
    meta = json.loads((saved / "metadata.json").read_text())
    # Auditable: identities, exact policy, failed trials kept with status + error, provenance.
    assert verdict["policy"] == json.loads((saved / "policy.json").read_text())
    assert verdict["policy"]["thresholds"] == {"passed": 0.05, "TokenF1": 0.03}
    assert inputs["candidate"]["agent_version"] == verdict["candidate"]["agent_version"]
    failed = [r for r in inputs["candidate_rows"] if r["status"] != "ok"]
    assert failed and failed[0]["error"] == "tool crashed"
    assert verdict["coverage"]["candidate"]["failed_trials"] == 1
    assert meta["format_version"] == bundle.FORMAT_VERSION and "argv" in meta
    assert meta["suites"]["baseline"]["config"] == {"path": "x.json", "version": "1"}
    assert "engine_source" in meta and meta["environment"]["numpy"]
    assert meta["manifests"]["baseline"]["manifest"]["params"]["api_key"] == "[redacted]"
    assert "sk-secret" not in (saved / "metadata.json").read_text()


@pytest.mark.parametrize("forge", [False, True])
def test_tampered_verdict_fails(saved, capsys, forge):
    def flip(v):
        v["verdict"] = "no_regression"
        v["gated_regression"] = False

    _edit(saved / "verdict.json", flip, forge_sums=forge)
    rc, out = _reproduce(saved, capsys)
    assert rc == 1 and "MISMATCH" in out
    assert ("checksum mismatch: verdict.json" in out) != forge
    assert "verdict.verdict: saved 'no_regression' != recomputed 'regression'" in out


def test_bundle_without_a_gated_metric_reproduces_as_incomplete(tmp_path, capsys):
    # OK trials with no pass/fail verdict and no gated score: only cost/latency compare. Such a
    # bundle reproduces as incomplete, and the pre-fix verdict for the same data (a complete
    # no_regression) no longer reproduces — offline replay cannot launder it into a pass.
    cases = [f"q{i}" for i in range(20)]
    unjudged = [{**row(c, 0), "passed": None, "scores": {}} for c in cases]
    store, b, c = store_pair(unjudged, [dict(r, cost_usd=0.5) for r in unjudged], cases=cases,
                             trials=1)
    inputs = asyncio.run(gate.load_inputs(store, b, c))
    policy = gate.DEFAULT_POLICY.with_overrides(n_boot=1000)
    out = bundle.write_bundle(tmp_path / "b", inputs, policy, gate.compare(inputs, policy), {})
    rc, text = _reproduce(out, capsys)
    assert rc == 0, text
    assert "REPRODUCED: verdict incomplete" in text

    def old_verdict(v):
        v["verdict"], v["complete"], v["incomplete_reasons"] = "no_regression", True, []

    _edit(out / "verdict.json", old_verdict, forge_sums=True)
    rc, text = _reproduce(out, capsys)
    assert rc == 1 and "verdict.verdict: saved 'no_regression' != recomputed 'incomplete'" in text


def test_new_bundle_records_the_stats_backend(saved):
    import numpy
    import scipy

    env = json.loads((saved / "metadata.json").read_text())["environment"]
    assert env["numpy"] == numpy.__version__ and env["scipy"] == scipy.__version__
    assert env["t_quantile"] == "scipy.stats.t.ppf"


def test_bundle_from_before_the_scipy_metadata_still_reproduces(saved, capsys):
    # Bundles exported before SciPy was recorded carry only python/numpy. Their bytes are not
    # rewritten and they verify unchanged: metadata is provenance, not part of the verdict.
    def old_env(m):
        m["environment"] = {"python": m["environment"]["python"],
                            "numpy": m["environment"]["numpy"]}

    _edit(saved / "metadata.json", old_env, forge_sums=True)
    rc, out = _reproduce(saved, capsys)
    assert rc == 0 and "REPRODUCED: verdict regression" in out


def test_verifying_without_scipy_fails_actionably_not_with_an_approximation(
        saved, capsys, monkeypatch):
    # The method-t bound cannot be recomputed without SciPy. The verifier must neither pass
    # nor report the normal-approximation bounds as data mismatches; it says what is missing.
    before = {p.name: p.read_bytes() for p in saved.iterdir()}
    block_scipy(monkeypatch)
    rc, out = _reproduce(saved, capsys)
    assert rc == 1 and "REPRODUCED" not in out and "CANNOT VERIFY" in out
    assert "cannot recompute the verdict: CI method 't' needs SciPy" in out
    assert "ci_bound" not in out
    assert {p.name: p.read_bytes() for p in saved.iterdir()} == before  # bundle untouched


def test_tampered_metric_number_fails(saved, capsys):
    def nudge(v):
        v["metrics"][1]["point_estimate"] += 1e-6

    _edit(saved / "verdict.json", nudge, forge_sums=True)
    rc, out = _reproduce(saved, capsys)
    assert rc == 1 and "verdict.metrics[1].point_estimate" in out


def test_tampered_data_fails(saved, capsys):
    def improve(d):
        for r in d["candidate_rows"]:
            if "TokenF1" in r["scores"]:
                r["scores"]["TokenF1"] += 0.04  # erase the drop

    _edit(saved / "inputs.json", improve, forge_sums=True)
    rc, out = _reproduce(saved, capsys)
    assert rc == 1 and "verdict.metrics" in out and "verdict.verdict" in out


def test_hiding_a_failed_trial_fails(saved, capsys):
    def hide(d):
        d["candidate_rows"] = [r for r in d["candidate_rows"] if r["status"] == "ok"]

    _edit(saved / "inputs.json", hide, forge_sums=True)
    rc, out = _reproduce(saved, capsys)
    assert rc == 1 and "verdict.coverage.candidate.failed_trials" in out


def test_tampered_policy_fails(saved, capsys):
    def loosen(p):
        p["thresholds"]["TokenF1"] = 0.05

    _edit(saved / "policy.json", loosen, forge_sums=True)
    rc, out = _reproduce(saved, capsys)
    assert rc == 1
    assert "verdict.json policy differs from policy.json" in out
    assert "verdict.policy.thresholds.TokenF1" in out


def test_missing_or_extra_files_fail(saved, capsys):
    (saved / "notes.txt").write_text("x")
    rc, out = _reproduce(saved, capsys)
    assert rc == 1 and "unexpected file" in out
    (saved / "notes.txt").unlink()
    (saved / "policy.json").unlink()
    rc, out = _reproduce(saved, capsys)
    assert rc == 1 and "missing file policy.json" in out


def test_bundle_is_never_overwritten(saved):
    before = {p.name: p.read_bytes() for p in saved.iterdir()}
    inputs = gate.ComparisonInputs.from_dict(json.loads((saved / "inputs.json").read_text()))
    with pytest.raises(FileExistsError):
        bundle.write_bundle(saved, inputs, gate.DEFAULT_POLICY,
                            gate.compare(inputs, gate.DEFAULT_POLICY), {})
    assert {p.name: p.read_bytes() for p in saved.iterdir()} == before


def _bundle_args(saved):
    inputs = gate.ComparisonInputs.from_dict(json.loads((saved / "inputs.json").read_text()))
    return inputs, gate.DEFAULT_POLICY, gate.compare(inputs, gate.DEFAULT_POLICY), {}


def test_interrupted_write_leaves_no_partial_bundle(saved, tmp_path, monkeypatch):
    args = _bundle_args(saved)
    parent = tmp_path / "exports"
    parent.mkdir()
    real_write = Path.write_bytes
    calls = []

    def flaky(self, data):
        calls.append(self.name)
        if len(calls) == 3:
            raise OSError("disk full")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    with pytest.raises(OSError, match="disk full"):
        bundle.write_bundle(parent / "b", *args)
    monkeypatch.undo()
    assert list(parent.iterdir()) == []  # neither the target nor the temp dir remains
    out = bundle.write_bundle(parent / "b", *args)
    assert sorted(p.name for p in out.iterdir()) == sorted((*bundle.FILES, bundle.SUMS))
    assert [p.name for p in parent.iterdir()] == ["b"]
    assert bundle.verify_bundle(out)[0] == []


def test_target_created_during_write_is_left_untouched(saved, tmp_path, monkeypatch):
    args = _bundle_args(saved)
    parent = tmp_path / "exports"
    parent.mkdir()
    target = parent / "b"
    real_mkdtemp = bundle.tempfile.mkdtemp

    def racing_mkdtemp(**kw):
        d = real_mkdtemp(**kw)
        target.mkdir()  # someone else claims the path while we write
        (target / "theirs.txt").write_text("keep")
        return d

    monkeypatch.setattr(bundle.tempfile, "mkdtemp", racing_mkdtemp)
    with pytest.raises(FileExistsError):
        bundle.write_bundle(target, *args)
    assert [p.name for p in parent.iterdir()] == ["b"]  # temp dir cleaned up
    assert [p.name for p in target.iterdir()] == ["theirs.txt"]
    assert (target / "theirs.txt").read_text() == "keep"
