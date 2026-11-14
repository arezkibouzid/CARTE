"""Small BM25 (Okapi) with no dependency."""
from __future__ import annotations

import math
from collections import Counter, defaultdict


class BM25:
    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.N = len(docs_tokens)
        self.dl = [len(d) for d in docs_tokens]
        self.avg = sum(self.dl) / max(1, self.N)
        df: Counter = Counter()
        self.inv: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, d in enumerate(docs_tokens):
            tf = Counter(d)
            df.update(tf.keys())
            for t, c in tf.items():
                self.inv[t].append((i, c))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def scores(self, qtokens: list[str]) -> dict[int, float]:
        out: dict[int, float] = defaultdict(float)
        for t in set(qtokens):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, tf in self.inv[t]:
                denom = tf + self.k1 * (1 - self.b + self.b * self.dl[i] / max(self.avg, 1e-9))
                out[i] += idf * tf * (self.k1 + 1) / denom
        return out

    def search(self, qtokens: list[str], k: int = 10) -> list[tuple[int, float]]:
        s = self.scores(qtokens)
        return sorted(s.items(), key=lambda x: -x[1])[:k]
