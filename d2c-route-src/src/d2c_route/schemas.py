"""Typed records exchanged between D2C-Route components.

Language models only ever produce instances of these schemas (validated with
pydantic); the solver and the verifier only ever consume them. No component
reads prose and no language model outputs coordinates.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Doctrine rules (C1)
# ---------------------------------------------------------------------------


class RuleType(str, Enum):
    HARD = "hard"  # edge filter: predicate must not be False on any used edge
    SOFT = "soft"  # cost term: weight * feature(edge)
    NETWORK = "network"  # plan-level parameter (alternates, overlap, headway)
    TEMPORAL = "temporal"  # time-related parameter (speed factor, halts)


class Scope(BaseModel):
    """When a rule applies. Empty lists mean "any"."""

    vehicle_classes: list[str] = Field(default_factory=list)
    phases: list[Literal["day", "night"]] = Field(default_factory=list)
    cargo: list[str] = Field(default_factory=list)

    def matches(self, vehicle_classes: set[str], phase: str, cargo: set[str]) -> bool:
        if self.vehicle_classes and not (set(self.vehicle_classes) & vehicle_classes):
            return False
        if self.phases and phase not in self.phases:
            return False
        if self.cargo and not (set(self.cargo) & cargo):
            return False
        return True


class Rule(BaseModel):
    """A doctrine rule compiled into a machine-checkable record.

    * ``hard``: ``predicate`` is a boolean expression over ``edge.*`` and
      ``vehicle.*`` (see :mod:`d2c_route.expr`).
    * ``soft``: ``feature`` is a numeric expression over ``edge.*`` in [0, 1]
      (larger is worse) multiplied by ``weight``.
    * ``network`` / ``temporal``: ``params`` holds known keys, see
      :data:`NETWORK_PARAMS` and :data:`TEMPORAL_PARAMS`.
    """

    id: str
    source: str = "unknown"
    type: RuleType
    scope: Scope = Field(default_factory=Scope)
    predicate: str | None = None
    feature: str | None = None
    weight: float | None = None
    params: dict[str, float] = Field(default_factory=dict)
    rationale: str = ""
    relaxable: bool = False  # may the verifier loop turn it soft when it blocks a mission?
    on_unknown: Literal["risk", "forbid"] = "risk"  # hard rule on missing map data: price it, or exclude the road

    @model_validator(mode="after")
    def _check_fields(self) -> "Rule":
        if self.type == RuleType.HARD and not self.predicate:
            raise ValueError(f"hard rule {self.id} needs a predicate")
        if self.type == RuleType.SOFT:
            if not self.feature:
                raise ValueError(f"soft rule {self.id} needs a feature")
            if self.weight is None:
                self.weight = 1.0
        if self.type == RuleType.NETWORK:
            unknown = set(self.params) - NETWORK_PARAMS
            if unknown or not self.params:
                raise ValueError(f"network rule {self.id}: params must be in {sorted(NETWORK_PARAMS)}")
        if self.type == RuleType.TEMPORAL:
            unknown = set(self.params) - TEMPORAL_PARAMS
            if unknown or not self.params:
                raise ValueError(f"temporal rule {self.id}: params must be in {sorted(TEMPORAL_PARAMS)}")
        return self


NETWORK_PARAMS = {
    "min_alternates",  # number of alternate supply routes (ASR) per convoy
    "max_overlap",  # max length-weighted overlap between MSR and an ASR, in [0, 1]
    "max_detour",  # max ASR cost / MSR cost
    "min_headway_min",  # minimum time gap between two convoys on the same road
}
TEMPORAL_PARAMS = {
    "speed_factor",  # multiplier on convoy speed (e.g. night driving)
    "max_speed_kmh",  # convoy speed cap
    "halt_every_min",  # driving time between halts
    "halt_duration_min",  # duration of a halt
}


# ---------------------------------------------------------------------------
# Mission (C2)
# ---------------------------------------------------------------------------


class LocationRef(BaseModel):
    """A place given by node id, gazetteer name, or coordinates (graph CRS)."""

    node: Any | None = None
    name: str | None = None
    xy: tuple[float, float] | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "LocationRef":
        if self.node is None and self.name is None and self.xy is None:
            raise ValueError("location needs node, name or xy")
        return self

    def __str__(self) -> str:
        if self.name:
            return self.name
        if self.node is not None:
            return f"node {self.node}"
        return f"({self.xy[0]:.4f}, {self.xy[1]:.4f})"


class VehicleGroup(BaseModel):
    type: str
    count: int = Field(ge=1)


class Convoy(BaseModel):
    id: str
    vehicles: list[VehicleGroup]
    origin: LocationRef
    destination: LocationRef
    earliest_departure_min: float = 0.0
    latest_arrival_min: float | None = None
    priority: int = 1  # 1 = highest
    cargo: list[str] = Field(default_factory=list)

    @field_validator("vehicles")
    @classmethod
    def _non_empty(cls, v: list[VehicleGroup]) -> list[VehicleGroup]:
        if not v:
            raise ValueError("convoy needs at least one vehicle group")
        return v


class MissionSpec(BaseModel):
    id: str = "mission"
    phase: Literal["day", "night"] = "day"
    convoys: list[Convoy]
    ambiguities: list[str] = Field(default_factory=list)  # flagged, never guessed


# ---------------------------------------------------------------------------
# Constraint program (C3)
# ---------------------------------------------------------------------------


class SoftTerm(BaseModel):
    rule_id: str
    feature: str
    weight: float


class ConvoyConstraints(BaseModel):
    convoy_id: str
    hard: list[Rule]
    soft: list[SoftTerm]
    network: dict[str, float]
    temporal: dict[str, float]
    relaxed: list[str] = Field(default_factory=list)  # rule ids turned soft by the repair loop


class ConstraintProgram(BaseModel):
    mission_id: str
    convoys: dict[str, ConvoyConstraints]
    unknown_penalty: float = 0.5  # soft cost when a hard predicate is unknown on an edge
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Incidents (C6)
# ---------------------------------------------------------------------------


class IncidentType(str, Enum):
    ROAD_BLOCKED = "road_blocked"
    BRIDGE_DESTROYED = "bridge_destroyed"
    THREAT_ZONE = "threat_zone"
    WEATHER = "weather"
    BREAKDOWN = "breakdown"


class Incident(BaseModel):
    id: str
    type: IncidentType
    location: LocationRef | None = None
    radius_m: float = 300.0
    severity: float = Field(default=1.0, ge=0.0, le=1.0)
    duration_min: float | None = None
    time_min: float = 0.0  # exercise clock when the incident is reported
    convoy_id: str | None = None  # for breakdowns
    text: str = ""


# ---------------------------------------------------------------------------
# Plans and reports (C4, C5, C7)
# ---------------------------------------------------------------------------


class Route(BaseModel):
    nodes: list[Any]
    length_m: float
    travel_time_min: float
    cost: float
    features: dict[str, float] = Field(default_factory=dict)  # rule_id -> length-weighted exposure (m)

    def edges(self) -> list[tuple[Any, Any]]:
        return list(zip(self.nodes[:-1], self.nodes[1:]))


class EdgeSlot(BaseModel):
    u: Any
    v: Any
    t_in: float
    t_out: float  # tail of the convoy leaves the edge


class Halt(BaseModel):
    node: Any
    t_start: float
    duration_min: float


class ConvoyPlan(BaseModel):
    convoy_id: str
    msr: Route | None
    asrs: list[Route] = Field(default_factory=list)
    departure_min: float = 0.0
    arrival_min: float | None = None
    schedule: list[EdgeSlot] = Field(default_factory=list)
    halts: list[Halt] = Field(default_factory=list)
    status: Literal["ok", "infeasible", "late", "delivered"] = "ok"
    messages: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    mission_id: str
    convoys: dict[str, ConvoyPlan]
    version: int = 0
    log: list[str] = Field(default_factory=list)


class Violation(BaseModel):
    convoy_id: str
    rule_id: str
    kind: Literal["hard", "unknown", "network", "temporal", "conflict", "closed", "relaxed"]
    detail: str
    edge: tuple[Any, Any] | None = None
