"""Vehicle catalogue and convoy profiles.

Values are illustrative orders of magnitude for generic vehicle classes, not
data on specific materiel. Replace them with the organisers' catalogue.
A convoy is constrained by its most demanding vehicle (heaviest, widest,
tallest, slowest), which is what bridge, clearance and slope rules check.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .schemas import Convoy


@dataclass(frozen=True)
class VehicleType:
    name: str
    vehicle_class: str  # light | medium | heavy | tracked
    weight_t: float
    mlc: int  # military load classification (STANAG 2021 style), illustrative
    width_m: float
    height_m: float
    max_speed_kmh: float
    max_slope_pct: float
    tracked: bool = False
    description: str = ""


DEFAULT_CATALOGUE: dict[str, VehicleType] = {
    v.name: v
    for v in [
        VehicleType("light_utility", "light", 4, 4, 2.1, 2.0, 90, 40, description="4x4 liaison vehicle"),
        VehicleType("medium_truck", "medium", 18, 16, 2.5, 3.2, 80, 30, description="6x6 cargo truck"),
        VehicleType("heavy_truck", "heavy", 32, 30, 2.55, 3.6, 70, 20, description="8x8 heavy cargo / PLS"),
        VehicleType("fuel_tanker", "heavy", 38, 35, 2.55, 3.5, 70, 18, description="fuel tanker, hazardous cargo"),
        VehicleType("ambulance", "light", 6, 6, 2.3, 2.8, 90, 35, description="field ambulance"),
        VehicleType(
            "tank_transporter",
            "heavy",
            100,
            100,
            3.6,
            4.0,
            50,
            10,
            description="heavy equipment transporter loaded with a main battle tank",
        ),
        VehicleType("ifv_tracked", "tracked", 30, 32, 3.3, 3.0, 60, 50, tracked=True, description="tracked IFV"),
    ]
}


def load_catalogue(path: str | Path | None = None) -> dict[str, VehicleType]:
    if path is None:
        return dict(DEFAULT_CATALOGUE)
    raw = json.loads(Path(path).read_text())
    return {d["name"]: VehicleType(**d) for d in raw}


@dataclass
class ConvoyProfile:
    """Binding characteristics of a convoy, exposed to rules as ``vehicle.*``."""

    convoy_id: str
    vehicle_classes: set[str]
    cargo: set[str]
    weight_t: float
    mlc: int
    width_m: float
    height_m: float
    max_speed_kmh: float
    max_slope_pct: float
    tracked: bool
    n_vehicles: int
    types: list[str] = field(default_factory=list)

    def as_vehicle(self) -> dict:
        d = asdict(self)
        d.pop("vehicle_classes")
        d.pop("cargo")
        d.pop("types")
        return d

    def column_length_m(self, gap_m: float = 50.0) -> float:
        """Approximate convoy column length (vehicle interval included)."""
        return self.n_vehicles * gap_m


def convoy_profile(convoy: Convoy, catalogue: dict[str, VehicleType] | None = None) -> ConvoyProfile:
    cat = catalogue or DEFAULT_CATALOGUE
    types = []
    for g in convoy.vehicles:
        if g.type not in cat:
            raise KeyError(f"unknown vehicle type {g.type!r}; known: {sorted(cat)}")
        types.append((cat[g.type], g.count))
    vs = [t for t, _ in types]
    return ConvoyProfile(
        convoy_id=convoy.id,
        vehicle_classes={v.vehicle_class for v in vs},
        cargo=set(convoy.cargo) | ({"fuel"} if any(v.name == "fuel_tanker" for v in vs) else set()),
        weight_t=max(v.weight_t for v in vs),
        mlc=max(v.mlc for v in vs),
        width_m=max(v.width_m for v in vs),
        height_m=max(v.height_m for v in vs),
        max_speed_kmh=min(v.max_speed_kmh for v in vs),
        max_slope_pct=min(v.max_slope_pct for v in vs),
        tracked=any(v.tracked for v in vs),
        n_vehicles=sum(c for _, c in types),
        types=[v.name for v in vs],
    )
