"""S2: mission specification -> convoy groups, march units, active requirements.

Convoys that share origin, destination, binding profile and dangerous-goods
status, and whose departure windows are compatible, form one *group* g with
n_g vehicles; groups keep candidate generation and network design small. The
scheduler still handles every *march unit*: a convoy longer than the
SCH.MARCH limit is split into march units that are scheduled separately.

The priority factor of a group is the only weight left in COA-Net's
objectives. By default it doubles per priority level of the order
(priority 1 of 3 gives 4, priority 3 gives 1), and it is displayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from .corridors import CorridorGraph
from .costing import GroupCoster
from .graph import is_geographic, location_xy, node_xy, resolve
from .requirements import ActiveSet, Requirement, activate
from .schemas import Convoy, MissionSpec
from .vehicles import Profile, VehicleType, convoy_profile, merge_profiles

GROUP_WINDOW_MIN = 60.0  # convoys whose earliest departures differ by more than this are not grouped


@dataclass
class Group:
    id: str
    convoys: list[Convoy]
    profile: Profile
    origin: Any
    destination: Any
    via: list[Any]
    weight: float
    earliest: float
    deadline: float | None
    active: ActiveSet
    coster: GroupCoster

    @property
    def n(self) -> int:
        return self.profile.n_vehicles

    @property
    def hazmat(self) -> bool:
        return self.profile.hazmat

    @property
    def convoy_ids(self) -> list[str]:
        return [c.id for c in self.convoys]


@dataclass
class MarchUnit:
    id: str
    convoy_id: str
    group_id: str
    vehicle_lengths: list[float]
    earliest: float
    deadline: float | None
    weight: float
    priority: int

    @property
    def n(self) -> int:
        return len(self.vehicle_lengths)


@dataclass
class MissionContext:
    mission: MissionSpec
    groups: dict[str, Group]
    units: dict[str, MarchUnit]
    profiles: dict[str, Profile]
    convoy_group: dict[str, str]
    notes: list[str] = field(default_factory=list)


def priority_weights(mission: MissionSpec) -> dict[str, float]:
    worst = max((c.priority for c in mission.convoys), default=1)
    return {c.id: float(2 ** (worst - c.priority)) for c in mission.convoys}


TERMINAL_MAX_MOVE_M = 5000.0


def _usable_terminals(H: CorridorGraph, g: "Group") -> list[str]:
    """Move a group's origin or destination to the nearest junction of the road network the column can use.

    A place named in an order is a town; the node nearest to its label can sit in streets
    the column may not drive (narrow, one-way, unpaved). The usable network is the largest
    strongly connected set of decision points over the group's feasible corridor arcs. A
    terminal that cannot reach it (origin) or be reached from it (destination) moves to its
    nearest junction, at most ``TERMINAL_MAX_MOVE_M`` away, and the move is reported."""
    import numpy as np

    G = H.G
    out_ok: dict[Any, list[Any]] = {}
    in_ok: dict[Any, list[Any]] = {}
    D = nx.DiGraph()
    for u, outs in H.out.items():
        for arc, v in outs:
            if g.coster.arc_ok(arc):
                out_ok.setdefault(u, []).append(v)
                in_ok.setdefault(v, []).append(u)
                D.add_edge(u, v)
    if D.number_of_nodes() == 0:
        return []
    main = max(nx.strongly_connected_components(D), key=len)

    def reaches(start, nbrs) -> bool:
        seen, stack = {start}, [start]
        while stack:
            n = stack.pop()
            if n in main:
                return True
            for m in nbrs.get(n, ()):
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        return False

    notes = []
    cand = list(main)
    xy = np.array([node_xy(G, n) for n in cand], float)
    for attr, nbrs, role in (("origin", out_ok, "start"), ("destination", in_ok, "end")):
        n0 = getattr(g, attr)
        if n0 in main or reaches(n0, nbrs):
            continue
        x, y = node_xy(G, n0)
        if is_geographic(G):
            d = np.hypot((xy[:, 0] - x) * 111_320 * np.cos(np.radians(y)), (xy[:, 1] - y) * 110_540)
        else:
            d = np.hypot(xy[:, 0] - x, xy[:, 1] - y)
        i = int(np.argmin(d))
        name = G.nodes[n0].get("name") or str(n0)
        if d[i] > TERMINAL_MAX_MOVE_M:
            notes.append(f"{g.id}: {attr} {name} is not connected to the roads the column can use within {TERMINAL_MAX_MOVE_M / 1000:g} km")
            continue
        new = cand[i]
        roads = sorted({str(e.get("name")) for _, _, e in G.edges(new, data=True) if e.get("name")})
        setattr(g, attr, new)
        if not G.nodes[new].get("name"):
            G.nodes[new]["name"] = f"{name} {role} point"
        notes.append(f"{g.id}: {attr} {name} moved {d[i]:.0f} m to the nearest junction of the roads the column can use"
                     + (f" ({', '.join(roads[:3])})" if roads else "") + "; the streets around the place are closed to it")
    return notes


def build_context(
    H: CorridorGraph,
    mission: MissionSpec,
    requirements: list[Requirement],
    catalogue: dict[str, VehicleType] | None = None,
    overrides: dict | None = None,
    group_convoys: bool = True,
    base: MissionContext | None = None,
) -> MissionContext:
    """Groups, march units and per-group road evaluation for a mission.

    With ``base`` (a context of the same mission and requirements), each
    group's coster is derived from the base one and only the roads whose
    overrides changed are re-evaluated.
    """
    G = H.G
    profiles = {c.id: convoy_profile(c, catalogue) for c in mission.convoys}
    weights = priority_weights(mission)
    nodes = {}
    for c in mission.convoys:
        o, d = resolve(G, c.origin), resolve(G, c.destination)
        via = [resolve(G, v) for v in c.via]
        nodes[c.id] = (o, d, via)
    for o, d, via in nodes.values():
        for n in (o, d, *via):
            H.ensure_decision_point(n)
    avoid = []
    for loc in mission.avoid:
        avoid.append((str(loc), location_xy(G, loc), mission.avoid_radius_m))

    # grouping
    buckets: dict[tuple, list[Convoy]] = {}
    for c in sorted(mission.convoys, key=lambda c: (c.priority, c.earliest_departure_min, c.id)):
        o, d, via = nodes[c.id]
        key = (o, d, tuple(via), profiles[c.id].signature()) if group_convoys else (c.id,)
        placed = False
        for k, members in buckets.items():
            if k[:-1] == key and abs(members[0].earliest_departure_min - c.earliest_departure_min) <= GROUP_WINDOW_MIN:
                members.append(c)
                placed = True
                break
        if not placed:
            buckets[key + (len(buckets),)] = [c]
    groups: dict[str, Group] = {}
    convoy_group: dict[str, str] = {}
    notes = []
    for members in buckets.values():
        gid = members[0].id if len(members) == 1 else "G-" + "+".join(m.id for m in members)
        prof = merge_profiles([profiles[m.id] for m in members])
        o, d, via = nodes[members[0].id]
        act = activate(requirements, [m.id for m in members], prof.vehicle_classes, prof.cargo, mission.phase)
        bg = base.groups.get(members[0].id if len(members) == 1 else "G-" + "+".join(m.id for m in members)) if base else None
        if bg is not None and [r.id for r in bg.active.reqs] == [r.id for r in act.reqs]:
            coster = bg.coster.derived(overrides or {})
        else:
            coster = GroupCoster(H, prof, act, mission.phase, avoid_zones=avoid, overrides=overrides)
        deadlines = [m.latest_arrival_min for m in members if m.latest_arrival_min is not None]
        groups[gid] = Group(
            id=gid,
            convoys=members,
            profile=prof,
            origin=o,
            destination=d,
            via=via,
            weight=max(weights[m.id] for m in members),
            earliest=min(m.earliest_departure_min for m in members),
            deadline=min(deadlines) if deadlines else None,
            active=act,
            coster=coster,
        )
        for m in members:
            convoy_group[m.id] = gid
        if len(members) > 1:
            notes.append(f"group {gid}: convoys {', '.join(m.id for m in members)} share origin, destination and profile")

    for g in groups.values():
        notes += _usable_terminals(H, g)

    # march units
    units: dict[str, MarchUnit] = {}
    for c in mission.convoys:
        g = groups[convoy_group[c.id]]
        limit = g.active.param("SCH.MARCH", "max_unit_vehicles", None) or 10**6
        lengths = profiles[c.id].vehicle_lengths
        chunks = [lengths[i : i + int(limit)] for i in range(0, len(lengths), int(limit))]
        for j, chunk in enumerate(chunks, start=1):
            uid = c.id if len(chunks) == 1 else f"{c.id}/{j}"
            units[uid] = MarchUnit(
                id=uid,
                convoy_id=c.id,
                group_id=g.id,
                vehicle_lengths=chunk,
                earliest=c.earliest_departure_min,
                deadline=c.latest_arrival_min,
                weight=weights[c.id],
                priority=c.priority,
            )
        if len(chunks) > 1:
            notes.append(f"{c.id} split into {len(chunks)} march units of at most {int(limit)} vehicles (SCH.MARCH)")
    return MissionContext(mission=mission, groups=groups, units=units, profiles=profiles, convoy_group=convoy_group, notes=notes)


__all__ = ["Group", "MarchUnit", "MissionContext", "build_context", "priority_weights"]
