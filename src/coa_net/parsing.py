"""Order parser (role L2) and incident parser (role L6).

Each parser asks the language model for a typed record (schema-constrained),
validates it by code (names resolve on the graph, times consistent, grading
valid), retries once, and falls back to a deterministic pattern parser,
ported from D2C-Route and extended with:

* orders: the ``Intent:`` line (kept verbatim for the intent mapper),
  prohibited places (``Avoid X`` / ``Do not cross X`` / ``Do not enter X``)
  and imposed via-points (``via X`` in a convoy line);
* incidents: the Admiralty grading, explicit (``graded A1`` or ``(B2)``) or
  from keywords (confirmed: credibility 1; unconfirmed, possible, reported,
  suspected: 3; rumour: 4; default B2), the report time (``06:05``),
  reconnaissance results (``classified MLC 120``, ``clearance 4.2 m``) and
  lifted closures (``reopened``, ``cleared``, ``repaired``).

Anything unclear goes to ``ambiguities``; nothing is guessed.
"""

from __future__ import annotations

import json
import re
from typing import Any

import networkx as nx

from .graph import _fold, gazetteer, resolve
from .llm import call_validated
from .schemas import Convoy, Incident, IncidentType, LocationRef, MissionSpec, VehicleGroup


def parse_time(s: str) -> float:
    """``06:05`` or ``6h05`` -> minutes from 00:00 of D-day."""
    h, m = re.split(r"[:h]", s.strip())
    return int(h) * 60 + int(m or 0)


# ---------------------------------------------------------------------------
# L2: logistic order
# ---------------------------------------------------------------------------

ORDER_SYSTEM = (
    "You parse military logistic movement orders into a structured mission specification. Copy the commander's "
    "intent verbatim into 'intent'. Use only the known place names for locations. Times are minutes from 00:00. "
    "Put anything unclear in 'ambiguities' instead of guessing."
)


def parse_order(text: str, G: nx.DiGraph | None = None, catalogue: dict | None = None, llm=None) -> MissionSpec:
    if llm is not None:
        hint = {"vehicle_types": sorted(catalogue) if catalogue else [], "known_places": sorted(gazetteer(G))[:200] if G is not None else []}

        def validate(out):
            try:
                spec = MissionSpec.model_validate(out)
            except Exception as exc:  # noqa: BLE001
                return None, [f"schema: {exc}"]
            return spec, _order_problems(spec, G, catalogue, text)

        spec, problems = call_validated(llm, ORDER_SYSTEM, json.dumps(hint) + "\n\nOrder:\n" + text, MissionSpec.model_json_schema(),
                                        "order", validate)
        if spec is not None:
            return spec
        fallback = _pattern_order(text, G, catalogue)
        fallback.ambiguities.append("order parsing by the language model rejected (" + "; ".join(problems[:3]) + "); pattern parser used")
        return fallback
    return _pattern_order(text, G, catalogue)


def _order_problems(spec: MissionSpec, G: nx.DiGraph | None, catalogue: dict | None, text: str) -> list[str]:
    problems = []
    if not spec.convoys:
        problems.append("no convoy")
    for c in spec.convoys:
        if catalogue:
            problems += [f"{c.id}: unknown vehicle type {v.type!r}" for v in c.vehicles if v.type not in catalogue]
        if G is not None:
            for loc in [c.origin, c.destination, *c.via]:
                try:
                    resolve(G, loc)
                except KeyError as exc:
                    problems.append(f"{c.id}: {exc}")
        if c.latest_arrival_min is not None and c.latest_arrival_min <= c.earliest_departure_min:
            problems.append(f"{c.id}: arrival deadline before departure")
    if spec.intent and " ".join(spec.intent.split()).lower() not in " ".join(text.split()).lower():
        problems.append("intent is not a verbatim copy of the order")
    return problems


_CONVOY_RE = re.compile(
    r"(?:convoy|convoi)\s+(?P<id>[\w-]+)\s*:\s*(?P<veh>.+?)\s+(?:from|de|depuis)\s+(?P<orig>.+?)\s+(?:to|à|vers)\s+(?P<dest>[^,.;]+)(?P<rest>[^\n]*)",
    re.I,
)
_VIA_RE = re.compile(r"\s+via\s+(.+)$", re.I)


