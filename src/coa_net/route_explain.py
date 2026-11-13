"""Traceable explanation of a computed route, segment by segment, from the solver's own data (no language model).

For every leg the chosen sections are grouped into a few readable *segments* (same road class, at least a few hundred
metres). For each segment the trace records:

- what it is (length, time, road class, legal speed, built-up share, model confidence),
- what it cost in the solver's objective, split into the base term (length or time) and the uncertainty term,
- the best *local alternative*: the cheapest way between the segment's two ends with the segment's sections forbidden
  (Dijkstra on the same priced network), and the difference in cost, km, minutes and confidence,
- a verdict: ``unavoidable`` (no alternative inside the allowed network), ``clearly best`` (alternative >= 10 % dearer)
  or ``narrow choice`` (alternative within 10 %), worded from the numbers above.

The route-level part lists the alternatives the solver actually compared (waypoint orders, the route without the
uncertainty cost, the BD TOPO / OSM routes when computed) and the limits of the data. Every sentence is built from
numbers in the trace, so a chatbot that only rephrases it cannot invent a justification.
"""
from __future__ import annotations

import numpy as np

CLASS_FR = {"motorway": "autoroute", "trunk": "voie rapide", "primary": "route principale", "secondary": "route secondaire",
            "tertiary": "route tertiaire", "unclassified": "petite route", "residential": "rue", "service": "voie de service",
            "track": "chemin"}


RANK_FR = {1: "route d'importance 1 (axe majeur)", 2: "route d'importance 2", 3: "route d'importance 3",
           4: "route d'importance 4", 5: "route d'importance 5 (locale)"}


def _cls(d: dict) -> str:
    """Road class: the OSM / BD TOPO class, or for roads read on the map the importance (1-5) the U-Net predicted."""
    h = d.get("highway") or d.get("scan_symbol") or "?"
    if d.get("source_id") in ("unet", "scan") and h == "unclassified" and d.get("road_rank") in RANK_FR:
        return RANK_FR[d["road_rank"]]
    return CLASS_FR.get(h, h)


def _source(d: dict, network: str) -> str:
    sid = str(d.get("source_id") or "")
    if sid == "osm" and "OSM" in network and network != "OSM":
        return "OSM (ajout)"
    if sid in ("unet", "scan"):
        return "lue sur l'image (U-Net)"
    return network


def _group(N, edges: list[int], min_m: float = 400.0, max_segments: int = 12, breaks: set | None = None) -> list[list[int]]:
    """Consecutive sections of the same road class, cut at the waypoints (``breaks``: node indices), short pieces merged
    into their neighbour (never across a waypoint), at most ``max_segments`` per stretch between waypoints."""
    breaks = breaks or set()
    stretches: list[list[int]] = [[]]
    for i in edges:
        stretches[-1].append(i)
        if int(N.v[i]) in breaks:
            stretches.append([])
    stretches = [st for st in stretches if st]
    total = float(sum(N.length_m[st].sum() for st in stretches))
    min_m = max(min_m, total / (2 * max_segments))  # long routes: a few readable segments, not every class change
    out = []
    for st in stretches:
        quota = max(1, round(max_segments * float(N.length_m[st].sum()) / max(total, 1.0)))
        out += _group_stretch(N, st, min_m, quota)
    return out


def _group_stretch(N, edges: list[int], min_m: float, max_segments: int) -> list[list[int]]:
    G = N.G
    groups: list[list[int]] = []
    last = None
    for i in edges:
        c = _cls(G.edges[N.edges[i]]) + ("|osm" if G.edges[N.edges[i]].get("source_id") == "osm" else "")
        if groups and c == last:
            groups[-1].append(i)
        else:
            groups.append([i])
            last = c
    L = lambda g: float(N.length_m[g].sum())
    # merge pieces shorter than min_m, then the shortest pair until max_segments
    changed = True
    while changed and len(groups) > 1:
        changed = False
        for k, g in enumerate(groups):
            if L(g) < min_m:
                j = k - 1 if k > 0 and (k == len(groups) - 1 or L(groups[k - 1]) <= L(groups[k + 1])) else k + 1
                a, b = sorted((k, j))
                groups[a:b + 1] = [groups[a] + groups[b]]
                changed = True
                break
    while len(groups) > max_segments:
        k = int(np.argmin([L(groups[i]) + L(groups[i + 1]) for i in range(len(groups) - 1)]))
        groups[k:k + 2] = [groups[k] + groups[k + 1]]
    return groups


