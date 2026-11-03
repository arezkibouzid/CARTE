"""End-to-end COA-Net session (methodology section 2.2).

    order text -> S2 mission and priority order -> S3 candidates -> S4 criticality
    -> S5 COAs -> S6 refinement -> S7 movement credits -> S8 verification, playbooks,
    reconnaissance, simulation -> S9 facts and briefing -> choice -> S10 incident loop

Every stage has a deterministic path; the language model (if any) only fills
typed records and phrases texts that code checks.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx

from .anticipation import Playbook, ReconTask, build_playbooks, value_of_information
from .candidates import CandidateSet, generate_candidates
from .coa import COAFacts, briefing, facts_markdown, facts_table
from .corridors import CorridorGraph
from .criticality import Criticality, assess, impact_table
from .design import Design, DesignProblem, coa_label, make_coas
from .explain import Contrast, explain_plan, explanations_markdown
from .incidents import IncidentOutcome, PlanState, handle_incident
from .intent import IntentResult, map_intent
from .mission import MissionContext, build_context
from .parsing import parse_incident, parse_order
from .refine import Refinement, geojson, refine
from .render import group_colors, render_coa_chart, render_map, render_movement, write_html
from .requirements import Requirement, load_requirements
from .schedule import Schedule, hhmm, march_table, schedule
from .schemas import Incident, IncidentType, MissionSpec
from .simulate import SimResult, monte_carlo, plan_elements, plan_from, script_uniform
from .vehicles import VehicleType, load_catalogue
from .verify import VerificationReport, verify


@dataclass
class COA:
    design: Design
    ref: Refinement
    sched: Schedule
    report: VerificationReport
    playbooks: list[Playbook] = field(default_factory=list)
    recce: list[ReconTask] = field(default_factory=list)
    sim: SimResult | None = None
    explanations: list[Contrast] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.design.name

    @property
    def label(self) -> str:
        return coa_label(self.design)


@dataclass
class COANet:
    H: CorridorGraph
    requirements: list[Requirement] = field(default_factory=load_requirements)
    catalogue: dict[str, VehicleType] = field(default_factory=load_catalogue)
    llm: Any = None
    use_cpsat: bool = True
    time_limit_s: float = 10.0
    sim_reps: int = 100
    seed: int = 0
    archetypes: list[str] | None = None
    explain: bool = True
    recce: bool = True
    recce_max_elements: int | None = None  # cap on what-if evaluations per COA (large maps)
    data_notes: list[str] = field(default_factory=list)  # data-driven policy changes and data facts, shown in the report

    mission: MissionSpec | None = None
    intent: IntentResult | None = None
    ctx: MissionContext | None = None
    cands: dict[str, CandidateSet] = field(default_factory=dict)
    crit: Criticality | None = None
    prob: DesignProblem | None = None
    coas: dict[str, COA] = field(default_factory=dict)
    default: str = ""
    chosen: str = ""
    facts: list[COAFacts] = field(default_factory=list)
    comparison: str = ""
    concerns: list[str] = field(default_factory=list)
    briefing_source: str = ""
    state: PlanState | None = None
    outcomes: list[IncidentOutcome] = field(default_factory=list)
    snapshots: list[tuple[str, str]] = field(default_factory=list)  # (label, map SVG) before and after each incident
    timings: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_graph(cls, G: nx.DiGraph, **kw) -> "COANet":
        return cls(CorridorGraph(G), **kw)

    # ------------------------------------------------------------------ planning
    def plan(self, order: str | MissionSpec, intent_order: list[str] | None = None, intent_eps: dict[str, float] | None = None) -> list[COA]:
        """S2-S9: parse the order, map the intent, design the COAs and brief them. Returns the COAs, default first."""
        H = self.H
        t = time.perf_counter()
        self.mission = order if isinstance(order, MissionSpec) else parse_order(order, H.G, self.catalogue, self.llm)
        if not self.mission.convoys:
            raise ValueError(f"no convoy found in the order; ambiguities: {self.mission.ambiguities}")
        self.intent = map_intent(self.mission.intent, self.llm)
        order_ = intent_order or self.intent.order
        eps = intent_eps or self.intent.eps
        self.ctx = build_context(H, self.mission, self.requirements, self.catalogue)
        self._tick("S2 mission and priorities", t)
        t = time.perf_counter()
        self.cands = {gid: generate_candidates(H, g) for gid, g in self.ctx.groups.items()}
        self._tick("S3 candidate routes", t)
        t = time.perf_counter()
        self.crit = assess(H, self.ctx.groups, self.cands, self.llm)
        self._tick("S4 criticality", t)
        t = time.perf_counter()
        self.prob = DesignProblem(H, self.ctx.groups, self.cands, self.crit, phase=self.mission.phase)
        designs, self.default = make_coas(self.prob, intent_order=order_, intent_eps=eps, archetypes=self.archetypes,
                                          use_cpsat=self.use_cpsat, time_limit_s=self.time_limit_s)
        self._tick("S5 network design (all COAs)", t)
        self.coas = {}
        worlds: dict = {}  # what-if worlds of the value of information, shared by the COAs (same context and criticality)
        for d in designs:
            t = time.perf_counter()
            ref = refine(H, self.ctx.groups, d, self.crit)
            self._tick("S6 refinement", t, add=True)
            t = time.perf_counter()
            sc = schedule(H, self.ctx, d.msr, use_cpsat=self.use_cpsat, time_limit_s=self.time_limit_s)
            self._tick("S7 movement credits", t, add=True)
            t = time.perf_counter()
            rep = verify(H, self.ctx, d, ref, sc, self.crit)
            books = build_playbooks(H, self.ctx, d, self.crit, ref, use_cpsat=self.use_cpsat, mission_id=self.mission.id)
            tasks = value_of_information(H, self.ctx, self.requirements, d, self.crit, ref=ref, use_cpsat=self.use_cpsat,
                                         max_elements=self.recce_max_elements, world_cache=worlds,
                                         base_cands=self.cands, rebase=True) if self.recce else []
            self._tick("S8 verification, playbooks, reconnaissance", t, add=True)
            self.coas[d.name] = COA(d, ref, sc, rep, books, tasks)
        t = time.perf_counter()
        self.simulate()
        self._tick("S8 simulation", t)
        if self.explain:
            t = time.perf_counter()
            roads: dict = {}  # doctrine-free road route per group, the same for every COA
            for c in self.coas.values():
                c.explanations = explain_plan(H, self.ctx, self.prob, c.design, c.sched, c.ref, llm=self.llm, use_cpsat=self.use_cpsat,
                                              road_cache=roads)
            self._tick("explanations", t)
        t = time.perf_counter()
        self.facts = facts_table(H, self.ctx, [c.design for c in self.coas.values()], {n: c.ref for n, c in self.coas.items()}, self.crit,
                                 {n: c.sim for n, c in self.coas.items() if c.sim is not None}, {n: c.recce for n, c in self.coas.items()},
                                 {n: c.report for n, c in self.coas.items()})
        self.comparison, self.concerns, self.briefing_source = briefing(self.facts, self.default, self.llm)
        self._tick("S9 briefing", t)
        return [self.coas[self.default]] + [c for n, c in self.coas.items() if n != self.default]

    def simulate(self, reps: int | None = None, window: tuple[float, float] | None = None) -> None:
        """Monte Carlo of every COA under the same script (one loss, uniform over the designated critical elements)."""
        reps = self.sim_reps if reps is None else reps
        if reps <= 0 or not self.coas:
            return
        plans = {n: plan_from(self.H, self.ctx, c.design, c.sched, c.playbooks, name=n) for n, c in self.coas.items()}
        els = plan_elements(self.H, list(plans.values()))
        if window is None:
            start = min(c.earliest for co in self.coas.values() for c in co.sched.credits.values())
            end = max(c.arrive for co in self.coas.values() for c in co.sched.credits.values())
            window = (start, start + max(10.0, 0.5 * (end - start)))
        script = script_uniform(els, window, seed=self.seed) if els else []
        for n, p in plans.items():
            self.coas[n].sim = monte_carlo(self.H, self.ctx, p, script, reps=reps, seed=self.seed)

    def choose(self, name: str | None = None) -> COA:
        """The operator's choice; without one, the COA built from the confirmed intent order."""
        name = name or self.default
        if name not in self.coas:
            label = {coa_label(c.design): n for n, c in self.coas.items()}
            name = next((n for lab, n in label.items() if name in lab.split("=")), name)
        c = self.coas[name]
        self.chosen = name
        self.state = PlanState(self.H, self.ctx, self.requirements, c.design.order, c.design.eps, c.design, self.crit, c.ref, c.sched,
                               list(c.playbooks), llm=self.llm, use_cpsat=self.use_cpsat, time_limit_s=self.time_limit_s,
                               recce_max_elements=self.recce_max_elements)
        self.snapshots = [(f"COA {c.label}: designated network before departure", self.map())]
        return c

    # ------------------------------------------------------------------ incidents
    def incident(self, report: str | Incident, time_min: float | None = None, refresh_playbooks: bool = True) -> IncidentOutcome:
        if self.state is None:
            self.choose()
        st = self.state
        inc = report if isinstance(report, Incident) else parse_incident(report, self.H.G, f"I-{len(self.outcomes) + 1}", time_min, self.llm)
        if time_min is not None:
            inc = inc.model_copy(update={"time_min": time_min})
        out = handle_incident(st, inc)
        if refresh_playbooks and out.mode in ("playbook", "repair", "recce") and st.design.msr:
            st.playbooks = build_playbooks(self.H, st.plan_ctx, st.design, st.crit, st.ref, use_cpsat=self.use_cpsat,
                                           mission_id=self.mission.id if self.mission else None)
        self.outcomes.append(out)
        marks = self._marks(out)
        self.snapshots.append((f"after {inc.id} at {hhmm(inc.time_min)}: {out.mode}", self.map(incidents=marks)))
        return out

    def _marks(self, out: IncidentOutcome) -> list[dict]:
        inc = out.incident
        if inc.type in (IncidentType.BRIDGE_DESTROYED, IncidentType.ROAD_BLOCKED):
            return [{"kind": "closure", "element": e, "label": inc.id} for e in out.newly_closed]
        if inc.type == IncidentType.THREAT_ZONE and inc.location is not None:
            from .graph import location_xy

            try:
                return [{"kind": "threat", "xy": location_xy(self.H.G, inc.location), "radius_m": inc.radius_m or 1000.0, "label": inc.id}]
            except Exception:  # noqa: BLE001 - a mark is optional
                return []
        return []

    # ------------------------------------------------------------------ outputs
    @property
    def current(self) -> tuple[Design, Refinement, Schedule, Criticality]:
        if self.state is not None:
            return self.state.design, self.state.ref, self.state.sched, self.state.crit
        c = self.coas[self.chosen or self.default]
        return c.design, c.ref, c.sched, self.crit

    def map(self, title: str = "", incidents: list[dict] | None = None) -> str:
        d, ref, _, crit = self.current
        colors = group_colors({c.group for c in self.coas[self.default].sched.credits.values()}) if self.coas else None
        origins = {gid: g.origin for gid, g in self.ctx.groups.items()} if self.ctx else None
        return render_map(self.H, ref, d, crit, title=title, incidents=incidents, colors=colors, origins=origins)

    def march_table_markdown(self) -> str:
        _, ref, sched, _ = self.current
        rows = march_table(self.H, sched, ref)
        out = ["| unit | route | point | place | head | tail | deadline |", "| --- | --- | --- | --- | --- | --- | --- |"]
        out += [f"| {r['unit']} | {r['route']} | {r['point']} | {r['place']} | {r['head']} | {r['tail']} | {r['deadline']} |" for r in rows]
        return "\n".join(out)

    def routes_markdown(self) -> str:
        d, ref, _, _ = self.current
        out = ["| route | role | groups | km | min | status | critical elements |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for nm, nr in sorted(ref.routes.items(), key=lambda kv: (kv[1].role, kv[0])):
            els = ", ".join(self.H.elements[e].name for e in nr.route.ordered_elements(self.H) if self.H.elements[e].kind != "articulation")
            out.append(f"| {nm} | {nr.role} | {', '.join(nr.groups)} | {nr.route.length_m / 1000:.1f} | {nr.route.time_min:.1f} | "
                       f"{nr.status}{' (' + nr.status_reason + ')' if nr.status_reason else ''} | {els or '-'} |")
        out.append("")
        out.append("Control posts: " + "; ".join(f"{p.id} {self.H.label(p.node)} ({', '.join(p.reasons)})" for p in ref.posts))
        return "\n".join(out)

    def annex_markdown(self) -> str:
        st = self.state
        books = st.playbooks if st is not None else self.coas[self.default].playbooks
        return "```\n" + "\n\n".join(pb.entry() for pb in books) + "\n```" if books else "No playbook."

    def report(self, out_dir: str | Path, title: str = "COA-Net plan") -> Path:
        """HTML report and its parts: maps, COA chart, facts, explanations, march table, annexes, FRAGOs, GeoJSON, metrics."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        H = self.H
        default = self.coas[self.default]
        panels = list(self.snapshots) or [(f"COA {default.label}", self.map())]
        first = self.coas[self.chosen or self.default]
        longest = max(first.design.msr.values(), key=lambda r: r.length_m)
        nm = first.ref.name_of(longest) or "main route"
        panels.append((f"Road movement graph along {nm} (before departure)", render_movement(H, first.sched, longest, first.ref)))
        panels.append(("COAs by objective (in the confirmed priority order)", render_coa_chart(self.facts)))
        for n, c in self.coas.items():
            if n != (self.chosen or self.default):
                panels.append((f"COA {c.label}: designated network", render_map(H, c.ref, c.design, self.crit)))
        sections = [
            ("Mission and priorities", self._mission_md()),
            ("COA comparison", facts_markdown(self.facts) + "\n\n" + self.comparison + "\n\nConcerns:\n\n"
             + "\n".join(f"- {x}" for x in self.concerns)),
            ("Explanations (chosen COA)", explanations_markdown(first.explanations) or "none"),
            ("Route network (current)", self.routes_markdown()),
            ("March table (current)", self.march_table_markdown()),
            ("Contingency annex (current playbooks)", self.annex_markdown()),
            ("Reconnaissance tasks", "```\n" + "\n\n".join(t.entry() for t in first.recce) + "\n```" if first.recce else "none"),
            ("Verification (chosen COA)", first.report.summary() + "\n\n" + "\n".join(
                f"- {f.level} {f.req} {f.tier} {f.status}: {f.subject}: {f.detail}" for f in first.report.findings)),
        ]
        for o in self.outcomes:
            done = f"no FRAGO needed, assessed in {o.seconds:.2f} s" if o.mode == "logged" else f"FRAGO computed in {o.seconds:.2f} s"
            body = [f"Mode: {o.mode}" + (f" ({o.playbook})" if o.playbook else "") + f"; {done}"
                    + (f", restoration options in {o.restore_seconds:.1f} s more." if o.restore_seconds else "."), "", o.summary, ""]
            body += [f"- {x}" for x in o.notes + o.restoration]
            if o.verification is not None:
                body += ["", "Verification: " + o.verification.summary()]
            body += ["", "```", o.frago, "```"]
            sections.append((f"{o.incident.id} at {hhmm(o.incident.time_min)}", "\n".join(body)))
        page = write_html(out / "report.html", title, panels, sections)
        for i, (_, svg) in enumerate(panels):
            (out / f"panel_{i:02d}.svg").write_text(svg, encoding="utf-8")
        d, ref, _, crit = self.current
        (out / "network.geojson").write_text(json.dumps(geojson(H, ref, d, crit), indent=1, default=str), encoding="utf-8")
        (out / "facts.md").write_text(facts_markdown(self.facts) + "\n\n" + self.comparison + "\n", encoding="utf-8")
        (out / "fragos.txt").write_text("\n\n".join(o.frago for o in self.outcomes if o.frago), encoding="utf-8")
        (out / "metrics.json").write_text(json.dumps(self.metrics(), indent=1, default=str), encoding="utf-8")
        return page

    def _mission_md(self) -> str:
        m = self.mission
        lines = [f"Order {m.id}, phase {m.phase}. Intent: \"{m.intent}\"" if m.intent else f"Order {m.id}, phase {m.phase}.", ""]
        lines += [f"- {c.id}: {', '.join(f'{v.count} x {v.type}' for v in c.vehicles)} from {c.origin.name or c.origin.node} to "
                  f"{c.destination.name or c.destination.node}, depart {hhmm(c.earliest_departure_min)}"
                  + (f", arrive before {hhmm(c.latest_arrival_min)}" if c.latest_arrival_min is not None else "") + f", priority {c.priority}"
                  for c in m.convoys]
        if m.ambiguities:
            lines += ["", "Ambiguities (flagged, not guessed): " + "; ".join(m.ambiguities)]
        if self.data_notes:
            lines += ["", "Data and assumptions (from the map data, to confirm):", ""] + [f"- {n}" for n in self.data_notes]
        lines += ["", self._model_line()]
        lines += ["", "```", self.intent.describe() if self.intent else "", "```", "", "Critical elements (structural impact x likelihood):", ""]
        lines += ["| element | kind | impact | likelihood | source |", "| --- | --- | --- | --- | --- |"]
        for row in impact_table(self.H, self.crit)[:8]:
            lines.append(f"| {row['name']} ({row['element']}) | {row['kind']} | {row['impact']} | {row['likelihood']} | {row['source']} |")
        return "\n".join(lines)

    def metrics(self) -> dict[str, Any]:
        out: dict[str, Any] = {"timings_s": {k: round(v, 3) for k, v in self.timings.items()}, "default": self.default, "chosen": self.chosen}
        out["coas"] = {}
        for n, c in self.coas.items():
            out["coas"][n] = {"label": c.label, "objectives": {k: round(v, 3) for k, v in c.design.objectives.items()},
                              "solver": c.design.solver, "status": c.design.status, "seconds": round(c.design.seconds, 3),
                              "verification": c.report.summary(), "playbooks": len(c.playbooks),
                              "recce": [t.entry() for t in c.recce], "simulation": c.sim.summary() if c.sim else None}
        out["incidents"] = [{"id": o.incident.id, "mode": o.mode, "seconds": round(o.seconds, 3),
                             "restore_seconds": round(o.restore_seconds, 3), "affected": o.affected,
                             "verification": o.verification.summary() if o.verification else None} for o in self.outcomes]
        if self.llm is not None and hasattr(self.llm, "usage"):
            u = self.llm.usage
            out["llm_usage"] = {"calls": u.calls, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "by_stage": u.by_stage}
            if hasattr(self.llm, "describe"):
                out["llm_usage"]["server"] = self.llm.describe()
        return out

    def _model_line(self) -> str:
        """Which language model ran, where it was served, and whether it stopped answering (report, section 1)."""
        llm = self.llm
        if llm is None:
            return "Language model: none; every model role used its deterministic fallback."
        d = llm.describe() if hasattr(llm, "describe") else {"model": type(llm).__name__, "base_url": "in process", "local": True}
        where = "local" if d.get("local") else "remote, accepted with COA_LLM_ALLOW_REMOTE=1"
        s = f"Language model: {d.get('model')} served at {d.get('base_url')} ({where})"
        u = getattr(llm, "usage", None)
        if u is not None:
            s += f"; {u.calls} calls, {u.input_tokens} input and {u.output_tokens} output tokens"
        if d.get("down"):
            s += f"; the server stopped answering ({d['down']}), so the later model roles used their deterministic fallbacks"
        return s + "."

    def _tick(self, key: str, t0: float, add: bool = False) -> None:
        dt = time.perf_counter() - t0
        self.timings[key] = self.timings.get(key, 0.0) + dt if add else dt


__all__ = ["COA", "COANet"]
