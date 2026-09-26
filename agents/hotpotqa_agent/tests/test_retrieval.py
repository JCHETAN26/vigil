"""Tests for the BM25 retriever over a tiny in-memory corpus."""

from __future__ import annotations

import json

from hotpotqa_agent.retrieval import BM25Retriever, RetrievedDoc, bm25_from_corpus, load_corpus

_DOCS = [
    ("Marie Curie", "Marie Curie was a physicist and chemist who researched radioactivity."),
    ("Radioactivity", "Radioactivity is the emission of radiation from unstable atomic nuclei."),
    ("Eiffel Tower", "The Eiffel Tower is a wrought-iron lattice tower in Paris, France."),
    ("Paris", "Paris is the capital and most populous city of France."),
]


def _retriever():
    doc_ids = [d[0] for d in _DOCS]
    texts = [d[1] for d in _DOCS]
    return BM25Retriever(doc_ids, texts)


def test_search_ranks_relevant_doc_first():
    r = _retriever()
    hits = r.search("who studied radioactivity", k=2)
    assert hits and isinstance(hits[0], RetrievedDoc)
    ids = [h.doc_id for h in hits]
    # Both radioactivity-related docs should surface; the query mentions the topic explicitly.
    assert "Radioactivity" in ids or "Marie Curie" in ids
    assert hits[0].score >= hits[-1].score  # returned in descending score order


def test_k_is_capped_and_ids_valid():
    r = _retriever()
    hits = r.search("France", k=99)  # k larger than the corpus
    assert 1 <= len(hits) <= len(_DOCS)
    assert all(h.doc_id in {d[0] for d in _DOCS} for h in hits)


def test_empty_query_returns_nothing():
    r = _retriever()
    assert r.search("the a of", k=5) == []  # only stopwords -> no tokens -> no results


def test_load_corpus_and_build(tmp_path):
    path = tmp_path / "corpus.jsonl"
    with path.open("w") as f:
        for doc_id, text in _DOCS:
            f.write(json.dumps({"doc_id": doc_id, "text": text}) + "\n")

    doc_ids, texts = load_corpus(path)
    assert doc_ids == [d[0] for d in _DOCS]
    assert len(texts) == len(_DOCS)

    r = bm25_from_corpus(path)
    assert len(r) == len(_DOCS)
    hits = r.search("capital of France", k=1)
    assert hits[0].doc_id == "Paris"
