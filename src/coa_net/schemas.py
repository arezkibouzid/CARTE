"""Typed records exchanged between COA-Net stages.

Language models only ever produce instances of these schemas (validated with
pydantic), and code only ever consumes them. No stage reads free prose, and
no language model outputs a route or a coordinate.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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
    via: list[LocationRef] = Field(default_factory=list)  # imposed via-points (RTE.ACCESS)
    rotations: int = Field(default=1, ge=1)

    @field_validator("vehicles")
    @classmethod
    def _non_empty(cls, v: list[VehicleGroup]) -> list[VehicleGroup]:
        if not v:
            raise ValueError("convoy needs at least one vehicle group")
        return v


class MissionSpec(BaseModel):
    """A parsed logistic order (section 3.5 of the methodology)."""

    id: str = "mission"
    phase: Literal["day", "night"] = "day"
    convoys: list[Convoy]
    intent: str = ""  # commander's intent, verbatim from the order
    avoid: list[LocationRef] = Field(default_factory=list)  # prohibited places from the order (RTE.ACCESS)
    avoid_radius_m: float = 800.0
    ambiguities: list[str] = Field(default_factory=list)  # flagged, never guessed


class IncidentType(str, Enum):
    ROAD_BLOCKED = "road_blocked"
    BRIDGE_DESTROYED = "bridge_destroyed"
    THREAT_ZONE = "threat_zone"
    WEATHER = "weather"
    BREAKDOWN = "breakdown"
    RECCE_RESULT = "recce_result"  # e.g. "bridge col 9 classified MLC 120"
    CLEARED = "cleared"  # an earlier closure is lifted


class Incident(BaseModel):
    id: str
    type: IncidentType
    location: LocationRef | None = None
    radius_m: float = 300.0
    severity: float = Field(default=1.0, ge=0.0, le=1.0)
    duration_min: float | None = None
    time_min: float = 0.0  # exercise clock when the incident is reported
    convoy_id: str | None = None  # for breakdowns
    reliability: Literal["A", "B", "C", "D", "E", "F"] = "F"  # Admiralty code: source reliability
    credibility: int = Field(default=6, ge=1, le=6)  # Admiralty code: information credibility
    attrs: dict[str, Any] = Field(default_factory=dict)  # recce results, e.g. {"mlc": 120}
    text: str = ""

    @property
    def grading(self) -> str:
        return f"{self.reliability}{self.credibility}"


__all__ = ["Convoy", "Incident", "IncidentType", "LocationRef", "MissionSpec", "VehicleGroup"]
