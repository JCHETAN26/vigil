"""HotpotQA (distractor setting) dataset support (design §5, Session 3).

Two concerns live here:

- **Pure data-shaping helpers** (``select_subset``, ``build_corpus``, ``to_case_spec``,
  ``stratified_subset``) that turn raw ``hotpot_dev_distractor_v1`` records into (a) a pooled
  retrieval corpus — every paragraph of the chosen questions, deduped by Wikipedia title, so
  retrieval is over thousands of documents rather than a question's own 10 — and (b) the case
  specs (question, gold answer, gold supporting titles, type). The ``bench`` build script uses
  these to write the reproducible artifacts; keeping them here makes them unit-testable without
  downloading the 45 MB source.
- **``HotpotQAAdapter``**, which reads a built cases artifact and materializes ``eval_cases``.

The retriever itself lives in the agent package (agents must not import the engine); the engine
only needs the cases + the scorers, which read the agent's ``retrievals``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from .base import Case, register_adapter

# Scorers every HotpotQA case is measured on: answer quality (EM + token F1 on final_answer)
# and retrieval quality (recall@5 for the first hop, recall@10 for two hops, nDCG@10, and the
# per-case "both gold paragraphs retrieved" boolean).
HOTPOTQA_SCORERS = [
    "ExactMatch",
    "TokenF1",
    "RetrievalRecall@5",
    "RetrievalRecall@10",
    "NDCG",
    "AllGoldRetrieved",
]


def supporting_titles(record: dict) -> list[str]:
    """The distinct gold paragraph titles from a record's supporting_facts, sorted."""
    return sorted({title for title, _sent in record.get("supporting_facts", [])})


def to_case_spec(record: dict) -> dict:
    """One case spec (question, gold answer, gold titles, type) from a raw HotpotQA record."""
    return {
        "case_id": record["_id"],
        "question": record["question"],
        "answer": record["answer"],
        "supporting_titles": supporting_titles(record),
        "type": record.get("type", "bridge"),
        "level": record.get("level", ""),
    }


def select_subset(records: list[dict], n: int) -> list[dict]:
    """A deterministic subset: the first ``n`` records by ``_id`` (stable across runs)."""
    return sorted(records, key=lambda r: r["_id"])[:n]


def _supporting_set(record: dict) -> set[tuple]:
    return {(title, sent) for title, sent in record.get("supporting_facts", [])}


def record_mismatches(mirror: list[dict], original: list[dict]) -> list[str]:
    """Compare mirror records against the original by ``_id`` and return human-readable
    mismatches for id/question/answer/supporting_facts (empty list = the mirror matches the
    original for every mirror record). Used to verify the HF parquet mirror against the
    official ``hotpot_dev_distractor_v1.json`` for the pinned subset."""
    by_id = {r["_id"]: r for r in original}
    out: list[str] = []
    for m in mirror:
        o = by_id.get(m["_id"])
        if o is None:
            out.append(f"{m['_id']}: id not present in original")
            continue
        if m["question"] != o["question"]:
            out.append(f"{m['_id']}: question differs")
        if m["answer"] != o["answer"]:
            out.append(f"{m['_id']}: answer differs")
        if _supporting_set(m) != _supporting_set(o):
            out.append(f"{m['_id']}: supporting_facts differ")
    return out


def build_corpus(records: list[dict]) -> list[dict]:
    """Pool every paragraph across ``records`` into ``[{doc_id, text}]``, keyed by Wikipedia
    title and deduped (first occurrence wins). doc_id = title, matching supporting_facts, so
    retrieval hits are comparable to gold by title."""
    corpus: list[dict] = []
    seen: set[str] = set()
    for record in records:
        for title, sentences in record.get("context", []):
            if title in seen:
                continue
            seen.add(title)
            corpus.append({"doc_id": title, "text": "".join(sentences)})
    return corpus


def stratified_subset(records: list[dict], n: int) -> list[str]:
    """Pick ``n`` case ids stratified by question ``type`` to match the input's type mix
    (bridge vs comparison), deterministically. Per-type counts are proportional (largest
    remainder to hit exactly ``n``); within a type, ids are taken in sorted order."""
    by_type: dict[str, list[str]] = {}
    for r in sorted(records, key=lambda r: r["_id"]):
        by_type.setdefault(r.get("type", "bridge"), []).append(r["_id"])

    total = sum(len(ids) for ids in by_type.values())
    if total == 0:
        return []
    # Proportional allocation with largest-remainder rounding so the counts sum to n.
    exact = {t: n * len(ids) / total for t, ids in by_type.items()}
    alloc = {t: int(v) for t, v in exact.items()}
    remainder = n - sum(alloc.values())
    for t, _ in sorted(exact.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)[:remainder]:
        alloc[t] += 1

    out: list[str] = []
    for t in sorted(by_type):
        out.extend(by_type[t][: alloc[t]])
    return out


# --- adapter -------------------------------------------------------------------------


@register_adapter
class HotpotQAAdapter:
    name = "hotpotqa"

    def __init__(self, path: str, version: str | None = None, subset_ids: list[str] | None = None):
        self.path = Path(path)
        doc = json.loads(self.path.read_text())
        self.version = version or doc.get("version") or "unversioned"
        self._cases = doc["cases"]
        self._subset = set(subset_ids) if subset_ids else None

    @classmethod
    def from_config(cls, config: dict) -> HotpotQAAdapter:
        if "path" not in config:
            raise ValueError("hotpotqa adapter config requires a 'path' (built cases artifact)")
        subset_ids = config.get("subset_ids")
        if subset_ids is None and config.get("subset_path"):
            subset_ids = [
                line.strip()
                for line in Path(config["subset_path"]).read_text().splitlines()
                if line.strip()
            ]
        return cls(path=config["path"], version=config.get("version"), subset_ids=subset_ids)

    def load(self) -> Iterator[Case]:
        for c in self._cases:
            if self._subset is not None and c["case_id"] not in self._subset:
                continue
            yield Case(
                case_id=c["case_id"],
                input=c["question"],
                expected={
                    "answer": c["answer"],
                    "supporting_titles": c["supporting_titles"],
                    "type": c.get("type", "bridge"),
                    "ndcg_k": 10,
                    "scorers": HOTPOTQA_SCORERS,
                    # TokenF1 (>= 0.8) decides pass/fail; everything else is informational.
                    "pass_scorers": ["TokenF1"],
                    "f1_threshold": 0.8,
                },
                tags=[c.get("type", "bridge")] + ([c["level"]] if c.get("level") else []),
            )
