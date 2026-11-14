"""Retrieval API: the one function other code (the planner, Nouhaila's pipeline, the UI) calls to search.

Input : a question (text), how many passages (k), a mode, optionally one document.
Output: passages copied from the documents, each with its source. Never generated text.
"""
from __future__ import annotations

from pathlib import Path

from .retrieve import MODES, Index

SCHEMA_VERSION = "1.0"
MAX_QUERY = 500
_cache: dict[str, Index] = {}


def load(work: str | Path) -> Index:
    key = str(Path(work).resolve())
    if key not in _cache:
        _cache[key] = Index.load(work)
    return _cache[key]


def retrieve(work_or_index, query: str, k: int = 8, mode: str = "hybrid", document: str | None = None) -> dict:
    """Return {"schema_version", "query", "mode", "k", "results": [...]}.

    results[i] = {"rank", "score", "passage_id", "text", "source": {document, page, paragraph, section, label},
                  "why": [how it was found: keyword, embedding, topic link, cross-reference, neighbour]}
    Raises ValueError on an empty or too long query, a bad mode, or an unknown document.
    """
    idx = work_or_index if isinstance(work_or_index, Index) else load(work_or_index)
    q = (query or "").strip()
    if not q:
        raise ValueError("empty question")
    if len(q) > MAX_QUERY:
        raise ValueError(f"question too long (max {MAX_QUERY} characters)")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not 1 <= k <= 50:
        raise ValueError("k must be between 1 and 50")
    docs = {c.doc for c in idx.chunks}
    if document is not None and document not in docs:
        raise ValueError(f"unknown document {document!r}. Available: {sorted(docs)}")
    # ask for more, then filter, so a document filter still returns k passages when they exist
    hits = idx.search(q, k if document is None else min(50, k * 5), mode)
    if document is not None:
        hits = [h for h in hits if h.chunk.doc == document][:k]
    results = [{"rank": i, "score": round(h.score, 4), "passage_id": h.chunk.id, "text": h.chunk.text,
                "source": {"document": h.chunk.doc, "page": h.chunk.page, "paragraph": h.chunk.para,
                           "section": h.chunk.section, "label": h.chunk.label},
                "why": h.reasons}
               for i, h in enumerate(hits, 1)]
    return {"schema_version": SCHEMA_VERSION, "query": q, "mode": mode, "k": k, "results": results}