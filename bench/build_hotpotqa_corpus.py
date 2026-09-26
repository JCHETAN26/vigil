"""Build the reproducible HotpotQA (distractor) artifacts for the eval suite.

Downloads the HotpotQA distractor dev set (cached locally), pins a subset of questions, and
writes three artifacts under the output dir (default: data/hotpotqa/).

The default source is the Hugging Face parquet mirror of the distractor validation split
(reliable; the original ``curtis.ml.cmu.edu`` JSON host is frequently down). Point ``--url`` at
the original ``hotpot_dev_distractor_v1.json`` to use it instead — both parse to the same
normalized records. Parquet needs ``pyarrow`` (a build-time-only tool dep, not needed to run
evals).

  corpus.v1.jsonl     one {"doc_id": title, "text": paragraph} per line — the POOLED corpus
                      (every paragraph of the pinned questions, deduped by title): thousands of
                      documents, so retrieval is realistic, not over a question's own 10.
  cases.v1.json       {version, source:{name,url,revision,sha256}, verification, count,
                      cases:[{case_id, question, answer, supporting_titles, type, level}]}
                      — read by the HotpotQAAdapter. `source` pins the HF dataset commit and a
                      sha256 of the exact bytes; `verification` records the cross-check below.
  subsets/dev20.txt   20 case ids for the development run (first 20 by id).
  subsets/base100.txt 100 case ids for the baseline, STRATIFIED by type (bridge/comparison)
                      to match the pinned set's distribution.

Deterministic: selection is by sorted _id, so re-running yields the same artifacts. The heavy
data-shaping lives in engine.datasets.hotpotqa and is unit-tested there.

    python bench/build_hotpotqa_corpus.py --n 500 --out data/hotpotqa
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# bench is tooling and may import the engine (only *agents* may not).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from engine.datasets.hotpotqa import (
    build_corpus,
    record_mismatches,
    select_subset,
    stratified_subset,
    to_case_spec,
)

# Hugging Face auto-converted parquet for hotpotqa/hotpot_qa, config "distractor", split
# "validation" (7,405 rows) — the same data as hotpot_dev_distractor_v1.json.
SOURCE_URL = (
    "https://huggingface.co/datasets/hotpotqa/hotpot_qa/resolve/"
    "refs%2Fconvert%2Fparquet/distractor/validation/0000.parquet"
)
# The original JSON, used only to verify the mirror when it is reachable (host is often down).
ORIGINAL_URL = "http://curtis.ml.cmu.edu/datasets/hotpot/hotpot_dev_distractor_v1.json"
VERSION = "v1"


def _meta_path(dest: Path) -> Path:
    return dest.with_suffix(dest.suffix + ".meta.json")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # do not follow
        return None


def _fetch_revision(url: str) -> str | None:
    """The Hugging Face dataset commit behind a resolve URL, from the ``X-Repo-Commit`` header.

    HF puts that header on the huggingface.co response, which then 302-redirects to a CDN that
    does not carry it — so we must read it from the pre-redirect response (redirects disabled)."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        req = urllib.request.Request(url, method="HEAD")
        try:
            with opener.open(req, timeout=15) as resp:
                return resp.headers.get("X-Repo-Commit")
        except urllib.error.HTTPError as redirect:  # blocked 3xx still carries the header
            return redirect.headers.get("X-Repo-Commit")
    except (urllib.error.URLError, OSError):
        return None


def _ensure_source(url: str, dest: Path) -> None:
    """Download the source (if not cached) and record its HF revision in a sidecar meta file."""
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {url} …")
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read()
        dest.write_bytes(data)
    else:
        print(f"using cached source {dest} ({dest.stat().st_size // (1024 * 1024)} MB)")
    # The commit isn't on the followed (CDN) download response, so resolve it separately.
    _meta_path(dest).write_text(json.dumps({"url": url, "revision": _fetch_revision(url)}))


def _source_meta(dest: Path, url: str) -> dict:
    """Provenance for the manifest: the mirror URL, its pinned revision (HF commit), and a
    sha256 of the exact bytes used, so a re-build can be checked for source drift."""
    revision = None
    if _meta_path(dest).exists():
        try:
            revision = json.loads(_meta_path(dest).read_text()).get("revision")
        except json.JSONDecodeError:
            revision = None
    kind = "HF parquet mirror (hotpotqa/hotpot_qa distractor/validation)" if dest.suffix == ".parquet" else "hotpot_dev_distractor_v1.json"
    return {
        "name": kind,
        "url": url,
        "revision": revision,
        "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
    }


