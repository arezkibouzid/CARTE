"""Shared steps used by the CLI, the web UI and the tests. A work folder holds everything one run produces."""
from __future__ import annotations

import json
from pathlib import Path

from .embed import get_embedder
from .extract import Rule, extract_chunks, reconcile
from .ingest import read_documents_ex
from .reliability import document_report, load_overrides
from .retrieve import Index
from .ingest import Chunk
from .signoff import read_rules, write_json, write_sheet
from .tables import TablesUnavailable, extract_tables, table_passage_text, write_tables

DRAFT, SKIPPED, CONFLICTS = "rules_draft.json", "skipped.json", "conflicts.json"
DECISIONS, APPROVED, SHEET = "decisions.json", "approved.json", "signoff.csv"


READING = "reading_report.json"


def build(paths, work: str | Path, embedder: str = "hashing", overrides: str | Path | None = None) -> tuple[Index, list[str]]:
    work = Path(work)
    chunks, warnings, flags = read_documents_ex(paths)
    if not chunks:
        raise ValueError("no text found. " + " ".join(warnings))
    tables, table_notes = _tables(paths, flags, work)
    warnings += table_notes
    for t in tables:   # one short passage per table: search finds the table, the CSV holds its numbers
        chunks.append(Chunk(len(chunks), f"{t['document']}:{t['page']}:T{t['index']}", t["document"], t["page"],
                            900 + t["index"], "TABLE", t["caption"], "", table_passage_text(t)))
    idx = Index(chunks, get_embedder(embedder)).build()
    idx.save(work)
    _write_sources(paths, work)
    ov = load_overrides(overrides)
    docs = [document_report(d, v["flags"], ov, v["scanned"]) for d, v in flags.items()]
    for d in docs:   # how many passages were set aside as tables or damaged
        d["tables_extracted"] = sum(t["document"] == d["document"] for t in tables)
        d["passages"] = sum(c.doc == d["document"] for c in chunks)
        d["table_passages"] = sum(c.doc == d["document"] and c.quality == "table" for c in chunks)
        d["noisy_passages"] = sum(c.doc == d["document"] and c.quality == "noisy" for c in chunks)
    (work / READING).write_text(json.dumps({"documents": docs}, ensure_ascii=False, indent=1), encoding="utf-8")
    import csv   # every page, so nothing is hidden by the short lists printed on screen
    with open(work / "reading_pages.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["document", "page", "quality", "odd_tokens", "words_lost_pct"])
        for d in docs:
            for page, quality, odd, lost in d["page_flags"]:
                w.writerow([d["document"], page, quality, odd, lost])
    return idx, warnings


def _tables(paths, flags, work: Path):
    """Tables of the digital PDFs (scanned manuals are skipped: no ruling lines to follow)."""
    files = []
    for p in map(Path, paths):
        files += sorted(f for f in p.rglob("*") if f.suffix.lower() == ".pdf") if p.is_dir() else [p]
    out, notes = [], []
    for f in files:
        if f.suffix.lower() != ".pdf" or f.stem not in flags or flags[f.stem]["scanned"]:
            continue
        try:
            out += extract_tables(f)
        except TablesUnavailable as e:
            return [], [f"tables not extracted: {e}"]
        except Exception as e:
            notes.append(f"{f.name}: tables not extracted ({e})")
    if out:
        write_tables(out, Path(work) / "tables")
    return out, notes


def load_reading(work: str | Path) -> dict:
    """{document: reading info}. Empty if the index was built before reliability existed."""
    p = Path(work) / READING
    return {d["document"]: d for d in json.loads(p.read_text(encoding="utf-8"))["documents"]} if p.exists() else {}


def _write_sources(paths, work: Path):
    """Fingerprint of every input file, so an export can say exactly which documents it came from."""
    import hashlib
    files = []
    for p in map(Path, paths):
        files += sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p]
    out = []
    for f in files:
        if f.suffix.lower() in {".pdf", ".txt", ".md"} and f.exists():
            out.append({"name": f.name, "sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "bytes": f.stat().st_size,
                        "path": str(f.resolve())})   # local only: lets the web page show the original page, never exported
    (work / "sources.json").write_text(json.dumps(out, indent=1), encoding="utf-8")


