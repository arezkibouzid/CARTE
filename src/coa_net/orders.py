"""Orders: structured plan difference and FRAGO (methodology S10 step 8, role L7, Appendix C.6).

Code computes the difference between the old and the new plan: the units
affected, their old and new routes told against the named routes, new times at
the start, control and release points, new route statuses and control posts,
and the triggering incident with its grading. The FRAGO follows the
five-paragraph skeleton and holds only what changed. A language model may
phrase it (role L7); every number, time and identifier of its text must then
appear in the difference, otherwise the templated FRAGO is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .llm import call_validated, unsupported
from .schedule import hhmm


@dataclass
class UnitChange:
    unit: str
    group: str
    action: str  # reroute | retime | hold | continue | stranded
    route: str = ""  # new route, told against the named routes
    old_route: str = ""
    depart: float | None = None
    arrive: float | None = None
    old_arrive: float | None = None
    deadline: float | None = None
    rp: str = ""
    note: str = ""

    def line(self) -> str:
        parts = []
        if self.action == "stranded":
            parts.append(f"{self.unit}: no usable route to {self.rp or 'destination'}; hold at current position and report")
        elif self.action == "reroute":
            parts.append(f"{self.unit}: reroute, {self.route}")
        elif self.action == "hold":
            parts.append(f"{self.unit}: hold, then {self.route}" if self.route else f"{self.unit}: hold")
        else:
            parts.append(f"{self.unit}: continue on {self.route}" if self.route else f"{self.unit}: continue")
        if self.depart is not None and self.action in ("reroute", "retime", "hold"):
            parts.append(f"depart {hhmm(self.depart)}")
        if self.arrive is not None:
            parts.append(f"arrival {hhmm(self.arrive)}" + (f" (deadline {hhmm(self.deadline)})" if self.deadline is not None else ""))
        if self.note:
            parts.append(self.note)
        return "; ".join(parts) + "."


@dataclass
class PlanDiff:
    number: int
    mission: str
    situation: str
    units: list[UnitChange] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)  # "MSR TIGER prohibited (closed: Pont-Est)"
    control: list[str] = field(default_factory=list)  # control posts, escorts, halts
    sustainment: list[str] = field(default_factory=list)
    signal: list[str] = field(default_factory=list)
    annex: str = ""

    def facts(self) -> str:
        lines = [self.situation, *[u.line() for u in self.units], *self.statuses, *self.control, *self.sustainment, *self.signal, self.annex,
                 f"FRAGO {self.number:02d} {self.mission}"]
        return "\n".join(x for x in lines if x)


def frago_template(diff: PlanDiff) -> str:
    letters = "abcdefghijklmnopqrstuvwxyz"
    ex = [f"   {letters[i]}. {u.line()}" for i, u in enumerate(diff.units)]
    coord = list(diff.statuses) + list(diff.control)
    if diff.annex:
        coord.append(diff.annex)
    if coord:
        ex.append(f"   {letters[len(ex)]}. Coordinating instructions: " + "; ".join(coord) + ".")
    if not ex:
        ex = ["   No change to movements."]
    out = [
        f"FRAGO {diff.number:02d} to {diff.mission}",
        f"1. Situation. {diff.situation}",
        "2. Mission. No change.",
        "3. Execution.",
        *ex,
        "4. Sustainment. " + ("; ".join(diff.sustainment) + "." if diff.sustainment else "No change."),
        "5. Command and signal. " + ("; ".join(diff.signal) + "." if diff.signal else "No change."),
    ]
    return "\n".join(out)


FRAGO_SYSTEM = (
    "You write fragmentary orders (FRAGO) for military road movements. Use the five-paragraph structure: "
    "1. Situation, 2. Mission, 3. Execution, 4. Sustainment, 5. Command and signal. State only the items of the "
    "difference you are given; copy unit names, route names, places and times exactly; add no instruction, number "
    "or place that is not in the difference."
)


def write_frago(diff: PlanDiff, llm=None) -> tuple[str, str]:
    """FRAGO text and its source ("template" or "model")."""
    template = frago_template(diff)
    if llm is None:
        return template, "template"
    facts = diff.facts()
    schema = {"type": "object", "properties": {"frago": {"type": "string"}}, "required": ["frago"]}

    def validate(out):
        text = str(out.get("frago", "")) if isinstance(out, dict) else ""
        problems = []
        for k in ("1.", "2.", "3.", "4.", "5."):
            if k not in text:
                problems.append(f"paragraph {k} missing")
        bad = unsupported(text, facts)
        if bad:
            problems.append("not in the difference: " + ", ".join(bad[:8]))
        return text, problems

    text, _problems = call_validated(llm, FRAGO_SYSTEM, "Plan difference:\n" + facts, schema, "frago", validate)
    return (text, "model") if text else (template, "template")


__all__ = ["PlanDiff", "UnitChange", "frago_template", "write_frago"]
