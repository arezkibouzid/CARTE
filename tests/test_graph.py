from coa_net.corridors import CorridorGraph
from coa_net.graph import ukey
from coa_net.scenarios import synthetic_theatre


def test_critical_elements_of_the_demo_theatre():
    H = CorridorGraph(synthetic_theatre(0).G)
    by_name = {el.name: el for el in H.elements.values() if el.kind != "articulation"}
    assert by_name["Pont-Ouest"].attrs["mlc"] == 120
    assert by_name["Pont-Centre"].attrs["mlc"] == 30
    assert by_name["Pont-Est"].attrs["mlc"] == 100
    assert by_name["bridge col 9"].attrs["mlc"] is None  # unknown, never guessed
    tunnel = next(el for el in H.elements.values() if el.kind == "tunnel")
    assert tunnel.attrs["max_height_m"] == 3.8


def test_corridors_cover_every_road_once():
    H = CorridorGraph(synthetic_theatre(0).G)
    keys = {ukey(u, v) for u, v in H.G.edges}
    covered = [ukey(u, v) for k in H.corridors.values() for u, v in zip(k.nodes[:-1], k.nodes[1:])]
    assert set(covered) == keys and len(covered) == len(keys)
    # every corridor joins two decision points and never crosses one in between
    for k in H.corridors.values():
        assert not (set(k.nodes[1:-1]) & H.dps)


def test_decision_points_include_places_and_element_ends():
    th = synthetic_theatre(0)
    H = CorridorGraph(th.G)
    named = {n for n, d in th.G.nodes(data=True) if d.get("name")}
    assert named <= H.dps
    for el in H.elements.values():
        if el.kind == "bridge":
            assert set(el.nodes) & H.dps