def extract(work: str | Path, llm=None, precedence: list[str] | None = None, manual: str | Path | None = None) -> dict:
    from .manual import load_manual
    work = Path(work)
    idx = Index.load(work)
    reading = load_reading(work)
    rules, skipped, problems = extract_chunks(idx.chunks, llm=llm, reliability=reading)
    # rules typed by hand: --manual FILE, else manual_rules.csv in the work folder, else in the folder above it
    candidates = [Path(manual)] if manual else [work / "manual_rules.csv", work.resolve().parent / "manual_rules.csv"]
    mfile = next((m for m in candidates if m.exists()), None)
    n_manual = 0
    if mfile:
        by_hand, mprob = load_manual(mfile, idx.chunks, reading)
        rules += by_hand
        problems += mprob
        n_manual = len(by_hand)
    kept, conflicts = reconcile(rules, precedence)
    from .audit import audit_changes
    try:
        previous = load_draft(work) if (work / DRAFT).exists() else None
    except Exception:
        previous = None
    audit = audit_changes(previous, kept, work) if previous is not None else {"changed": 0, "removed": 0}
    meta = {"llm": bool(llm), "precedence": precedence or [], "documents": sorted({c.doc for c in idx.chunks}),
            "note": "draft rules: not usable until approved by a reviewer"}
    write_json(kept, work / DRAFT, meta)
    (work / SKIPPED).write_text(json.dumps(skipped, ensure_ascii=False, indent=1), encoding="utf-8")
    (work / CONFLICTS).write_text(json.dumps(conflicts, ensure_ascii=False, indent=1), encoding="utf-8")
    write_sheet(kept, work / SHEET)
    return {"rules": len(kept), "raw_rules": len(rules), "skipped": len(skipped), "conflicts": len(conflicts),
            "problems": problems, "manual": n_manual, "audit": audit}


def load_draft(work: str | Path) -> list[Rule]:
    return read_rules(Path(work) / DRAFT)


def load_decisions(work: str | Path) -> dict:
    p = Path(work) / DECISIONS
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_decision(work: str | Path, rule_id: str, decision: str, reviewer: str = "", note: str = "", pdf_checked: bool = False,
                  edits: dict | None = None):
    if decision not in {"accept", "reject", "clear"}:
        raise ValueError("decision must be accept, reject or clear")
    edits = dict(edits or {})
    if decision == "accept":
        rule = next((r for r in load_draft(work) if r.id == rule_id), None)
        if rule is not None and rule.verify_against_pdf and not pdf_checked:
            raise ValueError("this rule needs a person to compare it with the PDF page: "
                             "tick 'compared with the PDF' before accepting")
        if rule is not None and rule.semantic_class in ("unclassified_evidence", "ambiguous_or_incomplete"):
            # not accepted as it is: a person must say what it is (class), where it applies (scope or conditions) and have
            # compared it with the PDF. A real constraint needs a planner template, which only a hand-typed rule has.
            from . import semantics as SEM
            new = edits.get("semantic_class")
            has_ctx = bool(edits.get("scope") or edits.get("conditions"))
            if new not in SEM.CLASSES:
                raise ValueError("the meaning is not established: it cannot be accepted as it is. Correct the class "
                                 "(edit_class) and write the scope or conditions, after reading the PDF")
            if not pdf_checked or not has_ctx:
                raise ValueError("a corrected class needs the PDF comparison and the scope or conditions written down")
            if rule.semantic_class == "unclassified_evidence" and SEM.is_hard(new):
                raise ValueError("unclassified evidence has no planner template: if it is a real constraint, type it in manual_rules.csv")
        if rule is not None and rule.needs_context and not (edits.get("scope") or edits.get("conditions")):
            if not note.strip():
                if rule.table_derived:
                    raise ValueError("this looks like a table row or holds several measurements: read the PDF and write the "
                                     "column header and row conditions in the note before accepting")
                raise ValueError("the context is incomplete (" + "; ".join(rule.context_issues)[:160] + "): read the PDF and "
                                 "write what applies in the note before accepting")
            edits["conditions"] = [note.strip()]   # the context the reviewer wrote
    fp = ""
    if decision == "accept":
        from .fingerprint import MEANING_EDITS, of_rule
        from .signoff import apply_edits
        if any(k in edits and edits[k] not in (None, "", []) for k in MEANING_EDITS) and not pdf_checked:
            raise ValueError("you changed what the rule says (value, unit, bound, strictness, class, scope or conditions): "
                             "compare with the PDF and tick 'compared with the PDF' before accepting")
        rule = next((r for r in load_draft(work) if r.id == rule_id), None)
        if rule is not None:
            fp = of_rule(apply_edits(rule, edits))     # the exact version the person approved
    d = load_decisions(work)
    if decision == "clear":
        d.pop(rule_id, None)
    else:
        d[rule_id] = {"decision": decision, "reviewer": reviewer, "note": note, "pdf_checked": bool(pdf_checked),
                      "edits": edits if decision == "accept" else {}, "fingerprint": fp}
    (Path(work) / DECISIONS).write_text(json.dumps(d, indent=1), encoding="utf-8")
    export_approved(work)


def export_approved(work: str | Path) -> int:
    """approved.json holds only rules a person accepted (with the person's edits). No decision means not approved."""
    from .signoff import apply_edits
    work = Path(work)
    dec = load_decisions(work)
    out = []
    for r in load_draft(work):
        if dec.get(r.id, {}).get("decision") == "accept":
            r = apply_edits(r, dec[r.id].get("edits"))
            r.status = "approved"
            out.append(r)
    write_json(out, work / APPROVED, {"approved_by": sorted({v["reviewer"] for v in dec.values() if v["reviewer"]})})
    return len(out)