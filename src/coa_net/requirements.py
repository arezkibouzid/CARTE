"""Doctrine as prioritised requirements (methodology section 4, Appendix A).

A requirement is an *instance* of one of 26 templates from a closed catalogue.
Each template belongs to one of five levels, which decides where it is
enforced:

========  ==========================  =====================================
level     checked on                  enforced in
========  ==========================  =====================================
SEG       each road edge              feasibility masks (costing), checker
RTE       one whole route             candidate generation, checker
NET       the set of designated       network design model, refinement
          routes
SCH       the timetable               movement-credit scheduler
PRO       exposure, critical points   masks, objectives, placement, waits
========  ==========================  =====================================

Tiers: T0 physical (filter, never relaxed), T1 "shall" (constraint, relaxed
only by an explicit waiver), T2 "should" (objective or "where an alternative
exists"), T3 efficiency (objective, freely traded).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, model_validator

Tier = Literal["T0", "T1", "T2", "T3"]
LEVELS = ("SEG", "RTE", "NET", "SCH", "PRO")


@dataclass(frozen=True)
class Param:
    name: str
    unit: str
    default: Any
    lo: float | None = None
    hi: float | None = None
    stricter: str | None = None  # "max" (larger is stricter), "min" (smaller is stricter) or None


@dataclass(frozen=True)
class Template:
    id: str
    level: str
    tiers: tuple[str, ...]  # allowed tiers, the first one is typical
    params: tuple[Param, ...]
    enforced_by: str
    unknown_default: str  # forbid | risk | recce | n/a
    d2c: str  # D2C-Route counterpart
    description: str
    seeds: tuple[str, ...] = ()  # seed phrases for retrieval (EN and FR)

    def param(self, name: str) -> Param:
        for p in self.params:
            if p.name == name:
                return p
        raise KeyError(f"{self.id} has no parameter {name!r}")


ALL_KINDS = ["bridge", "tunnel", "level_crossing", "ford", "ferry", "defile", "articulation"]

TEMPLATES: dict[str, Template] = {
    t.id: t
    for t in [
        Template("SEG.LOAD", "SEG", ("T0", "T1"),
                 (Param("check", "", "both"), Param("margin_mlc", "MLC", 0, 0, 50, "max")),
                 "mask, checker", "forbid on bridges, risk elsewhere", "H-BRIDGE-MLC, H-WEIGHT",
                 "Column MLC or weight against the element's load class or posted limit, with a margin.",
                 ("load class", "MLC", "bridge classification", "weight limit", "classe militaire", "limitation de tonnage")),
        Template("SEG.GAUGE", "SEG", ("T0", "T1"),
                 (Param("check", "", "both"), Param("width_margin_m", "m", 0.5, 0, 3, "max"), Param("height_margin_m", "m", 0.3, 0, 2, "max")),
                 "mask, checker", "forbid in tunnels and underpasses, risk for widths", "H-CLEARANCE, H-WIDTH",
                 "Width and height of the widest and tallest vehicle, with margins, against the road and its clearances.",
                 ("clearance", "carriageway width", "underpass", "gabarit", "hauteur libre", "largeur de chaussée")),
        Template("SEG.GRADE", "SEG", ("T0", "T1", "T3"),
                 (Param("max_slope_pct", "%", None, 0, 60, "min"), Param("prefer_max_slope_pct", "%", 8.0, 0, 60, "min")),
                 "mask (limit); objective preferences (T3)", "risk", "H-SLOPE, S-SLOPE",
                 "Maximum slope per profile (limit); gentle gradients preferred (T3).",
                 ("gradient", "slope", "climb", "pente", "déclivité")),
        Template("SEG.SURFACE", "SEG", ("T0", "T1", "T2"),
                 (Param("vehicle_classes", "", ["heavy"]), Param("min_track_rank", "rank", 6, 0, 6), Param("weather_threshold", "", 0.5, 0, 1)),
                 "mask; route type X/Y/Z by weather", "risk", "H-HEAVY-UNPAVED, S-WEATHER",
                 "Surfaces allowed per profile; degraded surfaces in bad weather.",
                 ("unpaved", "track", "surface", "piste", "revêtement", "chemin de terre")),
        Template("SEG.STATUS", "SEG", ("T0",),
                 (Param("closed_p", "", 0.5, 0.05, 1.0, "min"), Param("suspect_p", "", 0.2, 0.0, 1.0, "min"), Param("half_life_min", "min", 60, 1, 1440)),
                 "mask from the belief model", "n/a", "H-CLOSED",
                 "Closure from the belief model: closed at P >= closed_p, suspect from suspect_p.",
                 ("closed", "destroyed", "impassable", "fermé", "détruit", "coupé")),
        Template("SEG.CARGO", "SEG", ("T1", "T2"),
                 (Param("cargo", "", ["fuel", "ammunition"]), Param("element", "", "tunnel")),
                 "mask (tunnels); objective hazmat_builtup", "forbid in tunnels", "H-HAZMAT-TUNNEL, S-HAZMAT-BUILTUP",
                 "Dangerous goods kept out of tunnels or built-up areas.",
                 ("dangerous goods", "fuel", "ammunition", "matières dangereuses", "carburant", "munitions")),
        Template("SEG.AVOID", "SEG", ("T1", "T2", "T3"),
                 (Param("attribute", "", "built_up"), Param("min_road_rank", "rank", 5, 0, 6)),
                 "mask (T1); objectives builtup, cover, preferences", "risk", "S-BUILTUP, S-ROAD-CLASS, S-COVER",
                 "Attribute to avoid: built_up, forest, open (no cover), minor_road, unlit.",
                 ("avoid", "bypass", "prefer major roads", "cover", "éviter", "contourner", "agglomération")),
        Template("RTE.GAP_MAX", "RTE", ("T1", "T2"),
                 (Param("max_drive_min", "min", 120, 15, 600, "min"), Param("halt_place", "", "halt_area")),
                 "path resource (candidates)", "risk", "T-HALT",
                 "Maximum driving time between halts, halts at halt areas.",
                 ("halt", "every two hours", "rest", "halte", "pause", "toutes les deux heures")),
        Template("RTE.TURN", "RTE", ("T0", "T1"),
                 (Param("max_turn_deg", "deg", 120, 30, 180, "min"), Param("min_vehicle_length_m", "m", 15, 0, 40)),
                 "turn-expanded search (candidates)", "risk", "none",
                 "No turn sharper than a long vehicle can take.",
                 ("turning radius", "sharp turn", "hairpin", "rayon de braquage", "virage")),
        Template("RTE.CROSSINGS_MAX", "RTE", ("T1", "T2"),
                 (Param("element", "", "built_up"), Param("max_count", "", 1, 0, 20, "min")),
                 "path resource (candidates); design", "risk", "none",
                 "Maximum number of crossings of an element class (towns, level crossings) per route.",
                 ("no more than one", "cross at most", "traversée", "au plus une agglomération")),
        Template("RTE.DETOUR", "RTE", ("T1",),
                 (Param("rho", "x", 1.8, 1.0, 5.0, "min"),),
                 "design (guard)", "n/a", "N-ALTERNATE (max_detour)",
                 "An alternate route takes at most rho times the main route's travel time.",
                 ("alternate route", "detour", "itinéraire de délestage", "détour")),
        Template("RTE.ACCESS", "RTE", ("T1",),
                 (Param("avoid", "", []), Param("via", "", []), Param("radius_m", "m", 800, 0, 20000)),
                 "forcing and removal (candidates)", "n/a", "none",
                 "Start and release points, imposed via-points, prohibited areas from the order.",
                 ("start point", "release point", "via", "do not cross", "point de départ", "interdit")),
        Template("RTE.RANGE", "RTE", ("T1",),
                 (Param("max_km", "km", 300, 10, 2000, "min"),),
                 "path resource (candidates)", "risk", "none",
                 "Maximum distance between fuel or maintenance points.",
                 ("range", "refuel", "autonomie", "ravitaillement")),
        Template("NET.SURVIVE", "NET", ("T1", "T2"),
                 (Param("k", "", 1, 1, 2), Param("element_kinds", "", ALL_KINDS), Param("max_alternates", "", 2, 1, 4)),
                 "design (survivability constraint)", "elements of unknown status count as critical", "N-ALTERNATE (overlap), S-CHOKEPOINT",
                 "Every group keeps a designated route after the loss of any single critical element.",
                 ("alternate supply route", "single point of failure", "remain usable", "itinéraire de secours", "point de passage obligé")),
        Template("NET.SEPARATE", "NET", ("T2", "T1"),
                 (Param("narrow_margin_m", "m", 0.5, 0, 3),),
                 "design", "n/a", "none",
                 "Separate opposite flows on narrow roads.",
                 ("separate", "outbound and return", "one-way circuit", "sens unique", "séparer les flux")),
        Template("NET.LATERAL", "NET", ("T2",),
                 (Param("max_spacing_km", "km", 10, 1, 100, "min"), Param("link_max_km", "km", 3, 0.5, 20)),
                 "checker", "n/a", "none",
                 "A lateral link at least every D km between a main route and its alternate.",
                 ("lateral", "cross-link", "rocade", "liaison transversale")),
        Template("NET.CONTROL", "NET", ("T2",),
                 (Param("at_elements", "", True), Param("at_junctions", "", True), Param("max_spacing_km", "km", 15, 1, 200, "min")),
                 "placement (refinement)", "n/a", "none",
                 "Traffic control posts at critical elements, at splits and merges, with a maximum spacing.",
                 ("traffic control post", "control point", "poste de contrôle", "point de contrôle")),
        Template("NET.SIZE", "NET", ("T3", "T1"),
                 (Param("max_km", "km", None, 1, 10000, "min"),),
                 "objective network_size; cap (T1)", "n/a", "none",
                 "Designated length or number of routes: minimised (T3) or capped (T1).",
                 ("number of routes", "network size", "nombre d'itinéraires")),
        Template("NET.STATUS", "NET", ("T2",),
                 (Param("dispatch_if", "", ["heavy", "hazmat"]),),
                 "refinement", "n/a", "none",
                 "Route status (dispatch, supervised, reserved, open, prohibited) from route use.",
                 ("dispatch route", "supervised route", "itinéraire réservé", "itinéraire contrôlé")),
        Template("SCH.HEADWAY", "SCH", ("T1",),
                 (Param("headway_min", "min", 10, 0, 120, "max"), Param("meet_on_two_lane", "", True)),
                 "scheduler", "n/a", "N-HEADWAY",
                 "Time gap between successive columns, by direction; opposite columns meet only on two-lane roads.",
                 ("headway", "time gap", "interval between convoys", "intervalle", "créneau")),
        Template("SCH.MARCH", "SCH", ("T1",),
                 (Param("max_speed_kmh", "km/h", None, 5, 130, "min"), Param("night_speed_factor", "", None, 0.1, 1.0, "min"),
                  Param("interval_day_m", "m", None, 5, 500, "max"), Param("interval_night_m", "m", None, 5, 500, "max"),
                  Param("max_unit_vehicles", "", None, 2, 100, "min")),
                 "column model, scheduler", "n/a", "T-CONVOY-SPEED, T-HEAVY-SPEED, T-NIGHT",
                 "March speed per profile, vehicle interval by phase, night factor, maximum march-unit size.",
                 ("march speed", "vehicle interval", "blackout", "vitesse de marche", "intervalle entre véhicules")),
        Template("SCH.WINDOW", "SCH", ("T2", "T1"),
                 (Param("area", "", "built_up"), Param("start_min", "min", 450, 0, 2880), Param("end_min", "min", 540, 0, 2880)),
                 "scheduler", "n/a", "none",
                 "Forbidden time windows for areas or routes (for example towns at rush hour).",
                 ("between", "rush hour", "not before", "heures de pointe", "créneau horaire")),
        Template("SCH.HALT", "SCH", ("T1",),
                 (Param("halt_min", "min", 15, 0, 240, "max"), Param("halt_areas_only", "", True)),
                 "scheduler (capacity)", "risk (unknown capacity)", "T-HALT",
                 "Halt duration, at halt areas only, within their capacity.",
                 ("halt area", "aire de stationnement", "halte")),
        Template("SCH.VARY", "SCH", ("T2",),
                 (Param("min_change_share", "", 0.3, 0, 1, "max"),),
                 "checker (rotations)", "n/a", "none",
                 "Minimum share of route or timing changed between rotations.",
                 ("vary routes", "routine", "varier les itinéraires")),
        Template("PRO.EXPOSURE", "PRO", ("T1", "T2"),
                 (Param("max_threat", "", 0.8, 0, 1, "min"),),
                 "mask (limit); objective threat_exposure", "risk", "H-THREAT, S-THREAT",
                 "Maximum threat level per road (T1); exposure minimised (T2).",
                 ("threat", "exposure", "menace", "exposition")),
        Template("PRO.CRITPOINT", "PRO", ("T1", "T2"),
                 (Param("no_stop_within_m", "m", 800, 0, 5000, "max"), Param("element_kinds", "", ["bridge", "tunnel"]),
                  Param("post_if_p_ge", "", 0.2, 0, 1, "min")),
                 "forbidden waits (scheduler), placement (refinement)", "n/a", "none",
                 "No halt or wait near a critical element; control post where a strike is likely.",
                 ("do not halt", "near a bridge", "ne pas stationner", "à proximité d'un pont")),
    ]
}

assert len(TEMPLATES) == 26, len(TEMPLATES)

# ---------------------------------------------------------------------------
# Trigger words and tiers (section 4.3)
# ---------------------------------------------------------------------------

TRIGGERS_T1 = (
    "shall", "must", "is to", "are to", "is prohibited", "are prohibited", "prohibited", "require", "requires",
    "required", "may not", "may only", "will not", "is made", "is reduced", "is kept", "are kept", "is capped",
    "need", "needs",
    "doit", "doivent", "interdit", "impératif", "obligatoire", "est tenu",
)
TRIGGERS_T2 = (
    "should", "as far as possible", "wherever possible", "wherever an alternative exists", "where possible",
    "preferably", "normally", "recommended", "de préférence", "dans la mesure du possible", "il convient", "devrait",
)
TRIGGERS_T3 = ("prefer", "may", "can")


def tier_from_trigger(trigger: str) -> str | None:
    t = trigger.lower().strip()
    if not t:
        return None
    if any(re.search(rf"\b{re.escape(w)}\b", t) for w in TRIGGERS_T2):
        return "T2"
    if any(re.search(rf"\b{re.escape(w)}\b", t) for w in TRIGGERS_T1):
        return "T1"
    if any(re.search(rf"\b{re.escape(w)}\b", t) for w in TRIGGERS_T3):
        return "T3"
    return None


def find_trigger(sentence: str) -> str:
    """The modal phrase of a sentence (the first T2 phrase wins over T1, as 'should not' is weaker than 'shall')."""
    s = sentence.lower()
    for words in (TRIGGERS_T2, TRIGGERS_T1):
        hits = [(m.start(), w) for w in words for m in [re.search(rf"\b{re.escape(w)}\b", s)] if m]
        if hits:
            return min(hits)[1]
    return ""


def tier_consistent(tier: str, trigger: str, template: str) -> bool:
    """Tier must agree with the trigger words; T0 templates accept mandatory wording."""
    expected = tier_from_trigger(trigger)
    if expected is None:
        return False
    if tier == expected:
        return True
    return tier == "T0" and expected == "T1" and "T0" in TEMPLATES[template].tiers


# ---------------------------------------------------------------------------
# Requirement records
# ---------------------------------------------------------------------------


class Scope(BaseModel):
    """When a requirement applies. Empty lists mean "any"."""

    convoys: list[str] = Field(default_factory=list)
    vehicle_classes: list[str] = Field(default_factory=list)
    cargo: list[str] = Field(default_factory=list)
    phases: list[Literal["day", "night"]] = Field(default_factory=list)

    def matches(self, convoys: Iterable[str], vehicle_classes: set[str], cargo: set[str], phase: str) -> bool:
        if self.convoys and not (set(self.convoys) & set(convoys)):
            return False
        if self.vehicle_classes and not (set(self.vehicle_classes) & vehicle_classes):
            return False
        if self.cargo and not (set(self.cargo) & cargo):
            return False
        if self.phases and phase not in self.phases:
            return False
        return True


class Source(BaseModel):
    document: str = "seed base"
    paragraph: str = ""
    quote: str = ""


class Requirement(BaseModel):
    id: str
    template: str
    tier: Tier
    params: dict[str, Any] = Field(default_factory=dict)
    scope: Scope = Field(default_factory=Scope)
    on_unknown: Literal["forbid", "risk", "recce"] = "risk"
    source: Source = Field(default_factory=Source)
    trigger: str = ""
    explanation: str = ""
    status: Literal["draft", "approved", "rejected"] = "approved"
    assumption: bool = False  # value not taken from doctrine: to confirm with the instructors

    @model_validator(mode="after")
    def _check(self) -> "Requirement":
        t = TEMPLATES.get(self.template)
        if t is None:
            raise ValueError(f"unknown template {self.template!r}")
        if self.tier not in t.tiers:
            raise ValueError(f"{self.id}: tier {self.tier} not allowed for {self.template} (allowed {t.tiers})")
        known = {p.name for p in t.params}
        extra = set(self.params) - known
        if extra:
            raise ValueError(f"{self.id}: unknown parameters {sorted(extra)} for {self.template}")
        for p in t.params:
            v = self.params.get(p.name, p.default)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v is not None:
                if (p.lo is not None and v < p.lo) or (p.hi is not None and v > p.hi):
                    raise ValueError(f"{self.id}: {p.name}={v} outside [{p.lo}, {p.hi}] {p.unit}")
        return self

    @property
    def level(self) -> str:
        return TEMPLATES[self.template].level

    def p(self, name: str) -> Any:
        """Parameter value, or the template default."""
        if name in self.params and self.params[name] is not None:
            return self.params[name]
        return TEMPLATES[self.template].param(name).default

    def short(self) -> str:
        return f"{self.id} {self.template} ({self.tier})"


def load_requirements(path: str | Path | None = None) -> list[Requirement]:
    """The approved requirement base (default: the seed base shipped with the package)."""
    if path is None:
        raw = json.loads(resources.files("coa_net").joinpath("data", "seed_requirements.json").read_text(encoding="utf-8"))
    else:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Requirement.model_validate(r) for r in raw]


def save_requirements(reqs: list[Requirement], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps([r.model_dump(mode="json", exclude_defaults=False) for r in reqs], indent=1, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Validation of compiled instances (S1, step 4)
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def validate_instance(req: Requirement, corpus: str) -> list[str]:
    """Reasons to reject a compiled instance (empty list = valid)."""
    problems = []
    if req.source.quote and _norm(req.source.quote) not in _norm(corpus):
        problems.append("quote not found verbatim in the corpus")
    if not req.source.quote:
        problems.append("no quote")
    if not tier_consistent(req.tier, req.trigger, req.template):
        problems.append(f"tier {req.tier} disagrees with trigger {req.trigger!r}")
    return problems


def merge_conflicts(reqs: list[Requirement]) -> tuple[list[Requirement], list[str]]:
    """Merge duplicates; on conflicting values for the same template and scope keep the stricter one (T1) and flag it."""
    out: list[Requirement] = []
    notes: list[str] = []
    index: dict[tuple, Requirement] = {}
    for r in reqs:
        key = (r.template, r.tier, r.scope.model_dump_json(), r.params.get("check"), r.params.get("element"), r.params.get("attribute"), r.params.get("area"))
        if key not in index:
            index[key] = r
            out.append(r)
            continue
        kept = index[key]
        if kept.params == r.params:
            notes.append(f"{r.id} duplicates {kept.id}: merged")
            continue
        t = TEMPLATES[r.template]
        merged = dict(kept.params)
        for p in t.params:
            a, b = kept.params.get(p.name), r.params.get(p.name)
            if a is None or b is None or a == b:
                merged[p.name] = a if a is not None else b
            elif p.stricter == "max":
                merged[p.name] = max(a, b)
            elif p.stricter == "min":
                merged[p.name] = min(a, b)
        notes.append(f"{r.id} conflicts with {kept.id} on {r.template}: kept the stricter values {merged}")
        kept.params = merged
    return out, notes


# ---------------------------------------------------------------------------
# Activation (S2, step 4)
# ---------------------------------------------------------------------------


@dataclass
class ActiveSet:
    """Requirements active for one group (or one mission), with merged parameters."""

    reqs: list[Requirement]
    by_template: dict[str, list[Requirement]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for r in self.reqs:
            self.by_template.setdefault(r.template, []).append(r)

    def of(self, template: str, tiers: Iterable[str] | None = None) -> list[Requirement]:
        rs = self.by_template.get(template, [])
        return [r for r in rs if tiers is None or r.tier in tiers]

    def has(self, template: str, tiers: Iterable[str] | None = None) -> bool:
        return bool(self.of(template, tiers))

    def param(self, template: str, name: str, default: Any = None, tiers: Iterable[str] | None = None) -> Any:
        """Merged parameter over active instances: the stricter value wins."""
        vals = [r.params[name] for r in self.of(template, tiers) if r.params.get(name) is not None]
        if not vals:
            return default
        p = TEMPLATES[template].param(name)
        if p.stricter == "max":
            return max(vals)
        if p.stricter == "min":
            return min(vals)
        return vals[0]

    def ids(self, template: str) -> list[str]:
        return [r.id for r in self.of(template)]


def activate(reqs: list[Requirement], convoys: Iterable[str], vehicle_classes: set[str], cargo: set[str], phase: str) -> ActiveSet:
    convoys = list(convoys)
    return ActiveSet([r for r in reqs if r.status == "approved" and r.scope.matches(convoys, vehicle_classes, cargo, phase)])


def catalogue_markdown() -> str:
    lines = ["| Template | Level | Tiers | Parameters (default) | Enforced by | Unknowns | D2C-Route counterpart |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for t in TEMPLATES.values():
        ps = ", ".join(f"{p.name}={p.default}{(' ' + p.unit) if p.unit and p.default is not None else ''}" for p in t.params)
        lines.append(f"| {t.id} | {t.level} | {'/'.join(t.tiers)} | {ps} | {t.enforced_by} | {t.unknown_default} | {t.d2c} |")
    return "\n".join(lines)


__all__ = [
    "ALL_KINDS",
    "ActiveSet",
    "LEVELS",
    "Param",
    "Requirement",
    "Scope",
    "Source",
    "TEMPLATES",
    "Template",
    "activate",
    "catalogue_markdown",
    "find_trigger",
    "load_requirements",
    "merge_conflicts",
    "save_requirements",
    "tier_consistent",
    "tier_from_trigger",
    "validate_instance",
]
