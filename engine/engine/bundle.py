"""Regression evidence bundle: what ``engine regress --save DIR`` exports and
``bench/regression_verdict.py reproduce DIR`` re-verifies offline.

A bundle is a directory (never overwritten) holding:

  inputs.json    the resolved run/version identities (both sides), the suite case list, and
                 every raw scored (case, trial) row of the two selected versions — including
                 failed/timeout trials with their status and error
  policy.json    the effective ``engine.gate.Policy`` (thresholds, gated metrics, CI method,
                 alpha, n_boot, seed, failed-trial rule)
  verdict.json   ``engine.gate.Comparison.to_dict()`` as computed at export time
  metadata.json  provenance: format version, export time, engine source revision, python/numpy
                 versions, suite adapter + config, both version manifests (redacted), argv
  SHA256SUMS     sha256 of each file above

Verification fails on: a missing/extra file, any checksum mismatch, an unsupported format,
verdict context (policy, sides) disagreeing with policy.json/inputs.json, or a recomputed
verdict that differs from verdict.json beyond float round-off.

SHA256SUMS is stored in the bundle, unsigned: a mismatch shows a file changed relative to the
bundle's own manifest, not who produced the bundle or that its rows match the database. Anyone
who edits files and rewrites SHA256SUMS consistently passes the checksum step.

The bundle is written into a hidden temporary sibling directory and renamed into place only
once complete, so an interrupted export never leaves a partial bundle at ``out_dir``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import platform
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from engine import gate

FORMAT = "vigil-regress-bundle"
# 2: per-side metric availability (partial/one-sided/all-failed rules, per-metric
# ok_trials_missing + failure_zeros_only). v1 verdicts do not recompute under these rules.
FORMAT_VERSION = 2
FILES = ("inputs.json", "policy.json", "verdict.json", "metadata.json")
SUMS = "SHA256SUMS"
_REL_TOL = 1e-9
_ABS_TOL = 1e-12


def _dump(obj) -> bytes:
    return (json.dumps(gate.json_safe(obj), indent=2, sort_keys=True, allow_nan=False) + "\n"
            ).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def environment() -> dict:
    import numpy

    return {"python": platform.python_version(), "numpy": numpy.__version__}


def write_bundle(
    out_dir: str | Path,
    inputs: gate.ComparisonInputs,
    policy: gate.Policy,
    comparison: gate.Comparison,
    metadata: dict,
) -> Path:
    """Write a new bundle directory. Refuses (FileExistsError) if ``out_dir`` exists at all —
    evidence is never overwritten or merged."""
    out = Path(out_dir)
    if out.exists() or out.is_symlink():
        raise FileExistsError(f"refusing to overwrite existing path {out}")
    meta = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "environment": environment(),
        **metadata,
    }
    blobs = {
        "inputs.json": _dump(inputs.to_dict()),
        "policy.json": _dump(policy.to_dict()),
        "verdict.json": _dump(comparison.to_dict()),
        "metadata.json": _dump(meta),
    }
    blobs[SUMS] = "".join(f"{_sha(blobs[n])}  {n}\n" for n in FILES).encode()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{out.name}.partial-", dir=out.parent))
    try:
        for name, data in blobs.items():
            (tmp / name).write_bytes(data)
        out.mkdir()  # exclusive claim: a path created meanwhile fails here, never merges
        try:
            os.rename(tmp, out)  # replaces only the empty directory claimed just above
        except OSError:
            with contextlib.suppress(OSError):
                out.rmdir()  # ours and still empty; a non-empty directory is left alone
            raise
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)  # only the private temp dir made above
        raise
    return out


def _diff(a, b, path: str, out: list[str]) -> None:
    """Deep compare saved vs recomputed; floats within round-off, everything else exact."""
    if isinstance(a, bool) or isinstance(b, bool):
        if a is not b:
            out.append(f"{path}: saved {a!r} != recomputed {b!r}")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if not math.isclose(a, b, rel_tol=_REL_TOL, abs_tol=_ABS_TOL):
            out.append(f"{path}: saved {a!r} != recomputed {b!r}")
    elif isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: present only in {'saved' if k in a else 'recomputed'}")
            else:
                _diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length saved {len(a)} != recomputed {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{i}]", out)
    elif a != b:
        out.append(f"{path}: saved {a!r} != recomputed {b!r}")


def verify_bundle(bundle_dir: str | Path) -> tuple[list[str], dict | None]:
    """Re-verify a bundle offline. Returns (problems, recomputed verdict dict); an empty
    problem list means the saved verdict reproduces exactly from the saved data + policy."""
    d = Path(bundle_dir)
    problems: list[str] = []
    if not d.is_dir():
        return [f"{d} is not a directory"], None
    present = {p.name for p in d.iterdir()}
    for name in (*FILES, SUMS):
        if name not in present:
            problems.append(f"missing file {name}")
    extra = present - {*FILES, SUMS}
    if extra:
        problems.append(f"unexpected file(s): {sorted(extra)}")
    if problems:
        return problems, None

    listed: dict[str, str] = {}
    for line in (d / SUMS).read_text().splitlines():
        if line.strip():
            digest, _, name = line.partition("  ")
            listed[name] = digest
    if set(listed) != set(FILES):
        problems.append(f"{SUMS} lists {sorted(listed)}, expected {list(FILES)}")
    raw = {n: (d / n).read_bytes() for n in FILES}
    for n in FILES:
        if n in listed and listed[n] != _sha(raw[n]):
            problems.append(f"checksum mismatch: {n} differs from its {SUMS} entry")

    try:
        docs = {n: json.loads(raw[n]) for n in FILES}
    except ValueError as exc:
        return [*problems, f"invalid JSON: {exc}"], None
    meta = docs["metadata.json"]
    if meta.get("format") != FORMAT or meta.get("format_version") != FORMAT_VERSION:
        return [*problems, f"unsupported bundle format {meta.get('format')!r} "
                           f"v{meta.get('format_version')!r}"], None
    try:
        policy = gate.Policy.from_dict(docs["policy.json"])
        inputs = gate.ComparisonInputs.from_dict(docs["inputs.json"])
    except (KeyError, TypeError, ValueError) as exc:
        return [*problems, f"unreadable policy/inputs: {exc!r}"], None

    saved = docs["verdict.json"]
    if saved.get("policy") != docs["policy.json"]:
        problems.append("verdict.json policy differs from policy.json")
    for side in ("baseline", "candidate"):
        if saved.get(side) != docs["inputs.json"][side]:
            problems.append(f"verdict.json {side} identity differs from inputs.json")

    recomputed = json.loads(_dump(gate.compare(inputs, policy).to_dict()))
    mismatches: list[str] = []
    _diff(saved, recomputed, "verdict", mismatches)
    problems.extend(mismatches)
    return problems, recomputed
