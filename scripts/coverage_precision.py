"""Coverage and precision of every U-Net road network against BD TOPO, with the figures measured elsewhere (IoU, routes,
the 10 exercises), to choose the best configuration.

* completeness (coverage): share of the BD TOPO road length (the U-Net's label classes: no paths, tracks, stone tracks)
  within 25 m of the network, overall, main roads (importance 1-3) and minor roads (4-5);
* correctness (precision): share of the network length within 25 m of a BD TOPO road;
* quality: matched BD TOPO length / (BD TOPO length + network length that matches nothing);
* connectivity: share of the network length in its largest connected part.

    python scripts/coverage_precision.py --data examples --root outputs/hackathon100k --net "mix12k=prc/mix12k" ... --doc docs/prc_best_config.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import networkx as nx
import numpy as np
import shapely
from pyproj import Transformer
from scipy.spatial import cKDTree

from coa_net import scanunet
from coa_net.realdata import read_lines
from coa_net.scanunet import NOT_ROAD
from coa_net.scenario import ScenarioPaths

TOL, STEP = 25.0, 10.0


def sample(lines):
    pts, w, own = [], [], []
    for i, g in enumerate(lines):
        L = g.length
        if L <= 0:
            continue
        n = max(int(L // STEP), 1)
        pts.append(shapely.get_coordinates(shapely.line_interpolate_point(g, (np.arange(n) + 0.5) * L / n)))
        w.append(np.full(n, L / n))
        own.append(np.full(n, i))
    return np.vstack(pts), np.concatenate(w), np.concatenate(own)


def net_lines(G, tr):
    seen, lines, comp_of = set(), [], []
    U = nx.Graph()
    U.add_edges_from((a, b) for a, b in G.edges if a != b)
    comp = {n: i for i, c in enumerate(sorted(nx.connected_components(U), key=len, reverse=True)) for n in c}
    for a, b, d in G.edges(data=True):
        if a == b or (b, a) in seen:
            continue
        seen.add((a, b))
        c = np.asarray(d["coords"])
        x, y = tr.transform(c[:, 0], c[:, 1])
        lines.append(shapely.LineString(np.column_stack([x, y])))
        comp_of.append(comp.get(a, -1))
    return lines, np.asarray(comp_of)


def jfind(root: Path, pattern: str, key, rows_of=lambda d: d):
    for p in sorted(root.glob(pattern)):
        d = json.loads(p.read_text())
        for r in rows_of(d):
            if key(r):
                return r
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--net", action="append", required=True, metavar="NAME=DIR[:LABEL]", help="DIR relative to root; LABEL = name in the ablations")
    ap.add_argument("--doc", required=True)
    a = ap.parse_args()
    root = Path(a.root)
    paths = ScenarioPaths.from_dir(a.data)
    tr = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    bd, _ = read_lines(paths.bdtopo, "cp1252", ["NATURE", "IMPORTANCE"])
    bd = bd[~bd["NATURE"].fillna("").str.lower().isin(NOT_ROAD)].to_crs("EPSG:2154")
    imp = bd["IMPORTANCE"].astype(str).str.strip().values
    bxy, bw, bown = sample(bd.geometry.values)
    bmain = np.isin(imp[bown], ["1", "2", "3"])
    btree = cKDTree(bxy)
    rows = []
    for spec in a.net:
        name, rest = spec.split("=", 1)
        d, _, label = rest.partition(":")
        label = label or name
        G = scanunet.unet_network(root / d)
        lines, comp = net_lines(G, tr)
        nxy, nw, nown = sample(lines)
        dn, _ = btree.query(nxy, distance_upper_bound=TOL + 1)
        db, _ = cKDTree(nxy).query(bxy, distance_upper_bound=TOL + 1)
        tp_net, tp_bd = dn <= TOL, db <= TOL
        big = comp[nown] == 0
        r = {"name": name, "km": nw.sum() / 1000,
             "completeness": bw[tp_bd].sum() / bw.sum(), "completeness_main": bw[tp_bd & bmain].sum() / bw[bmain].sum(),
             "completeness_minor": bw[tp_bd & ~bmain].sum() / bw[~bmain].sum(),
             "correctness": nw[tp_net].sum() / nw.sum(), "quality": bw[tp_bd].sum() / (bw.sum() + nw[~tp_net].sum()),
             "largest_part": nw[big].sum() / nw.sum(), "sections": G.number_of_edges() // 2}
        sc = root / d / "unet_scores.json"
        if not sc.exists():
            src = (root / d / "unet_oof.tif").resolve().parent / "unet_scores.json"
            sc = src if src.exists() else sc
        if sc.exists():
            r["iou"] = json.loads(sc.read_text())["road_iou"]
        ar = jfind(root, "ablation*/ablation.json", lambda x: x["variant"].startswith(f"PRC {label} "), lambda dd: dd.get("rows", []))
        if ar:
            r["route_town"], r["reached"] = ar["town"]["score"], ar["reached"]
        exs = []
        for p in sorted(root.glob("exercise_ablation*/exercise_ablation.json")):
            exs = [x for x in json.loads(p.read_text()) if x.get("network") == f"PRC {label}" and "error" not in x]
            if exs:
                break
        if exs:
            m = lambda k: sum(abs(x[k]) if "gap" in k else x[k] for x in exs) / len(exs)
            r.update(ex_solved=len(exs), ex_gap_bd=m("BD TOPO length gap"), ex_gap_osm=m("OSM length gap"),
                     ex_ov_bd=m("BD TOPO overlap"), ex_ov_osm=m("OSM overlap"))
        rows.append(r)
        print({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}, flush=True)
    f = lambda v, p=True: "-" if v is None else (f"{v:.1%}" if p else f"{v:.3f}")
    L = ["# Which configuration covers the road network best, and which is the most precise?", "",
         "*Every U-Net road network of this work on the Hackathon sheet, out of fold, against BD TOPO roads of the U-Net's label "
         f"classes (paths, tracks and stone tracks excluded), within {TOL:.0f} m. Routes and exercises from the evaluations of each "
         "experiment (`examples` data only).*", "",
         "| network | km | completeness (coverage) | main roads | minor roads | correctness (precision) | quality | largest connected part | "
         "pixel IoU | route score town | pairs reached | 10 exercises: length gap BD TOPO / OSM | overlap BD TOPO / OSM |",
         "| --- |" + " --- |" * 12]
    for r in rows:
        L.append(f"| {r['name']} | {r['km']:,.0f} | {f(r['completeness'])} | {f(r['completeness_main'])} | {f(r['completeness_minor'])} | "
                 f"{f(r['correctness'])} | {f(r['quality'])} | {f(r['largest_part'])} | {f(r.get('iou'), False)} | {f(r.get('route_town'), False)} | "
                 f"{f(r.get('reached'))} | " + (f"{r['ex_gap_bd']:.1%} / {r['ex_gap_osm']:.1%}" + ("" if r['ex_solved'] == 10 else f" ({r['ex_solved']}/10)")
                                                + f" | {r['ex_ov_bd']:.1%} / {r['ex_ov_osm']:.1%} |" if r.get("ex_solved") else "- | - |"))
    (Path(a.doc)).write_text("\n".join(L) + "\n", encoding="utf-8")
    (root / "coverage_precision.json").write_text(json.dumps(rows, indent=1, default=float), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
