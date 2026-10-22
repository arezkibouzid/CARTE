"""A tiny, safe expression language for rule predicates and cost features.

Rules compiled by the LLM are stored as strings such as::

    edge.mlc >= vehicle.mlc
    not edge.tunnel or edge.max_height_m >= vehicle.height_m + 0.3
    min(1, edge.slope_pct / 12)

Expressions are parsed with :mod:`ast` and only a whitelisted subset of Python
is accepted, so LLM output can never execute code. Evaluation uses Kleene
three-valued logic: a missing map attribute (``None``) is *unknown*, and a
comparison involving an unknown value is unknown rather than silently true or
false. The optimiser treats an unknown hard predicate as allowed-with-risk and
the verifier reports it, which is the "explicit unknown" policy of the
proposal (section 6.1, C0).
"""

from __future__ import annotations

import ast
import operator
from functools import lru_cache
from typing import Any, Mapping

EDGE_ATTRS = {
    "length_m",
    "highway",
    "road_rank",  # 0 motorway ... 6 track, see graph.ROAD_RANK
    "maxspeed_kmh",
    "width_m",
    "lanes",
    "paved",
    "max_weight_t",
    "mlc",
    "bridge",
    "tunnel",
    "max_height_m",
    "slope_pct",
    "built_up",
    "forest",
    "centrality",
    "threat",
    "weather",
    "closed",
}
VEHICLE_ATTRS = {
    "weight_t",
    "mlc",
    "width_m",
    "height_m",
    "max_speed_kmh",
    "max_slope_pct",
    "tracked",
    "n_vehicles",
}
FUNCTIONS = {"min": min, "max": max, "abs": abs}


class _Unknown:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "UNKNOWN"

    def __bool__(self) -> bool:  # guard against accidental truthiness
        raise TypeError("UNKNOWN has no truth value")


UNKNOWN = _Unknown()

_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


class ExprError(ValueError):
    pass


def _validate(node: ast.AST) -> None:
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            if not isinstance(n.value, ast.Name) or n.value.id not in ("edge", "vehicle"):
                raise ExprError("only edge.<attr> and vehicle.<attr> are allowed")
            vocab = EDGE_ATTRS if n.value.id == "edge" else VEHICLE_ATTRS
            if n.attr not in vocab:
                raise ExprError(f"unknown attribute {n.value.id}.{n.attr}")
        elif isinstance(n, ast.Name):
            if n.id not in ("edge", "vehicle", "None", "True", "False") and n.id not in FUNCTIONS:
                raise ExprError(f"unknown name {n.id}")
        elif isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name) or n.func.id not in FUNCTIONS or n.keywords:
                raise ExprError("only min/max/abs calls are allowed")
        elif isinstance(n, ast.Constant):
            if not isinstance(n.value, (int, float, str, bool, type(None))):
                raise ExprError("unsupported constant")
        elif isinstance(
            n,
            (
                ast.Expression,
                ast.BoolOp,
                ast.And,
                ast.Or,
                ast.UnaryOp,
                ast.Not,
                ast.USub,
                ast.Compare,
                ast.BinOp,
                ast.Load,
                ast.Tuple,
                ast.List,
                ast.In,
                ast.NotIn,
                ast.Is,
                ast.IsNot,
                *_CMP,
                *_BIN,
            ),
        ):
            continue
        else:
            raise ExprError(f"unsupported syntax: {type(n).__name__}")


@lru_cache(maxsize=4096)
def compile_expr(src: str) -> ast.Expression:
    try:
        tree = ast.parse(src.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"cannot parse {src!r}: {exc}") from exc
    _validate(tree)
    return tree


def _and(values: list[Any]) -> Any:
    if any(v is False for v in values):
        return False
    if any(v is UNKNOWN for v in values):
        return UNKNOWN
    return True


def _or(values: list[Any]) -> Any:
    if any(v is True for v in values):
        return True
    if any(v is UNKNOWN for v in values):
        return UNKNOWN
    return False


def _truth(v: Any) -> Any:
    if v is UNKNOWN:
        return UNKNOWN
    return bool(v)


class _Evaluator:
    def __init__(self, edge: Mapping[str, Any], vehicle: Mapping[str, Any]):
        self.edge = edge
        self.vehicle = vehicle

    def ev(self, n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return self.ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return {"None": None, "True": True, "False": False}[n.id]
        if isinstance(n, ast.Attribute):
            src = self.edge if n.value.id == "edge" else self.vehicle
            v = src.get(n.attr)
            return UNKNOWN if v is None else v
        if isinstance(n, (ast.Tuple, ast.List)):
            return [self.ev(e) for e in n.elts]
        if isinstance(n, ast.BoolOp):
            # Kleene logic with short-circuit on the deciding value
            vals = []
            for e in n.values:
                v = _truth(self.ev(e))
                vals.append(v)
                if isinstance(n.op, ast.And) and v is False:
                    return False
                if isinstance(n.op, ast.Or) and v is True:
                    return True
            return _and(vals) if isinstance(n.op, ast.And) else _or(vals)
        if isinstance(n, ast.UnaryOp):
            v = self.ev(n.operand)
            if v is UNKNOWN:
                return UNKNOWN
            if isinstance(n.op, ast.Not):
                return not v
            return -v
        if isinstance(n, ast.BinOp):
            a, b = self.ev(n.left), self.ev(n.right)
            if a is UNKNOWN or b is UNKNOWN:
                return UNKNOWN
            if isinstance(n.op, ast.Div) and b == 0:
                return UNKNOWN
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.Call):
            args = [self.ev(a) for a in n.args]
            if any(a is UNKNOWN for a in args):
                return UNKNOWN
            return FUNCTIONS[n.func.id](*args)
        if isinstance(n, ast.Compare):
            left = self.ev(n.left)
            results = []
            for op, comp in zip(n.ops, n.comparators):
                right = self.ev(comp)
                if isinstance(op, (ast.Is, ast.IsNot)):
                    # "x is None" asks whether the attribute is missing: always decidable
                    missing = left is UNKNOWN or left is None
                    r = missing if right is None else left is right
                    results.append(r if isinstance(op, ast.Is) else not r)
                elif left is UNKNOWN or right is UNKNOWN:
                    results.append(UNKNOWN)
                elif isinstance(op, (ast.In, ast.NotIn)):
                    r = left in right
                    results.append(r if isinstance(op, ast.In) else not r)
                else:
                    results.append(_CMP[type(op)](left, right))
                left = right
            return _and(results)
        raise ExprError(f"unsupported node {type(n).__name__}")  # pragma: no cover


def evaluate(src: str, edge: Mapping[str, Any], vehicle: Mapping[str, Any] | None = None) -> Any:
    """Evaluate ``src``; returns a value, or :data:`UNKNOWN`."""
    return _Evaluator(edge, vehicle or {}).ev(compile_expr(src))


def eval_predicate(src: str, edge: Mapping[str, Any], vehicle: Mapping[str, Any]) -> bool | None:
    """Three-valued predicate: True, False, or None when map data is missing."""
    v = _truth(evaluate(src, edge, vehicle))
    return None if v is UNKNOWN else v


def eval_feature(src: str, edge: Mapping[str, Any], vehicle: Mapping[str, Any] | None = None) -> float:
    """Numeric cost feature clipped to [0, 1]; unknown counts as 0 (risk is
    priced separately by the unknown penalty)."""
    v = evaluate(src, edge, vehicle)
    if v is UNKNOWN or v is None:
        return 0.0
    return float(min(1.0, max(0.0, float(v))))


def is_valid(src: str) -> bool:
    try:
        compile_expr(src)
        return True
    except ExprError:
        return False
