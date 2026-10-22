"""End-to-end D2C-Route session: order -> constraints -> plan -> verify -> repair."""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from .constraints import activate, load_rules
from .explain import explain_plan
from .incidents import RepairReport, repair
from .llm import LLMClient
from .optimizer import EdgeCoster
from .parsing import parse_incident, parse_order
from .render import render_svg
from .schemas import ConstraintProgram, Incident, MissionSpec, Plan, Rule
from .vehicles import ConvoyProfile, VehicleType, convoy_profile, load_catalogue
from .verifier import Report, plan_verify_repair, verify


@dataclass
class D2CRoute:
    G: nx.DiGraph
    rules: list[Rule] = field(default_factory=load_rules)
    catalogue: dict[str, VehicleType] = field(default_factory=load_catalogue)
    llm: LLMClient | None = None
    unknown_penalty: float = 0.5
    stability: float = 0.3

    mission: MissionSpec | None = None
    profiles: dict[str, ConvoyProfile] = field(default_factory=dict)
    program: ConstraintProgram | None = None
    plan: Plan | None = None
    report: Report | None = None
    history: list[tuple[str, Plan]] = field(default_factory=list)
    repairs: list[RepairReport] = field(default_factory=list)
    _costers: dict[str, EdgeCoster] = field(default_factory=dict)
    _n_incidents: int = 0

    @property
    def rules_by_id(self) -> dict[str, Rule]:
        return {r.id: r for r in self.rules}

    def plan_mission(self, order: str | MissionSpec, deconflict: bool = True) -> tuple[Plan, Report]:
        self.mission = order if isinstance(order, MissionSpec) else parse_order(order, self.G, self.catalogue, self.llm)
        if not self.mission.convoys:
            raise ValueError(f"no convoy found in the order; ambiguities: {self.mission.ambiguities}")
        self.profiles = {c.id: convoy_profile(c, self.catalogue) for c in self.mission.convoys}
        self.program = activate(self.rules, self.mission, self.profiles, self.llm)
        self.program.unknown_penalty = self.unknown_penalty
        self._costers = {}
        self.plan, self.report = plan_verify_repair(
            self.G, self.mission, self.program, self.profiles, self.rules, self.llm, deconflict=deconflict, costers=self._costers
        )
        self.history.append(("initial plan", self.plan))
        return self.plan, self.report

    def incident(self, report: str | Incident, time_min: float = 0.0, mode: str = "local") -> tuple[Plan, RepairReport, Report]:
        if self.plan is None:
            raise RuntimeError("plan a mission first")
        self._n_incidents += 1
        inc = report if isinstance(report, Incident) else parse_incident(report, self.G, f"I-{self._n_incidents}", time_min, self.llm)
        self.plan, rr = repair(
            self.G, self.plan, self.mission, self.program, self.profiles, inc, self.stability, mode=mode, costers=self._costers
        )
        self.report = verify(self.G, self.plan, self.program, self.profiles, self.mission, self.rules)
        self.repairs.append(rr)
        self.history.append((f"{inc.id} {inc.type.value}" + (f" @ {inc.location}" if inc.location else ""), self.plan))
        return self.plan, rr, self.report

    def explain(self) -> str:
        return explain_plan(self.G, self.plan, self.mission, self.program, self.profiles, self.rules_by_id)

    def svg(self, title: str = "") -> str:
        return render_svg(self.G, self.plan, title)
