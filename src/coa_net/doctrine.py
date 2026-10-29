"""S1: doctrine -> requirement instances (role L1; methodology 4.1-4.8, S1).

Each sentence of a doctrine text is classified as *mapped* (one or more
instances of the 26 templates), *inexpressible* (a movement requirement no
template can state) or *out of scope* (not about route or movement planning).
Coverage = mapped / (mapped + inexpressible).

Language-model path (L1, offline, quality first): for each template, the
passages closest to its seed phrases are retrieved and the model fills
instances; code checks the schema, the verbatim quote, the tier against the
trigger words, and the parameter ranges; instances stay *draft* until a human
signs them off. Without a model, a pattern compiler covers the phrasings of
the sample doctrine and common variants; it is a fallback, not a general
reader of doctrine.

``D2C_EXPRESSIBLE`` gives, for each template (and tier), whether D2C-Route's
rule format can state the same requirement exactly, approximately or not at
all; it is the basis of the expressiveness comparison (hypothesis H5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from .llm import call_validated
from .requirements import TEMPLATES, Requirement, find_trigger, merge_conflicts, tier_from_trigger, validate_instance

# D2C-Route's four rule types (hard predicate, soft feature, network and temporal parameters) against the templates
D2C_EXPRESSIBLE: dict[str, str] = {
    "SEG.LOAD": "exact", "SEG.GAUGE": "exact", "SEG.GRADE": "exact", "SEG.SURFACE": "exact", "SEG.STATUS": "exact",
    "SEG.CARGO/T1": "exact", "SEG.CARGO/T2": "approximate", "SEG.AVOID/T1": "exact", "SEG.AVOID/T2": "approximate",
    "SEG.AVOID/T3": "approximate", "RTE.GAP_MAX": "approximate", "RTE.TURN": "none", "RTE.CROSSINGS_MAX": "none",
    "RTE.DETOUR": "approximate", "RTE.ACCESS": "none", "RTE.RANGE": "none", "NET.SURVIVE": "approximate",
    "NET.SEPARATE": "none", "NET.LATERAL": "none", "NET.CONTROL": "none", "NET.SIZE": "none", "NET.STATUS": "none",
    "SCH.HEADWAY": "approximate", "SCH.MARCH": "exact", "SCH.WINDOW": "none", "SCH.HALT": "approximate",
    "SCH.VARY": "none", "PRO.EXPOSURE/T1": "exact", "PRO.EXPOSURE/T2": "approximate", "PRO.CRITPOINT": "none",
}
RANK = {"exact": 0, "approximate": 1, "none": 2}


def d2c_class(req: Requirement) -> str:
    return D2C_EXPRESSIBLE.get(f"{req.template}/{req.tier}") or D2C_EXPRESSIBLE.get(req.template, "none")


@dataclass
class SentenceResult:
    paragraph: str
    sentence: str
    status: str  # mapped | inexpressible | out_of_scope
    requirements: list[Requirement] = field(default_factory=list)
    d2c: str = "none"
    note: str = ""


@dataclass
class CompileReport:
    sentences: list[SentenceResult]
    log: list[str]

    @property
    def coverage(self) -> float:
        n = sum(1 for s in self.sentences if s.status in ("mapped", "inexpressible"))
        return sum(1 for s in self.sentences if s.status == "mapped") / n if n else 0.0

    @property
    def d2c_coverage(self) -> tuple[float, float]:
        """Share of in-scope sentences D2C-Route can state (exactly, exactly or approximately)."""
        ins = [s for s in self.sentences if s.status in ("mapped", "inexpressible")]
        if not ins:
            return 0.0, 0.0
        return (sum(1 for s in ins if s.d2c == "exact") / len(ins), sum(1 for s in ins if s.d2c in ("exact", "approximate")) / len(ins))

    def table(self) -> list[dict[str, Any]]:
        return [{"paragraph": s.paragraph, "sentence": s.sentence, "status": s.status,
                 "templates": [f"{r.template} ({r.tier})" for r in s.requirements], "d2c": s.d2c, "note": s.note} for s in self.sentences]

    def markdown(self) -> str:
        lines = ["| # | Sentence | COA-Net | D2C-Route |", "| --- | --- | --- | --- |"]
        for i, s in enumerate(self.sentences, start=1):
            what = ", ".join(f"{r.template} {r.tier}" for r in s.requirements) or s.status.replace("_", " ")
            lines.append(f"| {i} | {s.sentence} | {what} | {s.d2c if s.status != 'out_of_scope' else 'out of scope'} |")
        ex, ap = self.d2c_coverage
        lines.append("")
        lines.append(f"COA-Net coverage {self.coverage:.1%}; D2C-Route exact {ex:.1%}, exact or approximate {ap:.1%} (in-scope sentences).")
        return "\n".join(lines)


def split_sentences(text: str) -> list[tuple[str, str]]:
    """(paragraph number, sentence) for every sentence of numbered paragraphs; titles and headers dropped."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        m = re.match(r"\s*(\d+(?:\.\d+)*)\.\s+(.*)", para, re.S)
        if not m:
            continue
        num, body = m.group(1), " ".join(m.group(2).split())
        parts = re.split(r"(?<=[.;])\s+(?=[A-Z])", body)
        if parts and len(parts[0].split()) <= 4 and parts[0].endswith("."):  # paragraph title such as "Route classification."
            parts = parts[1:]
        out += [(num, p.strip()) for p in parts if p.strip()]
    return out


