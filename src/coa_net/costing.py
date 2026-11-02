"""Per-group evaluation of roads and corridors (SEG and PRO requirements).

Every directed road edge is evaluated once per convoy group, three-valued:
a requirement holds, is violated, or cannot be decided because a map
attribute is missing. An undecided requirement follows its instance's
unknown policy: ``forbid`` and ``recce`` make the road unusable (``recce``
also lists it for reconnaissance), ``risk`` keeps it usable and records the
unknown. Corridor (arc) values are sums or extremes over their edges, so no
information is lost by contraction.

Travel time model:
speed = min(road speed, march speed cap) x night factor x (1 - 0.5 weather).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .corridors import Arc, CorridorGraph
from .graph import edge_midpoint, point_distance, speed_kmh, ukey
from .requirements import ActiveSet
from .vehicles import Profile

DAY_INTERVAL_M = 50.0  # default vehicle interval, used when no SCH.MARCH instance sets one


@dataclass
class EdgeEval:
    ok: bool
    time_min: float
    length_m: float
    threat: float
    builtup: bool
    open: bool
    pref: bool  # a T3 preference advises against this road
    unknown: list[tuple[str, str]] = field(default_factory=list)  # (requirement id, detail), policy "risk"
    blocked: list[tuple[str, str, str]] = field(default_factory=list)  # (requirement id, tier, detail)
    recce: list[tuple[str, str]] = field(default_factory=list)  # unknowns that block until reconnoitred


@dataclass
class ArcEval:
    ok: bool
    time_min: float
    length_m: float
    exposure: float  # km-equivalent: sum of length (km) x threat
    builtup_m: float
    open_m: float
    pref_m: float
    unknown_m: float
    max_threat: float
    blocked: list[tuple[str, str, str]]  # first violations, for explanations
    recce: list[tuple[str, str]]


class GroupCoster:
    """Evaluates the road and corridor graphs for one convoy group."""

    def __init__(
        self,
        H: CorridorGraph,
        profile: Profile,
        active: ActiveSet,
        phase: str = "day",
        avoid_zones: list[tuple[str, tuple[float, float], float]] | None = None,
        overrides: dict[tuple[Any, Any], dict] | None = None,
    ):
        self.H = H
        self.G = H.G
        self.profile = profile
        self.active = active
        self.phase = phase
        self.avoid_zones = avoid_zones or []  # (name, xy, radius_m) from RTE.ACCESS
        self.overrides = overrides or {}  # ukey -> attribute overrides (what-if, e.g. a recce answer)
        cap = active.param("SCH.MARCH", "max_speed_kmh", None)
        self.speed_cap = min(profile.max_speed_kmh, cap) if cap else profile.max_speed_kmh
        self.speed_factor = active.param("SCH.MARCH", "night_speed_factor", 1.0) if phase == "night" else 1.0
        key = "interval_night_m" if phase == "night" else "interval_day_m"
        self.interval_m = active.param("SCH.MARCH", key, DAY_INTERVAL_M)
        self.evals: dict[tuple[Any, Any], EdgeEval] = {}
        self.arcs: dict[Arc, ArcEval] = {}
        self.risk_elements: dict[str, list[tuple[str, str]]] = {}
        self.recompute()

    # ------------------------------------------------------------------ edges
    def attrs(self, u: Any, v: Any) -> dict:
        d = self.G[u][v]
        o = self.overrides.get(ukey(u, v))
        return {**d, **o} if o else d

    def edge_speed(self, d: dict) -> float:
        s = min(speed_kmh(d), self.speed_cap) * self.speed_factor
        return max(1.0, s * (1.0 - 0.5 * d.get("weather", 0.0)))

    def evaluate(self, u: Any, v: Any) -> EdgeEval:
        d = self.attrs(u, v)
        p = self.profile
        A = self.active
        blocked: list[tuple[str, str, str]] = []
        unknown: list[tuple[str, str]] = []
        recce: list[tuple[str, str]] = []

        def undecided(r, detail: str, policy: str | None = None) -> None:
            pol = policy or r.on_unknown
            if pol == "risk":
                unknown.append((r.id, detail))
            else:
                blocked.append((r.id, r.tier, detail + (" (to reconnoitre)" if pol == "recce" else " (unknown, forbidden)")))
                recce.append((r.id, detail))

        for r in A.of("SEG.LOAD", ("T0", "T1")):
            check = r.p("check")
            if check in ("bridge_mlc", "both") and d.get("bridge"):
                need = p.mlc + r.p("margin_mlc")
                if d.get("mlc") is None:
                    undecided(r, "bridge load class unknown")
                elif d["mlc"] < need:
                    blocked.append((r.id, r.tier, f"bridge MLC {d['mlc']} < column MLC {need}"))
            if check in ("posted_weight", "both") and d.get("max_weight_t") is not None and d["max_weight_t"] < p.weight_t:
                blocked.append((r.id, r.tier, f"posted limit {d['max_weight_t']:g} t < {p.weight_t:g} t"))
        for r in A.of("SEG.GAUGE", ("T0", "T1")):
            check = r.p("check")
            if check in ("width", "both"):
                need = p.width_m + r.p("width_margin_m")
                if d.get("width_m") is None:
                    undecided(r, "width unknown")
                elif d["width_m"] < need:
                    blocked.append((r.id, r.tier, f"width {d['width_m']:g} m < {need:.2f} m"))
            if check in ("height", "both") and d.get("tunnel"):
                need = p.height_m + r.p("height_margin_m")
                if d.get("max_height_m") is None:
                    undecided(r, "tunnel clearance unknown", "forbid")
                elif d["max_height_m"] < need:
                    blocked.append((r.id, r.tier, f"clearance {d['max_height_m']:g} m < {need:.1f} m"))
        for r in A.of("SEG.GRADE", ("T0", "T1")):
            limit = r.params.get("max_slope_pct") or p.max_slope_pct
            if d.get("slope_pct") is None:
                undecided(r, "slope unknown")
            elif d["slope_pct"] > limit:
                blocked.append((r.id, r.tier, f"slope {d['slope_pct']:g} % > {limit:g} %"))
        for r in A.of("SEG.SURFACE", ("T0", "T1")):
            if set(r.p("vehicle_classes")) & p.vehicle_classes and d.get("road_rank", 5) >= r.p("min_track_rank") and d.get("paved") is not True:
                if d.get("paved") is None:
                    undecided(r, "surface unknown on a track")
                else:
                    blocked.append((r.id, r.tier, "unpaved track"))
        for r in A.of("SEG.STATUS"):
            if d.get("closed") or d.get("closed_p", 0.0) >= r.p("closed_p"):
                blocked.append((r.id, "T0", f"closed (P={max(d.get('closed_p', 0.0), 1.0 if d.get('closed') else 0.0):.2f})"))
        for r in A.of("SEG.CARGO", ("T1",)):
            el = r.p("element")
            if (el == "tunnel" and d.get("tunnel")) or (el == "built_up" and d.get("built_up")):
                blocked.append((r.id, r.tier, f"{'/'.join(sorted(p.cargo))} in {el.replace('_', '-')}"))
        for r in A.of("SEG.AVOID", ("T1",)):
            if self._has(d, r.p("attribute"), r.p("min_road_rank")):
                blocked.append((r.id, r.tier, f"{r.p('attribute')} prohibited"))
        for r in A.of("PRO.EXPOSURE", ("T1",)):
            if d.get("threat", 0.0) >= r.p("max_threat"):
                blocked.append((r.id, r.tier, f"threat {d['threat']:.2f} >= {r.p('max_threat')}"))
        if self.avoid_zones:
            mid = edge_midpoint(self.G, u, v)
            for name, xy, radius in self.avoid_zones:
                if point_distance(self.G, mid, xy) <= radius:
                    rid = next(iter(A.ids("RTE.ACCESS")), "ORDER")
                    blocked.append((rid, "T1", f"prohibited area {name}"))
                    break

        pref = False
        for r in A.of("SEG.GRADE", ("T3",)):
            if d.get("slope_pct") is not None and d["slope_pct"] > r.p("prefer_max_slope_pct"):
                pref = True
        for r in A.of("SEG.AVOID", ("T3",)):
            if r.p("attribute") not in ("open", "built_up") and self._has(d, r.p("attribute"), r.p("min_road_rank")):
                pref = True
        if unknown:
            pref = True
        t = d["length_m"] / 1000.0 / self.edge_speed(d) * 60.0
        return EdgeEval(
            ok=not blocked,
            time_min=t,
            length_m=d["length_m"],
            threat=d.get("threat", 0.0),
            builtup=bool(d.get("built_up")),
            open=not d.get("forest"),
            pref=pref,
            unknown=unknown,
            blocked=blocked,
            recce=recce,
        )

    def _has(self, d: dict, attribute: str, min_rank: int = 5) -> bool:
        if attribute == "built_up":
            return bool(d.get("built_up"))
        if attribute == "forest":
            return bool(d.get("forest"))
        if attribute == "open":
            return not d.get("forest")
        if attribute == "minor_road":
            return d.get("road_rank", 5) >= min_rank
        if attribute == "unlit":
            return self.phase == "night" and d.get("lit") is False
        return bool(d.get(attribute))

    # ------------------------------------------------------------------ arcs
    def recompute(self, edges: list[tuple[Any, Any]] | None = None) -> None:
        if edges is None:
            todo = list(self.G.edges)
        else:
            todo = [e for e in edges if self.G.has_edge(*e)]
        for u, v in todo:
            self.evals[(u, v)] = self.evaluate(u, v)
        cids = {self.H.edge_corridor.get(ukey(u, v)) for u, v in todo} if edges is not None else set(self.H.corridors)
        for cid in cids:
            if cid is None:
                continue
            k = self.H.corridors[cid]
            for dirn, ok in ((1, k.fwd), (-1, k.bwd)):
                if ok:
                    self.arcs[(cid, dirn)] = self._arc(cid, dirn)
        self.risk_elements = {}
        for (u, v), e in self.evals.items():
            if e.unknown:
                eid = self.H.edge_elements.get(ukey(u, v))
                if eid:
                    self.risk_elements.setdefault(eid, []).extend(e.unknown)

    def _arc(self, cid: str, dirn: int) -> ArcEval:
        edges = self.H.corridors[cid].arc_edges(dirn)
        es = [self.evals[e] for e in edges]
        blocked = [b for e in es for b in e.blocked]
        return ArcEval(
            ok=all(e.ok for e in es),
            time_min=sum(e.time_min for e in es),
            length_m=sum(e.length_m for e in es),
            exposure=sum(e.length_m / 1000.0 * e.threat for e in es),
            builtup_m=sum(e.length_m for e in es if e.builtup),
            open_m=sum(e.length_m for e in es if e.open),
            pref_m=sum(e.length_m for e in es if e.pref),
            unknown_m=sum(e.length_m for e in es if e.unknown),
            max_threat=max((e.threat for e in es), default=0.0),
            blocked=blocked[:3],
            recce=[r for e in es for r in e.recce],
        )

    def arc_ok(self, arc: Arc) -> bool:
        a = self.arcs.get(arc)
        return bool(a and a.ok)

    def column_length_m(self) -> float:
        return self.profile.column_length_m(self.interval_m)

    def pass_min(self, arc: Arc, n_vehicle_lengths: list[float] | None = None) -> float:
        """Minutes the whole column needs to pass a point of the arc at the arc's mean speed."""
        a = self.arcs[arc]
        speed = a.length_m / 1000.0 / max(a.time_min / 60.0, 1e-9)
        lengths = n_vehicle_lengths if n_vehicle_lengths is not None else self.profile.vehicle_lengths
        L = sum(lengths) + max(0, len(lengths) - 1) * self.interval_m
        return 60.0 * L / 1000.0 / max(speed, 1.0)


__all__ = ["ArcEval", "DAY_INTERVAL_M", "EdgeEval", "GroupCoster"]
