"""Retrieval-quality scorers for RAG agents (HotpotQA): recall@k, nDCG@k, and a
"both gold paragraphs retrieved" boolean.

All three score against a single **merged ranking** per case: the document ids from every
search call the agent made, concatenated in call order and de-duplicated keeping the first
occurrence (design: multi-hop agents search several times; we judge the union they surfaced,
in the order they first surfaced it). Gold relevance is HotpotQA's supporting-paragraph
titles, from ``expected['supporting_titles']``.
"""

from __future__ import annotations

import math

from vigil import RunResult

from engine.datasets.base import Case

from .base import ScoreResult


def merged_ranking(result: RunResult) -> list[str]:
    """All retrieved doc ids across the agent's search calls, in first-retrieval order, deduped."""
    seen: set[str] = set()
    ordered: list[str] = []
    for retrieval in result.retrievals:
        for doc_id in retrieval.doc_ids:
            if doc_id not in seen:
                seen.add(doc_id)
                ordered.append(doc_id)
    return ordered


def _gold(case: Case) -> list[str]:
    return list(case.expected.get("supporting_titles", []))


class RetrievalRecall:
    """recall@k over the merged ranking: fraction of gold titles present in its top ``k``.
    ``k`` from ``expected['recall_k']`` (default 5). Passes at
    ``expected['recall_threshold']`` (default 1.0 — all gold within top-k)."""

    name = "RetrievalRecall"

    def score(self, case: Case, result: RunResult) -> ScoreResult:
        gold = _gold(case)
        k = int(case.expected.get("recall_k", 5))
        threshold = float(case.expected.get("recall_threshold", 1.0))
        topk = merged_ranking(result)[:k]
        if not gold:
            recall = 1.0
            hits: list[str] = []
        else:
            hits = [t for t in gold if t in topk]
            recall = len(hits) / len(gold)
        return ScoreResult(
            name=self.name,
            passed=recall >= threshold,
            score=recall,
            detail={"k": k, "gold": gold, "hits": hits, "topk": topk},
        )


class NDCG:
    """nDCG@k over the merged ranking with binary relevance (gold title = 1). ``k`` from
    ``expected['ndcg_k']`` (default 10). Passes at ``expected['ndcg_threshold']`` (default
    1.0 — a perfect ranking)."""

    name = "NDCG"

    def score(self, case: Case, result: RunResult) -> ScoreResult:
        gold = set(_gold(case))
        k = int(case.expected.get("ndcg_k", 10))
        threshold = float(case.expected.get("ndcg_threshold", 1.0))
        ranking = merged_ranking(result)[:k]

        if not gold:
            ndcg = 1.0
        else:
            dcg = sum(
                1.0 / math.log2(i + 2)  # rank i is 0-based; discount log2(rank+1) = log2(i+2)
                for i, doc_id in enumerate(ranking)
                if doc_id in gold
            )
            ideal_hits = min(len(gold), k)
            idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
            ndcg = dcg / idcg if idcg > 0 else 0.0
        return ScoreResult(
            name=self.name,
            passed=ndcg >= threshold,
            score=ndcg,
            detail={"k": k, "gold": sorted(gold), "ranking": ranking},
        )


class AllGoldRetrieved:
    """Whether **all** gold supporting paragraphs were retrieved anywhere in the merged
    ranking (no cutoff) — the per-case boolean multi-hop questions need, since answering
    requires every supporting paragraph."""

    name = "AllGoldRetrieved"

    def score(self, case: Case, result: RunResult) -> ScoreResult:
        gold = _gold(case)
        retrieved = set(merged_ranking(result))
        missing = [t for t in gold if t not in retrieved]
        passed = not missing
        return ScoreResult(
            name=self.name,
            passed=passed,
            score=1.0 if passed else 0.0,
            detail={"gold": gold, "missing": missing},
        )