_N = r"(\d+(?:[.,]\d+)?)"


def _f(x: str) -> float:
    return float(x.replace(",", "."))


Builder = Callable[[re.Match, str], list[dict]]


def _p(template: str, params: dict | None = None, scope: dict | None = None, tier: str | None = None, unk: str = "risk") -> dict:
    return {"template": template, "params": params or {}, "scope": scope or {}, "tier": tier, "on_unknown": unk}


def _hazmat_scope(s: str) -> dict:
    return {"cargo": ["fuel", "ammunition"]} if re.search(r"fuel|ammunition|dangerous goods", s) else {}


# (pattern on the lower-cased sentence, builder) - every matching pattern adds its instances
PATTERNS: list[tuple[str, Builder]] = [
    (r"bridge[^.]*(load class|mlc)", lambda m, s: [_p("SEG.LOAD", {"check": "bridge_mlc", "margin_mlc": 0}, tier="T0", unk="forbid")]),
    (r"(posted|gross) weight", lambda m, s: [_p("SEG.LOAD", {"check": "posted_weight"}, tier="T0")]),
    (r"(tunnel|underpass)[^.]*clearance[^.]*?" + _N + r"\s*m",
     lambda m, s: [_p("SEG.GAUGE", {"check": "height", "height_margin_m": _f(m.group(2))}, tier="T0", unk="forbid")]),
    (r"(carriageway|road)[^.]*?" + _N + r"\s*m wider",
     lambda m, s: [_p("SEG.GAUGE", {"check": "width", "width_margin_m": _f(m.group(2))}, tier="T0")]),
    (r"(fuel|ammunition|dangerous goods)[^.]*(shall not|must not|never|not)[^.]*tunnel",
     lambda m, s: [_p("SEG.CARGO", {"cargo": ["fuel", "ammunition"], "element": "tunnel"}, {"cargo": ["fuel", "ammunition"]}, "T1", "forbid")]),
    (r"(dangerous goods|fuel|ammunition)[^.]*(bypass|avoid)[^.]*built-up",
     lambda m, s: [_p("SEG.CARGO", {"cargo": ["fuel", "ammunition"], "element": "built_up"}, {"cargo": ["fuel", "ammunition"]})]),
    (r"(no more than|at most|not more than)\s*(one|two|three|\d+)\s*built-up",
     lambda m, s: [_p("RTE.CROSSINGS_MAX", {"element": "built_up", "max_count": {"one": 1, "two": 2, "three": 3}.get(m.group(2)) or int(m.group(2))},
                      _hazmat_scope(s))]),
    (r"heavy[^.]*(shall not|must not|never)[^.]*(unpaved|track)",
     lambda m, s: [_p("SEG.SURFACE", {"vehicle_classes": ["heavy"]}, {"vehicle_classes": ["heavy"]}, "T0")]),
    (r"(march|convoy) speed[^.]*?(exceed|above|more than)\s*" + _N + r"\s*km/h",
     lambda m, s: [_p("SCH.MARCH", {"max_speed_kmh": _f(m.group(3))}, {"vehicle_classes": ["heavy"]} if "heavy" in s else {})]),
    (r"turns? sharper than\s*" + _N + r"\s*deg",
     lambda m, s: [_p("RTE.TURN", {"max_turn_deg": _f(m.group(1)), "min_vehicle_length_m": 15})]),
    (r"alternate[^.]*(usable|remains)[^.]*(single|any)[^.]*(bridge|tunnel|defile)",
     lambda m, s: [_p("NET.SURVIVE", {"k": 1, "element_kinds": [k for k in ("bridge", "tunnel", "defile", "ford", "ferry", "level_crossing") if k in s] + ["articulation"]})]),
    (r"alternate[^.]*more than\s*" + _N + r"\s*times",
     lambda m, s: [_p("RTE.DETOUR", {"rho": _f(m.group(1))})]),
    (r"^(?!.*(dangerous goods|fuel|ammunition))[^.]*(avoid|bypass)[^.]*built-up",
     lambda m, s: [_p("SEG.AVOID", {"attribute": "built_up"})]),
    (r"opposite flows[^.]*separat|separat[^.]*opposite", lambda m, s: [_p("NET.SEPARATE", {"narrow_margin_m": 0.5})]),
    (r"traffic control post[^.]*bridge[^.]*?(?:every\s*" + _N + r"\s*km)?",
     lambda m, s: [_p("NET.CONTROL", {"at_elements": True, "at_junctions": True,
                                      **({"max_spacing_km": _f(re.search(r"every\s*" + _N + r"\s*km", s).group(1))} if re.search(r"every\s*" + _N + r"\s*km", s) else {})})]),
    (_N + r"\s*min(?:ute)?s?\s*(?:of\s*)?(?:headway|time gap|gap|interval)", lambda m, s: [_p("SCH.HEADWAY", {"headway_min": _f(m.group(1))})]),
    (r"night[^.]*?(?:reduced|reduce|lowered)[^.]*?" + _N + r"\s*%",
     lambda m, s: [_p("SCH.MARCH", {"night_speed_factor": round(1 - _f(m.group(1)) / 100, 3)}, {"phases": ["night"]})]),
    (r"halt of\s*" + _N + r"\s*min[^.]*every\s*" + _N + r"\s*(hours?|minutes?)",
     lambda m, s: [_p("RTE.GAP_MAX", {"max_drive_min": _f(m.group(2)) * (60 if m.group(3).startswith("hour") else 1)}),
                   _p("SCH.HALT", {"halt_min": _f(m.group(1)), "halt_areas_only": "halt area" in s})]),
    (r"(not|never)[^.]*(transit|enter|cross)[^.]*built-up[^.]*between\s*(\d{1,2}[:h]\d{2})\s*and\s*(\d{1,2}[:h]\d{2})",
     lambda m, s: [_p("SCH.WINDOW", {"area": "built_up", "start_min": _hm(m.group(3)), "end_min": _hm(m.group(4))})]),
    (r"(not|never)\s*(halt|stop|wait)[^.]*within\s*" + _N + r"\s*m of a (bridge|tunnel)",
     lambda m, s: [_p("PRO.CRITPOINT", {"no_stop_within_m": _f(m.group(3)), "element_kinds": [k for k in ("bridge", "tunnel") if k in s]})]),
    (r"(not|never)[^.]*cross[^.]*high threat", lambda m, s: [_p("PRO.EXPOSURE", {"max_threat": 0.8})]),
    (r"exposure[^.]*(minimi|reduc|limit)", lambda m, s: [_p("PRO.EXPOSURE", {})]),
    (r"(vary|change)[^.]*(route|timing)[^.]*(rotation|routine)", lambda m, s: [_p("SCH.VARY", {"min_change_share": 0.3})]),
]
OUT_OF_SCOPE = r"\b(radio|report|medical|medevac|evacuation|communication|signal|chain of command)\b"
MOVEMENT_WORDS = r"\b(route|road|convoy|column|bridge|tunnel|cross|halt|speed|vehicle|refuel|march|built-up|threat)\b"


