"""The output contract: what docrag hands to the planner, and the checks that guarantee its shape.

Only rules a person accepted are exported (unless provisional=True, then every draft rule is exported and
marked provisional). The planner must not read anything else from a work folder.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from . import pipeline as P
from . import semantics as SEM
from .fingerprint import of_export, of_rule
from .templates import TEMPLATES

SCHEMA_VERSION = "1.0"
EXPORT_NAME = "requirements_for_planner.json"
BOUNDS = {"max", "min", "exact", "flag", "delta-", "delta+"}
STRICTNESS = {"must", "should", "may"}
ADVISORY_STRICTNESS = STRICTNESS | {"none"}


class ExportError(Exception):
    pass


def load_template_map(path: str | Path | None) -> dict:
    """{docrag template id: planner template id}. Edit this file to match the planner's own list."""
    if not path:
        return {}
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(m, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in m.items()):
        raise ExportError("template map must be a JSON object of text to text")
    return m


def _rule_out(r, approved_by: str, provisional: bool, tmap: dict, pdf_checked: bool = False) -> dict:
    return {
        "id": r.id,
        "template": r.template,
        "template_planner": tmap.get(r.template),
        "bound": r.bound,
        "value": r.value,
        "unit": r.unit,
        "strictness": r.strictness,
        "semantic_class": r.semantic_class,
        "operator": r.operator,
        "binding": SEM.is_hard(r.semantic_class),     # False: advice, threshold or description. Never a hard constraint.
        "scope": r.scope,
        "conditions": r.conditions,
        "missing_information": r.missing,
        "quote": r.quote,                                  # word for word
        "applies_to": r.applies_to,                        # interpretation (kept apart from the quote and the evidence)
        "effect": r.effect,
        "interpretation": r.interpretation,
        "context_evidence": r.context,                     # neighbouring sentences and passages: evidence only
        "context_status": r.context_status,
        "source": {"document": r.doc, "page": r.page, "paragraph": r.para, "section": r.section},
        "also_stated_in": r.also_in,
        "conflict_note": r.conflict,
        "origin": r.origin,
        "reliability": r.reliability,
        "reliability_note": r.reliability_note,
        "verify_required": r.verify_against_pdf,
        "verified_against_pdf": bool(pdf_checked),
        "status": "provisional" if provisional else "approved",
        "approved_by": None if provisional else approved_by,
        "approval_fingerprint": None if provisional else of_rule(r),   # the exact version a person approved
    }


