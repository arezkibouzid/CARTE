"""Sign-off sheet: a reviewer accepts, edits or rejects each draft rule. Only approved rules are used."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from dataclasses import replace

from . import semantics as SEM
from .extract import CONTEXT_NOTE, Rule, check_rule
from .fingerprint import MEANING_EDITS
from .templates import TEMPLATES

PDF_OK = {"yes", "y", "oui", "x"}
COLUMNS = ["id", "decision", "destination", "block_reason", "semantic_class", "context_status", "template", "bound", "operator", "value", "unit", "strictness", "scope", "conditions",
           "applies_to", "effect", "interpretation", "context_issues", "context_before", "context_after", "missing", "checks", "reliability", "pdf_check", "pdf_checked", "origin", "conflict", "doc", "page", "para", "quote",
           "edit_class", "edit_bound", "edit_value", "edit_unit", "edit_strictness", "edit_scope", "edit_conditions", "note"]


def write_sheet(rules: list[Rule], path: str | Path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig so Excel reads accents
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rules:
            c = r.checks or {}
            checks = "OK" if c.get("all_ok") else "CHECK: " + ", ".join(k for k, v in c.items() if v is False)
            w.writerow({
                "id": r.id, "decision": "", "destination": r.destination, "block_reason": r.block_reason, "semantic_class": SEM.PLAIN.get(r.semantic_class, r.semantic_class),
                "context_status": r.context_status, "template": r.template, "bound": r.bound, "operator": r.operator,
                "applies_to": r.applies_to, "effect": r.effect, "interpretation": " | ".join(r.interpretation),
                "context_issues": " | ".join(r.context_issues),
                "context_before": (r.context or {}).get("previous_sentence", ""), "context_after": (r.context or {}).get("next_sentence", ""),
                "value": "" if r.value is None else r.value, "unit": r.unit or "",
                "strictness": r.strictness, "scope": "; ".join(r.scope),
                "conditions": "; ".join(r.conditions), "missing": "; ".join(r.missing),
                "checks": checks, "reliability": r.reliability if not r.reliability_note else f"{r.reliability}: {r.reliability_note}"[:160],
                "pdf_check": ("REQUIRED: this looks like a table row or holds several measurements. Open the PDF, read the column header and "
                              "row conditions, write them in edit_scope / edit_conditions, then write yes in pdf_checked") if r.table_derived
                else "REQUIRED: the context is incomplete (see context_issues). Read the PDF, write what applies in edit_scope / edit_conditions, then write yes in pdf_checked" if r.context_status == SEM.INCOMPLETE
                else "REQUIRED: compare with the PDF page, then write yes in pdf_checked" if r.verify_against_pdf else "",
                "origin": r.origin, "conflict": r.conflict or "", "doc": r.doc,
                "page": r.page, "para": r.para, "quote": r.quote})


def _split(cell: str) -> list[str]:
    return [x.strip() for x in (cell or "").replace("|", ";").split(";") if x.strip()]


def apply_edits(rule: Rule, edits: dict | None) -> Rule:
    """A copy of the rule with the reviewer's edits (value, unit, bound, strictness, scope, conditions) applied."""
    if not edits:
        return rule
    r = replace(rule)
    for k in ("bound", "value", "unit", "strictness", "semantic_class"):
        if k in edits:
            setattr(r, k, edits[k])
    if "bound" in edits:   # the operator follows the bound a person chose, so the two never contradict each other
        r.operator = {"max": "<=", "min": ">=", "exact": "=", "flag": ""}.get(edits["bound"], r.operator)
    for k in ("scope", "conditions"):
        if edits.get(k):
            setattr(r, k, list(edits[k]))
    if edits.get("scope") or edits.get("conditions"):
        r.missing = [m for m in r.missing if m != CONTEXT_NOTE]   # a person has now written the context
        if r.context_status == SEM.INCOMPLETE:
            r.context_status = "completed_by_reviewer"
    if any(k in edits and edits[k] not in (None, "", []) for k in MEANING_EDITS):
        r.verify_against_pdf = True    # a person changed what the rule says: the quotation can no longer vouch for it alone
    if not r.origin.endswith("+edited"):
        r.origin += "+edited"
    if r.semantic_class != rule.semantic_class or r.destination != rule.destination:   # audit trail: a person changed the meaning
        r.history = list(rule.history) + [{"change": "reviewer edit", "class": [rule.semantic_class, r.semantic_class],
                                           "destination": [rule.destination, r.destination]}]
    return r


