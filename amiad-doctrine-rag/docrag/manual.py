"""Rules typed by hand from a PDF page (a table, a diagram, a sentence the extractor missed).

They go through the same sign-off as the automatic ones. A rule typed by hand always needs the PDF comparison,
and it is checked like the others: the number must be stated in the quote, and the quote is looked for on the page.
A quote that is not in the text layer (a scanned table, a diagram) is allowed but shows as CHECK in the sign-off sheet.
"""
from __future__ import annotations

import csv
from pathlib import Path

from . import semantics as SEM
from .extract import Rule, _read_for, strictness_of
from .reliability import rule_reliability
from .templates import TEMPLATES
from .text import match_key, number_supported

BOUNDS = {"max", "min", "exact", "flag"}
COLUMNS = ["template", "bound", "value", "unit", "strictness", "scope", "conditions", "document", "page", "quote", "note", "semantic_class"]


def _split(s: str) -> list[str]:
    return [x.strip() for x in (s or "").replace(",", "|").split("|") if x.strip()]


def load_manual(path: str | Path, chunks, reliability: dict | None = None) -> tuple[list[Rule], list[str]]:
    path = Path(path)
    if not path.exists():
        return [], []
    docs = sorted({c.doc for c in chunks})
    page_q = {(d, row[0]): row[1] for d, info in (reliability or {}).items() for row in info.get("page_flags", [])}
    rules: list[Rule] = []
    problems: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        head = f.readline()
        f.seek(0)
        delim = ";" if head.count(";") > head.count(",") else ","
        for n, row in enumerate(csv.DictReader(f, delimiter=delim), 2):
            row = {k: (v or "").strip() for k, v in row.items() if k}
            if not any(row.values()) or row.get("template", "").startswith("#"):
                continue
            where = f"{path.name} line {n}"
            tpl, bound = row.get("template", ""), row.get("bound", "")
            if tpl not in TEMPLATES or tpl.startswith("EVID."):
                problems.append(f"{where}: unknown template {tpl!r} (one of {', '.join(TEMPLATES)})")
                continue
            if bound not in BOUNDS:
                problems.append(f"{where}: bound must be max, min, exact or flag")
                continue
            try:
                value = None if bound == "flag" else float(row["value"].replace(",", "."))
                page = int(row["page"])
            except ValueError:
                problems.append(f"{where}: value and page must be numbers")
                continue
            unit = row.get("unit") or None
            if bound != "flag" and TEMPLATES[tpl]["units"] and unit not in TEMPLATES[tpl]["units"]:
                problems.append(f"{where}: unit {unit!r} not valid for {tpl} (one of {sorted(TEMPLATES[tpl]['units'])})")
                continue
            match = [d for d in docs if row.get("document", "").lower() in d.lower()] if row.get("document") else []
            if len(match) != 1:
                problems.append(f"{where}: document {row.get('document')!r} must match exactly one of {docs}")
                continue
            doc, quote = match[0], row.get("quote", "")
            if not quote:
                problems.append(f"{where}: the quote is required")
                continue
            on_page = [c for c in chunks if c.doc == doc and c.page == page]
            holder = next((c for c in on_page if match_key(quote) in match_key(c.text)), None)
            in_source = holder is not None or match_key(quote) in match_key(" ".join(c.text for c in on_page))
            strict = row.get("strictness") or strictness_of(quote) or "must"
            r = Rule(tpl, bound, value, unit, strict, quote, holder.id if holder else f"{doc}:{page}:manual", doc, page,
                     holder.para if holder else 0, holder.section if holder else "", _split(row.get("scope", "")),
                     _split(row.get("conditions", "")), [], origin="manual")
            r.checks = {"quote_in_source": in_source,
                        "number_in_quote": None if value is None else number_supported(value, quote),
                        "modal_in_quote": strictness_of(quote) is not None,
                        "all_ok": bool(in_source and (value is None or number_supported(value, quote)))}
            level, note, _ = rule_reliability(getattr(holder, "quality", "ok"), (reliability or {}).get(doc),
                                              page_q.get((doc, page), "ok"))
            r.reliability, r.verify_against_pdf = level, True       # typed by hand: always compared with the PDF page
            r.reliability_note = ("typed by hand from the PDF page" + (f" ({row['note']})" if row.get("note") else "")
                                  + (f"; {note}" if note else ""))
            # The class comes from the wording, unless the person typed one. A typed rule whose quote reads as advice,
            # a threshold or a description is NOT turned into a constraint silently: the sheet shows the class.
            typed = row.get("semantic_class", "")
            rd = _read_for(r, quote)
            r.operator = {"max": "<=", "min": ">=", "exact": "="}.get(bound, "")
            if typed in SEM.CLASSES:
                r.semantic_class = typed
                r.interpretation = [f"class typed by hand: {SEM.PLAIN[typed]}"]
            elif rd is not None:
                r.semantic_class = rd.cls
                r.applies_to, r.effect, r.interpretation = rd.applies_to, rd.effect, list(rd.notes)
                if rd.cls == SEM.MANDATORY and r.conditions:
                    r.semantic_class = SEM.CONDITIONAL
            else:
                r.semantic_class = SEM.AMBIGUOUS
                r.interpretation = ["the wording could not be read"]
            if not r.hard:
                r.missing = list(r.missing) + [f"typed as a constraint, but the wording reads as: {SEM.PLAIN[r.semantic_class]}. "
                                               "Confirm in the sign-off sheet (edit_class) or it is not exported as a constraint"]
            r.assign_id()
            rules.append(r)
    return rules, problems