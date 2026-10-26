"""Offline verification of the frozen REAL HotpotQA pilot evidence (no database, no API key).

    python demo/evidence.py verify [EVIDENCE_DIR]      # default demo/evidence/hotpotqa-pilot

``make demo-evidence`` runs this. It reads only the committed files and refuses (exit 1) unless
all of these hold:

1. **Byte-for-byte pins.** Every file of ``bundle/`` matches the sha256 pinned in
   ``provenance.json``, and the bundle holds nothing else. The bundle's own SHA256SUMS is
   unsigned, so rewriting it after an edit passes the bundle's checksum step; the pins catch
   that too (they are committed next to the bundle, so they show a change, not who made it).
2. **Recomputation.** ``engine.bundle.verify_bundle`` recomputes the verdict from the saved raw
   per-(case, trial) rows and policy with the production ``engine.gate`` code and diffs every
   field against the saved verdict.json.
3. **Claims.** The recomputed verdict says what provenance.json and the README claim: the run,
   both versions, 20 paired cases, ``no_regression`` with neither gated metric regressed, 40 OK
   units, and the informational AllGoldRetrieved drop.
4. **Accounting.** span-accounting.json is consistent with the bundle: its canonical cost per
   version equals the bundle's per-unit cost sum, its stored sums are exactly twice that, and
   every native span was paired with its Vigil owner (121 pairs, nothing unresolved).

SciPy is required (the saved bound uses the Student-t quantile); without it the verifier says it
cannot verify rather than recomputing with a different statistic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from engine import bundle

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DIR = ROOT / "demo" / "evidence" / "hotpotqa-pilot"
_COST_TOL = 1e-6  # Postgres numeric costs carry 6 decimals


class EvidenceError(Exception):
    """The evidence does not verify; ``problems`` lists every reason found."""

    def __init__(self, problems: list[str], cannot_verify: bool = False):
        super().__init__("; ".join(problems))
        self.problems = problems
        self.cannot_verify = cannot_verify


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, problems: list[str]) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        problems.append(f"{path.name}: unreadable ({exc})")
        return None


def check_pins(evidence: Path, provenance: dict) -> list[str]:
    """Every bundle file matches its pinned sha256, and nothing else is in the bundle."""
    pins = provenance.get("bundle_files_sha256") or {}
    bdir = evidence / "bundle"
    if not pins:
        return ["provenance.json pins no bundle files"]
    if not bdir.is_dir():
        return [f"{bdir} is not a directory"]
    problems = []
    present = {p.name for p in bdir.iterdir()}
    for name in sorted(set(pins) - present):
        problems.append(f"bundle/{name} is missing")
    for name in sorted(present - set(pins)):
        problems.append(f"bundle/{name} is not part of the frozen evidence")
    for name in sorted(set(pins) & present):
        if _sha(bdir / name) != pins[name]:
            problems.append(f"bundle/{name} differs from the sha256 pinned in provenance.json")
    return problems


def _ok_cost(rows: list[dict]) -> float:
    return math.fsum(float(r["cost_usd"] or 0) for r in rows if r["status"] == "ok")


def check_claims(provenance: dict, inputs: dict, recomputed: dict) -> list[str]:
    """The recomputed verdict and the saved rows say what provenance.json claims."""
    problems: list[str] = []
    exp = provenance["expected"]
    design = provenance["design"]
    for side in ("baseline", "candidate"):
        got = recomputed[side]
        if got["run_id"] != provenance["run_id"]:
            problems.append(f"{side} run is {got['run_id']}, provenance says "
                            f"{provenance['run_id']}")
        if got["agent_version"] != design[side]["agent_version"]:
            problems.append(f"{side} version is {got['agent_version'][:12]}, provenance says "
                            f"{design[side]['agent_version'][:12]}")
        if got["git_sha"] != provenance["source_commit"]:
            problems.append(f"{side} source commit is {got['git_sha']}")
    if recomputed["verdict"] != exp["verdict"]:
        problems.append(f"verdict recomputes to {recomputed['verdict']}, claimed {exp['verdict']}")
    if recomputed["paired_cases"] != exp["paired_cases"]:
        problems.append(f"{recomputed['paired_cases']} paired cases, claimed {exp['paired_cases']}")
    metrics = {m["metric"]: m for m in recomputed["metrics"]}
    for name in exp["gated_metrics_not_regressed"]:
        m = metrics.get(name)
        if m is None or not m["gated"] or m["regressed"]:
            problems.append(f"gated metric {name} is not a compared, non-regressed gated metric")
    info = exp["informational"]
    m = metrics.get(info["metric"])
    if (m is None or m["gated"] != info["gated"] or m["status"] != info["status"]
            or not math.isclose(m["point_estimate"], info["point_estimate"], abs_tol=1e-9)):
        problems.append(f"{info['metric']} does not recompute to {info['point_estimate']:+.2f} "
                        f"({info['status']}, gated={info['gated']})")
    ok_units = sum(1 for side in ("baseline_rows", "candidate_rows")
                   for r in inputs[side] if r["status"] == "ok")
    if ok_units != design["successful_units"]:
        problems.append(f"{ok_units} OK units, claimed {design['successful_units']}")
    costs = {"baseline": _ok_cost(inputs["baseline_rows"]),
             "candidate": _ok_cost(inputs["candidate_rows"])}
    costs["total"] = costs["baseline"] + costs["candidate"]
    for k, claimed in exp["unit_cost_usd"].items():
        if not math.isclose(costs[k], claimed, abs_tol=_COST_TOL):
            problems.append(f"{k} unit cost sums to ${costs[k]:.6f}, claimed ${claimed:.6f}")
    return problems


def check_accounting(accounting: dict, provenance: dict, inputs: dict) -> list[str]:
    """span-accounting.json agrees with the bundle and with itself."""
    problems: list[str] = []
    if accounting.get("run_id") != provenance["run_id"]:
        problems.append("span-accounting.json is for another run")
    versions = accounting.get("versions") or {}
    by_version = {provenance["design"][s]["agent_version"]: inputs[f"{s}_rows"]
                  for s in ("baseline", "candidate")}
    if set(versions) != set(by_version):
        return [*problems, "span-accounting.json does not cover exactly the two bundle versions"]
    pairs = 0
    canonical_total = stored_total = 0.0
    for ver, v in versions.items():
        short = ver[:12]
        unit_cost = _ok_cost(by_version[ver])
        canonical, stored, pg = v["canonical"], v["as_stored"], v["postgres_ok_units"]
        if not math.isclose(canonical["cost_usd"], unit_cost, abs_tol=_COST_TOL):
            problems.append(f"{short}: canonical span cost ${canonical['cost_usd']:.6f} != bundle "
                            f"unit cost ${unit_cost:.6f}")
        for f in ("input_tokens", "output_tokens", "cost_usd"):
            if not math.isclose(canonical[f], pg[f], abs_tol=_COST_TOL):
                problems.append(f"{short}: canonical {f} {canonical[f]} != Postgres {pg[f]}")
            if not math.isclose(stored[f], 2 * canonical[f], abs_tol=2 * _COST_TOL):
                problems.append(f"{short}: stored {f} {stored[f]} is not twice canonical "
                                f"{canonical[f]} (the recorded double accounting)")
        if not (v["owner_spans"] == v["shadow_spans"] and v["unresolved_spans"] == 0
                and v["unowned_spans"] == 0 and v["single_owner"] is False):
            problems.append(f"{short}: not one proven native copy per owner span")
        if v["llm_span_rows"] != v["owner_spans"] + v["shadow_spans"]:
            problems.append(f"{short}: span rows != owners + shadows")
        pairs += v["shadow_spans"]
        canonical_total += canonical["cost_usd"]
        stored_total += stored["cost_usd"]
    if pairs != accounting["native_llm_spans"] or pairs != accounting[
            "native_spans_with_matching_usage_on_vigil_parent"]:
        problems.append(f"{pairs} reconciled pairs, record says {accounting['native_llm_spans']}")
    totals = accounting["totals"]
    if not math.isclose(totals["canonical_cost_usd"], canonical_total, abs_tol=_COST_TOL):
        problems.append("canonical total does not add up")
    if not math.isclose(totals["as_stored_cost_usd"], stored_total, abs_tol=_COST_TOL):
        problems.append("stored total does not add up")
    claimed = provenance["expected"]["unit_cost_usd"]["total"]
    if not math.isclose(totals["canonical_cost_usd"], claimed, abs_tol=_COST_TOL):
        problems.append(f"canonical total ${totals['canonical_cost_usd']:.6f} != claimed "
                        f"${claimed:.6f}")
    return problems


def verify(evidence_dir: str | Path = DEFAULT_DIR) -> dict:
    """Verify the evidence directory; returns the recomputed verdict or raises EvidenceError."""
    evidence = Path(evidence_dir)
    problems: list[str] = []
    provenance = _load(evidence / "provenance.json", problems)
    accounting = _load(evidence / "span-accounting.json", problems)
    if provenance is None or accounting is None:
        raise EvidenceError(problems)
    problems += check_pins(evidence, provenance)
    bundle_problems, recomputed = bundle.verify_bundle(evidence / "bundle")
    if recomputed is None and any(p.startswith("cannot recompute") for p in bundle_problems):
        raise EvidenceError([*problems, *bundle_problems], cannot_verify=True)
    problems += [f"bundle: {p}" for p in bundle_problems]
    if recomputed is not None:
        inputs = json.loads((evidence / "bundle" / "inputs.json").read_text())
        try:
            problems += check_claims(provenance, inputs, recomputed)
            problems += check_accounting(accounting, provenance, inputs)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"evidence record is malformed: {exc!r}")
    if problems:
        raise EvidenceError(problems)
    return recomputed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("demo/evidence.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    vp = sub.add_parser("verify", help="verify the frozen pilot evidence offline")
    vp.add_argument("evidence_dir", nargs="?", default=str(DEFAULT_DIR))
    args = ap.parse_args(argv)
    try:
        rec = verify(args.evidence_dir)
    except EvidenceError as exc:
        head = "CANNOT VERIFY (environment)" if exc.cannot_verify else "REFUSED"
        print(f"{head}: {args.evidence_dir} ({len(exc.problems)} problem(s)):")
        for p in exc.problems[:50]:
            print(f"  - {p}")
        return 1
    here = bundle.environment()
    metrics = {m["metric"]: m for m in rec["metrics"]}
    cost = json.loads((Path(args.evidence_dir) / "provenance.json").read_text())[
        "expected"]["unit_cost_usd"]
    print(
        f"VERIFIED: REAL exploratory HotpotQA pilot, run {rec['baseline']['run_id']}\n"
        f"  bundle files match their pins; verdict recomputed with engine.gate: {rec['verdict']} "
        f"({rec['paired_cases']} paired cases, {len(rec['metrics'])} metrics)\n"
        f"  gated: passed {metrics['passed']['point_estimate']:+.4f}, "
        f"TokenF1 {metrics['TokenF1']['point_estimate']:+.4f} (neither regressed); "
        f"informational AllGoldRetrieved {metrics['AllGoldRetrieved']['point_estimate']:+.2f}\n"
        f"  unit cost ${cost['baseline']:.6f} + ${cost['candidate']:.6f} = ${cost['total']:.6f} "
        "(cross-checked against span-accounting.json: stored spans doubled, every native copy "
        "paired with its Vigil span)\n"
        f"  environment: {here}\n"
        "  Exploratory and low-power: no_regression here is not equivalence, and nothing in it "
        "is a quality or performance claim."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
