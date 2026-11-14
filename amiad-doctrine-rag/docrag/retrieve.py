"""Index and hybrid search: BM25 + embeddings (reciprocal-rank fusion), then graph expansion."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .text import quantities
from .acronyms import expand_query, find_acronyms
from .bm25 import BM25
from .embed import HashingEmbedder, get_embedder
from .graph import DocGraph
from .ingest import Chunk
from .text import tokens

MODES = ("bm25", "dense", "hybrid", "graph")


@dataclass
class Hit:
    chunk: Chunk
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.chunk.id, "score": round(self.score, 4), "cite": self.chunk.cite(),
                "text": self.chunk.text, "reasons": self.reasons}


def index_texts(chunks: list[Chunk]) -> list[str]:
    """Text used for searching: the passage plus its section title and, for list items, the sentence that
    introduces the list. The passage shown to the user is never changed.

    A list item such as "Reception and dispatch of materiel;" says nothing about road terminals by itself.
    """
    out = []
    for i, c in enumerate(chunks):
        lead = ""
        j = i - 1
        while j >= 0 and i - j <= 15 and chunks[j].doc == c.doc and chunks[j].section_id == c.section_id:
            if chunks[j].text.rstrip().endswith(":"):
                lead = chunks[j].text
                break
            if len(chunks[j].text) > 300:  # a long paragraph is not a list item: stop
                break
            j -= 1
        if lead == c.text:
            lead = ""
        parts = [c.section, lead, c.text]
        out.append(". ".join(p for p in parts if p))
    return out


LOW_CONTENT_CHARS = 40


class Index:
    def __init__(self, chunks: list[Chunk], embedder=None):
        self.chunks = chunks
        self.embedder = embedder or HashingEmbedder()
        self.built = False

    def build(self) -> "Index":
        texts = index_texts(self.chunks)
        self.bm25 = BM25([tokens(t) for t in texts])
        self.embedder.fit(texts)
        self.dense = self.embedder.encode(texts) if texts else np.zeros((0, 1), dtype=np.float32)
        self.graph = DocGraph(self.chunks)
        self.acronyms = find_acronyms([c.text for c in self.chunks])
        self.built = True
        return self

    # ---- search ------------------------------------------------------------------------------
    def _low_content(self) -> set[int]:
        """Short passages without a measurement (running headers, titles, "Section 4 - Reporting"). They stay in the
        index and the graph, but are not offered as answers: BM25 favours short texts, so a 30-character header that
        repeats the document title outranks the paragraph that answers the question."""
        if getattr(self, "_low", None) is None:
            self._low = {i for i, c in enumerate(self.chunks) if len(c.text) < LOW_CONTENT_CHARS and not quantities(c.text)}
        return self._low

    def _bm25_rank(self, q: str, n: int = 50) -> list[int]:
        low = self._low_content()
        return [i for i, _ in self.bm25.search(tokens(q), n + len(low)) if i not in low][:n]

    def _dense_rank(self, q: str, n: int = 50) -> list[int]:
        if not len(self.chunks):
            return []
        sims = self.dense @ self.embedder.encode([q])[0]
        low = self._low_content()
        order = np.argsort(-sims)[: n + len(low)]
        return [int(i) for i in order if sims[i] > 0 and int(i) not in low][:n]

    def search(self, query: str, k: int = 8, mode: str = "hybrid") -> list[Hit]:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        query = expand_query(query, getattr(self, "acronyms", {}))
        b, d = self._bm25_rank(query), self._dense_rank(query)
        reasons: dict[int, list[str]] = {}
        score: dict[int, float] = {}

        def add(rank_list, label, weight=1.0):
            for r, i in enumerate(rank_list, 1):
                score[i] = score.get(i, 0.0) + weight / (60 + r)
                reasons.setdefault(i, []).append(f"{label} #{r}")

        if mode in ("bm25", "hybrid", "graph"):
            add(b, "bm25")
        if mode in ("dense", "hybrid", "graph"):
            add(d, "dense")
        top = max(score.values(), default=0.0)
        if top:
            score = {i: s / top for i, s in score.items()}
        if mode == "graph" and score:
            seeds = dict(sorted(score.items(), key=lambda x: -x[1])[:10])
            for i, (extra, why) in self.graph.expand(seeds, query).items():
                score[i] = score.get(i, 0.0) + extra
                reasons.setdefault(i, []).extend(why)
        ranked = sorted(score.items(), key=lambda x: -x[1])[:k]
        return [Hit(self.chunks[i], s, reasons.get(i, [])) for i, s in ranked]

    # ---- persistence ---------------------------------------------------------------------------
    def save(self, folder: str | Path):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "chunks.json").write_text(
            json.dumps([c.to_dict() for c in self.chunks], ensure_ascii=False, indent=1), encoding="utf-8")
        (folder / "meta.json").write_text(json.dumps({"embedder": self.embedder.name}), encoding="utf-8")
        np.save(folder / "dense.npy", self.dense)
        st = self.embedder.state()
        if st:
            np.savez(folder / "embedder_state.npz", **st)

    @classmethod
    def load(cls, folder: str | Path) -> "Index":
        folder = Path(folder)
        chunks = [Chunk(**d) for d in json.loads((folder / "chunks.json").read_text(encoding="utf-8"))]
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        if meta["embedder"] == "hashing":
            st = np.load(folder / "embedder_state.npz")
            emb = HashingEmbedder.from_state(st)
        else:
            emb = get_embedder(meta["embedder"])
        idx = cls(chunks, emb)
        idx.bm25 = BM25([tokens(t) for t in index_texts(chunks)])
        idx.dense = np.load(folder / "dense.npy")
        idx.graph = DocGraph(chunks)
        idx.acronyms = find_acronyms([c.text for c in chunks])
        idx.built = True
        return idx