def _hm(s: str) -> int:
    h, m = re.split(r"[:h]", s)
    return int(h) * 60 + int(m or 0)


def pattern_sentence(sentence: str) -> list[dict]:
    s = sentence.lower()
    out: list[dict] = []
    for pat, build in PATTERNS:
        m = re.search(pat, s)
        if m:
            out += build(m, s)
    return out


def _instance(raw: dict, rid: str, sentence: str, paragraph: str, doc: str) -> Requirement:
    trigger = find_trigger(sentence)
    tier = raw.get("tier")
    if tier is None:
        tier = tier_from_trigger(trigger) or "T1"
        if tier not in TEMPLATES[raw["template"]].tiers:
            tier = TEMPLATES[raw["template"]].tiers[0]
    return Requirement(id=rid, template=raw["template"], tier=tier, params=raw.get("params", {}), scope=raw.get("scope", {}),
                       on_unknown=raw.get("on_unknown", "risk"), trigger=trigger, status="draft",
                       source={"document": doc, "paragraph": paragraph, "quote": sentence},
                       explanation=TEMPLATES[raw["template"]].description)


def compile_doctrine(text: str, doc_name: str = "doctrine", llm=None, id_prefix: str = "D") -> tuple[list[Requirement], CompileReport]:
    """Doctrine text -> draft requirement instances and a per-sentence report."""
    log: list[str] = []
    results: list[SentenceResult] = []
    reqs: list[Requirement] = []
    n = 0
    llm_raw = _llm_instances(text, llm, log) if llm is not None else {}
    for para, sent in split_sentences(text):
        raws = llm_raw.get(sent) if llm is not None and sent in llm_raw else pattern_sentence(sent)
        sr = SentenceResult(para, sent, "mapped")
        for raw in raws:
            n += 1
            try:
                r = _instance(raw, f"{id_prefix}-{n:02d}", sent, para, doc_name)
            except (ValidationError, ValueError, KeyError) as exc:
                log.append(f"{para}: rejected {raw.get('template')}: {exc}")
                continue
            problems = validate_instance(r, text)
            if problems:
                log.append(f"{para}: {r.id} {r.template} rejected: {'; '.join(problems)}")
                continue
            sr.requirements.append(r)
        if not sr.requirements:
            low = sent.lower()
            if re.search(OUT_OF_SCOPE, low) and not re.search(r"\b(bridge|tunnel|speed|halt)\b", low):
                sr.status, sr.note = "out_of_scope", "not about route or movement planning"
            elif re.search(MOVEMENT_WORDS, low):
                sr.status, sr.note = "inexpressible", "no template states it (to log as a catalogue gap)"
            else:
                sr.status, sr.note = "out_of_scope", "no movement requirement"
            sr.d2c = "none"
        else:
            sr.d2c = max((d2c_class(r) for r in sr.requirements), key=lambda c: RANK[c])
        reqs += sr.requirements
        results.append(sr)
    merged, notes = merge_conflicts(reqs)
    log += notes
    return merged, CompileReport(results, log)


