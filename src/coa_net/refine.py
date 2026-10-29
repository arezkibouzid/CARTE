"""S6: refinement and control measures (methodology 5, S6).

1. Road level. Designated routes are kept as road-node sequences (the corridor
   graph never loses its edges); the rule checker (S8) re-checks them edge by edge.
2. Halts. The candidate generator already chose the fewest halt areas that keep
   RTE.GAP_MAX; capacities are left to the scheduler (S7).
3. Traffic control posts (NET.CONTROL): at each critical element of a
   designated route, at each junction where designated routes split or merge,
   then wherever the spacing along a route would exceed the maximum (greedy
   cover). If posts are limited, elements with the highest strike likelihood
   come first. PRO.CRITPOINT adds a post and an escort where the likelihood
   reaches its threshold.
4. Route status (NET.STATUS): dispatch for main routes used by heavy or
   dangerous-goods groups, supervised for the other main routes, reserved for
   alternates, prohibited where an element is closed. These rules are to be
   confirmed with the instructors.
5. Route names, start and release points, the decision points where each
   alternate leaves and rejoins its main route, and a GeoJSON overlay.

Start point (SP) and release point (RP) are the group's origin and
destination, which are decision points of the corridor graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .candidates import Route
from .corridors import CorridorGraph
from .criticality import Criticality
from .design import Design
from .graph import is_geographic, node_xy, path_edges, ukey
from .mission import Group

ROUTE_NAMES = ["LION", "TIGER", "BEAR", "WOLF", "EAGLE", "FALCON", "HAWK", "FOX", "PUMA", "LYNX", "COBRA", "VIPER",
               "BISON", "OTTER", "RAVEN", "HERON", "JAGUAR", "MOOSE", "PANTHER", "SHARK"]
STATUSES = ("dispatch", "supervised", "reserved", "open", "prohibited")


@dataclass
class NamedRoute:
    name: str
    role: str  # MSR | ASR
    route: Route
    groups: list[str]
    status: str = "supervised"
    status_reason: str = ""
    sp: Any = None
    rp: Any = None
    branch: Any = None  # ASR: last decision point shared with the MSR before they part
    rejoin: Any = None  # ASR: first decision point shared with the MSR after they part
    covers: list[str] = field(default_factory=list)  # ASR: critical elements of the MSR it avoids
    classification: str = ""

    @property
    def key(self) -> tuple:
        return self.route.key


@dataclass
class ControlPost:
    id: str
    node: Any
    xy: tuple[float, float]
    reasons: list[str]
    element: str | None = None
    routes: list[str] = field(default_factory=list)
    likelihood: float = 0.0
    escort: bool = False
    kind: str = "element"  # element | junction | spacing


@dataclass
class Refinement:
    design: str
    routes: dict[str, NamedRoute]
    assignment: dict[str, dict[str, Any]]  # group -> {"MSR": name, "ASR": [names]}
    posts: list[ControlPost]
    notes: list[str] = field(default_factory=list)
    retired: set[str] = field(default_factory=set)  # names used earlier in the operation, never given to another route

    def msr(self, gid: str) -> NamedRoute:
        return self.routes[self.assignment[gid]["MSR"]]

    def asrs(self, gid: str) -> list[NamedRoute]:
        return [self.routes[n] for n in self.assignment[gid]["ASR"]]

    def name_of(self, route: Route) -> str | None:
        return next((n for n, r in self.routes.items() if r.key == route.key), None)

    def posts_along(self, H: CorridorGraph, route: Route) -> list[tuple[ControlPost, float]]:
        """Control posts on a route with their distance from the start (m), in driving order."""
        pos = chainage(H, route)
        out = [(p, pos[p.node]) for p in self.posts if p.node in pos]
        return sorted(out, key=lambda t: t[1])


def chainage(H: CorridorGraph, route: Route) -> dict[Any, float]:
    """Distance (m) from the route start to the first visit of each road node."""
    G = H.G
    out: dict[Any, float] = {route.nodes[0]: 0.0} if route.nodes else {}
    x = 0.0
    for u, v in path_edges(route.nodes):
        x += G[u][v]["length_m"]
        out.setdefault(v, x)
    return out


def _common_prefix(a: list, b: list) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _closed_on(H: CorridorGraph, route: Route, closed_p: float) -> list[str]:
    G = H.G
    out = []
    for u, v in path_edges(route.nodes):
        d = G[u][v]
        if d.get("closed") or d.get("closed_p", 0.0) >= closed_p:
            eid = H.edge_elements.get((u, v) if str(u) <= str(v) else (v, u))
            out.append(H.elements[eid].name if eid else (d.get("name") or f"road {u}-{v}"))
    return sorted(set(out))


def refine(H: CorridorGraph, groups: dict[str, Group], design: Design, crit: Criticality | None = None,
           max_posts: int | None = None, previous: Refinement | None = None) -> Refinement:
    """Name, classify and control the designated routes. With ``previous`` (a repair), a route that is
    still designated keeps its name, so orders already issued stay valid."""
    order = sorted(design.msr, key=lambda g: (-groups[g].weight, groups[g].earliest, g))
    routes: dict[str, NamedRoute] = {}
    assignment: dict[str, dict[str, Any]] = {}
    old = {nr.key: nm for nm, nr in previous.routes.items()} if previous is not None else {}
    taken = (set(previous.routes) | previous.retired) if previous is not None else set()
    names = iter([n for n in ROUTE_NAMES + [f"ROUTE-{i}" for i in range(1, 200)] if n not in taken])

    def named(r: Route, role: str, gid: str) -> str:
        for nm, nr in routes.items():
            if nr.key == r.key:
                if gid not in nr.groups:
                    nr.groups.append(gid)
                return nm
        nm = old.get(r.key)
        if nm is None:  # the remaining part of a route designated before a repair keeps its name
            nm = next((n for k, n in old.items() if len(k) > len(r.key) and k[-len(r.key):] == r.key and n not in routes), None)
        if nm is None or nm in routes:
            nm = next(names)
        routes[nm] = NamedRoute(nm, role, r, [gid], sp=r.nodes[0], rp=r.nodes[-1], classification=H.route_classification(r.arcs))
        return nm

    for gid in order:
        assignment[gid] = {"MSR": named(design.msr[gid], "MSR", gid), "ASR": []}
    for gid in order:
        msr = design.msr[gid]
        for a in design.asrs.get(gid, []):
            nm = named(a, "ASR", gid)
            assignment[gid]["ASR"].append(nm)
            nr = routes[nm]
            if nr.role == "ASR" and nr.branch is None:
                md, ad = msr.dps(H), a.dps(H)
                i = _common_prefix(md, ad)
                j = _common_prefix(md[::-1], ad[::-1])
                nr.branch = ad[i - 1] if i else ad[0]
                nr.rejoin = ad[len(ad) - j] if j else ad[-1]
                nr.covers = sorted(msr.elements - a.elements)

    # route status (NET.STATUS)
    notes: list[str] = []
    for nr in routes.values():
        gs = [groups[g] for g in nr.groups]
        closed_p = min((g.active.param("SEG.STATUS", "closed_p", 0.5) for g in gs), default=0.5)
        closed = _closed_on(H, nr.route, closed_p)
        rules = [r for g in gs for r in g.active.of("NET.STATUS")]
        if closed:
            nr.status, nr.status_reason = "prohibited", f"closed: {', '.join(closed)}"
        elif nr.role == "ASR":
            nr.status, nr.status_reason = "reserved", "alternate kept free for re-routing"
        else:
            dispatch_if = set(rules[0].p("dispatch_if")) if rules else set()
            why = [w for w, flag in (("heavy", any(g.profile.heavy for g in gs)), ("hazmat", any(g.hazmat for g in gs))) if w in dispatch_if and flag]
            if why:
                nr.status = "dispatch"
                nr.status_reason = f"movement credit for every move ({' and '.join(why).replace('hazmat', 'dangerous goods')}; {rules[0].id})"
            else:
                nr.status, nr.status_reason = "supervised", "main route"

    posts = _control_posts(H, groups, design, crit, routes, max_posts, notes)
    return Refinement(design=design.name, routes=routes, assignment=assignment, posts=posts, notes=notes,
                      retired=taken - set(routes))


def _control_posts(H: CorridorGraph, groups: dict[str, Group], design: Design, crit: Criticality | None,
                   routes: dict[str, NamedRoute], max_posts: int | None, notes: list[str]) -> list[ControlPost]:
    G = H.G
    acts = [g.active for g in groups.values() if g.id in design.msr]
    control = [r for a in acts for r in a.of("NET.CONTROL")]
    critpoint = [r for a in acts for r in a.of("PRO.CRITPOINT")]
    at_elements = any(r.p("at_elements") for r in control)
    at_junctions = any(r.p("at_junctions") for r in control)
    spacing_km = min((r.p("max_spacing_km") for r in control), default=None)
    post_p = min((r.p("post_if_p_ge") for r in critpoint), default=None)
    lik = crit.likelihood if crit is not None else {}
    found: dict[Any, ControlPost] = {}

    def add(node, reason, kind, element=None, likelihood=0.0, escort=False, route=None):
        p = found.get(node)
        if p is None:
            p = found[node] = ControlPost("", node, node_xy(G, node), [], element, [], likelihood, escort, kind)
        if reason not in p.reasons:
            p.reasons.append(reason)
        if element and not p.element:
            p.element, p.kind = element, "element"
        p.likelihood = max(p.likelihood, likelihood)
        p.escort = p.escort or escort
        if route and route not in p.routes:
            p.routes.append(route)

    terminals = {n for g in design.msr for n in (groups[g].origin, groups[g].destination)}
    # 1. critical elements (and likely targets under PRO.CRITPOINT)
    for nm, nr in routes.items():
        for eid in nr.route.ordered_elements(H):
            el = H.elements[eid]
            pi = lik.get(eid, 0.0)
            likely = post_p is not None and pi >= post_p
            if not (at_elements or likely):
                continue
            entry = next((n for n in nr.route.nodes if n in el.nodes), None)
            if entry is None:
                continue
            why = f"critical element {el.name} ({eid})"
            if likely:
                rid = critpoint[0].id
                why += f"; likely target (strike likelihood {pi:.2f}): escort, no halt nearby ({rid})"
            add(entry, why, "element", eid, pi, likely, nm)
    # 2. junctions where designated routes split or merge
    if at_junctions:
        incident: dict[Any, set[str]] = {}
        for nr in routes.values():
            for a in nr.route.arcs:
                for n in (H.arc_start(a), H.arc_end(a)):
                    incident.setdefault(n, set()).add(a[0])
        for n, cs in incident.items():
            if len(cs) >= 3 and n not in terminals:
                on = [nm for nm, nr in routes.items() if n in nr.route.nodes]
                add(n, f"routes split or merge ({', '.join(on)})", "junction")
    # 3. spacing along each route (greedy cover)
    if spacing_km:
        for nm, nr in routes.items():
            pos = chainage(H, nr.route)
            dps = nr.route.dps(H)
            last = 0.0
            for prev, cur in zip(dps, dps[1:]):
                if cur in found:
                    last = pos[cur]
                    continue
                if pos[cur] - last > spacing_km * 1000 and prev in pos and pos[prev] > last and prev not in terminals:
                    add(prev, f"spacing: at most {spacing_km:g} km between posts", "spacing", route=nm)
                    last = pos[prev]
    posts = list(found.values())
    for p in posts:  # every route through the node is controlled by the post
        for nm, nr in routes.items():
            if p.node in nr.route.nodes and nm not in p.routes:
                p.routes.append(nm)
    if max_posts is not None and len(posts) > max_posts:
        rank = {"element": 0, "junction": 1, "spacing": 2}
        posts.sort(key=lambda p: (rank[p.kind], -p.likelihood, -(crit.impact.get(p.element, 0.0) if crit and p.element else 0.0)))
        notes.append(f"control posts limited to {max_posts} of {len(posts)}: likely targets first")
        posts = posts[:max_posts]
    route_index = {nm: i for i, nm in enumerate(routes)}

    def where(p: ControlPost) -> tuple:
        best = min(((route_index[nm], chainage(H, routes[nm].route).get(p.node, 0.0)) for nm in p.routes), default=(99, 0.0))
        return best

    posts.sort(key=where)
    for i, p in enumerate(posts, start=1):
        p.id = f"TCP-{i}"
    return posts


def road_name(H: CorridorGraph, arc) -> str:
    """Road name(s) of a corridor, or its identifier."""
    names = [n for n in H.corridors[arc[0]].names if n]
    return "/".join(names[:2]) if names else f"road {arc[0]}"


def route_story(H: CorridorGraph, route: Route, ref: Refinement | None = None, prefer: list[str] | None = None,
                arcs=None) -> str:
    """A route told against the named routes: "BEAR from Alpha to J26, then N7 to J31, then EAGLE to Echo".

    ``prefer`` lists route names to match first (e.g. the group's own); ``arcs`` tells only part of the route."""
    arcs = tuple(route.arcs if arcs is None else arcs)
    if not arcs:
        return f"stay at {H.label(route.nodes[-1])}" if route.nodes else "no movement"
    named: dict = {}
    if ref is not None:
        for nm in list(prefer or []) + [n for n in ref.routes if n not in (prefer or [])]:
            if nm in ref.routes:
                for a in ref.routes[nm].route.arcs:
                    named.setdefault(a, nm)
    stretches: list[list] = []
    for a in arcs:
        lab = named.get(a) or road_name(H, a)
        if lab.startswith("road K"):
            lab = "local roads"
        if stretches and not named.get(a) and all(ukey(u, v) in H.edge_elements for u, v in path_edges(H.corridors[a[0]].nodes)):
            stretches[-1][2] = H.arc_end(a)  # a bridge or tunnel carries the road it is on
            continue
        if stretches and stretches[-1][0] == lab:
            stretches[-1][2] = H.arc_end(a)
        else:
            stretches.append([lab, H.arc_start(a), H.arc_end(a)])
    parts = [f"{lab} to {H.label(e)}" if i else f"{lab} from {H.label(s)} to {H.label(e)}" for i, (lab, s, e) in enumerate(stretches)]
    return ", then ".join(parts)


def geojson(H: CorridorGraph, ref: Refinement, design: Design, crit: Criticality | None = None) -> dict:
    """Route network overlay: named routes with status, control posts, halt areas, critical elements, SP/RP."""
    G = H.G
    feats = []

    def pt(n):
        x, y = node_xy(G, n)
        return [round(x, 7), round(y, 7)] if is_geographic(G) else [round(x, 1), round(y, 1)]

    for nm, nr in ref.routes.items():
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [pt(n) for n in nr.route.nodes]},
            "properties": {"kind": "route", "name": nm, "role": nr.role, "groups": nr.groups, "status": nr.status,
                           "status_reason": nr.status_reason, "km": round(nr.route.length_m / 1000, 2),
                           "minutes": round(nr.route.time_min, 1), "classification": nr.classification,
                           "critical_elements": nr.route.ordered_elements(H)},
        })
    for p in ref.posts:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": pt(p.node)},
                      "properties": {"kind": "control_post", "id": p.id, "place": H.label(p.node), "reasons": p.reasons,
                                     "routes": p.routes, "escort": p.escort}})
    halts = {}
    for nm, nr in ref.routes.items():
        for n in nr.route.halts or []:
            halts.setdefault(n, []).append(nm)
    for n, rs in halts.items():
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": pt(n)},
                      "properties": {"kind": "halt_area", "name": G.nodes[n].get("halt_name", H.label(n)),
                                     "capacity": G.nodes[n].get("halt_capacity"), "routes": rs}})
    used = {e for nr in ref.routes.values() for e in nr.route.elements}
    for e in sorted(used):
        el = H.elements[e]
        irr = sorted(g for g, s in design.irreducible.items() if e in s and e in design.msr[g].elements)
        acc = sorted(g for g, s in design.accepted.items() if e in s)
        x, y = el.xy
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [round(x, 7), round(y, 7)] if is_geographic(G) else [round(x, 1), round(y, 1)]},
                      "properties": {"kind": "critical_element", "id": e, "type": el.kind, "name": el.name,
                                     "irreducible_spof_for": irr, "accepted_spof_for": acc,
                                     "likelihood": round(crit.likelihood.get(e, 0.0), 3) if crit else None}})
    for gid, a in ref.assignment.items():
        r = ref.routes[a["MSR"]].route
        for role, n in (("SP", r.nodes[0]), ("RP", r.nodes[-1])):
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": pt(n)},
                          "properties": {"kind": role, "group": gid, "place": H.label(n)}})
    return {"type": "FeatureCollection", "name": f"COA {design.name} route network", "crs_note": str(G.graph.get("crs", "local")),
            "features": feats}


__all__ = ["ControlPost", "NamedRoute", "ROUTE_NAMES", "Refinement", "STATUSES", "chainage", "geojson", "refine", "road_name", "route_story"]
