"""Evaluation: retrieval (does each search mode find the passages that matter?) and extraction
(are the right rules found, and are none invented?). Gold files are small JSON lists written by hand."""
from __future__ import annotations

import json
from pathlib import Path

from .retrieve import MODES, Index

KS = (3, 5, 8, 10)


def eval_retrieval(index: Index, gold_path: str | Path, modes=MODES) -> dict:
    """gold: [{"query": str, "must_contain": [substrings, one per passage that must be found]}]"""
    gold = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    res: dict = {}
    for mode in modes:
        recall = {k: 0.0 for k in KS}
        mrr = 0.0
        for g in gold:
            hits = index.search(g["query"], k=max(KS), mode=mode)
            texts = [h.chunk.text.lower() for h in hits]
            markers = [m.lower() for m in g["must_contain"]]
            for k in KS:
                found = sum(any(m in t for t in texts[:k]) for m in markers)
                recall[k] += found / len(markers)
            first = next((i for i, t in enumerate(texts, 1) if any(m in t for m in markers)), None)
            mrr += 1 / first if first else 0.0
        n = len(gold)
        res[mode] = {**{f"recall@{k}": round(recall[k] / n, 3) for k in KS}, "mrr": round(mrr / n, 3)}
    return res


def retrieval_misses(index: Index, gold_path: str | Path, mode: str, k: int = 5) -> list[dict]:
    """Questions where some right passage is not in the top k, with the rank where it was found (or None)."""
    out = []
    for g in json.loads(Path(gold_path).read_text(encoding="utf-8")):
        texts = [h.chunk.text.lower() for h in index.search(g["query"], k=50, mode=mode)]
        for m in g["must_contain"]:
            rank = next((i for i, t in enumerate(texts, 1) if m.lower() in t), None)
            if rank is None or rank > k:
                out.append({"query": g["query"], "expected": m, "found_at_rank": rank})
    return out


def _same(r, g) -> bool:
    if r.template != g["template"]:
        return False
    if g.get("doc") and g["doc"] not in r.doc:
        return False
    if g.get("value") is None:
        return r.value is None
    return r.value is not None and abs(r.value - g["value"]) < 1e-6 and (g.get("unit") in (None, r.unit))


def eval_extraction(rules, gold_path: str | Path) -> dict:
    """gold: {"docs": [document name parts], "rules": [{"doc"?, "template", "value", "unit", "bound"?, "strictness"?}]}.
    Every rule extracted from the listed documents that matches no gold rule is a false positive."""
    gold_all = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    gold, docs = gold_all["rules"], gold_all.get("docs", [])
    # unclassified evidence is not a rule (meaning not established, never exported): it is not counted as an extracted rule
    scoped = [r for r in rules if (not docs or any(d in r.doc for d in docs)) and r.destination != "unclassified_evidence"]
    used: set[int] = set()
    missed, wrong = [], []
    tp = 0
    for g in gold:
        hit = next((r for r in scoped if id(r) not in used and _same(r, g)), None)
        if hit is None:
            missed.append(g)
            continue
        used.add(id(hit))
        tp += 1
        for key in ("bound", "strictness"):
            if g.get(key) and getattr(hit, key) != g[key]:
                wrong.append({"gold": g, "got": {key: getattr(hit, key)}})
    extra = [r for r in scoped if id(r) not in used]
    precision = tp / (tp + len(extra)) if (tp + len(extra)) else 1.0
    recall = tp / len(gold) if gold else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"gold": len(gold), "found": tp, "missed": missed, "false_positives": [r.to_dict() for r in extra],
            "bound_or_strictness_wrong": wrong,
            "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}


def format_retrieval(res: dict) -> str:
    cols = [f"recall@{k}" for k in KS] + ["mrr"]
    lines = ["mode      " + "  ".join(f"{c:>9}" for c in cols)]
    for mode, m in res.items():
        lines.append(f"{mode:<9} " + "  ".join(f"{m[c]:>9.3f}" for c in cols))
    return "\n".join(lines)