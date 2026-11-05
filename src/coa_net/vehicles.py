"""Vehicle catalogue, binding profiles and march columns.

Values are illustrative orders of magnitude for generic vehicle classes, not
data on specific materiel. Replace them with the organisers' catalogue.

A convoy is constrained by its most demanding vehicle (heaviest, widest,
tallest, longest, slowest). COA-Net also makes the column explicit
(methodology section 3.4):

    L_k = sum_i len_i + (n_k - 1) d_k            column length (m)
    pass_k(s) = 60 L_k / (1000 s)                 minutes to pass a point at s km/h
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .schemas import Convoy

HAZMAT_CARGO = {"fuel", "ammunition", "explosives", "dangerous_goods"}


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
    length_m: float = 8.0
    tracked: bool = False
    description: str = ""


DEFAULT_CATALOGUE: dict[str, VehicleType] = {
    v.name: v
    for v in [
        VehicleType("light_utility", "light", 4, 4, 2.1, 2.0, 90, 40, length_m=5.0, description="4x4 liaison vehicle"),
        VehicleType("medium_truck", "medium", 18, 16, 2.5, 3.2, 80, 30, length_m=8.0, description="6x6 cargo truck"),
        VehicleType("heavy_truck", "heavy", 32, 30, 2.55, 3.6, 70, 20, length_m=10.0, description="8x8 heavy cargo / PLS"),
        VehicleType("fuel_tanker", "heavy", 38, 35, 2.55, 3.5, 70, 18, length_m=12.0, description="fuel tanker, hazardous cargo"),
        VehicleType("ambulance", "light", 6, 6, 2.3, 2.8, 90, 35, length_m=6.0, description="field ambulance"),
        VehicleType(
            "tank_transporter",
            "heavy",
            100,
            100,
            3.6,
            4.0,
            50,
            10,
            length_m=20.0,
            description="heavy equipment transporter loaded with a main battle tank",
        ),
        VehicleType(
            "pfm_carrier",
            "heavy",
            32,
            32,
            3.0,
            3.9,
            70,
            15,
            length_m=12.0,
            description="carrier of a motorised floating bridge (PFM) bay; ASSUMED values, to confirm with the ETLO",
        ),
        VehicleType("ifv_tracked", "tracked", 30, 32, 3.3, 3.0, 60, 50, length_m=7.0, tracked=True, description="tracked IFV"),
    ]
}


def load_catalogue(path: str | Path | None = None) -> dict[str, VehicleType]:
    if path is None:
        return dict(DEFAULT_CATALOGUE)
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {d["name"]: VehicleType(**d) for d in raw}


@dataclass
class Profile:
    """Binding characteristics of a convoy or of a group of convoys."""

    ids: list[str]
    vehicle_classes: set[str]
    cargo: set[str]
    weight_t: float
    mlc: int
    width_m: float
    height_m: float
    length_m: float  # longest vehicle (turns, tight places)
    max_speed_kmh: float
    max_slope_pct: float
    tracked: bool
    vehicle_lengths: list[float] = field(default_factory=list)  # one entry per vehicle
    types: list[str] = field(default_factory=list)

    @property
    def n_vehicles(self) -> int:
        return len(self.vehicle_lengths)

    @property
    def hazmat(self) -> bool:
        return bool(self.cargo & HAZMAT_CARGO)

    @property
    def heavy(self) -> bool:
        return "heavy" in self.vehicle_classes

    def column_length_m(self, interval_m: float) -> float:
        """L = sum of vehicle lengths + (n - 1) vehicle intervals."""
        n = self.n_vehicles
        return sum(self.vehicle_lengths) + max(0, n - 1) * interval_m

    def signature(self) -> tuple:
        """Profiles with the same signature have the same feasible roads."""
        return (self.weight_t, self.mlc, self.width_m, self.height_m, self.length_m, self.max_slope_pct,
                self.max_speed_kmh, self.tracked, tuple(sorted(self.vehicle_classes)), self.hazmat)


def convoy_profile(convoy: Convoy, catalogue: dict[str, VehicleType] | None = None) -> Profile:
    cat = catalogue or DEFAULT_CATALOGUE
    types = []
    for g in convoy.vehicles:
        if g.type not in cat:
            raise KeyError(f"unknown vehicle type {g.type!r}; known: {sorted(cat)}")
        types.append((cat[g.type], g.count))
    vs = [t for t, _ in types]
    return Profile(
        ids=[convoy.id],
        vehicle_classes={v.vehicle_class for v in vs},
        cargo={c.lower() for c in convoy.cargo} | ({"fuel"} if any(v.name == "fuel_tanker" for v in vs) else set()),
        weight_t=max(v.weight_t for v in vs),
        mlc=max(v.mlc for v in vs),
        width_m=max(v.width_m for v in vs),
        height_m=max(v.height_m for v in vs),
        length_m=max(v.length_m for v in vs),
        max_speed_kmh=min(v.max_speed_kmh for v in vs),
        max_slope_pct=min(v.max_slope_pct for v in vs),
        tracked=any(v.tracked for v in vs),
        vehicle_lengths=[v.length_m for v, c in types for _ in range(c)],
        types=[v.name for v in vs],
    )


def merge_profiles(profiles: list[Profile]) -> Profile:
    """Binding profile of a group: the most demanding value over its convoys."""
    return Profile(
        ids=[i for p in profiles for i in p.ids],
        vehicle_classes=set().union(*(p.vehicle_classes for p in profiles)),
        cargo=set().union(*(p.cargo for p in profiles)),
        weight_t=max(p.weight_t for p in profiles),
        mlc=max(p.mlc for p in profiles),
        width_m=max(p.width_m for p in profiles),
        height_m=max(p.height_m for p in profiles),
        length_m=max(p.length_m for p in profiles),
        max_speed_kmh=min(p.max_speed_kmh for p in profiles),
        max_slope_pct=min(p.max_slope_pct for p in profiles),
        tracked=any(p.tracked for p in profiles),
        vehicle_lengths=[x for p in profiles for x in p.vehicle_lengths],
        types=sorted({t for p in profiles for t in p.types}),
    )


__all__ = ["DEFAULT_CATALOGUE", "HAZMAT_CARGO", "Profile", "VehicleType", "convoy_profile", "load_catalogue", "merge_profiles"]