def _verify_against_original(subset: list[dict], original_url: str, timeout: float) -> str:
    """Best-effort: when the original JSON host is reachable, assert the mirror's id/question/
    answer/supporting_facts match it for the pinned subset. Fails loudly on any mismatch;
    silently skipped (with a note) when the host is down."""
    print(f"verifying mirror against original ({original_url}) …")
    try:
        req = urllib.request.Request(original_url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            original = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 - unreachable host is expected, not fatal
        note = f"skipped: original unreachable ({type(exc).__name__})"
        print(f"  {note}")
        return note
    mismatches = record_mismatches(subset, original)
    if mismatches:
        raise SystemExit(
            "mirror verification FAILED against the original for the pinned subset:\n  "
            + "\n  ".join(mismatches[:20])
            + (f"\n  … and {len(mismatches) - 20} more" if len(mismatches) > 20 else "")
        )
    print(f"  passed: {len(subset)} questions match the original")
    return "passed"


def _load_records(path: Path) -> list[dict]:
    """Load normalized HotpotQA records from a cached parquet (HF mirror) or the original JSON.
    Both yield records with keys: _id, question, answer, type, level, supporting_facts
    (as [[title, sent_id], …]), context (as [[title, [sentences]], …])."""
    if path.suffix == ".parquet":
        return _read_parquet(path)
    return json.loads(path.read_text())


def _read_parquet(path: Path) -> list[dict]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "reading the parquet source needs pyarrow: `pip install pyarrow`, or pass "
            "--url pointing at hotpot_dev_distractor_v1.json to use the original JSON."
        ) from exc

    rows = pq.read_table(path).to_pylist()
    records = []
    for r in rows:
        sf = r["supporting_facts"]
        ctx = r["context"]
        records.append(
            {
                "_id": r["id"],
                "question": r["question"],
                "answer": r["answer"],
                "type": r.get("type") or "bridge",
                "level": r.get("level") or "",
                "supporting_facts": list(zip(sf["title"], sf["sent_id"])),
                "context": [[t, list(s)] for t, s in zip(ctx["title"], ctx["sentences"])],
            }
        )
    return records


def build(records: list[dict], n: int, out: Path, source: dict, verification: str) -> None:
    subset = select_subset(records, n)
    corpus = build_corpus(subset)
    cases = [to_case_spec(r) for r in subset]

    out.mkdir(parents=True, exist_ok=True)
    corpus_path = out / f"corpus.{VERSION}.jsonl"
    with corpus_path.open("w") as f:
        for doc in corpus:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    subsets_dir = out / "subsets"
    subsets_dir.mkdir(exist_ok=True)
    dev20 = [c["case_id"] for c in cases[:20]]
    # base100 is stratified by type AND disjoint from dev20, so the measurement baseline never
    # reuses a question we sanity-checked on during development.
    base100 = stratified_subset(subset, 100, exclude=set(dev20))
    (subsets_dir / "dev20.txt").write_text("\n".join(dev20) + "\n")
    (subsets_dir / "base100.txt").write_text("\n".join(base100) + "\n")

    # Report the type mix so the stratification is auditable.
    def mix(ids: list[str]) -> dict[str, int]:
        by_id = {c["case_id"]: c["type"] for c in cases}
        counts: dict[str, int] = {}
        for i in ids:
            counts[by_id[i]] = counts.get(by_id[i], 0) + 1
        return counts

    # Record the subsets in the manifest so the dev/baseline split is documented and auditable.
    overlap = sorted(set(dev20) & set(base100))
    subsets_manifest = {
        "dev20": {"n": len(dev20), "mix": mix(dev20)},
        "base100": {
            "n": len(base100),
            "mix": mix(base100),
            "stratified_by": "type",
            "disjoint_from": "dev20",
        },
        "dev20_base100_overlap": len(overlap),
    }

    cases_path = out / f"cases.{VERSION}.json"
    cases_path.write_text(
        json.dumps(
            {
                "version": VERSION,
                "source": source,  # {name, url, revision (HF commit), sha256}
                "verification": verification,  # 'passed' | 'disabled' | 'skipped: …'
                "count": len(cases),
                "subsets": subsets_manifest,  # dev/baseline split: sizes, type mix, disjointness
                "cases": cases,
            },
            ensure_ascii=False,
        )
    )

    print(f"corpus:  {len(corpus)} unique paragraphs -> {corpus_path}")
    print(f"cases:   {len(cases)} questions -> {cases_path}")
    print(f"subsets: dev20 (n=20) mix={mix(dev20)}, base100 (n={len(base100)}) mix={mix(base100)}")
    print(f"full set mix: {mix([c['case_id'] for c in cases])}")
    print(f"source:  revision={source['revision']} sha256={source['sha256'][:12]}… verify={verification}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser("build_hotpotqa_corpus")
    p.add_argument("--n", type=int, default=500, help="number of questions to pin (corpus pool)")
    p.add_argument("--out", default="data/hotpotqa", help="output directory for artifacts")
    p.add_argument("--url", default=SOURCE_URL)
    p.add_argument("--cache", default="data/hotpotqa/hotpot_dev_distractor.parquet")
    p.add_argument("--original-url", default=ORIGINAL_URL, help="original JSON, for verification")
    p.add_argument("--verify-timeout", type=float, default=15.0)
    p.add_argument("--no-verify", action="store_true", help="skip the original cross-check")
    args = p.parse_args(argv)

    cache = Path(args.cache)
    _ensure_source(args.url, cache)
    records = _load_records(cache)
    print(f"loaded {len(records)} dev records")

    subset = select_subset(records, args.n)
    verification = (
        "disabled"
        if args.no_verify
        else _verify_against_original(subset, args.original_url, args.verify_timeout)
    )
    source = _source_meta(cache, args.url)
    build(records, args.n, Path(args.out), source, verification)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
