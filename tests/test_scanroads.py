"""Road network read from a scanned map: a synthetic IGN-like palette GeoTIFF with known roads."""

import math

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
pytest.importorskip("cv2")
pytest.importorskip("skimage")

from coa_net.scanroads import (C_BLUE, C_CASING, C_CASING_LIGHT, C_ORANGE, C_RED, C_WATER, C_WHITE, Piece, ScanParams,  # noqa: E402
                               classify_rgb, load_scenario_scan, separate_expressway_crossings)

PAL = {0: (255, 255, 255), 1: (81, 81, 80), 2: (225, 164, 42), 3: (134, 134, 134), 4: (132, 208, 240), 5: (16, 116, 182), 6: (0, 0, 0)}
RES = 8e-5  # degrees, as the organisers' 1:100 000 scan


def _scan(tmp_path):
    """600 x 600 px: a white road (y=200, grey casing) and an orange road (x=300) crossing it, a dashed track
    (x=150) leaving the white road southwards, a river (y=450, 8 px wide) under the orange road."""
    H = W = 600
    a = np.zeros((H, W), np.uint8)
    a[440:448, :] = 4                                  # river, light blue, 8 px
    # white road: casings at rows 197 and 203, white inside
    a[197, :] = 1
    a[203, :] = 1
    # orange road: casings at cols 296 and 304, orange fill between
    a[:, 297:304] = 2
    a[:, 296] = 1
    a[:, 304] = 1
    a[198:203, 297:304] = 2                            # the crossing is drawn in the orange road's fill
    # dashed track from the white road down to row 400 at x=150: mid-grey casings, 4 px on / 3 px off
    for r in range(204, 400):
        if (r // 4) % 2 == 0 or (r % 7) < 4:
            a[r, 147] = 3
            a[r, 153] = 3
    a[203, 148:153] = 0                                # the track opens into the white road
    # text: a black block over the white road far from the junction (a label cutting the road)
    a[194:207, 470:478] = 6
    path = tmp_path / "scan.tif"
    transform = rasterio.transform.from_origin(-2.0, 48.0 + H * RES, RES, RES)
    with rasterio.open(path, "w", driver="GTiff", height=H, width=W, count=1, dtype="uint8", crs="EPSG:4326",
                       transform=transform) as dst:
        dst.write(a, 1)
        dst.write_colormap(1, {k: v + (255,) for k, v in PAL.items()})
    return str(path)


def test_colour_rules():
    rgb = np.array([(81, 81, 80), (134, 134, 134), (225, 164, 42), (202, 86, 67), (16, 116, 182), (132, 208, 240), (255, 255, 255),
                    (216, 208, 192)], np.uint8)
    assert list(classify_rgb(rgb)) == [C_CASING, C_CASING_LIGHT, C_ORANGE, C_RED, C_BLUE, C_WATER, C_WHITE, 0]


def test_network_from_synthetic_scan(tmp_path):
    tif = _scan(tmp_path)
    p = ScanParams(min_component_m=200.0)
    G, rep, srep = load_scenario_scan(tif, params=p)
    sym = {d.get("scan_symbol") for _, _, d in G.edges(data=True)}
    assert {"orange", "white", "dashed"} <= sym
    # every road two-way
    assert all(G.has_edge(v, u) for u, v in G.edges)
    # the crossroads is one junction with four roads, near (col 300, row 200)
    cx, cy = -2.0 + 300.5 * RES, 48.0 + (600 - 200.5) * RES
    deg4 = [n for n in G if len(set(G.successors(n))) >= 4]
    assert deg4, "crossroads not found"
    d = min(math.hypot((G.nodes[n]["x"] - cx) / RES, (G.nodes[n]["y"] - cy) / RES) for n in deg4)
    assert d < 4, f"crossroads {d:.1f} px from where it is drawn"
    # the orange road crosses the river on a bridge; the label gap on the white road was joined
    assert any(dd.get("bridge") and dd.get("scan_symbol") == "orange" for _, _, dd in G.edges(data=True))
    assert srep.gaps_joined >= 1
    # the track is a track (unpaved), the others paved
    for _, _, dd in G.edges(data=True):
        if dd.get("scan_symbol") == "dashed":
            assert dd["highway"] == "track" and dd["paved"] is False
        if dd.get("scan_symbol") == "orange":
            assert dd["highway"] == "secondary"
    # width, load class, slope: unknown
    assert all(dd.get("width_m") is None and dd.get("slope_pct") is None for _, _, dd in G.edges(data=True))


def test_expressway_crossing_is_not_a_junction():
    def piece(pts, a, b, s):
        return Piece(np.asarray(pts, float), a, b, {"symbol": s})

    # junction 1 at (0, 0): expressway west-east, a white road north-south straight across it
    ps = [piece([(0, 0), (0, -20)], 1, -1, "blue"), piece([(0, 0), (0, 20)], 1, -2, "blue"),
          piece([(0, 0), (-20, 0)], 1, -3, "white"), piece([(0, 0), (20, 0)], 1, -4, "white"),
          # junction 2: a slip road joining the expressway (three arms) stays a junction
          piece([(50, 0), (50, -20)], 2, -5, "blue"), piece([(50, 0), (50, 20)], 2, -6, "blue"), piece([(50, 0), (70, 15)], 2, -7, "white")]
    assert separate_expressway_crossings(ps) == 1
    whites = [pc for pc in ps[:4] if pc.sym["symbol"] == "white"]
    assert {pc.a for pc in whites} != {1} and whites[0].a == whites[1].a
    assert ps[6].a == 2


def test_dual_carriageway_lines_move_onto_the_median():
    from coa_net.scanroads import centre_dual_carriageways

    xs = np.arange(0, 200, dtype=float)
    north = Piece(np.column_stack([np.full_like(xs, 100.0), xs]), 1, 2, {"symbol": "blue"})
    south = Piece(np.column_stack([np.full_like(xs, 110.0), xs]), 3, 4, {"symbol": "blue"})
    side = Piece(np.array([[100.0, 0.0], [60.0, 0.0]]), 1, -1, {"symbol": "white"})  # a road ending on the north line
    moved, _ = centre_dual_carriageways([north, south, side], max_sep_px=14, keep_px=1.0)
    assert moved == 2
    assert abs(north.rc[100, 0] - 104.0) < 0.2 and abs(south.rc[100, 0] - 106.0) < 0.2
    assert np.allclose(side.rc[0], north.rc[0])  # the road on the junction moved with it


def test_corrector_applies_probabilities_and_gate():
    import networkx as nx

    from coa_net.scanlearn import _key, apply_corrections, uncertainty_penalty

    G = nx.DiGraph()
    for n, (x, y) in enumerate([(0, 0), (0.01, 0), (0.02, 0), (0.03, 0)]):
        G.add_node(n, x=x, y=y)
    attrs = [dict(scan_symbol="white", scan_gap=False, highway="unclassified", road_rank=5, source_id="scan", bridge=False),
             dict(scan_symbol="white", scan_gap=True, highway="unclassified", road_rank=5, source_id="scan", bridge=False),
             dict(scan_symbol="white", scan_gap=False, highway="unclassified", road_rank=5, source_id="scan", bridge=True)]
    for (u, v), d in zip([(0, 1), (1, 2), (2, 3)], attrs):
        G.add_edge(u, v, **d)
        G.add_edge(v, u, **d)
    P = {"roads": {_key(G, 0, 1): {"road_real": 0.9, "white_imp4": 0.8}, _key(G, 1, 2): {"road_real": 0.6},
                   _key(G, 2, 3): {"road_real": 0.95, "bridge_real": 0.1}}}
    ch = apply_corrections(G, P, enabled={"drop_gap", "white_to_rank4"})
    assert ch["white roads to rank 4"] == 1 and G.edges[0, 1]["road_rank"] == 4
    assert G.has_edge(1, 2) and abs(uncertainty_penalty(G.edges[1, 2]) - 2.2) < 1e-9  # a likely gap stays, priced 1 + 3 (1 - p)
    assert uncertainty_penalty({"scan_p_real": 0.9}) == 1.0 and abs(uncertainty_penalty({"scan_p_real": 0.3}) - 2.2) < 1e-9
    assert G.edges[2, 3]["bridge"]  # bridge_to_culvert not switched on
    assert uncertainty_penalty({"scan_gap": True}) == 1.5 and uncertainty_penalty({}) == 1.0


def test_real_bench_missions_are_seeded():
    from coa_net.realbench import random_real_mission

    places = {"A": (-2.0, 48.0), "B": (-2.0, 48.5), "C": (-2.6, 48.2), "D": (-1.5, 47.8)}
    m1, m2 = random_real_mission(places, 7), random_real_mission(places, 7)
    assert m1 == m2 and 1 <= len(m1.convoys) <= 4
    for c in m1.convoys:
        assert c.origin.name != c.destination.name and c.latest_arrival_min - c.earliest_departure_min == 180


def test_route_checks_list_gaps_doubtful_roads_and_bridges():
    import networkx as nx

    from coa_net.scanlearn import route_checks

    G = nx.DiGraph()
    for n in range(5):
        G.add_node(n, x=n * 0.01, y=48.0)
    kinds = [dict(), dict(scan_gap=True, scan_p_real=0.6), dict(scan_p_real=0.4), dict(scan_p_real=0.3), dict(bridge=True, scan_p_real=0.95)]
    for i, k in enumerate(kinds[:4]):
        G.add_edge(i, i + 1, source_id="scan", length_m=1000.0, **k)
    G.add_edge(4, 3, source_id="scan", length_m=1000.0)
    ch = route_checks(G, [0, 1, 2, 3, 4])
    assert [c["kind"].split(":")[0] for c in ch] == ["label-gap join", "doubtful road"]
    assert abs(ch[1]["start_km"] - 2.0) < 1e-9 and abs(ch[1]["end_km"] - 4.0) < 1e-9 and ch[1]["p"] == 0.3
