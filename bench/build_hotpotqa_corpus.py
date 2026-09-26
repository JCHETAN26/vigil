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
  cases.v1.json       {version, source, count, cases:[{case_id, question, answer,
                      supporting_titles, type, level}]} — read by the HotpotQAAdapter.
  subsets/dev20.txt   20 case ids for the development run (first 20 by id).
  subsets/base100.txt 100 case ids for the baseline, STRATIFIED by type (bridge/comparison)
                      to match the pinned set's distribution.

Deterministic: selection is by sorted _id, so re-running yields the same artifacts. The heavy
data-shaping lives in engine.datasets.hotpotqa and is unit-tested there.

    python bench/build_hotpotqa_corpus.py --n 500 --out data/hotpotqa
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

# bench is tooling and may import the engine (only *agents* may not).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from engine.datasets.hotpotqa import (
    build_corpus,
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
VERSION = "v1"


def _download(url: str, dest: Path) -> Path:
    if dest.exists():
        print(f"using cached source {dest} ({dest.stat().st_size // (1024 * 1024)} MB)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url} …")
    urllib.request.urlretrieve(url, dest)
    return dest


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


def build(records: list[dict], n: int, out: Path) -> None:
    subset = select_subset(records, n)
    corpus = build_corpus(subset)
    cases = [to_case_spec(r) for r in subset]

    out.mkdir(parents=True, exist_ok=True)
    corpus_path = out / f"corpus.{VERSION}.jsonl"
    with corpus_path.open("w") as f:
        for doc in corpus:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    cases_path = out / f"cases.{VERSION}.json"
    cases_path.write_text(
        json.dumps(
            {"version": VERSION, "source": "hotpot_dev_distractor_v1", "count": len(cases), "cases": cases},
            ensure_ascii=False,
        )
    )

    subsets_dir = out / "subsets"
    subsets_dir.mkdir(exist_ok=True)
    dev20 = [c["case_id"] for c in cases[:20]]
    base100 = stratified_subset(subset, 100)
    (subsets_dir / "dev20.txt").write_text("\n".join(dev20) + "\n")
    (subsets_dir / "base100.txt").write_text("\n".join(base100) + "\n")

    # Report the type mix so the stratification is auditable.
    def mix(ids: list[str]) -> dict[str, int]:
        by_id = {c["case_id"]: c["type"] for c in cases}
        counts: dict[str, int] = {}
        for i in ids:
            counts[by_id[i]] = counts.get(by_id[i], 0) + 1
        return counts

    print(f"corpus:  {len(corpus)} unique paragraphs -> {corpus_path}")
    print(f"cases:   {len(cases)} questions -> {cases_path}")
    print(f"subsets: dev20 (n=20) mix={mix(dev20)}, base100 (n={len(base100)}) mix={mix(base100)}")
    print(f"full set mix: {mix([c['case_id'] for c in cases])}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser("build_hotpotqa_corpus")
    p.add_argument("--n", type=int, default=500, help="number of questions to pin (corpus pool)")
    p.add_argument("--out", default="data/hotpotqa", help="output directory for artifacts")
    p.add_argument("--url", default=SOURCE_URL)
    p.add_argument("--cache", default="data/hotpotqa/hotpot_dev_distractor.parquet")
    args = p.parse_args(argv)

    src = _download(args.url, Path(args.cache))
    records = _load_records(src)
    print(f"loaded {len(records)} dev records")
    build(records, args.n, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
