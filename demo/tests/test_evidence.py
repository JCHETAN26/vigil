"""The committed REAL pilot evidence verifies offline, and every kind of tampering is refused:
a byte change, a consistently re-checksummed edit, an edit that also rewrites the pins (caught by
recomputing the verdict with engine.gate), an extra file, and an altered accounting record."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import evidence

from engine import bundle


@pytest.fixture
def copy(tmp_path) -> Path:
    dst = tmp_path / "hotpotqa-pilot"
    shutil.copytree(evidence.DEFAULT_DIR, dst)
    return dst


def _problems(path: Path) -> list[str]:
    with pytest.raises(evidence.EvidenceError) as exc:
        evidence.verify(path)
    return exc.value.problems


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _rewrite_sums(bdir: Path) -> None:
    (bdir / "SHA256SUMS").write_text(
        "".join(f"{_sha(bdir / n)}  {n}\n" for n in bundle.FILES))


def _repin(evid: Path) -> None:
    prov = json.loads((evid / "provenance.json").read_text())
    prov["bundle_files_sha256"] = {p.name: _sha(p) for p in (evid / "bundle").iterdir()}
    (evid / "provenance.json").write_text(json.dumps(prov))


def _edit_inputs(evid: Path, edit) -> None:
    path = evid / "bundle" / "inputs.json"
    doc = json.loads(path.read_text())
    edit(doc)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def test_committed_evidence_verifies_with_the_production_gate():
    rec = evidence.verify()
    assert rec["verdict"] == "no_regression" and rec["paired_cases"] == 20
    assert rec["baseline"]["run_id"] == "cafae810-f2e9-4c50-b34d-cfac8b18c509"
    assert evidence.main(["verify"]) == 0


def test_committed_bundle_is_byte_identical_to_its_pins():
    prov = json.loads((evidence.DEFAULT_DIR / "provenance.json").read_text())
    files = {p.name: _sha(p) for p in (evidence.DEFAULT_DIR / "bundle").iterdir()}
    assert files == prov["bundle_files_sha256"] and len(files) == 5


def test_a_changed_byte_is_refused(copy):
    p = copy / "bundle" / "verdict.json"
    p.write_bytes(p.read_bytes().replace(b'"no_regression"', b'"regression"', 1))
    problems = _problems(copy)
    assert any("verdict.json differs from the sha256 pinned" in x for x in problems)
    assert any("checksum mismatch: verdict.json" in x for x in problems)


def test_an_edit_with_rewritten_sha256sums_is_still_refused_by_the_pins(copy):
    def flip(doc):  # one candidate case fails instead of passing
        row = next(r for r in doc["candidate_rows"] if r["passed"])
        row["passed"] = False
    _edit_inputs(copy, flip)
    _rewrite_sums(copy / "bundle")
    problems = _problems(copy)
    assert any("inputs.json differs from the sha256 pinned" in x for x in problems)
    assert not any("checksum mismatch" in x for x in problems)  # the bundle's own check passes


def test_an_edit_with_rewritten_sums_and_pins_is_refused_by_recomputation(copy):
    # A forger who also rewrites the pins still cannot make the saved verdict match the data:
    # the gate recomputes it from the rows and every field is compared.
    def sink(doc):
        for r in doc["candidate_rows"]:
            r["passed"] = False
            r["scores"]["TokenF1"] = 0.0
    _edit_inputs(copy, sink)
    _rewrite_sums(copy / "bundle")
    _repin(copy)
    problems = _problems(copy)
    assert not any("pinned" in x for x in problems)
    assert any(x.startswith("bundle: verdict.verdict: saved 'no_regression'") for x in problems)
    assert any("verdict recomputes to regression" in x for x in problems)


def test_an_extra_file_is_refused(copy):
    (copy / "bundle" / "notes.txt").write_text("not part of the evidence")
    problems = _problems(copy)
    assert any("notes.txt is not part of the frozen evidence" in x for x in problems)


@pytest.mark.parametrize("edit, expect", [
    (lambda a: a["versions"]["c2eda30a02146432a38387ed183420243d6752683d87856ddd8cb38e447d5b4c"][
        "canonical"].update(cost_usd=0.2), "canonical span cost"),
    (lambda a: a["totals"].update(canonical_cost_usd=0.505902), "canonical total"),
    (lambda a: a["versions"]["4b6ff74526af186f0df0017aa96d9d34e3c6b86a476c8aa39f85d2b95d2d3949"]
        .update(unresolved_spans=3), "not one proven native copy"),
    (lambda a: a.update(run_id="45e1b86b-a1b1-472c-8774-3820d1b73ef7"), "another run"),
])
def test_an_inconsistent_accounting_record_is_refused(copy, edit, expect):
    path = copy / "span-accounting.json"
    doc = json.loads(path.read_text())
    edit(doc)
    path.write_text(json.dumps(doc))
    assert any(expect in x for x in _problems(copy))


def test_claims_must_match_the_recomputed_verdict(copy):
    path = copy / "provenance.json"
    doc = json.loads(path.read_text())
    doc["expected"]["verdict"] = "regression"
    doc["expected"]["unit_cost_usd"]["total"] = 0.505902  # the doubled span sum
    path.write_text(json.dumps(doc))
    problems = _problems(copy)
    assert any("verdict recomputes to no_regression, claimed regression" in x for x in problems)
    assert any("total unit cost sums to $0.252951" in x for x in problems)


def test_missing_scipy_cannot_verify_rather_than_approximating(copy, monkeypatch):
    import engine.regression as reg

    def no_scipy(q, df):
        raise reg.StatsDependencyError("CI method 't' needs SciPy")
    monkeypatch.setattr(reg, "_t_ppf", no_scipy)
    with pytest.raises(evidence.EvidenceError) as exc:
        evidence.verify(copy)
    assert exc.value.cannot_verify
