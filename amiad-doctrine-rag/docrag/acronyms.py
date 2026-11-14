"""Acronyms found in the documents themselves ("military load classification (MLC)").

A question may use the long form while the passage only says the acronym. The list is read from the indexed text
(nothing typed by hand), and a question that uses the long form is widened with the acronym before it is searched.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

_SMALL = {"of", "and", "the", "for", "in", "to", "on", "a", "an", "de", "des", "du", "la", "le", "et"}
_OPEN = re.compile(r"\(\s*([A-Z][A-Z0-9]{1,7})s?\s*\)")


def _initials(words: list[str]) -> str:
    return "".join(w[0].lower() for w in words if w.lower() not in _SMALL)


def find_acronyms(texts: list[str]) -> dict[str, str]:
    """{acronym: long form}, most frequent long form wins. A long form is kept only if its initials spell the acronym."""
    seen: dict[str, Counter] = defaultdict(Counter)
    for t in texts:
        for m in _OPEN.finditer(t):
            acro = m.group(1)
            before = re.findall(r"[A-Za-z][A-Za-z-]*", t[max(0, m.start() - 120):m.start()])
            for k in range(len(acro), len(acro) + 4):
                cand = before[-k:]
                if len(cand) == k and _initials(cand) == acro.lower() and cand[0].lower() not in _SMALL:
                    seen[acro][" ".join(cand).lower()] += 1
                    break
    return {a: c.most_common(1)[0][0] for a, c in seen.items()}


def expand_query(query: str, acronyms: dict[str, str]) -> str:
    """Add the acronym when the long form is asked ("Joint Logistic Support Group" -> "JLSG").

    The reverse (adding the long form to an acronym) was measured and made the results worse, so it is not done."""
    low = query.lower()
    extra = [acro for acro, long in acronyms.items()
             if re.search(rf"\b{re.escape(long)}\b", low) and not re.search(rf"\b{acro}\b", query)]
    return f"{query} {' '.join(extra)}" if extra else query