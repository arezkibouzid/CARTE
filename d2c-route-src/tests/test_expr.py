import pytest

from d2c_route.expr import ExprError, compile_expr, eval_feature, eval_predicate

VEH = {"mlc": 40, "height_m": 3.6, "weight_t": 38}


def test_three_valued_logic():
    assert eval_predicate("not edge.bridge or edge.mlc >= vehicle.mlc", {"bridge": False, "mlc": None}, VEH) is True
    assert eval_predicate("not edge.bridge or edge.mlc >= vehicle.mlc", {"bridge": True, "mlc": 30}, VEH) is False
    assert eval_predicate("not edge.bridge or edge.mlc >= vehicle.mlc", {"bridge": True, "mlc": None}, VEH) is None
    assert eval_predicate("edge.max_weight_t is None or edge.max_weight_t >= vehicle.weight_t", {"max_weight_t": None}, VEH) is True
    # False dominates unknown in a conjunction, True dominates in a disjunction
    assert eval_predicate("edge.mlc >= 10 and edge.tunnel", {"mlc": None, "tunnel": False}, VEH) is False
    assert eval_predicate("edge.mlc >= 10 or edge.tunnel", {"mlc": None, "tunnel": True}, VEH) is True


def test_feature_clipping_and_unknown():
    assert eval_feature("edge.slope_pct / 15", {"slope_pct": 30}) == 1.0
    assert eval_feature("edge.slope_pct / 15", {"slope_pct": None}) == 0.0
    assert eval_feature("not edge.forest", {"forest": False}) == 1.0


@pytest.mark.parametrize(
    "src",
    ["__import__('os')", "edge.__class__", "open('x')", "edge.unknown_attr > 1", "lambda: 1", "[x for x in edge]"],
)
def test_rejects_unsafe_or_unknown(src):
    with pytest.raises(ExprError):
        compile_expr(src)