def _local_alternative(S, N, seg: list[int], cost: np.ndarray, t: np.ndarray, limit: float):
    """Cheapest path between the segment's ends with its sections (both directions) forbidden."""
    from scipy.sparse.csgraph import dijkstra

    forbid = np.zeros(len(N.edges), bool)
    for i in seg:
        forbid[i] = True
        j = N.eidx.get((int(N.v[i]), int(N.u[i])))
        if j is not None:
            forbid[j] = True
    a, b = int(N.u[seg[0]]), int(N.v[seg[-1]])
    A = S.matrix(forbid)
    d, pred = dijkstra(A, directed=True, indices=a, return_predecessors=True, limit=limit)
    if not np.isfinite(d[b]):
        return None
    e = S.edges_of(pred, a, b)
    if not e:
        return None
    e = np.array(e)
    out = {"cost": float(d[b]), "km": float(N.length_m[e].sum()) / 1000, "min": float(t[e].sum())}
    if getattr(N, "confidence", None) is not None:
        out["confidence"] = float(np.exp(-np.average(N.risk[e], weights=N.length_m[e])))
    return out


def explain_route(N, res: dict, objective: str, cap: float, max_segments: int = 14) -> dict:
    """The trace of a route solved by :func:`coa_net.pdfexercises.solve` (``res`` must hold ``_solver`` and ``_leg_edges``)."""
    from pyproj import Transformer

    from .pdfexercises import L93

    S = res["_solver"]
    t = N.time_min(cap)
    base = N.length_m if objective == "length" else t
    unit, scale = ("km", 1000.0) if objective == "length" else ("min", 1.0)
    has_conf = getattr(N, "confidence", None) is not None
    lam = getattr(N, "conf_strength", 0.0) if has_conf else 0.0
    to_ll = Transformer.from_crs(L93, "EPSG:4326", always_xy=True)
    legs_out = []
    for li, (leg, edges) in enumerate(zip(res["legs"], res["_leg_edges"])):
        segs = []
        breaks = {int(p["node"]) for p in res["points"].values()}
        for k, g in enumerate(_group(N, edges, max_segments=max_segments, breaks=breaks)):
            e = np.array(g)
            D = [N.G.edges[N.edges[i]] for i in g]
            km = float(N.length_m[e].sum()) / 1000
            seg = {"id": f"L{li + 1}S{k + 1}", "leg": li, "index": k, "road_class": _cls(D[0]),
                   "source": _source(D[0], N.name),
                   "km": round(km, 2), "min": round(float(t[e].sum()), 1),
                   "legal_kmh": sorted({int(s) for s in N.speed[e]}), "km_built_up": round(float((N.length_m[e] * N.urban_share[e]).sum()) / 1000, 2),
                   "names": sorted({d.get("name") or d.get("ref") for d in D if d.get("name") or d.get("ref")})[:3],
                   "bridges": sum(1 for d in D if d.get("bridge")),
                   "cost_base": round(float(base[e].sum()) / scale, 2), "cost_priced": round(float(S.cost[e].sum()) / scale, 2)}
            if has_conf:
                seg["confidence"] = round(float(np.exp(-np.average(N.risk[e], weights=N.length_m[e]))), 3)
                seg["min_confidence"] = round(float(N.confidence[e].min()), 3)
                seg["uncertainty_cost"] = round(float((base[e] * lam * N.risk[e]).sum()) / scale, 2)
            x0, y0 = N.xy[int(N.u[g[0]])]
            x1, y1 = N.xy[int(N.v[g[-1]])]
            seg["from"], seg["to"] = [[round(float(v), 5) for v in to_ll.transform(x, y)] for x, y in ((x0, y0), (x1, y1))]
            alt = _local_alternative(S, N, g, S.cost, t, limit=float(S.cost[e].sum()) * 6 + 20000 / (1 if objective == "length" else 600))
            if alt is None:
                seg["alternative"] = None
                seg["verdict"] = "unavoidable"
                seg["why"] = ("aucun autre passage entre ses deux extrémités dans le réseau autorisé (zone de déploiement, zones à "
                              "éviter) : passage obligé, par exemple la seule route qui dessert un point")
            else:
                d_cost = alt["cost"] / scale - seg["cost_priced"]
                rel = d_cost / max(seg["cost_priced"], 1e-6)
                a = {"km": round(alt["km"], 2), "min": round(alt["min"], 1), "cost": round(alt["cost"] / scale, 2),
                     "extra_cost": round(d_cost, 2), "extra_cost_pct": round(100 * rel, 1),
                     "extra_km": round(alt["km"] - km, 2), "extra_min": round(alt["min"] - seg["min"], 1)}
                if "confidence" in alt:
                    a["confidence"] = round(alt["confidence"], 3)
                seg["alternative"] = a
                seg["verdict"] = "clearly best" if rel >= 0.10 else "narrow choice"
                parts = [f"le meilleur contournement local coûte {a['extra_cost']:+.2f} {unit} ({a['extra_cost_pct']:+.0f} %) : "
                         f"{a['km']:.1f} km et {a['min']:.0f} min contre {km:.1f} km et {seg['min']:.0f} min"]
                if has_conf and "confidence" in a:
                    if a["confidence"] < seg["confidence"] - 0.05:
                        parts.append(f", et il est moins sûr (confiance {a['confidence']:.2f} contre {seg['confidence']:.2f})")
                    elif a["confidence"] > seg["confidence"] + 0.05:
                        parts.append(f"; bien que plus sûr (confiance {a['confidence']:.2f} contre {seg['confidence']:.2f}), son surcoût "
                                     "de longueur/temps l'emporte sur le coût d'incertitude")
                seg["why"] = "".join(parts)
            segs.append(seg)
        legs_out.append({"leg": li, "name": leg["name"], "segments": segs})

    alts = []
    if "order_ranking" in res and len(res["order_ranking"]) > 1:
        best = res["order_ranking"][0]["cost"]
        for r in res["order_ranking"][1:4]:
            alts.append({"kind": "waypoint order", "what": " -> ".join(r["order"]), "cost": r["cost"], "extra_cost": round(r["cost"] - best, 2),
                         "rejected_because": f"coût total supérieur de {r['cost'] - best:.2f} {unit}"})
    u = res.get("unpriced_optimum")
    if u:
        if u.get("same_route"):
            alts.append({"kind": "without uncertainty cost", "what": "même tracé", "rejected_because": "identique : le coût d'incertitude n'a rien changé"})
        else:
            S0 = res.get("_solver_unpriced")
            extra = {}
            if has_conf and S0 is not None:
                r0 = S0.route([res["points"][k]["node"] for k in res["visit_order"]]) if res.get("visit_order") else None
                if r0:
                    e0 = np.array(r0[0])
                    extra = {"confidence": round(float(np.exp(-np.average(N.risk[e0], weights=N.length_m[e0]))), 3),
                             "km_low_confidence": round(float(N.length_m[e0][N.confidence[e0] < 0.5].sum()) / 1000, 2)}
            alts.append({"kind": "without uncertainty cost", "what": f"{u['length_km']} km, {u['duration_min']} min", **extra,
                         "rejected_because": "plus court ou plus rapide, mais passe davantage par des routes dont l'image est incertaine"
                         + (f" (confiance {extra['confidence']:.2f}, {extra['km_low_confidence']} km peu sûrs)" if extra else "")})
    ur = res.get("unconstrained_reference")
    if ur:
        what = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in ur.items())
        extra = ""
        if res.get("shared_allowed_near_points_m"):
            extra = f" ; les deux trajets peuvent partager des tronçons à moins de {res['shared_allowed_near_points_m']} m des points"
        alts.append({"kind": "aller et retour sans contrainte de tracés distincts", "what": what,
                     "rejected_because": "l'aller et le retour doivent emprunter des routes différentes ; le retour interdit les "
                                         f"tronçons de l'aller (et ceux à moins de 20 m){extra}"})
    limits = []
    for k, p in res["points"].items():
        if p["snap_m"] > 150:
            limits.append(f"{k} est à {p['snap_m']:.0f} m de la route extraite la plus proche (point rattaché au carrefour le plus proche)")
    if has_conf:
        low = sum(s["km"] for l in legs_out for s in l["segments"] if s.get("confidence", 1) < 0.5)
        if low > 0:
            limits.append(f"{low:.1f} km du trajet sont sur des segments de confiance < 0.5 : à vérifier (reconnaissance ou autre source)")
    if any(s["source"] == "lue sur l'image (U-Net)" for l in legs_out for s in l["segments"]):
        limits.append("routes lues sur la carte scannée : classe et vitesse déduites du symbole, pas de nom, ni largeur, ni "
                      "charge admissible ; ponts et tunnels seulement s'ils sont dessinés")
    if any(s["source"] == "OSM (ajout)" for l in legs_out for s in l["segments"]):
        limits.append("certaines sections viennent d'OSM (complément) : elles ne sont pas jugées par le modèle d'image")
    return {"objective": objective, "unit": unit, "uncertainty_strength": lam if has_conf else None,
            "criteria": [f"objectif : {'distance la plus courte' if objective == 'length' else 'durée la plus courte'}",
                         f"vitesse du convoi plafonnée à {cap:.0f} km/h, 50 km/h en agglomération, limites légales sinon",
                         "réseau limité à la zone de déploiement, zones à éviter retirées",
                         *([f"coût d'incertitude : chaque mètre coûte (1 + {lam:g} × risque), risque = -ln(confiance du modèle)"] if has_conf else [])],
            "legs": legs_out, "alternatives": alts, "limitations": limits}


