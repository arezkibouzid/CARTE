"""S0: corridor graph, critical elements and map card (methodology 3.2, 3.3).

The road graph is contracted between *decision points* (junctions, both ends
of every critical element, town entries and exits, halt areas, named places,
fuel points). A *corridor* is a maximal chain of road edges between two
decision points. Because every branching node is a decision point, every road
path between decision points is a concatenation of whole corridors, so
shortest paths on the corridor graph equal shortest paths on the road graph.

A *critical element* is a place whose loss cuts or degrades a route: a
bridge, tunnel, level crossing, ford, ferry or defile (from edge flags), or,
per convoy group, an articulation point between its origin and destination
(added by :mod:`coa_net.candidates`).

A directed traversal of a corridor is an *arc* ``(corridor_id, +1 | -1)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from .graph import CRITICAL_KINDS, edge_midpoint, node_xy, path_edges, point_distance, ukey

Arc = tuple[str, int]

KIND_PREFIX = {
    "bridge": "B",
    "tunnel": "T",
    "level_crossing": "LX",
    "ford": "FD",
    "ferry": "FY",
    "defile": "DF",
    "articulation": "AP",
}

# obstruction thresholds for the route classification string (to confirm with the organisers)
OB_CLEARANCE_M = 4.3
OB_WIDTH_M = 5.5
OB_SLOPE_PCT = 7.0


@dataclass
class Element:
    id: str
    kind: str
    name: str
    edges: frozenset  # undirected edge keys (ukey)
    nodes: frozenset
    xy: tuple[float, float]
    attrs: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        extra = ""
        if self.kind == "bridge":
            extra = f" MLC{self.attrs['mlc']}" if self.attrs.get("mlc") is not None else " MLC?"
        elif self.kind == "tunnel":
            h = self.attrs.get("max_height_m")
            extra = f" clr {h} m" if h is not None else " clr ?"
        return f"{self.id} {self.kind.replace('_', ' ')}{extra}"


@dataclass
class Corridor:
    id: str
    nodes: list[Any]  # canonical orientation
    fwd: bool  # drivable along the canonical orientation
    bwd: bool  # drivable against it
    length_m: float
    names: list[str]
    elements: list[str]  # critical elements along the canonical orientation
    element_pos: dict[str, float]  # chainage (m) of each element's middle, canonical orientation
    builtup_m: float
    builtup_runs: list[tuple[float, float]]
    forest_m: float

    def arc_nodes(self, d: int) -> list[Any]:
        return self.nodes if d > 0 else self.nodes[::-1]

    def arc_edges(self, d: int) -> list[tuple[Any, Any]]:
        return path_edges(self.arc_nodes(d))

    def arc_elements(self, d: int) -> list[str]:
        return self.elements if d > 0 else self.elements[::-1]

    def element_chainage(self, eid: str, d: int) -> float:
        """Distance (m) from the start of the arc to the element."""
        x = self.element_pos[eid]
        return x if d > 0 else self.length_m - x


class CorridorGraph:
    """Contracted road graph H = (D, A)."""

    def __init__(self, G: nx.DiGraph, extra_decision_points: set | None = None, max_rank: int | None = None):
        self.G = G
        self.max_rank = max_rank
        self.elements: dict[str, Element] = {}
        self.edge_elements: dict[tuple, str] = {}  # ukey -> element id
        self._detect_elements()
        self.extra_dps = set(extra_decision_points or ())
        self.build()

    # ------------------------------------------------------------------ elements
    def _detect_elements(self) -> None:
        G = self.G
        found = []
        for kind in CRITICAL_KINDS:
            U = nx.Graph()
            for u, v, d in G.edges(data=True):
                if d.get(kind):
                    U.add_edge(u, v)
            for comp_edges in (U.subgraph(c).edges for c in nx.connected_components(U)):
                keys = frozenset(ukey(u, v) for u, v in comp_edges)
                nodes = frozenset(n for k in keys for n in k)
                mids = [edge_midpoint(G, *k) for k in keys]
                xy = (sum(p[0] for p in mids) / len(mids), sum(p[1] for p in mids) / len(mids))
                found.append((kind, keys, nodes, xy))
        counters: dict[str, int] = {}
        for kind, keys, nodes, xy in sorted(found, key=lambda f: (CRITICAL_KINDS.index(f[0]), round(f[3][0], 6), round(f[3][1], 6))):
            counters[kind] = counters.get(kind, 0) + 1
            eid = f"{KIND_PREFIX[kind]}{counters[kind]}"
            datas = [self._edge_data(k) for k in keys]
            attrs = {
                "mlc": _min_known(d.get("mlc") for d in datas),
                "mlc_unknown": any(d.get("mlc") is None for d in datas) if kind == "bridge" else False,
                "max_height_m": _min_known(d.get("max_height_m") for d in datas),
                "max_weight_t": _min_known(d.get("max_weight_t") for d in datas),
                "width_m": _min_known(d.get("width_m") for d in datas),
                "highway": datas[0].get("highway"),
                "length_m": sum(d["length_m"] for d in datas),
                "road": next((d.get("name") for d in datas if d.get("name")), ""),
            }
            name = next((self.G.nodes[n].get("name") for n in sorted(nodes, key=str) if self.G.nodes[n].get("name")), None)
            name = name or attrs["road"] or f"{kind.replace('_', ' ')} {eid}"
            el = Element(id=eid, kind=kind, name=name, edges=keys, nodes=nodes, xy=xy, attrs=attrs)
            self.elements[eid] = el
            for k in keys:
                self.edge_elements[k] = eid

    def _edge_data(self, k: tuple) -> dict:
        u, v = k
        return self.G[u][v] if self.G.has_edge(u, v) else self.G[v][u]

    def add_articulation(self, node: Any) -> Element:
        eid = f"AP-{node}"
        if eid not in self.elements:
            name = self.G.nodes[node].get("name") or f"junction {node}"
            self.elements[eid] = Element(eid, "articulation", name, frozenset(), frozenset([node]), node_xy(self.G, node), {})
        return self.elements[eid]

    # ------------------------------------------------------------------ contraction
    def _in_backbone(self, d: dict) -> bool:
        if self.max_rank is None:
            return True
        return d.get("road_rank", 5) <= self.max_rank or any(d.get(k) for k in CRITICAL_KINDS)

    def build(self) -> None:
        G = self.G
        U = nx.Graph()
        for u, v, d in G.edges(data=True):
            if self._in_backbone(d):
                U.add_edge(u, v)
        if self.max_rank is not None:
            self._patch_terminals(U)
        self.U = U
        dps = {n for n in U if U.degree(n) != 2}
        for el in self.elements.values():
            if el.kind == "articulation":
                continue
            for n in el.nodes:
                if n in U and any(ukey(n, m) not in el.edges for m in U[n]):
                    dps.add(n)  # an end of the element
        for n in U:
            nd = G.nodes[n]
            if nd.get("name") or nd.get("halt_capacity") or nd.get("fuel_point"):
                dps.add(n)
            flags = {self._edge_data(ukey(n, m)).get("built_up", False) for m in U[n]}
            if len(flags) > 1:
                dps.add(n)  # town entry or exit
        dps |= {n for n in self.extra_dps if n in U}
        # isolated cycles without any decision point
        for comp in nx.connected_components(U):
            if not (comp & dps):
                dps.add(min(comp, key=str))
        self.dps = dps

        self.corridors: dict[str, Corridor] = {}
        self.edge_corridor: dict[tuple, str] = {}
        chains = []
        seen: set[tuple] = set()
        for s in sorted(dps, key=str):
            for m in sorted(U[s], key=str):
                if ukey(s, m) in seen:
                    continue
                nodes = [s, m]
                seen.add(ukey(s, m))
                while nodes[-1] not in dps:
                    nxt = [x for x in U[nodes[-1]] if x != nodes[-2] and ukey(nodes[-1], x) not in seen]
                    if not nxt:
                        break
                    seen.add(ukey(nodes[-1], nxt[0]))
                    nodes.append(nxt[0])
                chains.append(nodes)
        for i, nodes in enumerate(chains, start=1):
            self._add_corridor(f"K{i}", nodes)
        self._index()

    def _patch_terminals(self, U: nx.Graph) -> None:
        """Connect named places, halt areas and fuel points to the backbone by their shortest road."""
        G = self.G
        und = G.to_undirected(as_view=True)
        for n, nd in G.nodes(data=True):
            if (nd.get("name") or nd.get("halt_capacity") or nd.get("fuel_point") or n in self.extra_dps) and n not in U:
                lengths, paths = nx.single_source_dijkstra(und, n, weight="length_m")
                target = min((t for t in lengths if t in U), key=lambda t: lengths[t], default=None)
                if target is not None:
                    nx.add_path(U, paths[target])

    def _add_corridor(self, cid: str, nodes: list[Any]) -> None:
        G = self.G
        edges = path_edges(nodes)
        fwd = all(G.has_edge(u, v) for u, v in edges)
        bwd = all(G.has_edge(v, u) for u, v in edges)
        datas = [G[u][v] if G.has_edge(u, v) else G[v][u] for u, v in edges]
        length = sum(d["length_m"] for d in datas)
        names: list[str] = []
        for d in datas:
            nm = d.get("name") or ""
            if nm and nm not in names:
                names.append(nm)
        elements: list[str] = []
        pos: dict[str, list[float]] = {}
        x = 0.0
        runs: list[tuple[float, float]] = []
        builtup = forest = 0.0
        for (u, v), d in zip(edges, datas):
            eid = self.edge_elements.get(ukey(u, v))
            if eid is not None:
                if eid not in elements:
                    elements.append(eid)
                pos.setdefault(eid, []).append(x + d["length_m"] / 2)
            if d.get("built_up"):
                builtup += d["length_m"]
                if runs and abs(runs[-1][1] - x) < 1e-6:
                    runs[-1] = (runs[-1][0], x + d["length_m"])
                else:
                    runs.append((x, x + d["length_m"]))
            if d.get("forest"):
                forest += d["length_m"]
            x += d["length_m"]
        k = Corridor(
            id=cid,
            nodes=list(nodes),
            fwd=fwd,
            bwd=bwd,
            length_m=length,
            names=names,
            elements=elements,
            element_pos={e: sum(p) / len(p) for e, p in pos.items()},
            builtup_m=builtup,
            builtup_runs=runs,
            forest_m=forest,
        )
        self.corridors[cid] = k
        for u, v in edges:
            self.edge_corridor[ukey(u, v)] = cid

    def _index(self) -> None:
        self.out: dict[Any, list[tuple[Arc, Any]]] = {n: [] for n in self.dps}
        for cid, k in sorted(self.corridors.items(), key=lambda kv: int(kv[0][1:])):
            if k.fwd:
                self.out.setdefault(k.nodes[0], []).append(((cid, 1), k.nodes[-1]))
            if k.bwd:
                self.out.setdefault(k.nodes[-1], []).append(((cid, -1), k.nodes[0]))
        labels = {}
        j = 0
        for n in sorted(self.dps, key=lambda n: (node_xy(self.G, n)[1], node_xy(self.G, n)[0])):
            nd = self.G.nodes[n]
            if nd.get("name"):
                labels[n] = nd["name"]
            elif nd.get("halt_name"):
                labels[n] = nd["halt_name"]
            else:
                j += 1
                labels[n] = f"J{j}"
        self.dp_label = labels

    def ensure_decision_point(self, node: Any) -> None:
        """Make ``node`` a decision point (origins, destinations and via-points must be)."""
        if node in self.dps:
            return
        if node not in self.G:
            raise KeyError(f"node {node!r} not in the road graph")
        self.extra_dps.add(node)
        self.build()  # corridor ids are renumbered: call before planning, never during a plan

    # ------------------------------------------------------------------ helpers
    def arc_start(self, arc: Arc) -> Any:
        k = self.corridors[arc[0]]
        return k.nodes[0] if arc[1] > 0 else k.nodes[-1]

    def arc_end(self, arc: Arc) -> Any:
        k = self.corridors[arc[0]]
        return k.nodes[-1] if arc[1] > 0 else k.nodes[0]

    def arc_nodes(self, arc: Arc) -> list[Any]:
        return self.corridors[arc[0]].arc_nodes(arc[1])

    def arc_edges(self, arc: Arc) -> list[tuple[Any, Any]]:
        return self.corridors[arc[0]].arc_edges(arc[1])

    def path_nodes(self, arcs: list[Arc] | tuple[Arc, ...]) -> list[Any]:
        out: list[Any] = []
        for a in arcs:
            ns = self.arc_nodes(a)
            out.extend(ns if not out else ns[1:])
        return out

    def label(self, n: Any) -> str:
        return self.dp_label.get(n) or self.G.nodes[n].get("name") or f"node {n}"

    def element_corridors(self, eid: str) -> set[str]:
        el = self.elements[eid]
        if el.kind == "articulation":
            return set()
        return {self.edge_corridor[k] for k in el.edges if k in self.edge_corridor}

    def nearest_place(self, xy: tuple[float, float], max_m: float = 2500.0) -> str | None:
        best = None
        for n, d in self.G.nodes(data=True):
            if d.get("name"):
                dist = point_distance(self.G, node_xy(self.G, n), xy)
                if dist <= max_m and (best is None or dist < best[0]):
                    best = (dist, d["name"])
        return best[1] if best else None

    # ------------------------------------------------------------------ classification and map card
    def classification(self, cid: str) -> str:
        """NATO route classification string ``wid / type / mlc / OB`` (worst over the corridor)."""
        k = self.corridors[cid]
        return classify(self.G[u][v] if self.G.has_edge(u, v) else self.G[v][u] for u, v in path_edges(k.nodes))

    def route_classification(self, arcs) -> str:
        """Classification of a whole route: the worst value over its road edges."""
        return classify(self.G[u][v] for a in arcs for u, v in self.arc_edges(a))

    def threat(self, cid: str) -> float:
        k = self.corridors[cid]
        return max((self.G[u][v]["threat"] if self.G.has_edge(u, v) else self.G[v][u]["threat"]) for u, v in path_edges(k.nodes))

    def card_line(self, cid: str) -> str:
        k = self.corridors[cid]
        a, b = self.label(k.nodes[0]), self.label(k.nodes[-1])
        direction = ">" if k.fwd and not k.bwd else ("<" if k.bwd and not k.fwd else "-")
        towns = ""
        if k.builtup_runs:
            parts = []
            for s, e in k.builtup_runs:
                mid = self._point_at(k, (s + e) / 2)
                parts.append(f"{self.nearest_place(mid) or 'built-up'} {(e - s) / 1000:.1f} km")
            towns = f"towns {len(k.builtup_runs)} ({'; '.join(parts)})"
        else:
            towns = "towns 0"
        crit = ", ".join(self.elements[e].label() for e in k.elements) or "none"
        halts = []
        for n in (k.nodes[0], k.nodes[-1]):
            cap = self.G.nodes[n].get("halt_capacity")
            if cap:
                halts.append(f"{self.G.nodes[n].get('halt_name', 'HA')} ({cap} veh)")
        forest = 100 * k.forest_m / max(k.length_m, 1)
        return (
            f"{cid} | {a}{direction}{b} | {'/'.join(k.names) or 'road'} | {k.length_m / 1000:.1f} km | "
            f"{self.classification(cid)} | {towns} | crit {crit} | halts {', '.join(halts) or 'none'} | "
            f"forest {forest:.0f}% | threat {self.threat(cid):.1f}"
        )

    def map_card(self, cids: list[str] | set[str]) -> str:
        return "\n".join(self.card_line(c) for c in sorted(cids, key=lambda c: int(c[1:])))

    def element_card(self, eid: str, carries: str = "", impact: str = "", threats: str = "none reported") -> str:
        """Critical-element card for the red team (methodology Appendix C.2)."""
        el = self.elements[eid]
        G = self.G
        forest = sum(1 for k in el.edges if self._edge_data(k).get("forest")) / max(1, len(el.edges))
        builtup = any(self._edge_data(k).get("built_up") for k in el.edges)
        surroundings = "built-up" if builtup else ("forest" if forest > 0.5 else "open ground")
        alt = [e for e in self.elements.values() if e.kind == el.kind and e.id != el.id]
        alt.sort(key=lambda e: point_distance(G, e.xy, el.xy))
        nearest = f"{alt[0].name} ({alt[0].label()})" if alt else "none"
        cls = el.label()
        return (
            f"{el.id} | {el.kind.replace('_', ' ')} | {el.name} | {cls} | surroundings: {surroundings}\n"
            f"nearest alternative of the same kind: {nearest} | reported threats nearby: {threats}\n"
            f"carries: {carries or 'n/a'} | impact: {impact or 'n/a'}"
        )

    def _point_at(self, k: Corridor, chainage: float) -> tuple[float, float]:
        x = 0.0
        G = self.G
        for u, v in path_edges(k.nodes):
            d = G[u][v] if G.has_edge(u, v) else G[v][u]
            if x + d["length_m"] >= chainage:
                f = (chainage - x) / max(d["length_m"], 1e-9)
                (x1, y1), (x2, y2) = node_xy(G, u), node_xy(G, v)
                return (x1 + f * (x2 - x1), y1 + f * (y2 - y1))
            x += d["length_m"]
        return node_xy(G, k.nodes[-1])

    def point_on_arc(self, arc: Arc, chainage: float) -> tuple[float, float]:
        k = self.corridors[arc[0]]
        return self._point_at(k, chainage if arc[1] > 0 else k.length_m - chainage)

    def stats(self) -> dict[str, int]:
        return {
            "road_nodes": self.G.number_of_nodes(),
            "road_edges": self.G.number_of_edges(),
            "decision_points": len(self.dps),
            "corridors": len(self.corridors),
            "critical_elements": sum(1 for e in self.elements.values() if e.kind != "articulation"),
        }


def classify(datas) -> str:
    """``wid / type / mlc / OB`` over a sequence of road edges: X all-weather, Y limited, Z fair-weather."""
    datas = list(datas)
    widths = [d.get("width_m") for d in datas]
    known_w = [w for w in widths if w is not None]
    wid = (f"{min(known_w):.1f}m" if known_w else "?m") + ("?" if known_w and None in widths else "")
    rtype = "X"
    for d in datas:
        if d.get("paved") is False:
            rtype = "Z"
        elif d.get("paved") is None and rtype == "X":
            rtype = "Y"
        if d.get("weather", 0) >= 0.5 and rtype == "X":
            rtype = "Y"
    mlcs = [d.get("mlc") for d in datas if d.get("bridge")]
    if mlcs:
        known = [m for m in mlcs if m is not None]
        mlc = str(min(known)) if known and None not in mlcs else ("?" if not known else f"{min(known)}?")
    else:
        w = [d.get("max_weight_t") for d in datas if d.get("max_weight_t") is not None]
        mlc = f"{int(min(w))}t" if w else "-"
    obs = []
    clr = [d.get("max_height_m") for d in datas if d.get("tunnel")]
    if clr:
        known = [c for c in clr if c is not None]
        if not known or min(known) < OB_CLEARANCE_M:
            obs.append(f"clr {min(known):.1f} m" if known else "clr ?")
    if known_w and min(known_w) < OB_WIDTH_M:
        obs.append(f"width {min(known_w):.1f} m")
    slopes = [d.get("slope_pct") for d in datas if d.get("slope_pct") is not None]
    if slopes and max(slopes) > OB_SLOPE_PCT:
        obs.append(f"slope {max(slopes):.0f}%")
    if any(d.get("ford") for d in datas):
        obs.append("ford")
    if any(d.get("ferry") for d in datas):
        obs.append("ferry")
    return f"{wid}/{rtype}/{mlc}" + (f"/OB({', '.join(obs)})" if obs else "")


def _min_known(values) -> Any:
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


__all__ = ["Arc", "Corridor", "CorridorGraph", "Element", "KIND_PREFIX", "classify"]
