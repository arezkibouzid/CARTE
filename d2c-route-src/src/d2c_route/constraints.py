"""Rule base and C3: constraint activation and weighting.

The rule base is the compiled, context-frugal form of the doctrine. For a
mission, only rules whose scope matches each convoy are activated. An LLM may
then re-weight soft terms within bounds (it sees the MissionSpec and the
active rules only, a few hundred tokens), but it can never drop a hard rule:
relaxations only happen in the verifier loop and are logged.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from .expr import compile_expr
from .schemas import ConstraintProgram, ConvoyConstraints, MissionSpec, Rule, RuleType, SoftTerm
from .vehicles import ConvoyProfile

if TYPE_CHECKING:
    from .llm import LLMClient

# How several matching network/temporal rules combine: always the stricter value.
_MERGE = {
    "min_alternates": max,
    "max_overlap": min,
    "max_detour": min,
    "min_headway_min": max,
    "speed_factor": min,
    "max_speed_kmh": min,
    "halt_every_min": min,
    "halt_duration_min": max,
}
WEIGHT_BOUNDS = (0.0, 10.0)


# Rules that make incident edits (closures, threat, weather) bite. They are
# operational, not doctrinal, so they are added to any compiled rule base.
SYSTEM_RULE_IDS = ("H-CLOSED", "H-THREAT", "S-THREAT", "S-WEATHER")


def _seed() -> list[dict]:
    return json.loads(resources.files("d2c_route").joinpath("data", "seed_rules.json").read_text())


def load_rules(path: str | Path | None = None, include_system: bool = True) -> list[Rule]:
    if path is None:
        raw = _seed()
    else:
        raw = json.loads(Path(path).read_text())
        if include_system:
            have = {r["id"] for r in raw}
            raw += [r for r in _seed() if r["id"] in SYSTEM_RULE_IDS and r["id"] not in have]
    rules = [Rule.model_validate(r) for r in raw]
    for r in rules:
        for src in (r.predicate, r.feature):
            if src:
                compile_expr(src)  # fail fast on an invalid expression
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate rule ids in rule base")
    return rules


def save_rules(rules: list[Rule], path: str | Path) -> None:
    Path(path).write_text(json.dumps([r.model_dump(mode="json") for r in rules], indent=1, ensure_ascii=False))


def activate(
    rules: list[Rule],
    mission: MissionSpec,
    profiles: dict[str, ConvoyProfile],
    llm: "LLMClient | None" = None,
) -> ConstraintProgram:
    program = ConstraintProgram(mission_id=mission.id, convoys={})
    for convoy in mission.convoys:
        p = profiles[convoy.id]
        active = [r for r in rules if r.scope.matches(p.vehicle_classes, mission.phase, p.cargo)]
        network: dict[str, float] = {}
        temporal: dict[str, float] = {}
        for r in active:
            target = network if r.type == RuleType.NETWORK else temporal if r.type == RuleType.TEMPORAL else None
            if target is None:
                continue
            for k, v in r.params.items():
                target[k] = _MERGE[k](target[k], v) if k in target else v
        program.convoys[convoy.id] = ConvoyConstraints(
            convoy_id=convoy.id,
            hard=[r for r in active if r.type == RuleType.HARD],
            soft=[SoftTerm(rule_id=r.id, feature=r.feature, weight=r.weight) for r in active if r.type == RuleType.SOFT],
            network=network,
            temporal=temporal,
        )
    if llm is not None:
        _llm_reweight(program, mission, rules, llm)
    return program


def _llm_reweight(program: ConstraintProgram, mission: MissionSpec, rules: list[Rule], llm: "LLMClient") -> None:
    """Ask the LLM to adjust soft weights from mission priorities (bounded)."""
    by_id = {r.id: r for r in rules}
    soft_ids = sorted({t.rule_id for c in program.convoys.values() for t in c.soft})
    if not soft_ids:
        return
    schema = {
        "type": "object",
        "properties": {
            "weights": {
                "type": "object",
                "additionalProperties": {"type": "number"},
            },
            "justification": {"type": "string"},
        },
        "required": ["weights", "justification"],
    }
    prompt = {
        "mission": mission.model_dump(mode="json"),
        "soft_rules": [
            {"id": i, "default_weight": by_id[i].weight, "rationale": by_id[i].rationale} for i in soft_ids
        ],
        "instruction": (
            "Adjust soft rule weights to reflect this mission's priorities. Keep defaults unless the "
            f"mission justifies a change. Weights must be in [{WEIGHT_BOUNDS[0]}, {WEIGHT_BOUNDS[1]}]."
        ),
    }
    try:
        out = llm.complete_json(
            "You weight doctrine rules for a military convoy route planner. Answer in JSON.",
            json.dumps(prompt, ensure_ascii=False),
            schema,
            stage="activation",
        )
        weights = {k: float(v) for k, v in out.get("weights", {}).items() if k in soft_ids}
    except Exception as exc:  # noqa: BLE001 - the LLM is optional, defaults stay valid
        program.notes.append(f"LLM re-weighting skipped: {exc}")
        return
    lo, hi = WEIGHT_BOUNDS
    for c in program.convoys.values():
        for t in c.soft:
            if t.rule_id in weights:
                t.weight = min(hi, max(lo, weights[t.rule_id]))
    if weights:
        program.notes.append(f"LLM weights {weights}: {out.get('justification', '')}")
