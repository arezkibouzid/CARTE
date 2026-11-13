"""GraphHopper backend: export on every machine; the server tests need Java 17+ and graphhopper-web-*.jar (COA_GH_HOME)."""

import math
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")
pytest.importorskip("pyproj")

import networkx as nx  # noqa: E402

from coa_net import gh  # noqa: E402
from coa_net.exercise import RoadArrays, Router, solve_distinct  # noqa: E402

LON0, LAT0 = -2.5, 47.9
STEP_M = 2000.0
DLAT = STEP_M / 111_320.0
DLON = STEP_M / (111_320.0 * math.cos(math.radians(LAT0)))


def grid(nx_: int = 12, ny: int = 8, seed: int = 0):
    """A road grid (2 km cells) with random speeds; zones = west and east columns."""
    rng = np.random.default_rng(seed)
    G = nx.DiGraph()
    for i in range(nx_):
        for j in range(ny):
            G.add_node((i, j), x=LON0 + i * DLON, y=LAT0 + j * DLAT)
    evals = {}
    for i in range(nx_):
        for j in range(ny):
            for di, dj in ((1, 0), (0, 1)):
                a, b = (i, j), (i + di, j + dj)
                if b not in G:
                    continue
                speed = float(rng.choice([30, 50, 70, 90]))
                for p, q in ((a, b), (b, a)):
                    pa, pq = G.nodes[p], G.nodes[q]
                    mid = ((pa["x"] + pq["x"]) / 2, (pa["y"] + pq["y"]) / 2)
                    G.add_edge(p, q, length_m=STEP_M, coords=[(pa["x"], pa["y"]), mid, (pq["x"], pq["y"])])
                    evals[(p, q)] = SimpleNamespace(time_min=STEP_M / 1000 / speed * 60, ok=True)
    R = RoadArrays.build(G, evals)
    xs = np.array([n[0] for n in R.nodes])
    src, dst = np.flatnonzero(xs == 0), np.flatnonzero(xs == nx_ - 1)
    return R, src, dst


def test_export_writes_roads_and_connectors(tmp_path):
    R, src, dst = grid()
    ex = gh.export_osm(R, tmp_path / "g.osm.pbf", R.time_min, src, dst)
    assert ex.path.stat().st_size > 0
    assert ex.ways == len(R.edges) + len(src) + len(dst)
    assert 29 <= ex.speeds_kmh[0] and ex.speeds_kmh[1] <= 91


def test_custom_model_gives_each_area_its_own_id():
    sq = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    cm = gh.custom_model([sq, sq, sq])
    ids = [f["id"] for f in cm["areas"]["features"]]
    assert len(set(ids)) == 3  # GraphHopper rejects duplicate ids
    assert all(f"in_{i}" in cm["priority"][-1]["if"] for i in ids)
    assert gh.custom_model()["priority"][0] == {"if": "!car_access", "multiply_by": "0"}


def test_turn_penalty_follows_the_scipy_turn_cost():
    tp = gh.turn_penalty(turn_min=1.0, sharp_deg=75.0, sharp_min=1.0)
    assert tp[0]["if"].startswith("road_class == SERVICE") and tp[0]["add"] == "0"  # connectors are free
    assert tp[-1] == {"else": "", "add": "Infinity"}  # no U-turn
    adds = [float(s["add"]) for s in tp[1:-1]]
    assert adds[0] == 0.0 and adds == sorted(adds)  # straight on is free, sharper costs more
    m = [12.0 * i for i in range(len(adds))]
    expect = [60 * ((a / 90) ** 2 + (1.0 if a > 75 else 0.0)) for a in m]
    assert adds == pytest.approx(expect, abs=0.05)
    assert m[-1] == 168.0  # the last bin before the U-turn ban


def test_exclusion_polygon_covers_masked_roads():
    import shapely

    R, src, dst = grid()
    edges = [int(i) for i in np.flatnonzero(R.u == R.node_index[(5, 3)])[:1]]
    poly = shapely.from_geojson(__import__("json").dumps(gh.exclusion_polygon(R, edges, 3000.0)))
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    for e in R.near(shapely.union_all(R.lines[edges]), 3000.0):
        c = shapely.get_coordinates(R.lines[e])
        line = shapely.linestrings(np.column_stack(tr.transform(c[:, 0], c[:, 1])))
        assert poly.intersects(line)


def _home_ok() -> bool:
    home = gh.default_home()
    return bool(list(home.glob("graphhopper-web-*.jar"))) and bool(list(home.glob("jdk*/bin/java")) + list(home.glob("jdk*/bin/java.exe")))


@pytest.mark.skipif(not _home_ok(), reason="GraphHopper jar or JDK not found (COA_GH_HOME)")
def test_graphhopper_matches_scipy_on_a_grid(tmp_path):
    R, src, dst = grid()
    ex = gh.export_osm(R, tmp_path / "g.osm.pbf", R.time_min, src, dst)
    with gh.GraphHopperServer(ex.path, tmp_path / "gh", port=8999, heap="512m") as srv:
        router = gh.GHRouter(srv.url, ex)
        p = router.shortest(None)
        best = Router(R, src, dst).shortest(R.ok)
        assert p and best
        # same optimum up to GraphHopper's 2 km/h speed resolution
        assert R.time_min[p].sum() == pytest.approx(R.time_min[best].sum(), rel=0.03)
        assert int(R.u[p[0]]) in set(src) and int(R.v[p[-1]]) in set(dst)
        assert all(R.v[a] == R.u[b] for a, b in zip(p[:-1], p[1:]))
        alts = router.alternatives(None, 4)
        assert len(alts) >= 2
        res = gh.solve_distinct_gh(R, router, 2, 3000.0, R.time_min, n_seeds=4, n_second=2)
        ref = solve_distinct(R, src, dst, 2, 3000.0, n_seeds=4)
        assert len(res.itineraries) == 2
        a, b = (it.line for it in res.itineraries)
        assert a.distance(b) >= 3000.0
        assert len(ref.itineraries) == 2


@pytest.mark.skipif(not _home_ok(), reason="GraphHopper jar or JDK not found (COA_GH_HOME)")
def test_graphhopper_with_turn_costs_matches_the_turn_aware_router(tmp_path):
    from coa_net.exercise import TurnRouter

    R, src, dst = grid()
    tr = TurnRouter(R, src, dst, turn_min=1.0, sharp_deg=75.0, sharp_min=1.0)

    def priced(p):
        return float(R.time_min[p].sum() + np.nansum(tr.turn_cost_of(p)))

    ex = gh.export_osm(R, tmp_path / "g.osm.pbf", R.time_min, src, dst)
    with gh.GraphHopperServer(ex.path, tmp_path / "gh", port=8997, heap="512m", turns=gh.turn_penalty()) as srv:
        router = gh.GHRouter(srv.url, ex)
        p = router.shortest(None)
        best = tr.shortest(R.ok)
        plain = Router(R, src, dst).shortest(R.ok)
        assert p and best
        # GraphHopper's turn angles are quantised to 12 degrees (90 -> 84 or 96): close to the line-graph optimum
        assert priced(p) == pytest.approx(priced(best), rel=0.05)
        assert np.count_nonzero(tr.turns_of(p) > 45) <= np.count_nonzero(tr.turns_of(plain) > 45)
        assert len(router.alternatives(None, 4)) >= 2  # alternative_route works edge-based