def export_for_planner(work: str | Path, template_map: dict | None = None, provisional: bool = False,
                       allow_empty: bool = False, out: str | Path | None = None) -> Path:
    work = Path(work)
    if not (work / P.DRAFT).exists():
        raise ExportError("no draft rules yet. Run the extraction first.")
    draft = P.load_draft(work)
    dec = P.load_decisions(work)
    tmap = template_map or {}
    if provisional:
        chosen = [(r, "", False) for r in draft if r.semantic_class != SEM.UNCLASSIFIED]   # unclassified evidence is never exported
    else:
        from .signoff import apply_edits   # the reviewer's edits (value, unit, scope, conditions) are part of the approval
        chosen = [(apply_edits(r, dec[r.id].get("edits")), dec[r.id].get("reviewer", ""), dec[r.id].get("pdf_checked", False))
                  for r in draft if dec.get(r.id, {}).get("decision") == "accept"]
    if not chosen and not allow_empty:
        raise ExportError("no approved rules. Accept rules first (or use provisional mode for a test run).")
    if not provisional:
        stale = [r.id for r, _, _ in chosen if dec[r.id].get("fingerprint") != of_rule(r)]
        if stale:   # changed after approval (or approved before versions were recorded): the approval no longer applies
            raise ExportError("changed after approval, or approved without a recorded version: approve again: " + ", ".join(stale))
        unverified = [r.id for r, _, chk in chosen if r.verify_against_pdf and not chk]
        if unverified:   # typed, edited or unreliable: nobody compared it with the PDF
            raise ExportError("approved without the PDF comparison that these rules require: " + ", ".join(unverified))
        unresolved = [r.id for r, _, _ in chosen if r.semantic_class == SEM.UNCLASSIFIED]
        if unresolved:
            raise ExportError("approved but still unclassified evidence (meaning, scope and conditions not established), "
                              "not exported: " + ", ".join(unresolved))
        bad = [r.id for r, _, _ in chosen if r.context_status == SEM.INCOMPLETE or r.semantic_class == SEM.AMBIGUOUS]
        if bad:   # approved without the context being written, or an ambiguous rule never classified by a person
            raise ExportError("approved rules with incomplete context or ambiguous wording, not exported: " + ", ".join(bad))
    out_all = [_rule_out(r, rev, provisional, tmap, chk) for r, rev, chk in chosen]
    rules = [x for x in out_all if x["binding"]]
    advisory = [x for x in out_all if not x["binding"]]      # approved, but not constraints: listed apart
    sources = json.loads((work / "sources.json").read_text(encoding="utf-8")) if (work / "sources.json").exists() else []
    sources = [{k: v for k, v in s.items() if k in ("name", "sha256", "bytes")} for s in sources]   # no local paths in the export
    body = {"schema_version": SCHEMA_VERSION, "tool": "docrag " + __version__,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provisional": provisional, "source_documents": sources,
            "unmapped_templates": sorted({r["template"] for r in rules if tmap and not r["template_planner"]}),
            "rules": rules, "advisory": advisory,
            "note": "rules = binding constraints. advisory = approved information that is NOT a constraint (advice, thresholds, descriptions)."}
    body["rules_sha256"] = hashlib.sha256(json.dumps(rules, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    body["advisory_sha256"] = hashlib.sha256(json.dumps(advisory, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    dest = Path(out) if out else work / "export" / EXPORT_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    return dest


class _PageText(str):
    """The text of one page of the index. `quote in page` is true when the quote lies in a passage that STARTS on this page
    (a sentence may run over to the next page), or lies wholly in the part of a joined passage that starts on this page.
    A quote that starts in the part that came from the next page is NOT on this page."""
    def __new__(cls, parts):
        o = super().__new__(cls, " ".join(p[0] for p in parts))
        o.parts = parts
        return o

    def __contains__(self, q):
        for here, rest in self.parts:
            if q in here or (q in rest and rest.find(q) < len(here)):
                return True
        return False


def _source_passages(work: str | Path) -> dict[tuple, str]:
    """(document, page) -> all passage text of that page, in the form used to compare quotes."""
    from .ingest import Chunk
    from .text import match_key
    out: dict[tuple, list] = {}
    for d in json.loads((Path(work) / "chunks.json").read_text(encoding="utf-8")):
        c = Chunk(**d)
        for pg, here, rest in c.page_slices():
            out.setdefault((c.doc, pg), []).append((match_key(here), match_key(rest)))
    return {k: _PageText(v) for k, v in out.items()}


def _operator_in_quote(quote: str, value: float) -> str:
    """Read the quote again, independently of the extraction, and say how the number compares ('' if it cannot be told)."""
    from .text import quantities
    for q in quantities(quote):
        if abs(q["value"] - value) < 1e-9:
            rd = SEM.analyse(quote, (q["start"], q["end"]))
            return rd.operator if rd is not None else ""
    return ""


def validate_export(path: str | Path, work: str | Path | None = None) -> list[str]:
    """Return a list of problems (empty list = file respects the contract). The planner side can run this too.

    Always checks that the number is stated in the quote. With `work` (the folder of the index) it also checks
    that the quote is word for word in the cited page of the source document."""
    from .text import match_key, number_supported
    errs: list[str] = []
    pages = _source_passages(work) if work is not None else None
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [f"cannot read file: {e}"]
    if d.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version must be {SCHEMA_VERSION}")
    rules = d.get("rules")
    if not isinstance(rules, list):
        return errs + ["rules must be a list"]
    if d.get("rules_sha256") != hashlib.sha256(json.dumps(rules, sort_keys=True, ensure_ascii=False).encode()).hexdigest():
        errs.append("rules were changed after export (hash mismatch)")
    recorded = {}
    if work is not None and (Path(work) / P.DECISIONS).exists():
        recorded = json.loads((Path(work) / P.DECISIONS).read_text(encoding="utf-8"))

    def human_checks(r, w):
        """Every exported entry, automatic, hand-typed or edited, passes the same structural checks and is tied to the exact
        version a person approved."""
        for f in ("scope", "conditions"):
            v = r.get(f)
            if not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in v):
                errs.append(f"{w}: {f} must be a list of short, non-empty texts")
        if not r.get("origin"):
            errs.append(f"{w}: no provenance (origin)")
        if d.get("provisional"):
            return
        if r.get("approval_fingerprint") != of_export(r):
            errs.append(f"{w}: changed after approval (value, unit, operator, bound, scope, conditions, class or quotation "
                        "no longer match the version a person approved): approve again")
        if recorded:
            rec = recorded.get(r.get("id")) or {}
            if rec.get("decision") != "accept" or rec.get("fingerprint") != of_export(r):
                errs.append(f"{w}: does not match the approval recorded for this id: approve again")

    adv = d.get("advisory", [])
    if not isinstance(adv, list):
        errs.append("advisory must be a list")
        adv = []
    elif d.get("advisory_sha256") != hashlib.sha256(json.dumps(adv, sort_keys=True, ensure_ascii=False).encode()).hexdigest():
        errs.append("advisory entries were changed after export (hash mismatch)")
    for i, r in enumerate(adv):
        w = f"advisory {i} ({r.get('id')})"
        human_checks(r, w)
        if r.get("binding") is not False or SEM.is_hard(r.get("semantic_class", "")) and r.get("semantic_class"):
            errs.append(f"{w}: an advisory entry must not be binding")
        if r.get("semantic_class") == SEM.UNCLASSIFIED:
            errs.append(f"{w}: unclassified evidence (meaning not established) must never be exported")
        if r.get("context_status") == SEM.INCOMPLETE:
            errs.append(f"{w}: context incomplete")
    seen = set()
    for i, r in enumerate(rules):
        w = f"rule {i} ({r.get('id')})"
        human_checks(r, w)
        if r.get("semantic_class") is not None and r.get("semantic_class") not in SEM.HARD:
            errs.append(f"{w}: class {r.get('semantic_class')!r} is not a constraint (advice, threshold, description or ambiguous)")
        if r.get("binding") is False:
            errs.append(f"{w}: marked non-binding but listed as a rule")
        if str(r.get("template", "")).startswith("EVID.") or r.get("semantic_class") == SEM.UNCLASSIFIED:
            errs.append(f"{w}: unclassified evidence is not a planner rule")
        if r.get("context_status") == SEM.INCOMPLETE and not d.get("provisional"):
            errs.append(f"{w}: the context is incomplete and nobody completed it")
        if r.get("operator") not in (None, "", *SEM.OPERATORS):
            errs.append(f"{w}: bad operator")
        op, bd = r.get("operator") or "", r.get("bound")
        # SEG.TOWN is the one template where they differ on purpose: 'avoid towns of MORE than 10 000' keeps operator '>' (how the
        # number compares with the town) and bound 'max' (the planner's limit on the town size).
        if r.get("template") != "SEG.TOWN" and ((op in ("<", "<=") and bd != "max") or (op in (">", ">=") and bd != "min")):
            errs.append(f"{w}: the operator {op!r} and the bound {bd!r} contradict each other (minimum and maximum mixed up)")
        by_hand = str(r.get("origin", "")).startswith("manual") or "+edited" in str(r.get("origin", ""))
        if by_hand and not d.get("provisional") and not r.get("verified_against_pdf"):
            errs.append(f"{w}: typed or edited by a person, so the quotation cannot vouch for it: a PDF comparison is required")
        if op and isinstance(r.get("value"), (int, float)) and r.get("quote") and not by_hand:
            again = _operator_in_quote(r["quote"], float(r["value"]))
            if again and again != op:
                errs.append(f"{w}: the operator {op!r} does not match the wording of the quote (read again: {again!r})")
        if r.get("id") in seen:
            errs.append(f"{w}: duplicate id")
        seen.add(r.get("id"))
        if r.get("template") not in TEMPLATES:
            errs.append(f"{w}: unknown template")
        if r.get("bound") not in BOUNDS:
            errs.append(f"{w}: bad bound")
        if r.get("strictness") not in STRICTNESS:
            errs.append(f"{w}: bad strictness")
        if r.get("bound") == "flag":
            if r.get("value") is not None:
                errs.append(f"{w}: flag rule must have no value")
        elif not isinstance(r.get("value"), (int, float)) or isinstance(r.get("value"), bool):
            errs.append(f"{w}: value must be a number")
        units = TEMPLATES.get(r.get("template"), {}).get("units") or set()
        if r.get("bound") != "flag" and units and r.get("unit") not in units:
            errs.append(f"{w}: unit {r.get('unit')!r} not valid for {r.get('template')}")
        src = r.get("source") or {}
        if not (src.get("document") and isinstance(src.get("page"), int) and r.get("quote")):
            errs.append(f"{w}: missing source or quote")
        if isinstance(r.get("value"), (int, float)) and r.get("quote") and not number_supported(float(r["value"]), r["quote"]):
            errs.append(f"{w}: the value {r['value']} is not stated in the quote")
        if pages is not None and src.get("document") and r.get("quote"):
            if match_key(r["quote"]) not in pages.get((src["document"], src.get("page")), ""):
                errs.append(f"{w}: the quote is not in {src['document']} page {src.get('page')} of the index")
        if d.get("provisional"):
            if r.get("status") != "provisional":
                errs.append(f"{w}: provisional file holds a non-provisional rule")
        elif r.get("status") != "approved" or not r.get("approved_by"):
            errs.append(f"{w}: not approved by a named person")
        elif r.get("verify_required") and not r.get("verified_against_pdf"):
            errs.append(f"{w}: source not fully reliable and not compared with the PDF")
    return errs