def _pattern_order(text: str, G: nx.DiGraph | None = None, catalogue: dict | None = None) -> MissionSpec:
    phase = "night" if re.search(r"phase\s*:\s*(night|nuit)|\bnight move|\bde nuit\b", text, re.I) else "day"
    m = re.search(r"(?:order|ordre)\s+([\w-]+)", text, re.I)
    intent = re.search(r"^\s*(?:intent|intention)\s*:\s*(.+)$", text, re.I | re.M)
    convoys, amb, avoid = [], [], []
    for cm in _CONVOY_RE.finditer(text):
        rest = cm.group("rest")
        vehicles = [VehicleGroup(count=int(n), type=t.lower()) for n, t in re.findall(r"(\d+)\s*x\s*([\w-]+)", cm.group("veh"))]
        if not vehicles:
            amb.append(f"convoy {cm.group('id')}: no vehicles recognised in {cm.group('veh')!r}")
            continue
        dest = cm.group("dest").strip()
        via = []
        vm = _VIA_RE.search(dest)
        if vm:
            via = [LocationRef(name=v.strip()) for v in re.split(r"\s+(?:and|then|et)\s+|,", vm.group(1)) if v.strip()]
            dest = dest[: vm.start()].strip()
        dep = re.search(r"(?:depart\w*|départ)\s*(?:at\s*|à\s*)?(\d{1,2}[:h]\d{2})", rest, re.I)
        arr = re.search(r"(?:arriv\w*)\s*(?:before|by|no later than|NLT|avant)\s*(\d{1,2}[:h]\d{2})", rest, re.I)
        prio = re.search(r"(?:priority|priorité)\s*(\d)", rest, re.I)
        cargo = re.search(r"(?:cargo|chargement)\s*:?\s*([\w ,]+)", rest, re.I)
        t_dep = parse_time(dep.group(1)) if dep else 0.0
        t_arr = parse_time(arr.group(1)) if arr else None
        if t_arr is not None and t_arr <= t_dep:
            t_arr += 24 * 60  # the deadline falls after midnight
        convoys.append(Convoy(
            id=cm.group("id"),
            vehicles=vehicles,
            origin=LocationRef(name=cm.group("orig").strip()),
            destination=LocationRef(name=dest),
            via=via,
            earliest_departure_min=t_dep,
            latest_arrival_min=t_arr,
            priority=int(prio.group(1)) if prio else 1,
            cargo=[c.strip().lower() for c in cargo.group(1).split(",") if c.strip()] if cargo else [],
        ))
    for am in re.finditer(r"(?:^|[.;]\s*)(?i:avoid|do not (?:cross|enter|use)|never (?:cross|enter))\s+([A-Z][\w-]*(?:[ -][A-Z][\w-]*)*)", text, re.M):
        name = am.group(1).strip()
        if G is not None:
            try:
                resolve(G, LocationRef(name=name))
            except KeyError:
                amb.append(f"prohibited place {name!r} not found on the map")
                continue
        avoid.append(LocationRef(name=name))
    spec = MissionSpec(id=m.group(1) if m else "mission", phase=phase, convoys=convoys, ambiguities=amb,
                       intent=intent.group(1).strip() if intent else "", avoid=avoid)
    if catalogue:
        for c in spec.convoys:
            spec.ambiguities += [f"{c.id}: unknown vehicle type {v.type!r}" for v in c.vehicles if v.type not in catalogue]
    return spec


# ---------------------------------------------------------------------------
# L6: incident report
# ---------------------------------------------------------------------------

INCIDENT_SYSTEM = (
    "You turn military incident reports into structured records with the source grading of the Admiralty code "
    "(reliability A-F, credibility 1-6). Use a known place name for the location; never invent coordinates. "
    "Use credibility 1 only when the report says the fact is confirmed."
)

_INCIDENT_KEYWORDS = [
    (IncidentType.RECCE_RESULT, r"classif\w*[^.]*mlc|recce[^.]*(mlc|clearance)|reconnaissance[^.]*(mlc|clearance|classe)|mlc\s*\d+[^.]*(confirmed|classified)"),
    (IncidentType.CLEARED, r"reopen|re-open|cleared|repaired|open again|rouvert|dégagé"),
    (IncidentType.BRIDGE_DESTROYED, r"bridge[^.]*(destroyed|down|collapsed|damaged)|pont[^.]*(détruit|detruit|effondré|endommagé)"),
    (IncidentType.BREAKDOWN, r"breakdown|broke down|broken down|panne"),
    (IncidentType.THREAT_ZONE, r"threat|enemy|ambush|ied|sniper|menace|ennemi|embuscade"),
    (IncidentType.WEATHER, r"flood|snow|ice|fog|mud|weather|inond|neige|verglas|brouillard|boue|météo"),
    (IncidentType.ROAD_BLOCKED, r"block|closed|crater|obstruct|bouch|coupée|fermée|barr|obstru"),
]


