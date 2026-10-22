"""Language front-ends: C1 doctrine compiler, C2 order parser, C6 incident parser.

Each front-end asks the LLM for a typed JSON record, validates it, and falls
back to a deterministic pattern parser when no LLM is configured or the
answer is invalid. The fallbacks cover the sample formats shipped in
``examples/`` and keep the pipeline testable offline; they are not meant to
understand arbitrary doctrine.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import networkx as nx
from pydantic import ValidationError

from .expr import EDGE_ATTRS, VEHICLE_ATTRS, ExprError, compile_expr
from .graph import _fold, gazetteer
from .schemas import (
    NETWORK_PARAMS,
    TEMPORAL_PARAMS,
    Convoy,
    Incident,
    IncidentType,
    LocationRef,
    MissionSpec,
    Rule,
    VehicleGroup,
)

if TYPE_CHECKING:
    from .llm import LLMClient


# ---------------------------------------------------------------------------
# C1: doctrine compiler
# ---------------------------------------------------------------------------

DSL_GUIDE = f"""Rule language:
- type "hard": "predicate" is a boolean expression that must hold on every road used.
- type "soft": "feature" is a numeric expression in [0,1] (1 = worst) and "weight" > 0.
- type "network": "params" uses keys {sorted(NETWORK_PARAMS)}.
- type "temporal": "params" uses keys {sorted(TEMPORAL_PARAMS)}.
Expressions use Python syntax with and/or/not, comparisons, + - * /, min/max/abs,
"x is None", and only these names:
  edge.{{{", ".join(sorted(EDGE_ATTRS))}}}
  vehicle.{{{", ".join(sorted(VEHICLE_ATTRS))}}}