def trace_text(tr: dict, focus: str | None = None, max_chars: int = 6000) -> str:
    """Compact French text of a trace for a language model; ``focus`` (segment id) keeps that segment in full."""
    L = ["Critères : " + " ; ".join(tr["criteria"])]
    for l in tr["legs"]:
        L.append(f"Trajet {l['name']} :")
        for s in l["segments"]:
            conf = f", confiance {s['confidence']:.2f}" if "confidence" in s else ""
            extra = (f", limites {'/'.join(str(v) for v in s['legal_kmh'])} km/h, {s['km_built_up']} km en agglomération, "
                     f"{s['bridges']} pont(s) dessiné(s)")
            line = f"- [{s['id']}] {s['road_class']} ({s['source']}), {s['km']} km, {s['min']} min{conf}{extra} ; {s['verdict']} : {s['why']}"
            if focus and s["id"] == focus:
                line += f" | détails : {s}"
            L.append(line)
    if tr["alternatives"]:
        L.append("Alternatives comparées par le solveur :")
        L += [f"- {a['kind']} : {a['what']} — rejetée : {a['rejected_because']}" for a in tr["alternatives"]]
    if tr["limitations"]:
        L.append("Limites des données :")
        L += [f"- {x}" for x in tr["limitations"]]
    txt = "\n".join(L)
    return txt if len(txt) <= max_chars else txt[:max_chars - 20] + "\n[… tronqué]"
