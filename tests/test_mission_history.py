"""Mission history: the next leg starts where the previous one ended and may not reuse its path (forbidden by default)."""

from __future__ import annotations

import numpy as np
import shapely

from coa_net.pdfexercises import to_l93
from interface.backend import amiad_api as A
from interface.backend import text_order as T


class FakeNet:
    """Three one-way sections in Lambert-93: the old leg (two sections) and a parallel road 500 m away."""

    def __init__(self):
        p = lambda lon, lat: tuple(to_l93([(lon, lat)])[0])
        a, b, c = p(5.00, 49.00), p(5.05, 49.00), p(5.10, 49.00)
        d, e = p(5.00, 49.0045), p(5.10, 49.0045)
        self.lines = np.array([shapely.LineString([a, b]), shapely.LineString([b, c]), shapely.LineString([d, e])])
        self.tree = shapely.STRtree(self.lines)
        self.edges = [0, 1, 2]
        self.xy = np.array([a, b, c, d, e])
        self.u, self.v = np.array([0, 1, 3]), np.array([1, 2, 4])
        self.length_m = shapely.length(self.lines)


def test_history_mask_keeps_only_the_old_path():
    N = FakeNet()
    m = A.history_mask(N, [[[5.00, 49.00], [5.10, 49.00]]])
    assert m.tolist() == [True, True, False]  # the parallel road 500 m away stays allowed


def test_relaxation_only_around_the_shared_point():
    N = FakeNet()
    near = A.near_points_mask(N, [[5.0, 49.0]], 4000)
    assert near.tolist() == [True, False, False]  # only the section touching the shared point can be reopened


def test_written_orders_continue_and_forbid():
    assert T.history_of("Puis va à WP1 sans repasser par le trajet précédent") == (True, "forbid")
    assert T.history_of("Va à PA1 en évitant si possible le trajet précédent") == (False, "penalize")
    assert T.history_of("ensuite rejoins PA2 par le même chemin") == (True, "free")
    assert T.history_of("De PD4 à PA4") == (False, "unspecified")
    r = T.parse("Puis va à 49.11, 5.27 sans repasser par le trajet précédent", [], use_llm=False)
    assert r["scenario"]["_continue"] and r["scenario"]["historyMode"] == "forbid" and "start" not in r["scenario"]
