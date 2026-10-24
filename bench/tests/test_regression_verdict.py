"""Offline reproduction of `engine regress --save` bundles (bench/regression_verdict.py).

Bundles are produced by the real CLI command against an in-memory fake store, then verified;
tampering with the verdict, the raw rows, or the policy must fail reproduction — even when the
tamperer also rewrites SHA256SUMS. The synthetic rows are correctness fixtures, not quality or
performance evidence."""

from __future__ import annotations

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

import regression_verdict as rv
from triage_fakes import V1, f1_rows, f1_shift_pair, row, store_pair

from engine import bundle, cli, gate


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
