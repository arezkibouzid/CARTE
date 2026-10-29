"""S2: commander's intent -> priority order of the objectives (role L3; methodology 4.4).

The intent mapper returns every objective of the menu exactly once, in order,
with tolerances, the quotes that justify the top of the order, and T1
candidates (imperatives such as "do not cross X", which the operator may
confirm as constraints). Deadlines are not an objective: lateness is a guard
placed before every order.

Language-model path (L3): schema-constrained output, checked by code (a
permutation of the menu, quotes found verbatim in the intent, tolerances in
range), one retry, then the keyword mapper: each sentence of the intent is
mapped to at most one objective by patterns, objectives are ordered by first
mention, and the others follow in the default relative order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .design import DEFAULT_EPS, DEFAULT_ORDER, MENU, OBJECTIVE_INFO
from .llm import call_validated

# (objective, pattern) tried in this order on every sentence; the first match wins
PATTERNS: list[tuple[str, str]] = [
    ("hazmat_builtup", r"(tanker|fuel|ammunition|dangerous goods|hazmat|carburant|munition)[^.]*(town|village|built-up|urban|agglom)|"
                       r"(town|village|built-up|urban)[^.]*(tanker|fuel|ammunition|dangerous goods)"),
    ("resilience", r"(strike|target|destroy|cut|lose|loss|interdict)[^.]*(crossing|bridge|tunnel|chokepoint|route)|"
                   r"(alternate|redundan|resilien|backup|fallback|second route|itinéraire de secours)"),
    ("threat_exposure", r"enemy|threat|ambush|ied|sniper|contact|exposure|menace|ennemi|embuscade"),
    ("builtup", r"\b(town|towns|village|villages|built-up|urban|civilian|population|agglomération)"),
    ("travel_time", r"\b(fast|faster|quick|quickly|speed|urgent|as soon as possible|asap|rapid|without delay|time is critical)"),
    ("network_size", r"(few|fewer|limited|minimum|economi|small)[^.]*(route|asset|post|engineer|marking)|economy of"),
    ("cover", r"\b(cover|concealment|conceal|observation|out of sight|forest|camouflage|discretion)"),
    ("doctrine", r"\b(doctrine|regulation|halt|rest|by the book)"),
    ("preferences", r"\b(main road|good road|paved|major road|gentle|comfort)"),
]
DEADLINE = r"\b(before|by|no later than|NLT|avant)\s+\d{1,2}[:h]\d{2}"
IMPERATIVE = r"\b(do not|don't|never|must not|shall not|no)\b[^.]*"


@dataclass
class IntentResult:
    order: list[str]
    eps: dict[str, float]
    quotes: dict[str, str] = field(default_factory=dict)
    t1_candidates: list[str] = field(default_factory=list)
    source: str = "keywords"
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = ["Priority order (lateness against deadlines is always checked first):"]
        for i, k in enumerate(self.order, start=1):
            unit, what = OBJECTIVE_INFO[k]
            tol = f", tolerance {self.eps[k]:.0%}" if self.eps.get(k) else ""
            q = f' - "{self.quotes[k]}"' if k in self.quotes else ""
            lines.append(f"  {i}. {k} ({what}, {unit}{tol}){q}")
        if self.t1_candidates:
            lines.append("Constraints to confirm (T1 candidates): " + "; ".join(self.t1_candidates))
        return "\n".join(lines)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;!])\s+", text) if s.strip()]


def keyword_intent(intent: str) -> IntentResult:
    mentioned: list[str] = []
    quotes: dict[str, str] = {}
    t1: list[str] = []
    notes: list[str] = []
    eps = dict(DEFAULT_EPS)
    for s in _sentences(intent):
        low = s.lower()
        hit = next((k for k, pat in PATTERNS if re.search(pat, low)), None)
        if hit is None and re.search(DEADLINE, s, re.I):
            notes.append(f'"{s}": deadline, checked by the lateness guard')
            continue
        if hit is not None and hit not in mentioned:
            mentioned.append(hit)
            quotes[hit] = s
        m = re.search(r"(\d+)\s*%\s*(?:more|extra|longer)", low)
        if m and hit in ("travel_time", "resilience", None):
            eps["travel_time"] = int(m.group(1)) / 100.0
        if re.search(IMPERATIVE, low) or re.match(r"(keep|avoid|stay)\b", low):
            t1.append(s)
    order = mentioned + [k for k in DEFAULT_ORDER if k not in mentioned]
    return IntentResult(order, eps, quotes, t1, "keywords", notes)


INTENT_SYSTEM = (
    "You map a commander's intent for a logistic movement to a priority order over a fixed menu of objectives. "
    "Return every objective exactly once, most important first. Justify the top three with exact quotes from the "
    "intent. Turn imperatives into constraint candidates. Keep the default relative order for objectives the intent "
    "does not mention. Deadlines are checked separately: do not rank them."
)


def map_intent(intent: str, llm=None) -> IntentResult:
    if not intent.strip():
        return IntentResult(list(DEFAULT_ORDER), dict(DEFAULT_EPS), source="default", notes=["no intent stated: default order"])
    base = keyword_intent(intent)
    if llm is None:
        return base
    schema = {
        "type": "object",
        "properties": {
            "order": {"type": "array", "items": {"type": "string", "enum": MENU}},
            "tolerances": {"type": "object", "additionalProperties": {"type": "number"}},
            "quotes": {"type": "object", "additionalProperties": {"type": "string"}},
            "t1_candidates": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["order", "quotes"],
    }
    menu = "\n".join(f"- {k}: {OBJECTIVE_INFO[k][1]}" for k in MENU)
    norm = " ".join(intent.split()).lower()

    def validate(out):
        if not isinstance(out, dict):
            return None, ["not an object"]
        order = list(out.get("order", []))
        problems = []
        if sorted(order) != sorted(MENU):
            problems.append("order is not a permutation of the menu")
        quotes = {k: str(v) for k, v in (out.get("quotes") or {}).items() if k in MENU}
        for k, q in quotes.items():
            if " ".join(q.split()).lower() not in norm:
                problems.append(f"quote for {k} not found in the intent")
        eps = dict(DEFAULT_EPS)
        for k, v in (out.get("tolerances") or {}).items():
            if k in MENU and 0.0 <= float(v) <= 0.5:
                eps[k] = float(v)
            elif k in MENU:
                problems.append(f"tolerance {k}={v} outside [0, 0.5]")
        t1 = [str(c) for c in out.get("t1_candidates", [])]
        return IntentResult(order, eps, quotes, t1, "model"), problems

    res, problems = call_validated(llm, INTENT_SYSTEM, f"Objective menu:\n{menu}\n\nIntent:\n{intent}", schema, "intent", validate)
    if res is None:
        base.notes.append("intent mapping by the language model rejected (" + "; ".join(problems[:3]) + "); keyword mapper used")
        return base
    return res


__all__ = ["IntentResult", "keyword_intent", "map_intent"]