def grading_from_text(text: str) -> tuple[str, int]:
    """Admiralty grading stated in the report, or derived from its wording."""
    m = re.search(r"\b(?:graded|grading|cotation|coté)?\s*\(?\b([A-F])([1-6])\b\)?", text)
    if m and (re.search(r"graded|grading|cotation|coté", text, re.I) or re.search(r"\(\s*[A-F][1-6]\s*\)", text)):
        return m.group(1), int(m.group(2))
    low = text.lower()
    if re.search(r"unconfirmed|not confirmed|possible|reported|suspected|non confirmé|possible|signalé|suspect", low):
        return "B", 3
    if re.search(r"rumou?r|rumeur", low):
        return "C", 4
    if re.search(r"confirmed|confirmé|observed|observé", low):
        return "B", 1
    return "B", 2


def parse_incident(text: str, G: nx.DiGraph, incident_id: str = "I-1", time_min: float | None = None, llm=None) -> Incident:
    t = time_min
    if t is None:
        tm = re.search(r"\b(\d{1,2}[:h]\d{2})\b", text)
        t = parse_time(tm.group(1)) if tm else 0.0
    if llm is not None:
        def validate(out):
            if not isinstance(out, dict):
                return None, ["not an object"]
            try:
                inc = Incident.model_validate({**out, "id": incident_id, "time_min": t, "text": text})
            except Exception as exc:  # noqa: BLE001
                return None, [f"schema: {exc}"]
            problems = []
            if inc.location is not None:
                try:
                    resolve(G, inc.location)
                except KeyError as exc:
                    problems.append(str(exc))
            if inc.credibility == 1 and not re.search(r"confirm|graded\s*[A-F]1|\([A-F]1\)", text, re.I):
                problems.append("credibility 1 without a confirmation in the report")
            return inc, problems

        inc, _problems = call_validated(llm, INCIDENT_SYSTEM, json.dumps({"known_places": sorted(gazetteer(G))[:200]}) + "\n\nReport:\n" + text,
                                        Incident.model_json_schema(), "incident", validate)
        if inc is not None:
            return inc
    return _pattern_incident(text, G, incident_id, t)


def _pattern_incident(text: str, G: nx.DiGraph, incident_id: str, time_min: float) -> Incident:
    low = text.lower()
    itype = next((k for k, pat in _INCIDENT_KEYWORDS if re.search(pat, low)), IncidentType.ROAD_BLOCKED)
    loc: LocationRef | None = None
    m = re.search(r"\(\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*\)", text)
    if m:
        loc = LocationRef(xy=(float(m.group(1)), float(m.group(2))))
    else:
        folded = _fold(text)
        gz = gazetteer(G)
        names = sorted(gz, key=len, reverse=True)
        hit = next((n for n in names if re.search(rf"\b{re.escape(n)}\b", folded)), None)
        if hit:
            loc = LocationRef(name=G.nodes[gz[hit]].get("name") or hit)
    r = re.search(r"(\d+(?:\.\d+)?)\s*(km|m)\b\s*(?:radius|rayon)|(?:radius|rayon)\s*(?:of\s*|de\s*)?(\d+(?:\.\d+)?)\s*(km|m)\b", low)
    radius = 300.0
    if r:
        val, unit = (r.group(1), r.group(2)) if r.group(1) else (r.group(3), r.group(4))
        radius = float(val) * (1000 if unit == "km" else 1)
    convoy = re.search(r"(?:convoy|convoi)\s+([\w-]+)", text, re.I)
    dur = re.search(r"(\d+)\s*(?:min|minutes)\b", low)
    sev = 0.9 if itype == IncidentType.THREAT_ZONE and re.search(r"high|confirmed|élevée|confirmée", low) else (
        0.6 if itype in (IncidentType.THREAT_ZONE, IncidentType.WEATHER) else 1.0)
    attrs: dict[str, Any] = {}
    if itype == IncidentType.RECCE_RESULT:
        mm = re.search(r"mlc\s*(\d+)", low)
        if mm:
            attrs["mlc"] = int(mm.group(1))
        hm = re.search(r"clearance\s*(?:of\s*)?(\d+(?:[.,]\d+)?)\s*m", low)
        if hm:
            attrs["max_height_m"] = float(hm.group(1).replace(",", "."))
        wm = re.search(r"width\s*(?:of\s*)?(\d+(?:[.,]\d+)?)\s*m", low)
        if wm:
            attrs["width_m"] = float(wm.group(1).replace(",", "."))
    rel, cred = grading_from_text(text)
    return Incident(id=incident_id, type=itype, location=loc, radius_m=radius, severity=sev,
                    duration_min=float(dur.group(1)) if dur and itype in (IncidentType.BREAKDOWN, IncidentType.ROAD_BLOCKED) else None,
                    time_min=time_min, convoy_id=convoy.group(1) if convoy else None, reliability=rel, credibility=cred,
                    attrs=attrs, text=text)


__all__ = ["grading_from_text", "parse_incident", "parse_order", "parse_time"]
