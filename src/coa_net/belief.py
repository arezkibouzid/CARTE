"""Belief model for the dynamic state (methodology 3.6).

Every report carries NATO's source-reliability and information-credibility
grading (the Admiralty code, A-F and 1-6). A lookup table turns the grading
into the probability q_r that the report is true:

    q = 0.5 + (base(credibility) - 0.5) x factor(reliability)

so that A1 gives 0.95, B2 0.8, C3 0.5 and an ungraded report (F6) 0.5 (values
to calibrate with the instructors). Independent reports are fused by noisy-OR,
and unconfirmed reports decay with the half-life of SEG.STATUS:

    P_e = 1 - prod_r (1 - q_r),        q_r(t) = q_r 2^(-(t - t_r) / T_half)   (unconfirmed only)

Decision rule (tier 0): a road is closed if P_e >= 0.5; suspect if
0.2 <= P_e < 0.5, which keeps it usable with an exposure penalty and asks for
a confirmation; below 0.2 the report is only logged. A threat report raises the
threat level of the roads in its radius to severity x q, falling off with
distance as in D2C-Route. Confirmed reports (credibility 1) do not decay; they
stay until cleared or until their stated duration expires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from .graph import edge_midpoint, point_distance

CREDIBILITY_BASE = {1: 0.95, 2: 0.8, 3: 0.5, 4: 0.3, 5: 0.1, 6: 0.5}
RELIABILITY_FACTOR = {"A": 1.0, "B": 1.0, "C": 0.8, "D": 0.6, "E": 0.3, "F": 0.5}
TOL = 1e-9


def report_probability(reliability: str, credibility: int) -> float:
    """Probability that a report graded ``reliability``/``credibility`` is true."""
    base = CREDIBILITY_BASE.get(int(credibility), 0.5)
    return 0.5 + (base - 0.5) * RELIABILITY_FACTOR.get(str(reliability).upper(), 0.5)


@dataclass
class Report:
    id: str
    kind: str  # closure | threat | weather
    edges: list[tuple[Any, Any]]
    q: float
    time: float
    confirmed: bool
    severity: float = 1.0
    duration: float | None = None
    center: tuple[float, float] | None = None
    radius: float = 0.0
    grading: str = ""
    cleared_at: float | None = None


@dataclass
class BeliefState:
    G: nx.DiGraph
    half_life_min: float = 60.0
    closed_p: float = 0.5
    suspect_p: float = 0.2
    reports: list[Report] = field(default_factory=list)

    def add(self, report: Report) -> Report:
        self.reports.append(report)
        return report

    def clear(self, edges: list[tuple[Any, Any]], t: float) -> None:
        keys = set(edges) | {(v, u) for u, v in edges}
        for r in self.reports:
            if r.cleared_at is None and keys & set(r.edges):
                r.cleared_at = t

    def q_at(self, r: Report, t: float) -> float:
        if t < r.time or (r.cleared_at is not None and t >= r.cleared_at):
            return 0.0
        if r.duration is not None and t > r.time + r.duration:
            return 0.0
        if r.confirmed:
            return r.q
        return r.q * 2.0 ** (-(t - r.time) / max(self.half_life_min, 1e-9))

    def closure_p(self, edge: tuple[Any, Any], t: float) -> float:
        keep = 1.0
        for r in self.reports:
            if r.kind == "closure" and edge in r.edges:
                keep *= 1.0 - self.q_at(r, t)
        return 1.0 - keep

    def status(self, edge: tuple[Any, Any], t: float) -> str:
        p = self.closure_p(edge, t)
        if p >= self.closed_p - TOL:
            return "closed"
        if p >= self.suspect_p - TOL:
            return "suspect"
        return "logged" if p > 0 else "clear"

    def apply(self, t: float) -> list[tuple[Any, Any]]:
        """Write closed_p, closed, threat and weather of every reported road at time t; return the edges changed."""
        G = self.G
        touched = {e for r in self.reports for e in r.edges if G.has_edge(*e)}
        changed = []
        for u, v in touched:
            d = G[u][v]
            p = self.closure_p((u, v), t)
            threat = d.get("threat0", 0.0)
            weather = d.get("weather0", 0.0)
            for r in self.reports:
                if (u, v) not in r.edges:
                    continue
                q = self.q_at(r, t)
                if r.kind == "threat" and q > 0:
                    dist = point_distance(G, edge_midpoint(G, u, v), r.center) if r.center else 0.0
                    threat = max(threat, r.severity * q * (1.0 - 0.5 * dist / max(r.radius, 1.0)))
                elif r.kind == "weather" and q > 0:
                    weather = max(weather, r.severity * q)
            if self.suspect_p - TOL <= p < self.closed_p - TOL:
                threat = max(threat, p)  # suspect: usable, with an exposure penalty
            new = {"closed_p": round(p, 4), "closed": p >= self.closed_p - TOL, "threat": round(threat, 4), "weather": round(weather, 4)}
            if any(d.get(k) != val for k, val in new.items()):
                d.update(new)
                changed.append((u, v))
        return sorted(changed, key=str)

    def suspects(self, t: float) -> list[tuple[tuple[Any, Any], float]]:
        """Roads whose closure is suspected (confirmation requests)."""
        out = {}
        for r in self.reports:
            for e in r.edges:
                p = self.closure_p(e, t)
                if self.suspect_p - TOL <= p < self.closed_p - TOL:
                    out[e] = p
        return sorted(out.items(), key=lambda kv: -kv[1])


__all__ = ["BeliefState", "CREDIBILITY_BASE", "RELIABILITY_FACTOR", "Report", "report_probability"]
