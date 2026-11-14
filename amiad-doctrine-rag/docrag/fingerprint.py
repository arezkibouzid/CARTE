"""The exact reviewed version of a rule. An approval is tied to it: if the value, unit, operator, bound, scope, conditions,
class, strictness or the quotation change after the person approved, the approval no longer applies and the rule must be
approved again. The fingerprint is computed from the rule as the person saw it AFTER their edits."""
from __future__ import annotations

import hashlib
import json

MEANING_EDITS = ("bound", "value", "unit", "strictness", "semantic_class", "scope", "conditions")


def _norm(xs) -> list:
    return [" ".join(str(x).split()) for x in (xs or [])]


def of_fields(id, template, bound, value, unit, operator, strictness, semantic_class, scope, conditions, quote, doc, page) -> str:
    body = {"id": id, "template": template, "bound": bound, "value": value, "unit": unit, "operator": operator or "",
            "strictness": strictness, "class": semantic_class or "", "scope": _norm(scope), "conditions": _norm(conditions),
            "quote": " ".join((quote or "").split()), "doc": doc, "page": page}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def of_rule(r) -> str:
    return of_fields(r.id, r.template, r.bound, r.value, r.unit, r.operator, r.strictness, r.semantic_class,
                     r.scope, r.conditions, r.quote, r.doc, r.page)


def of_export(x: dict) -> str:
    s = x.get("source") or {}
    return of_fields(x.get("id"), x.get("template"), x.get("bound"), x.get("value"), x.get("unit"), x.get("operator"),
                     x.get("strictness"), x.get("semantic_class"), x.get("scope"), x.get("conditions"), x.get("quote"),
                     s.get("document"), s.get("page"))