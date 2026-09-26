"""Retrieval for the HotpotQA agent: a `Retriever` interface with a `bm25s` (Lucene BM25)
implementation over the pooled corpus.

Lives in the agent package, not the engine: agents must not import the engine, and retrieval is
part of the agent under test. A dense retriever can be added later behind the same `Retriever`
protocol without touching the agent loop or the engine's scorers (which only read the
resulting doc-ids off `RunResult.retrievals`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import bm25s


@dataclass
class RetrievedDoc:
    doc_id: str
    text: str
    score: float


@runtime_checkable
class Retriever(Protocol):
    def search(self, query: str, k: int) -> list[RetrievedDoc]: ...


class BM25Retriever:
    """BM25 retrieval over an in-memory corpus using the Lucene variant (`bm25s`)."""

    def __init__(self, doc_ids: list[str], texts: list[str]):
        if len(doc_ids) != len(texts):
            raise ValueError("doc_ids and texts must be the same length")
        self._doc_ids = list(doc_ids)
        self._texts = list(texts)
        corpus_tokens = bm25s.tokenize(self._texts, stopwords="en", show_progress=False)
        self._bm25 = bm25s.BM25(method="lucene")
        self._bm25.index(corpus_tokens, show_progress=False)

    def __len__(self) -> int:
        return len(self._doc_ids)

    def search(self, query: str, k: int) -> list[RetrievedDoc]:
        if not self._doc_ids:
            return []
        query_tokens = bm25s.tokenize(query, stopwords="en", show_progress=False)
        # A query of only stopwords/punctuation tokenizes to nothing; bm25s would error.
        if not _has_tokens(query_tokens):
            return []
        k = min(k, len(self._doc_ids))
        indices, scores = self._bm25.retrieve(query_tokens, k=k, show_progress=False)
        out: list[RetrievedDoc] = []
        for idx, score in zip(indices[0], scores[0]):
            out.append(
                RetrievedDoc(
                    doc_id=self._doc_ids[int(idx)], text=self._texts[int(idx)], score=float(score)
                )
            )
        return out


def _has_tokens(tokenized) -> bool:
    # bm25s.tokenize returns a Tokenized(ids, vocab); ids is a list with one entry per document.
    ids = getattr(tokenized, "ids", tokenized)
    return bool(ids) and bool(ids[0])


def load_corpus(path: str | Path) -> tuple[list[str], list[str]]:
    """Read a corpus.jsonl artifact ({doc_id, text} per line) into parallel id/text lists."""
    doc_ids: list[str] = []
    texts: list[str] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        doc = json.loads(line)
        doc_ids.append(doc["doc_id"])
        texts.append(doc["text"])
    return doc_ids, texts


def bm25_from_corpus(path: str | Path) -> BM25Retriever:
    return BM25Retriever(*load_corpus(path))
