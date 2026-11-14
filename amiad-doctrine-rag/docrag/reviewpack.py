"""Team review pack: export every candidate to CSV files a teammate can work on, import the completed files back.

Nothing here approves anything by itself. The import turns each completed row into the same sign-off decision the sheet
and the review screen use, so every safety check (PDF comparison, context, class, exact reviewed version) still applies."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from . import pipeline as P
from . import semantics as SEM
from .fingerprint import of_rule

DECISIONS_OK = ("ACCEPT", "CORRECT", "REJECT", "UNSURE")
YES = {"yes", "y", "oui", "x", "true", "1"}
NO = {"no", "n", "non", "false", "0", ""}

# read-only evidence (a tamper check protects it) and the fields a teammate fills in
READONLY = ["review_id", "priority", "topic", "origin_kind", "destination", "document", "pdf_page", "quote", "proposed_class",
            "proposed_value", "proposed_unit", "proposed_operator", "proposed_bound", "proposed_scope", "proposed_conditions",
            "trust_warnings", "block_reason"]
EDITABLE = ["decision", "pdf_checked", "corrected_class", "corrected_value", "corrected_unit", "corrected_operator",
            "corrected_bound", "corrected_scope", "corrected_conditions", "reviewer_name", "comments", "reviewed_at"]
EVIDENCE = ["context_before", "context_after", "exceptions_hint", "heading", "pdf_file", "why_this_class", "proposed_template",
            "proposed_strictness", "version_fp", "pack_id"]
COLUMNS = READONLY + EDITABLE + EVIDENCE + ["row_check"]
CHECKED = READONLY + EVIDENCE            # everything a teammate must not change

EX_TOPICS = {"SEG.HEIGHT": "bridge height / clearance", "SEG.MLC": "bridge load (MLC)", "SEG.WEIGHT": "weight / axle load",
             "SEG.SPEED": "speed", "NET.GAP": "convoy spacing", "SEG.WIDTH": "road restriction (width)",
             "SEG.GRADE": "road restriction (gradient)", "SEG.TURN": "road restriction (turn radius)",
             "SEG.TOWN": "road restriction (towns)", "NET.DISTINCT": "itinerary separation", "SCH.HALT": "halts / scheduling",
             "PRO.ESCORT": "escort", "PRO.HAZMAT": "hazardous cargo"}
EX11_20 = {"SEG.HEIGHT", "SEG.MLC", "SEG.WEIGHT", "SEG.SPEED", "NET.GAP", "SEG.WIDTH", "SEG.GRADE", "SEG.TURN", "SEG.TOWN"}
UNIT_TOPIC = {"m": "distance", "ft": "distance", "km": "distance", "t": "mass", "km/h": "speed", "mph": "speed", "min": "time", "h": "time"}
CORR_BOUND = {"<": "max", "<=": "max", ">": "min", ">=": "min", "=": "exact"}


# ---- export ---------------------------------------------------------------------------------------------------------

def _origin_kind(r) -> str:
    return "manual" if str(r.origin).startswith("manual") else "automatic"


def _topic(r) -> str:
    if r.template.startswith("EVID."):
        return "unclassified (" + UNIT_TOPIC.get(r.unit or "", "other") + ")"
    return EX_TOPICS.get(r.template, r.template)


def _priority(r) -> int:
    if r.template in EX11_20 and r.destination in ("constraint", "blocked"):
        return 1
    if r.template in EX11_20:
        return 2
    if r.destination == "unclassified_evidence":
        return 3 if (r.unit or "") in UNIT_TOPIC else 4
    return 3


def _warnings(r) -> str:
    w = []
    if _origin_kind(r) == "manual":
        w.append("typed by hand: compare with the PDF")
    if r.verify_against_pdf:
        w.append("PDF comparison mandatory")
    if r.table_derived:
        w.append("table row or several measurements: the value may belong to another row or column")
    if r.reliability in ("low", "unknown"):
        w.append(f"reading reliability {r.reliability}" + (f" ({r.reliability_note})" if r.reliability_note else ""))
    elif r.reliability_note and "page" in r.reliability_note.lower():
        w.append(r.reliability_note)
    if r.conflict:
        w.append(r.conflict)
    w += [m for m in r.missing if m]
    return " | ".join(dict.fromkeys(w))


def _exceptions(r) -> str:
    text = " | ".join(r.context_issues)
    ctx = r.context or {}
    out = []
    if "exception" in text:
        out.append("next sentence: " + ctx.get("next_sentence", ""))
    if "previous sentence may limit" in text:
        out.append("previous sentence: " + ctx.get("previous_sentence", ""))
    return " | ".join(out)


def _pdf_files(work: Path) -> dict:
    p = work / "sources.json"
    if not p.exists():
        return {}
    return {Path(s["name"]).stem: s["name"] for s in json.loads(p.read_text(encoding="utf-8"))}


def _fields(r, pdf_files: dict, pack_id: str) -> dict:
    ctx = r.context or {}
    unclassified = r.destination == "unclassified_evidence"
    row = {
        "review_id": r.id, "priority": _priority(r), "topic": _topic(r), "origin_kind": _origin_kind(r), "destination": r.destination,
        "document": r.doc, "pdf_page": r.page, "quote": r.quote,
        "proposed_class": SEM.PLAIN.get(r.semantic_class, r.semantic_class) if not unclassified else "unclassified (meaning not established)",
        "proposed_value": "" if r.value is None else f"{r.value:g}", "proposed_unit": r.unit or "",
        "proposed_operator": r.operator, "proposed_bound": "" if unclassified else r.bound,
        "proposed_scope": "; ".join(r.scope), "proposed_conditions": "; ".join(r.conditions),
        "trust_warnings": _warnings(r), "block_reason": r.block_reason or ("unclassified" if unclassified else ""),
        "context_before": ctx.get("previous_sentence", ""), "context_after": ctx.get("next_sentence", ""),
        "exceptions_hint": _exceptions(r), "heading": r.section or ctx.get("heading", ""),
        "pdf_file": pdf_files.get(r.doc, ""), "why_this_class": " | ".join(r.interpretation),
        "proposed_template": r.template, "proposed_strictness": r.strictness, "version_fp": of_rule(r), "pack_id": pack_id,
    }
    for k in EDITABLE:
        row[k] = ""
    row["row_check"] = row_check(row)
    return row


def row_check(row: dict) -> str:
    return hashlib.sha1("\x1f".join(str(row.get(k, "")) for k in CHECKED).encode()).hexdigest()[:12]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:60]


def write_csv(path: Path, rows: list[dict], delimiter: str = ";"):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=delimiter)
        w.writeheader()
        w.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8-sig")
    head = text.splitlines()[0] if text else ""
    delim = ";" if head.count(";") > head.count(",") else ","
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: (v or "") for k, v in row.items() if k} for row in csv.DictReader(f, delimiter=delim)]


def make_pack(work, out, max_rows: int = 60, delimiter: str = ";") -> dict:
    work, out = Path(work), Path(out)
    rules = P.load_draft(work)
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate rule ids in the draft: run tools\\check_stable.py first")
    pack_id = hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()[:10]
    pdfs = _pdf_files(work)
    rows = [_fields(r, pdfs, pack_id) for r in rules]
    rows.sort(key=lambda x: (x["priority"], x["document"], int(x["pdf_page"]), x["review_id"]))
    (out / "by_document").mkdir(parents=True, exist_ok=True)
    for old in (out / "by_document").glob("*.csv"):
        old.unlink()
    write_csv(out / "review_MASTER.csv", rows, delimiter)
    files = OrderedDict()
    for doc in sorted({r["document"] for r in rows}):
        mine = [r for r in rows if r["document"] == doc]
        parts = [mine[i:i + max_rows] for i in range(0, len(mine), max_rows)]
        for n, part in enumerate(parts, 1):
            name = f"{_safe(doc)}_part{n}of{len(parts)}.csv"
            write_csv(out / "by_document" / name, part, delimiter)
            files[name] = {"document": doc, "rows": len(part), "priority_1": sum(r["priority"] == 1 for r in part)}
    manifest = {"pack_id": pack_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "candidates": len(rows),
                "by_destination": dict(Counter(r["destination"] for r in rows)), "by_priority": dict(sorted(Counter(r["priority"] for r in rows).items())),
                "files": files, "delimiter": delimiter,
                "note": "Edit only the files in by_document, one person per file. review_MASTER.csv is for reading and searching."}
    (out / "PACK_MANIFEST.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    write_progress(out / "REVIEW_PROGRESS.csv", rows)
    return manifest


# ---- progress -------------------------------------------------------------------------------------------------------

def write_progress(path: Path, rows: list[dict], delimiter: str = ";"):
    docs = OrderedDict()
    for r in rows:
        docs.setdefault(r["document"], []).append(r)
    cols = ["document", "total", "reviewed", "not_reviewed", "accepted", "corrected", "rejected", "unsure", "pdf_checked_yes",
            "priority1_total", "priority1_reviewed"]
    def line(name, rs):
        dec = Counter((r.get("decision") or "").strip().upper() for r in rs)
        reviewed = sum(dec[d] for d in DECISIONS_OK)
        return {"document": name, "total": len(rs), "reviewed": reviewed, "not_reviewed": len(rs) - reviewed, "accepted": dec["ACCEPT"],
                "corrected": dec["CORRECT"], "rejected": dec["REJECT"], "unsure": dec["UNSURE"],
                "pdf_checked_yes": sum((r.get("pdf_checked") or "").strip().lower() in YES for r in rs),
                "priority1_total": sum(str(r.get("priority")) == "1" for r in rs),
                "priority1_reviewed": sum(str(r.get("priority")) == "1" and (r.get("decision") or "").strip().upper() in DECISIONS_OK for r in rs)}
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=delimiter)
        w.writeheader()
        for d, rs in docs.items():
            w.writerow(line(d, rs))
        w.writerow(line("ALL DOCUMENTS", rows))


# ---- import ---------------------------------------------------------------------------------------------------------

def _split(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[;|]", s or "") if x.strip()]


def _same_review(a: dict, b: dict) -> bool:
    return all((a.get(k) or "").strip().lower() == (b.get(k) or "").strip().lower() for k in EDITABLE if k not in ("reviewed_at",))


def _to_sheet_row(row: dict, rule) -> tuple[dict | None, str]:
    """A completed review row as a sign-off sheet row, or (None, why it cannot be applied)."""
    dec = row["decision"].strip().upper()
    pdf = row["pdf_checked"].strip().lower()
    corrected = {k: row[k].strip() for k in EDITABLE if k.startswith("corrected_") and row[k].strip()}
    out = {"id": rule.id, "decision": "", "pdf_checked": "yes" if pdf in YES else "", "note": row["comments"].strip()}
    if dec in ("ACCEPT", "CORRECT", "REJECT") and not row["reviewer_name"].strip():
        return None, "reviewer_name is empty"
    if dec == "UNSURE":
        return out, ""
    if dec == "REJECT":
        out["decision"] = "reject"
        return out, ""
    if pdf not in YES:
        return None, f"{dec} needs pdf_checked = yes (compare with the PDF first)"
    if dec == "ACCEPT":
        if corrected:
            return None, "ACCEPT with corrected fields: use CORRECT"
        if rule.destination not in ("constraint", "advisory"):
            return None, f"this candidate is {rule.destination}: it cannot be ACCEPTed as it is. Use CORRECT (class, scope or conditions) after the PDF comparison"
        out["decision"] = "accept"
        return out, ""
    # CORRECT
    if not corrected:
        return None, "CORRECT without any corrected_* field"
    out["decision"] = "edit"
    if "corrected_class" in corrected:
        cls = corrected["corrected_class"]
        known = {v: k for k, v in SEM.PLAIN.items()}
        cls = known.get(cls, cls)
        if cls not in SEM.CLASSES:
            return None, f"corrected_class must be one of {', '.join(SEM.CLASSES)}"
        out["edit_class"] = cls
    if "corrected_value" in corrected:
        out["edit_value"] = corrected["corrected_value"].replace(",", ".")
    if "corrected_unit" in corrected:
        out["edit_unit"] = corrected["corrected_unit"]
    bound = corrected.get("corrected_bound")
    op = corrected.get("corrected_operator")
    if op:
        if op not in CORR_BOUND:
            return None, "corrected_operator must be one of < <= > >= ="
        if bound and bound != CORR_BOUND[op] and op != "=":
            return None, f"corrected_operator {op} and corrected_bound {bound} contradict each other"
        bound = bound or CORR_BOUND[op]
    if bound:
        if bound not in ("max", "min", "exact"):
            return None, "corrected_bound must be max, min or exact"
        out["edit_bound"] = bound
    if "corrected_scope" in corrected:
        out["edit_scope"] = corrected["corrected_scope"]
    if "corrected_conditions" in corrected:
        out["edit_conditions"] = corrected["corrected_conditions"]
    return out, ""


def import_reviews(work, paths, apply: bool = False, allow_replace: bool = False, report_dir=None) -> dict:
    """Read completed review CSVs and (with apply) record the decisions. Nothing is overwritten silently:
    duplicates, conflicts, stale rows, altered evidence and refused rows are listed and left alone."""
    import tempfile
    from .signoff import sheet_decisions, COLUMNS as SHEET_COLUMNS
    work = Path(work)
    files = []
    for p in map(Path, paths):
        files += sorted(p.glob("*.csv")) if p.is_dir() else [p]
    rules = {r.id: r for r in P.load_draft(work)}
    log = []                     # one entry per row that carries a decision
    seen: dict[str, tuple] = {}  # review_id -> (file, row)
    all_rows = []
    for f in files:
        for n, row in enumerate(read_csv(f), 2):
            if "review_id" not in row:
                log.append({"file": f.name, "line": n, "review_id": "", "decision": "", "result": "skipped", "detail": "not a review file (no review_id column)"})
                break
            all_rows.append(row)
            rid, dec = row["review_id"].strip(), row["decision"].strip().upper()
            if not dec:
                continue
            ent = {"file": f.name, "line": n, "review_id": rid, "decision": dec}
            if dec not in DECISIONS_OK:
                log.append({**ent, "result": "refused", "detail": f"decision must be one of {', '.join(DECISIONS_OK)}"}); continue
            if rid not in rules:
                log.append({**ent, "result": "refused", "detail": "unknown id: not in the current draft (was the extraction rerun?)"}); continue
            if row.get("row_check") != row_check(row):
                log.append({**ent, "result": "refused", "detail": "the evidence columns (quote, page, proposed values...) were changed in the file"}); continue
            if row.get("version_fp") != of_rule(rules[rid]):
                log.append({**ent, "result": "stale", "detail": "the rule changed since the pack was made: review it again in a new pack"}); continue
            if rid in seen:
                other_file, other = seen[rid]
                if other is None:
                    log.append({**ent, "result": "conflict", "detail": "this id already has conflicting decisions: none applied"}); continue
                if _same_review(other, row):
                    log.append({**ent, "result": "duplicate_identical", "detail": f"same review also in {other_file}: counted once"}); continue
                log.append({**ent, "result": "conflict", "detail": f"different decisions for the same id in {other_file} and {f.name}: none applied"})
                seen[rid] = (other_file, None)      # both sides are left alone
                continue
            seen[rid] = (f.name, row)
    # a conflict cancels the first row too
    conflicted = {e["review_id"] for e in log if e["result"] == "conflict"}
    todo = [(fname, row) for rid, (fname, row) in seen.items() if row is not None and rid not in conflicted]
    for rid in conflicted:
        log.append({"file": seen[rid][0], "line": "", "review_id": rid, "decision": "", "result": "conflict", "detail": "first file's decision also left alone"})
    decisions = P.load_decisions(work)
    sheet_rows, candidates_for_manual = [], []
    for fname, row in todo:
        r = rules[row["review_id"]]
        ent = {"file": fname, "line": "", "review_id": r.id, "decision": row["decision"].strip().upper()}
        sr, why = _to_sheet_row(row, r)
        if sr is None:
            log.append({**ent, "result": "refused", "detail": why}); continue
        if ent["decision"] == "CORRECT" and r.destination == "unclassified_evidence" and SEM.is_hard(sr.get("edit_class", "")):
            candidates_for_manual.append((r, row, sr))
            log.append({**ent, "result": "needs_manual_rule", "detail": "a real constraint has no planner template here: written to manual_rules_from_review.csv for the lead to complete"}); continue
        if sr["decision"] == "":                       # UNSURE
            log.append({**ent, "result": "recorded_unsure", "detail": row["comments"].strip()[:120]}); continue
        sheet_rows.append((fname, row, sr))
    # the existing safety checks decide: the same function as the sign-off sheet
    tmp_sheet = Path(tempfile.mkdtemp()) / "from_review.csv"
    with open(tmp_sheet, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        for _, _, sr in sheet_rows:
            w.writerow({k: sr.get(k, "") for k in SHEET_COLUMNS})
    by_id = {r.id: r for r in rules.values()}
    accepted_ids, msgs = {}, []
    ok, msgs = sheet_decisions(list(by_id.values()), tmp_sheet, "")
    for fname, row, sr in sheet_rows:
        ent = {"file": fname, "line": "", "review_id": sr["id"], "decision": row["decision"].strip().upper()}
        d = ok.get(sr["id"])
        if d is None:
            detail = next((m for m in msgs if m.startswith(sr["id"])), "refused by the safety checks")
            log.append({**ent, "result": "refused", "detail": detail.split(": ", 1)[-1][:200]}); continue
        d["reviewer"] = row["reviewer_name"].strip()
        d["note"] = row["comments"].strip()
        prev = decisions.get(sr["id"])
        if prev:
            same = prev.get("decision") == d["decision"] and prev.get("edits") == d["edits"] and prev.get("reviewer") == d["reviewer"]
            if same:
                log.append({**ent, "result": "already_applied", "detail": "identical decision already recorded"}); continue
            if not allow_replace:
                log.append({**ent, "result": "conflict_existing", "detail": f"a different decision is already recorded ({prev.get('decision')} by {prev.get('reviewer')}): not overwritten. Use --allow-replace after agreeing who is right"}); continue
        log.append({**ent, "result": "to_apply" if not apply else "applied", "detail": "replaces: " + json.dumps(prev) if prev else ""})
        accepted_ids[sr["id"]] = (d, row)
    if apply:
        if (work / P.DECISIONS).exists():
            (work / "decisions_before_review_import.json").write_text((work / P.DECISIONS).read_text(encoding="utf-8"), encoding="utf-8")
        for rid, (d, row) in accepted_ids.items():
            P.save_decision(work, rid, d["decision"], d["reviewer"], d["note"], d["pdf_checked"], d["edits"])
        with open(work / "review_log.jsonl", "a", encoding="utf-8") as f:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for e in log:
                f.write(json.dumps({**e, "imported_at": now}, ensure_ascii=False) + "\n")
        if candidates_for_manual:
            with open(work / "manual_rules_from_review.csv", "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                if f.tell() == 0:
                    w.writerow(["template", "bound", "value", "unit", "strictness", "scope", "conditions", "document", "page", "quote", "note"])
                for r, row, sr in candidates_for_manual:
                    w.writerow(["", sr.get("edit_bound", ""), sr.get("edit_value", row["proposed_value"]), sr.get("edit_unit", row["proposed_unit"]),
                                "must", sr.get("edit_scope", ""), sr.get("edit_conditions", ""), r.doc, r.page, r.quote,
                                f"from review by {row['reviewer_name']}: {row['comments']}"])
    outdir = Path(report_dir) if report_dir else work
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "import_report.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["file", "line", "review_id", "decision", "result", "detail"], delimiter=";")
        w.writeheader()
        for e in log:
            w.writerow({k: e.get(k, "") for k in w.fieldnames})
    write_progress(outdir / "REVIEW_PROGRESS.csv", all_rows)
    counts = Counter(e["result"] for e in log)
    return {"files": len(files), "rows": len(all_rows), "with_decision": sum(bool(r["decision"].strip()) for r in all_rows),
            "results": dict(counts), "applied": counts.get("applied", 0), "report": str(outdir / "import_report.csv")}