Units: metres, tonnes, km/h, percent, minutes. road_rank: 0 motorway .. 6 track.
"scope" may restrict vehicle_classes (light, medium, heavy, tracked), phases (day, night), cargo (fuel, ammunition, ...).
Only extract rules stated in the text. Never invent numbers. Give each rule a short rationale quoting the text."""


def chunk_text(text: str, max_chars: int = 2500) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > max_chars:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _rule_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["hard", "soft", "network", "temporal"]},
                        "scope": {
                            "type": "object",
                            "properties": {
                                "vehicle_classes": {"type": "array", "items": {"type": "string"}},
                                "phases": {"type": "array", "items": {"type": "string", "enum": ["day", "night"]}},
                                "cargo": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                        "predicate": {"type": ["string", "null"]},
                        "feature": {"type": ["string", "null"]},
                        "weight": {"type": ["number", "null"]},
                        "params": {"type": "object", "additionalProperties": {"type": "number"}},
                        "rationale": {"type": "string"},
                    },
                    "required": ["type", "rationale"],
                },
            }
        },
        "required": ["rules"],
    }


def _validate_rule(raw: dict, rid: str, source: str) -> Rule:
    raw = {k: v for k, v in raw.items() if v is not None}
    rule = Rule.model_validate({**raw, "id": rid, "source": source})
    for src in (rule.predicate, rule.feature):
        if src:
            compile_expr(src)
    return rule


def _rule_key(r: Rule) -> tuple:
    return (r.type, r.predicate, r.feature, tuple(sorted(r.params.items())), tuple(r.scope.vehicle_classes), tuple(r.scope.phases), tuple(r.scope.cargo))


def compile_doctrine(
    text: str,
    doc_name: str = "doctrine",
    llm: "LLMClient | None" = None,
    id_prefix: str = "C",
) -> tuple[list[Rule], list[str]]:
    """Doctrine prose -> validated, de-duplicated rules (+ a log of rejects)."""
    rules: list[Rule] = []
    log: list[str] = []
    seen: set[tuple] = set()
    n = 0
    for i, chunk in enumerate(chunk_text(text)):
        source = f"{doc_name}, chunk {i + 1}"
        raws: list[dict] = []
        if llm is not None:
            try:
                out = llm.complete_json(
                    "You compile military movement doctrine into machine-checkable rules.\n" + DSL_GUIDE,
                    "Text:\n" + chunk,
                    _rule_schema(),
                    stage="doctrine",
                )
                raws = list(out.get("rules", []))
            except Exception as exc:  # noqa: BLE001
                log.append(f"{source}: LLM failed ({exc}); pattern fallback used")
                raws = _pattern_rules(chunk)
        else:
            raws = _pattern_rules(chunk)
        for raw in raws:
            n += 1
            try:
                rule = _validate_rule(raw, f"{id_prefix}-{n:03d}", source)
            except (ValidationError, ExprError, ValueError) as exc:
                log.append(f"{source}: rejected {raw!r}: {exc}")
                continue
            key = _rule_key(rule)
            if key in seen:
                log.append(f"{source}: duplicate of an earlier rule dropped")
                continue
            seen.add(key)
            rules.append(rule)
    return rules, log


_NUM = r"(\d+(?:[.,]\d+)?)"


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def _pattern_rules(text: str) -> list[dict]:
    """Offline fallback: recognises a handful of common phrasings, sentence by sentence."""
    out: list[dict] = []
    for sent in re.split(r"(?<=[.;])\s+", text):
        s = sent.lower()
        quote = sent.strip()[:200]
        if "bridge" in s and ("load class" in s or "mlc" in s):
            out.append({"type": "hard", "predicate": "not edge.bridge or edge.mlc >= vehicle.mlc", "rationale": quote})
        m = re.search(r"(tunnel|underpass)[^.]*clearance[^.]*?" + _NUM + r"\s*m", s)
        if m:
            out.append(
                {"type": "hard", "predicate": f"not edge.tunnel or edge.max_height_m >= vehicle.height_m + {_f(m.group(2))}", "rationale": quote}
            )
        if re.search(r"(fuel|ammunition|dangerous goods)[^.]*(shall not|must not|never|prohibited)[^.]*tunnel", s):
            out.append({"type": "hard", "scope": {"cargo": ["fuel", "ammunition"]}, "predicate": "not edge.tunnel", "rationale": quote})
        if re.search(r"heavy[^.]*(shall not|must not|never)[^.]*(unpaved|track)", s):
            out.append({"type": "hard", "scope": {"vehicle_classes": ["heavy"]}, "predicate": "edge.road_rank < 6 or edge.paved", "rationale": quote})
        if re.search(r"(avoid|bypass)[^.]*built-up", s):
            scope = {"cargo": ["fuel", "ammunition"]} if re.search(r"fuel|ammunition|dangerous", s) else {}
            out.append({"type": "soft", "scope": scope, "feature": "edge.built_up", "weight": 2.0 if scope else 1.0, "rationale": quote})
        if re.search(r"(avoid|minimi[sz]e)[^.]*(chokepoint|single point)", s):
            out.append({"type": "soft", "feature": "edge.centrality", "weight": 0.5, "rationale": quote})
        m = re.search(r"at least (one|two|three|\d+) alternate", s)
        if m:
            k = {"one": 1, "two": 2, "three": 3}.get(m.group(1)) or int(m.group(1))
            params: dict[str, float] = {"min_alternates": k}
            m2 = re.search(r"(?:share|overlap)[^.]*?" + _NUM + r"\s*%", s)
            if m2:
                params["max_overlap"] = _f(m2.group(1)) / 100
            out.append({"type": "network", "params": params, "rationale": quote})
        m = re.search(_NUM + r"\s*min(?:ute)?s?\s*(?:of\s*)?(?:headway|time gap|gap|interval) between (?:successive )?convoys", s)
        if m:
            out.append({"type": "network", "params": {"min_headway_min": _f(m.group(1))}, "rationale": quote})
        m = re.search(r"night[^.]*?(?:reduced|reduce|lowered)[^.]*?" + _NUM + r"\s*%", s)
        if m:
            out.append({"type": "temporal", "scope": {"phases": ["night"]}, "params": {"speed_factor": 1 - _f(m.group(1)) / 100}, "rationale": quote})
        m = re.search(r"(?:march|convoy) speed[^.]*?(?:exceed|above|more than)\s*" + _NUM + r"\s*km/h", s)
        if m:
            scope = {"vehicle_classes": ["heavy"]} if "heavy" in s else {}
            out.append({"type": "temporal", "scope": scope, "params": {"max_speed_kmh": _f(m.group(1))}, "rationale": quote})
        m = re.search(r"halt[^.]*every\s*" + _NUM + r"\s*(hours?|minutes?)[^.]*?" + _NUM + r"\s*min", s)
        if m:
            every = _f(m.group(1)) * (60 if m.group(2).startswith("hour") else 1)
            out.append({"type": "temporal", "params": {"halt_every_min": every, "halt_duration_min": _f(m.group(3))}, "rationale": quote})
    return out


# ---------------------------------------------------------------------------
# C2: logistic order parser
# ---------------------------------------------------------------------------


def _hhmm(s: str) -> float:
    h, m = re.split(r"[:h]", s)
    return int(h) * 60 + int(m or 0)


def parse_order(
    text: str,
    G: nx.DiGraph | None = None,
    catalogue: dict | None = None,
    llm: "LLMClient | None" = None,
) -> MissionSpec:
    """Logistic order text -> MissionSpec. Times are minutes from 00:00 of D-day."""
    if llm is not None:
        try:
            return _llm_order(text, G, catalogue, llm)
        except Exception as exc:  # noqa: BLE001
            spec = _pattern_order(text, catalogue)
            spec.ambiguities.append(f"LLM order parsing failed ({exc}); pattern fallback used")
            return spec
    return _pattern_order(text, catalogue)


def _llm_order(text: str, G, catalogue, llm) -> MissionSpec:
    places = sorted(gazetteer(G))[:200] if G is not None else []
    hint = {
        "vehicle_types": sorted(catalogue) if catalogue else [],
        "known_places": places,
        "rules": "Times in minutes from 00:00. Use location {'name': ...} with a known place when possible. "
        "Put anything unclear in 'ambiguities' instead of guessing.",
    }
    out = llm.complete_json(
        "You parse military logistic movement orders into a structured mission specification.",
        json.dumps(hint) + "\n\nOrder:\n" + text,
        MissionSpec.model_json_schema(),
        stage="order",
    )
    spec = MissionSpec.model_validate(out)
    _check_vehicles(spec, catalogue)
    return spec


def _check_vehicles(spec: MissionSpec, catalogue: dict | None) -> None:
    if not catalogue:
        return
    for c in spec.convoys:
        for g in c.vehicles:
            if g.type not in catalogue:
                spec.ambiguities.append(f"{c.id}: unknown vehicle type {g.type!r}")


_CONVOY_RE = re.compile(
    r"(?:convoy|convoi)\s+(?P<id>[\w-]+)\s*:\s*(?P<veh>.+?)\s+(?:from|de|depuis)\s+(?P<orig>.+?)\s+(?:to|à|vers)\s+(?P<dest>[^,.;]+)(?P<rest>[^\n]*)",
    re.I,
)


def _pattern_order(text: str, catalogue: dict | None = None) -> MissionSpec:
    phase = "night" if re.search(r"phase\s*:\s*(night|nuit)|\bnight move|\bde nuit\b", text, re.I) else "day"
    m = re.search(r"(?:order|ordre)\s+([\w-]+)", text, re.I)
    convoys, amb = [], []
    for cm in _CONVOY_RE.finditer(text):
        rest = cm.group("rest")
        vehicles = [VehicleGroup(count=int(n), type=t.lower()) for n, t in re.findall(r"(\d+)\s*x\s*([\w-]+)", cm.group("veh"))]
        if not vehicles:
            amb.append(f"convoy {cm.group('id')}: no vehicles recognised in {cm.group('veh')!r}")
            continue
        dep = re.search(r"(?:depart\w*|départ)\s*(?:at\s*|à\s*)?(\d{1,2}[:h]\d{2})", rest, re.I)
        arr = re.search(r"(?:arriv\w*)\s*(?:before|by|no later than|NLT|avant)\s*(\d{1,2}[:h]\d{2})", rest, re.I)
        prio = re.search(r"(?:priority|priorité)\s*(\d)", rest, re.I)
        cargo = re.search(r"(?:cargo|chargement)\s*:?\s*([\w ,]+)", rest, re.I)
        convoys.append(
            Convoy(
                id=cm.group("id"),
                vehicles=vehicles,
                origin=LocationRef(name=cm.group("orig").strip()),
                destination=LocationRef(name=cm.group("dest").strip()),
                earliest_departure_min=_hhmm(dep.group(1)) if dep else 0.0,
                latest_arrival_min=_hhmm(arr.group(1)) if arr else None,
                priority=int(prio.group(1)) if prio else 1,
                cargo=[c.strip().lower() for c in cargo.group(1).split(",") if c.strip()] if cargo else [],
            )
        )
    spec = MissionSpec(id=m.group(1) if m else "mission", phase=phase, convoys=convoys, ambiguities=amb)
    _check_vehicles(spec, catalogue)
    return spec


# ---------------------------------------------------------------------------
# C6: incident parser
# ---------------------------------------------------------------------------

_INCIDENT_KEYWORDS = [
    (IncidentType.BRIDGE_DESTROYED, r"bridge[^.]*(destroyed|down|collapsed|damaged)|pont[^.]*(détruit|detruit|effondré|endommagé)"),
    (IncidentType.THREAT_ZONE, r"threat|enemy|ambush|ied|sniper|menace|ennemi|embuscade"),
    (IncidentType.BREAKDOWN, r"breakdown|broke down|broken down|panne"),
    (IncidentType.WEATHER, r"flood|snow|ice|fog|mud|weather|inond|neige|verglas|brouillard|boue|météo"),
    (IncidentType.ROAD_BLOCKED, r"block|closed|crater|obstruct|bouch|coupée|fermée|barr|obstru"),
]


def parse_incident(
    text: str,
    G: nx.DiGraph,
    incident_id: str = "I-1",
    time_min: float = 0.0,
    llm: "LLMClient | None" = None,
) -> Incident:
    if llm is not None:
        try:
            out = llm.complete_json(
                "You turn military incident reports into structured records. Use a known place name for "
                "location when possible; never invent coordinates.",
                json.dumps({"known_places": sorted(gazetteer(G))[:200]}) + "\n\nReport:\n" + text,
                Incident.model_json_schema(),
                stage="incident",
            )
            out.update({"id": incident_id, "time_min": time_min, "text": text})
            inc = Incident.model_validate(out)
            if inc.location is not None:
                from .graph import resolve

                resolve(G, inc.location)  # must be resolvable, otherwise fall back
            return inc
        except Exception:  # noqa: BLE001
            pass
    return _pattern_incident(text, G, incident_id, time_min)


def _pattern_incident(text: str, G: nx.DiGraph, incident_id: str, time_min: float) -> Incident:
    low = text.lower()
    itype = next((t for t, pat in _INCIDENT_KEYWORDS if re.search(pat, low)), IncidentType.ROAD_BLOCKED)
    loc: LocationRef | None = None
    m = re.search(r"\(\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*\)", text)
    if m:
        loc = LocationRef(xy=(float(m.group(1)), float(m.group(2))))
    else:
        folded = _fold(text)
        names = sorted(gazetteer(G), key=len, reverse=True)
        hit = next((n for n in names if re.search(rf"\b{re.escape(n)}\b", folded)), None)
        if hit:
            node = gazetteer(G)[hit]
            loc = LocationRef(name=G.nodes[node].get("name") or hit)
    r = re.search(r"(\d+(?:\.\d+)?)\s*(km|m)\b\s*(?:radius|rayon)|(?:radius|rayon)\s*(?:of\s*|de\s*)?(\d+(?:\.\d+)?)\s*(km|m)\b", low)
    radius = 300.0
    if r:
        val, unit = (r.group(1), r.group(2)) if r.group(1) else (r.group(3), r.group(4))
        radius = float(val) * (1000 if unit == "km" else 1)
    convoy = re.search(r"(?:convoy|convoi)\s+([\w-]+)", text, re.I)
    dur = re.search(r"(\d+)\s*(?:min|minutes)", low)
    sev = 0.9 if itype == IncidentType.THREAT_ZONE and re.search(r"high|confirmed|élevée|confirmée", low) else (
        0.6 if itype in (IncidentType.THREAT_ZONE, IncidentType.WEATHER) else 1.0
    )
    return Incident(
        id=incident_id,
        type=itype,
        location=loc,
        radius_m=radius,
        severity=sev,
        duration_min=float(dur.group(1)) if dur else None,
        time_min=time_min,
        convoy_id=convoy.group(1) if convoy else None,
        text=text,
    )


__all__: list[Any] = ["compile_doctrine", "parse_order", "parse_incident", "chunk_text", "DSL_GUIDE"]
