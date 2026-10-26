"""Offline reproduction of a saved regression verdict.

    python -m engine regress --baseline <A> --candidate <B> --save DIR    # export (needs DB)
    python bench/regression_verdict.py reproduce DIR                      # verify (no DB)

`reproduce` checks the bundle's checksums and format, then recomputes the verdict from the
saved raw per-(case, trial) rows and the saved policy with the same engine.gate code the CLI
and API use, and fails (exit 1) on any mismatch: a file that differs from the bundle's own
(unsigned) SHA256SUMS — a consistency check, not proof of origin — a policy or run/version
identity that disagrees between files, or any verdict field (including every metric's point
estimate, bound, flags, and the coverage audit) that does not recompute. The bundle format is
documented in engine/engine/bundle.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from engine import bundle


def reproduce(bundle_dir: str) -> int:
    problems, recomputed = bundle.verify_bundle(bundle_dir)
    meta_path = Path(bundle_dir) / "metadata.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text())
            saved_env = meta.get("environment", {})
            here = bundle.environment()
            if saved_env and saved_env != here:
                print(f"note: exported under {saved_env}, verifying under {here}")
        except ValueError:
            pass
    if problems:
        if recomputed is None and any(p.startswith("cannot recompute") for p in problems):
            # An environment problem (e.g. SciPy missing), not evidence the saved data is wrong.
            print(f"CANNOT VERIFY: {bundle_dir} could not be recomputed here "
                  f"({len(problems)} problem(s)); fix the environment and rerun:")
        else:
            print(f"MISMATCH: {bundle_dir} does not reproduce ({len(problems)} problem(s)):")
        for p in problems[:50]:
            print(f"  - {p}")
        if len(problems) > 50:
            print(f"  ... {len(problems) - 50} more")
        return 1
    b, c = recomputed["baseline"], recomputed["candidate"]
    print(
        f"REPRODUCED: verdict {recomputed['verdict']} "
        f"(candidate {c['run_id'][:12]}@{c['agent_version'][:12]} vs baseline "
        f"{b['run_id'][:12]}@{b['agent_version'][:12]}, {recomputed['paired_cases']} paired "
        f"cases, {len(recomputed['metrics'])} metric(s))"
    )
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("regression_verdict")
    sub = ap.add_subparsers(dest="command", required=True)
    rp = sub.add_parser("reproduce", help="recompute a saved verdict offline and compare")
    rp.add_argument("bundle_dir")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    return reproduce(args.bundle_dir)


if __name__ == "__main__":
    raise SystemExit(main())
