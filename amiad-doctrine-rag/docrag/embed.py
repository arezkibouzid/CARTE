"""Embedders.

HashingEmbedder is an offline, deterministic STAND-IN (word, bigram and character n-gram features,
hashed). It is lexical: it does not understand meaning. For real semantic search plug a local
sentence-embedding model with  --embedder st:<model-name-or-folder>  (needs sentence-transformers).
"""
from __future__ import annotations

import math
import zlib

import numpy as np

from .text import tokens


def _h(s: str, dim: int) -> int:
    return zlib.crc32(s.encode("utf-8")) % dim


class HashingEmbedder:
    name = "hashing"

    def __init__(self, dim: int = 2048):
        self.dim = dim
        self.idf = np.ones(dim, dtype=np.float32)

    def _features(self, text: str) -> list[str]:
        w = tokens(text)
        feats = list(w)
        feats += [f"{a}_{b}" for a, b in zip(w, w[1:])]
        for x in w:
            if len(x) >= 5:
                feats += [f"#{x[i:i + 4]}" for i in range(len(x) - 3)]
        return feats

    def _tf(self, text: str) -> dict[int, float]:
        tf: dict[int, float] = {}
        for f in self._features(text):
            i = _h(f, self.dim)
            tf[i] = tf.get(i, 0.0) + 1.0
        return tf

    def fit(self, texts: list[str]) -> "HashingEmbedder":
        df = np.zeros(self.dim, dtype=np.float32)
        for t in texts:
            for i in self._tf(t):
                df[i] += 1
        n = max(1, len(texts))
        self.idf = (np.log((n + 1) / (df + 1)) + 1).astype(np.float32)
        return self

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, t in enumerate(texts):
            for i, c in self._tf(t).items():
                out[r, i] = (1 + math.log(c)) * self.idf[i]
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return out / norms

    def state(self) -> dict:
        return {"idf": self.idf, "dim": np.array([self.dim])}

    @classmethod
    def from_state(cls, st) -> "HashingEmbedder":
        e = cls(int(st["dim"][0]))
        e.idf = st["idf"]
        return e


class SentenceTransformerEmbedder:
    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer  # optional dependency

        self.name = f"st:{model}"
        self.model_name = model
        self.model = SentenceTransformer(model)

    def fit(self, texts):
        return self

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False),
                          dtype=np.float32)

    def state(self) -> dict:
        return {}


def get_embedder(name: str = "hashing"):
    if name == "hashing":
        return HashingEmbedder()
    if name.startswith("st:"):
        return SentenceTransformerEmbedder(name[3:])
    raise ValueError(f"unknown embedder {name!r}; use 'hashing' or 'st:<model>'")
