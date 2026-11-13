"""Synthetic theatre and scenario generator (stand-in until real data arrives).

The road network is a jittered road grid with
a river crossed by four bridges of different load classes (one unclassified),
a town on the direct axis, a forest with tracks, a hilly sector, a low
tunnel on the N7 and a road limited to 12 t, plus halt-area candidates (with
a capacity in vehicles) and fuel points.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from importlib import resources

import networkx as nx

from .graph import finalize_graph
from .schemas import Convoy, Incident, IncidentType, LocationRef, MissionSpec, VehicleGroup

SPACING_M = 1000.0
RIVER_ROW = 4  # river between rows 4 and 5
ARTERIAL_ROWS = (2, 5, 7)  # never thinned: D2 (south), riverside road, N7 (north)

WIDTH_BY_HIGHWAY = {"trunk": 7.0, "primary": 6.5, "secondary": 6.0, "tertiary": 5.5, "unclassified": 4.5, "residential": 5.0, "track": 3.0}

# halt-area candidates of the demo theatre: (column, row) -> capacity in vehicles
DEMO_HALT_AREAS = {(1, 2): 12, (9, 2): 10, (12, 2): 8, (1, 7): 15, (4, 7): 8, (12, 7): 10}


@dataclass
class Theatre:
    G: nx.DiGraph
    places: dict[str, tuple[int, int]]
    bridges: dict[int, int | None]  # column -> MLC (None = unknown)
    planted: list[str] = field(default_factory=list)


def _nid(c: int, r: int) -> int:
    return r * 100 + c


def synthetic_theatre(seed: int = 0, ncols: int = 14, nrows: int = 10, drop_ratio: float = 0.06, halt_areas: bool = True) -> Theatre:
    rng = random.Random(seed)
    G = nx.DiGraph(crs="local", name=f"synthetic-{seed}")
    for r in range(nrows):
        for c in range(ncols):
            jx, jy = rng.uniform(-150, 150), rng.uniform(-150, 150)
            G.add_node(_nid(c, r), x=c * SPACING_M + jx, y=r * SPACING_M + jy)

    town = {(c, r) for c in range(5, 9) for r in range(5, 8)}
    forest = {(c, r) for c in range(9, 12) for r in range(6, 10)}
    hill = {(c, r) for c in range(1, 5) for r in range(6, 10)}
    # bridges: column -> MLC; the central one (straight line) is weak
    bridges = {1: 120, 4: 30, 9: None, 12: 100}
    if seed:
        cols = sorted(rng.sample(range(1, ncols - 1), 4))
        mlcs = [120, 30, rng.choice([None, 50]), 100]
        rng.shuffle(mlcs)
        bridges = dict(zip(cols, mlcs))
        if 30 not in bridges.values():
            bridges[cols[1]] = 30

    def highway(c1, r1, c2, r2) -> str:
        if r1 == r2 and r1 in (2, 7):
            return "primary"
        if c1 == c2 == 1:
            return "trunk"
        if c1 == c2 == ncols - 2:
            return "secondary"
        cells = {(c1, r1), (c2, r2)}
        if cells <= town:
            return "residential"
        if cells <= forest and r1 != 7:
            return "track"
        return "tertiary" if (c1 + r1) % 3 == 0 else "unclassified"

    def add(c1, r1, c2, r2, **extra):
        a, b = _nid(c1, r1), _nid(c2, r2)
        dx = G.nodes[a]["x"] - G.nodes[b]["x"]
        dy = G.nodes[a]["y"] - G.nodes[b]["y"]
        hw = extra.pop("highway", None) or highway(c1, r1, c2, r2)
        cells = {(c1, r1), (c2, r2)}
        d = {
            "length_m": (dx * dx + dy * dy) ** 0.5 * 1.05,
            "highway": hw,
            "width_m": None if rng.random() < 0.2 else WIDTH_BY_HIGHWAY[hw],
            "paved": (None if rng.random() < 0.3 else False) if hw == "track" else True,
            "built_up": cells <= town,
            "forest": bool(cells & forest),
            "slope_pct": round(rng.uniform(8, 14), 1) if cells <= hill and r1 != r2 else round(rng.uniform(0, 4), 1),
            "name": extra.pop("name", ""),
        }
        d.update(extra)
        G.add_edge(a, b, **d)
        G.add_edge(b, a, **dict(d, slope_pct=d["slope_pct"]))

    for r in range(nrows):
        for c in range(ncols):
            if c + 1 < ncols:
                name = {2: "D2", 5: "D5", 7: "N7"}.get(r, "")
                if (c, r) == (10, 7):
                    add(c, r, c + 1, r, tunnel=True, max_height_m=3.8, name="N7 tunnel")
                elif (c, r) == (3, 8):
                    add(c, r, c + 1, r, max_weight_t=12.0, name="C38 (12 t)")
                elif not (r not in ARTERIAL_ROWS and rng.random() < drop_ratio):
                    add(c, r, c + 1, r, name=name)
            if r + 1 < nrows:
                if r == RIVER_ROW:
                    if c in bridges:
                        hw = "trunk" if c == 1 else ("secondary" if c == ncols - 2 else "tertiary")
                        add(c, r, c, r + 1, highway=hw, bridge=True, mlc=bridges[c], name=f"bridge col {c}")
                    continue
                name = {1: "N1", ncols - 2: f"D{ncols - 2}"}.get(c, "")
                if c in (1, ncols - 2) or rng.random() >= drop_ratio:
                    add(c, r, c, r + 1, name=name)

    places = {
        "Alpha": (2, 0),
        "Bravo": (10, 0),
        "Charlie": (6, 2),
        "Delta": (6, 9),
        "Echo": (12, 9),
        "Ville-Centre": (6, 6),
        "Carrefour-Nord": (6, 8),
        "Pont-Ouest": (1, RIVER_ROW),
        "Pont-Centre": (4, RIVER_ROW),
        "Pont-Est": (12, RIVER_ROW),
    }
    if seed:  # bridge names follow the shuffled bridge columns
        cols = sorted(bridges)
        places.update({"Pont-Ouest": (cols[0], RIVER_ROW), "Pont-Centre": (cols[1], RIVER_ROW), "Pont-Est": (cols[-1], RIVER_ROW)})
    for name, (c, r) in places.items():
        G.nodes[_nid(c, r)]["name"] = name
    # keep only the largest strongly connected component (dropped edges may isolate nodes)
    keep = max(nx.strongly_connected_components(G), key=len)
    G.remove_nodes_from([n for n in list(G) if n not in keep])
    finalize_graph(G, seed=seed)
    planted = [
        f"bridge MLC by column: {bridges} (MLC 30 bridge on the direct line)",
        "N7 tunnel clearance 3.8 m (blocks 4.0 m tank transporters and dangerous goods)",
        "town Ville-Centre is built-up and on the direct Charlie-Delta axis",
        "C38 limited to 12 t",
        "forest tracks unpaved (some paved status unknown); ~20% widths unknown",
    ]
    th = Theatre(G=G, places=places, bridges=bridges, planted=planted)
    if halt_areas:
        add_halt_areas(th, seed)
    for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo"):
        n = _nid(*places[name])
        if n in G:
            G.nodes[n]["fuel_point"] = True
    return th


def add_halt_areas(th: Theatre, seed: int = 0) -> None:
    """Halt-area candidates (node attribute ``halt_capacity``, in vehicles).

    Seed 0 uses fixed positions on the N1, D2, D12 and N7; other seeds draw
    six junctions of the arterial rows outside the town and away from the river."""
    G = th.G
    if seed == 0:
        chosen = dict(DEMO_HALT_AREAS)
    else:
        rng = random.Random(10_000 + seed)
        cells = [(c, r) for r in (2, 7) for c in range(1, 13) if not (5 <= c <= 8 and r == 7)]
        chosen = {cell: rng.randint(8, 16) for cell in rng.sample(cells, 6)}
    for i, ((c, r), cap) in enumerate(sorted(chosen.items(), key=lambda kv: (kv[0][1], kv[0][0])), start=1):
        n = _nid(c, r)
        if n in G:
            G.nodes[n]["halt_capacity"] = cap
            G.nodes[n]["halt_name"] = f"HA-{i}"
    th.planted.append(f"{len(chosen)} halt-area candidates on main roads (capacity 8-16 vehicles)")


def sample_text(name: str) -> str:
    return resources.files("coa_net").joinpath("data", name).read_text(encoding="utf-8")


def sample_incidents() -> list[dict]:
    return json.loads(sample_text("sample_incidents.json"))


VEHICLE_MIXES = [
    [("heavy_truck", 6), ("fuel_tanker", 2)],
    [("tank_transporter", 2)],
    [("medium_truck", 4), ("ambulance", 1)],
    [("fuel_tanker", 4)],
    [("light_utility", 3), ("medium_truck", 3)],
    [("ifv_tracked", 4)],
    [("heavy_truck", 8)],
]


def random_mission(theatre: Theatre, seed: int, n_convoys: int | None = None, phase: str | None = None) -> MissionSpec:
    """Seeded random mission (same seed, same mission)."""
    rng = random.Random(seed)
    south = [p for p, (_, r) in theatre.places.items() if r < RIVER_ROW and not p.startswith("Pont")]
    north = [p for p, (_, r) in theatre.places.items() if r > RIVER_ROW + 1 and not p.startswith("Pont")]
    n = n_convoys or rng.randint(1, 6)
    convoys = []
    for i in range(n):
        mix = rng.choice(VEHICLE_MIXES)
        cargo = ["fuel"] if any(t == "fuel_tanker" for t, _ in mix) else []
        dep = 360 + 10 * rng.randint(0, 6)
        convoys.append(
            Convoy(
                id=f"C{i + 1}",
                vehicles=[VehicleGroup(type=t, count=k) for t, k in mix],
                origin=LocationRef(name=rng.choice(south)),
                destination=LocationRef(name=rng.choice(north)),
                earliest_departure_min=dep,
                latest_arrival_min=dep + 180,
                priority=rng.randint(1, 3),
                cargo=cargo,
            )
        )
    return MissionSpec(id=f"random-{seed}", phase=phase or rng.choice(["day", "night"]), convoys=convoys)


def random_incident(theatre: Theatre, seed: int, time_min: float) -> Incident:
    rng = random.Random(seed)
    kind = rng.choice([IncidentType.BRIDGE_DESTROYED, IncidentType.ROAD_BLOCKED, IncidentType.THREAT_ZONE])
    if kind == IncidentType.BRIDGE_DESTROYED:
        place = rng.choice([p for p in theatre.places if p.startswith("Pont")])
        return Incident(id=f"I-{seed}", type=kind, location=LocationRef(name=place), radius_m=200, time_min=time_min,
                        reliability="B", credibility=2)
    place = rng.choice(["Ville-Centre", "Carrefour-Nord"])  # never on an origin or destination
    radius = 400 if kind == IncidentType.ROAD_BLOCKED else 1200
    return Incident(id=f"I-{seed}", type=kind, location=LocationRef(name=place), radius_m=radius, severity=0.9,
                    time_min=time_min, reliability="B", credibility=2)


__all__ = [
    "DEMO_HALT_AREAS",
    "Theatre",
    "add_halt_areas",
    "random_incident",
    "random_mission",
    "sample_incidents",
    "sample_text",
    "synthetic_theatre",
]