def sheet_decisions(rules: list[Rule], sheet: str | Path, reviewer: str = "") -> tuple[dict, list[str]]:
    """Read the filled-in sheet. Returns ({rule id: decision}, messages). A decision is
    {decision accept|reject, reviewer, pdf_checked, edits, note}. A rule that fails a check stays undecided (message)."""
    by_id = {r.id: r for r in rules}
    msgs: list[str] = []
    out: dict = {}
    with open(sheet, newline="", encoding="utf-8-sig") as f:
        head = f.readline()
        f.seek(0)
        # a French Excel saves the sheet with ";" instead of ","
        delim = ";" if head.count(";") > head.count(",") else ","
        for row in csv.DictReader(f, delimiter=delim):
            r = by_id.get(row["id"])
            if r is None:
                msgs.append(f"{row['id']}: not in rule list, ignored")
                continue
            d = (row.get("decision") or "").strip().lower()
            if not d:
                continue
            if d not in ("accept", "edit", "reject"):
                msgs.append(f"{r.id}: unknown decision {d!r}; rule stays draft")
                continue
            note = (row.get("note") or "").strip()
            if d == "reject":
                out[r.id] = {"decision": "reject", "reviewer": reviewer, "pdf_checked": False, "edits": {}, "note": note}
                continue
            pdf_ok = (row.get("pdf_checked") or "").strip().lower() in PDF_OK
            new_class = (row.get("edit_class") or "").strip()
            if new_class and new_class not in SEM.CLASSES:
                msgs.append(f"{r.id}: edit_class must be one of {', '.join(SEM.CLASSES)}; rule stays draft")
                continue
            if r.semantic_class == SEM.UNCLASSIFIED:
                if not new_class:
                    msgs.append(f"{r.id}: unclassified evidence. A person must establish what it is: edit_class, then "
                                "edit_scope / edit_conditions and pdf_checked = yes; rule stays draft")
                    continue
                if SEM.is_hard(new_class):
                    msgs.append(f"{r.id}: unclassified evidence cannot become a constraint here (it has no planner template). "
                                "If it is a real constraint, type it in manual_rules.csv with a real template; rule stays draft")
                    continue
                if not (pdf_ok and (_split(row.get("edit_scope")) or _split(row.get("edit_conditions")))):
                    msgs.append(f"{r.id}: unclassified evidence needs pdf_checked = yes and the scope or conditions in "
                                "edit_scope / edit_conditions before it is accepted as evidence; rule stays draft")
                    continue
            if r.semantic_class == SEM.AMBIGUOUS and not new_class:
                msgs.append(f"{r.id}: the wording is ambiguous. A person must decide what it is: write the class in edit_class "
                            "(and the context in edit_scope / edit_conditions); rule stays draft")
                continue
            promote = bool(new_class) and SEM.is_hard(new_class) and not r.hard
            if promote and not (pdf_ok and (_split(row.get("edit_scope")) or _split(row.get("edit_conditions")))):
                msgs.append(f"{r.id}: read as '{SEM.PLAIN.get(r.semantic_class)}'. To use it as a constraint: compare with the PDF "
                            "(pdf_checked = yes) and write why in edit_scope / edit_conditions; rule stays draft")
                continue
            if r.verify_against_pdf and not pdf_ok:
                msgs.append(f"{r.id}: a person must compare it with the PDF page first, then write yes in pdf_checked; rule stays draft")
                continue
            edits: dict = {}
            if new_class:
                edits["semantic_class"] = new_class
            try:
                if d == "edit":
                    if row.get("edit_bound"):
                        if row["edit_bound"] not in {"max", "min", "exact", "flag"}:
                            raise ValueError("bound must be max, min, exact or flag")
                        edits["bound"] = row["edit_bound"]
                    if row.get("edit_value"):
                        edits["value"] = float(row["edit_value"])
                    if row.get("edit_unit"):
                        units = TEMPLATES[r.template]["units"]
                        if units and row["edit_unit"] not in units:
                            raise ValueError(f"unit must be one of {sorted(units)}")
                        edits["unit"] = row["edit_unit"]
                    if row.get("edit_strictness"):
                        if row["edit_strictness"] not in {"must", "should", "may"}:
                            raise ValueError("strictness must be must, should or may")
                        edits["strictness"] = row["edit_strictness"]
            except (ValueError, KeyError) as e:
                msgs.append(f"{r.id}: invalid edit ({e}); rule stays draft")
                continue
            if _split(row.get("edit_scope")):
                edits["scope"] = _split(row["edit_scope"])
            if _split(row.get("edit_conditions")):
                edits["conditions"] = _split(row["edit_conditions"])
            if r.needs_context and not (edits.get("scope") or edits.get("conditions")):
                why = "table row or several measurements" if r.table_derived else "incomplete context: " + "; ".join(r.context_issues)[:200]
                msgs.append(f"{r.id}: {why}. Read the PDF and write what applies in edit_scope or edit_conditions; rule stays draft")
                continue
            if any(k in edits for k in MEANING_EDITS) and not pdf_ok:
                msgs.append(f"{r.id}: you changed what the rule says ({', '.join(k for k in MEANING_EDITS if k in edits)}). "
                            "Compare with the PDF and write yes in pdf_checked; rule stays draft")
                continue
            out[r.id] = {"decision": "accept", "reviewer": reviewer, "pdf_checked": pdf_ok, "edits": edits, "note": note}
    return out, msgs


def apply_sheet(rules: list[Rule], sheet: str | Path, reviewer: str = "") -> tuple[list[Rule], list[str]]:
    """Return (approved rules, messages). accept approves, reject rejects, edit approves the edited fields.
    The rules are changed in place (status and edits); `docrag approve` stores the same decisions on disk."""
    decisions, msgs = sheet_decisions(rules, sheet, reviewer)
    approved: list[Rule] = []
    for i, r in enumerate(rules):
        d = decisions.get(r.id)
        if not d:
            continue
        if d["decision"] == "reject":
            r.status = "rejected"
            continue
        r = rules[i] = apply_edits(r, d["edits"])
        r.status = "approved"
        approved.append(r)
    return approved, msgs


def write_json(rules: list[Rule], path: str | Path, meta: dict | None = None):
    Path(path).write_text(
        json.dumps({"meta": meta or {}, "rules": [r.to_dict() for r in rules]}, ensure_ascii=False, indent=1),
        encoding="utf-8")


def read_rules(path: str | Path) -> list[Rule]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for d in data["rules"]:
        d = {k: v for k, v in d.items() if k != "tier"}
        out.append(Rule(**d))
    return out