L1_SYSTEM = (
    "You extract requirements for military road movement from doctrine by filling one template. Never invent values. "
    "One instance per distinct requirement; copy the quote verbatim; set the tier from the modal verb (shall/must: T1 or "
    "T0 for physical limits; should: T2; prefer/may: T3); leave a parameter empty when the text does not state it; take "
    "the scope only from the text."
)


def _llm_instances(text: str, llm, log: list[str]) -> dict[str, list[dict]]:
    """L1: for every template, retrieve the closest sentences (lexical stand-in for bge-m3) and ask for instances."""
    sents = [s for _p_, s in split_sentences(text)]
    out: dict[str, list[dict]] = {s: [] for s in sents}
    for t in TEMPLATES.values():
        seeds = [w.lower() for w in t.seeds] + [w.lower() for w in t.description.split() if len(w) > 6][:6]
        scored = sorted(sents, key=lambda s: -sum(1 for w in seeds if w in s.lower()))
        passages = [s for s in scored[:8] if any(w in s.lower() for w in seeds)]
        if not passages:
            continue
        card = f"{t.id} ({t.level}): {t.description} Tiers {', '.join(t.tiers)}. Parameters: " + ", ".join(
            f"{p.name} [{p.unit}] default {p.default}" + (f" range {p.lo}-{p.hi}" if p.lo is not None else "") for p in t.params)
        schema = {"type": "object", "properties": {"instances": {"type": "array", "items": {"type": "object", "properties": {
            "quote": {"type": "string"}, "tier": {"type": "string", "enum": list(t.tiers)}, "params": {"type": "object"},
            "scope": {"type": "object"}}, "required": ["quote", "tier"]}}}, "required": ["instances"]}

        def validate(o, t=t):
            rows = o.get("instances", []) if isinstance(o, dict) else []
            ok, problems = [], []
            for r in rows:
                q = " ".join(str(r.get("quote", "")).split())
                if q not in sents:
                    problems.append("quote is not one of the passages")
                    continue
                ok.append((q, {"template": t.id, "tier": r.get("tier"), "params": r.get("params") or {}, "scope": r.get("scope") or {}}))
            return ok, problems if not ok and rows else []

        got, problems = call_validated(llm, L1_SYSTEM, f"Template card:\n{card}\n\nPassages:\n" + "\n".join(f"- {p}" for p in passages),
                                       schema, "doctrine", validate)
        if got is None:
            log.append(f"{t.id}: model output rejected ({'; '.join(problems[:2])})")
            continue
        for q, raw in got:
            out[q].append(raw)
    return out


__all__ = ["CompileReport", "D2C_EXPRESSIBLE", "SentenceResult", "compile_doctrine", "d2c_class", "pattern_sentence", "split_sentences"]
