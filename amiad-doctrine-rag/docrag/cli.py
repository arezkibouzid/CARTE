"""Command line: docrag <command> ... (run `docrag --help`)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import pipeline as P
from .evaluate import eval_extraction, eval_retrieval, format_retrieval
from .retrieve import MODES, Index


def _llm(args):
    if not args.llm:
        return None
    from .llm import LLMUnavailable, OpenAICompatClient
    client = OpenAICompatClient(base_url=args.llm_url, model=args.llm_model)
    try:
        print("model server found, models:", ", ".join(client.models()) or "none listed")
    except LLMUnavailable as e:
        print(f"model server not reachable ({e}). Continuing with the pattern extractor only.")
        return None
    return client


def cmd_index(a):
    idx, warns = P.build(a.paths, a.work, a.embedder, a.reliability)
    print(f"{len(idx.chunks)} passages from {len({c.doc for c in idx.chunks})} document(s) -> {a.work}")
    print("graph:", idx.graph.stats())
    print("reading reliability per document (details: docrag reliability -w " + str(a.work) + ")")
    for d in P.load_reading(a.work).values():
        print(f"  {d['level'].upper():<7} {d['document']}" + ("  (rules must be compared with the PDF)" if d["verify_against_pdf"] else ""))
    for w in warns:
        print("warning:", w)


def cmd_reliability(a):
    docs = P.load_reading(a.work)
    if not docs:
        raise ValueError("no reading report in this work folder: run `docrag index` first")
    if a.pages:
        for d in docs.values():
            if a.doc and a.doc.lower() not in d["document"].lower():
                continue
            qmap = {row[0]: row[1:] for row in d["page_flags"]}
            for pg in a.pages:
                q, odd, lost = qmap.get(pg, ("not in this document", 0, 0))
                print(f"{d['document']} p.{pg}: {q}" + (f" ({odd} odd tokens, {lost}% of the words lost)" if q != "not in this document" else ""))
        return
    for d in docs.values():
        print(f"\n{d['document']}: reliability {d['level'].upper()} (automatic signals say {d['automatic_level']}), {d['pages']} pages, "
              f"{d['passages']} passages")
        for r in d["reasons"]:
            print("  -", r)
        print(f"  pages with OCR-like noise: {d['noisy_pages'][:25] or 'none'}")
        print(f"  pages losing text:         {d.get('lossy_pages', [])[:25] or 'none'}")
        if d.get("graphic_pages") or d.get("mixed_pages"):
            print(f"  graphic pages (marked by hand, no rules): {len(d['graphic_pages'])}; mixed signs and text: {len(d['mixed_pages'])}")
        print(f"  pages with tables:         {d['table_pages'][:25] or 'none'}")
        print(f"  pages with figures:        {d['figure_pages'][:25] or 'none'}")
        print(f"  tables extracted to CSV (digital pages only): {d.get('tables_extracted', 0)}")
        print(f"  passages set aside as tables: {d['table_passages']}, passages that look damaged: {d['noisy_passages']}")
        print("  rules from this document: " + ("must be compared with the PDF before acceptance" if d["verify_against_pdf"]
                                                else "no mandatory PDF comparison"))


def cmd_tables(a):
    from .tables import extract_tables, write_tables
    allt = []
    for f in a.files:
        allt += extract_tables(f, a.pages or None)
    write_tables(allt, a.out)
    print(f"{len(allt)} tables -> {a.out} (one CSV each, and tables.json)")
    for t in allt:
        c = t["checks"]
        print(f"  {t['document']} p.{t['page']} #{t['index']}: {t['n_rows']} rows x {t['n_cols']} cols, "
              f"numbers {int(c['numeric_share'] * 100)}%, found in page text {int(c['numbers_found_in_page_text'] * 100)}%"
              + (f"  [{t['caption'][:60]}]" if t["caption"] else ""))


def cmd_search(a):
    if a.json:
        from .api import retrieve
        print(json.dumps(retrieve(a.work, a.query, a.k, a.mode, a.document), ensure_ascii=False, indent=1))
        return
    idx = Index.load(a.work)
    for i, h in enumerate(idx.search(a.query, a.k, a.mode), 1):
        print(f"{i}. [{h.score:.2f}] {h.chunk.cite()}  ({', '.join(h.reasons[:3])})")
        print("   " + h.chunk.text.replace("\n", " ")[:300])


def cmd_extract(a):
    prec = [p.strip() for p in a.precedence.split(",") if p.strip()] if a.precedence else None
    r = P.extract(a.work, _llm(a), prec, a.manual)
    if r.get("manual"):
        print(f"{r['manual']} rule(s) typed by hand read from the manual rules file")
    print(f"{r['rules']} draft rules ({r['raw_rules']} before merging), {r['skipped']} sentences left out and logged, "
          f"{r['conflicts']} conflicts")
    from collections import Counter
    levels = Counter(x.reliability for x in P.load_draft(a.work))
    print("rules by source reliability:", ", ".join(f"{k} {v}" for k, v in sorted(levels.items())))
    draft = P.load_draft(a.work)
    print("rules by meaning:", ", ".join(f"{k} {v}" for k, v in sorted(Counter(x.semantic_class for x in draft).items())))
    print("destinations:", ", ".join(f"{k} {v}" for k, v in sorted(Counter(x.destination for x in draft).items())))
    au = r.get("audit") or {}
    if au.get("changed") or au.get("removed"):
        print(f"audit: {au.get('changed', 0)} candidate(s) changed class or destination, {au.get('removed', 0)} no longer produced "
              "(details in audit_log.jsonl)")
    print("incomplete context (blocked until a person completes it):", sum(x.context_status == "incomplete_context" for x in draft))
    for p in r["problems"][:10]:
        print("note:", p)
    print(f"Next: open {Path(a.work) / P.SHEET} (or run `docrag ui {a.work}`) and accept/reject each rule.")


def cmd_approve(a):
    from .signoff import sheet_decisions
    rules = P.load_draft(a.work)
    decisions, msgs = sheet_decisions(rules, a.sheet, a.reviewer)
    for m in msgs:
        print("note:", m)
    for rid, d in decisions.items():   # same checks as the sheet: PDF comparison and table context were verified there
        try:
            P.save_decision(a.work, rid, d["decision"], a.reviewer, d.get("note", ""), d["pdf_checked"], d["edits"])
        except ValueError as e:
            print(f"note: {rid}: {e}; rule stays draft")
    print(f"{P.export_approved(a.work)} approved rules -> {Path(a.work) / P.APPROVED}")


def cmd_export(a):
    from .contract import ExportError, export_for_planner, load_template_map, validate_export
    try:
        dest = export_for_planner(a.work, load_template_map(a.template_map), a.provisional, a.allow_empty, a.out)
    except ExportError as e:
        print("error:", e)
        return 2
    problems = validate_export(dest, a.work)
    d = json.loads(dest.read_text(encoding="utf-8"))
    print(f"{len(d['rules'])} rules ({'PROVISIONAL, not approved' if d['provisional'] else 'approved'}) -> {dest}")
    if d["unmapped_templates"]:
        print("no planner template for:", ", ".join(d["unmapped_templates"]), "(add them to the template map)")
    if problems:
        print("contract problems:")
        for p in problems:
            print(" -", p)
        return 3


def cmd_validate(a):
    from .contract import validate_export
    problems = validate_export(a.file, a.work)
    print("OK, file respects the contract" if not problems else "\n".join(problems))
    return 1 if problems else 0


def cmd_check(a):
    from .check import check_document, public, render_html
    r = check_document(a.file)
    d = public(r)
    out = Path(a.out) if a.out else Path(Path(a.file).stem + "_reading_check.html")
    out.write_text(render_html(r), encoding="utf-8")
    print(f"{d['verdict']}: {d['pages']} pages, {d['passages']} passages, {d['coverage'] * 100:.1f}% of the words kept")
    for p in d["problems"]:
        print(" -", p)
    print(f"Pages to compare by hand with the PDF: {d['sample_pages']}")
    print(f"Full report: {out}")
    return 0 if d["verdict"] != "BAD" else 1


def cmd_eval(a):
    idx = Index.load(a.work)
    if a.queries:
        print("Retrieval (higher is better):")
        print(format_retrieval(eval_retrieval(idx, a.queries)))
        if a.details:
            from .evaluate import retrieval_misses
            for mode in MODES:
                miss = retrieval_misses(idx, a.queries, mode, 5)
                print(f"\n{mode}: {len(miss)} right passage(s) not in the top 5")
                for m in miss:
                    print(f"  - {m['query']}  -> found at rank {m['found_at_rank']}")
    if a.rules:
        from .extract import extract_chunks
        rules, _, _ = extract_chunks(idx.chunks)
        e = eval_extraction(rules, a.rules)
        print(f"\nExtraction: found {e['found']}/{e['gold']}  precision {e['precision']}  recall {e['recall']}")
        for m in e["missed"]:
            print("  missed:", m)
        for f in e["false_positives"]:
            print("  extra :", f["template"], f["value"], f["unit"], "|", f["quote"][:80])
        for w in e["bound_or_strictness_wrong"]:
            print("  wrong detail:", w)


def cmd_ui(a):
    from .ui import serve
    serve(a.work, a.port, a.open)


def cmd_llmcheck(a):
    from .llm import LLMUnavailable, OpenAICompatClient
    c = OpenAICompatClient(base_url=a.llm_url, model=a.llm_model)
    try:
        print("reachable. models:", c.models())
    except LLMUnavailable as e:
        print("not reachable:", e)
        return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="docrag", description="Document search and rule extraction with human sign-off.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("index", help="read documents (PDF, TXT, MD) and build the search index")
    s.add_argument("paths", nargs="+")
    s.add_argument("-w", "--work", default="work")
    s.add_argument("--embedder", default="hashing", help='"hashing" (offline, lexical) or "st:<sentence-transformers model>"')
    s.add_argument("--reliability", help="manual reliability file (default: reliability_overrides.json in the repo)")
    s.set_defaults(f=cmd_index)

    s = sub.add_parser("tables", help="extract the tables of PDFs that have a text layer (needs pdfplumber)")
    s.add_argument("files", nargs="+")
    s.add_argument("-o", "--out", default="tables")
    s.add_argument("--pages", type=int, nargs="*", help="only these pages")
    s.set_defaults(f=cmd_tables)

    s = sub.add_parser("reliability", help="show how far each document's text can be trusted, and why")
    s.add_argument("-w", "--work", default="work")
    s.add_argument("--doc", help="part of a document name (with --pages)")
    s.add_argument("--pages", type=int, nargs="*", help="show the status of these pages, for example --doc STANAG_2015 --pages 33 69 172")
    s.set_defaults(f=cmd_reliability)

    s = sub.add_parser("search", help="search the index")
    s.add_argument("query")
    s.add_argument("-w", "--work", default="work")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--mode", choices=MODES, default="hybrid")
    s.add_argument("--json", action="store_true", help="print the result in the documented JSON format")
    s.add_argument("--document", help="search one document only (name as shown in the interface)")
    s.set_defaults(f=cmd_search)

    s = sub.add_parser("extract", help="extract draft rules, log what was left out, write the sign-off sheet")
    s.add_argument("-w", "--work", default="work")
    s.add_argument("--precedence", default="", help="comma list, most binding document first, e.g. national,standard")
    s.add_argument("--llm", action="store_true", help="also use a local model server (see README)")
    s.add_argument("--llm-url")
    s.add_argument("--llm-model")
    s.add_argument("--manual", help="CSV of rules typed by hand (default: manual_rules.csv in the work folder or the repo root)")
    s.set_defaults(f=cmd_extract)

    s = sub.add_parser("approve", help="read the filled-in sign-off CSV and write approved.json")
    s.add_argument("sheet")
    s.add_argument("-w", "--work", default="work")
    s.add_argument("--reviewer", required=True)
    s.set_defaults(f=cmd_approve)

    s = sub.add_parser("export", help="write requirements_for_planner.json (approved rules only)")
    s.add_argument("-w", "--work", default="work")
    s.add_argument("--template-map", help="JSON file: docrag template id -> planner template id")
    s.add_argument("--provisional", action="store_true", help="TEST ONLY: include unapproved draft rules, marked provisional")
    s.add_argument("--allow-empty", action="store_true")
    s.add_argument("-o", "--out")
    s.set_defaults(f=cmd_export)

    s = sub.add_parser("validate", help="check an exported file against the contract")
    s.add_argument("file")
    s.add_argument("-w", "--work", help="index folder: also check that every quote is on the cited page of the source")
    s.set_defaults(f=cmd_validate)

    s = sub.add_parser("check", help="check that a document was read and cut into passages correctly")
    s.add_argument("file")
    s.add_argument("-o", "--out", help="HTML report to write")
    s.set_defaults(f=cmd_check)

    s = sub.add_parser("eval", help="measure retrieval and extraction against hand-written gold files")
    s.add_argument("-w", "--work", default="work")
    s.add_argument("--queries")
    s.add_argument("--rules")
    s.add_argument("--details", action="store_true", help="list the questions each mode got wrong")
    s.set_defaults(f=cmd_eval)

    s = sub.add_parser("ui", help="small web interface on this computer")
    s.add_argument("work", nargs="?", default="work")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open the browser")
    s.set_defaults(f=cmd_ui)

    s = sub.add_parser("llm-check", help="test the connection to a local model server")
    s.add_argument("--llm-url")
    s.add_argument("--llm-model")
    s.set_defaults(f=cmd_llmcheck)

    a = ap.parse_args(argv)
    try:
        return a.f(a) or 0
    except (ValueError, FileNotFoundError) as e:
        print("error:", e)
        return 2
    except Exception as e:
        from .tables import TablesUnavailable
        if not isinstance(e, TablesUnavailable):
            raise
        print("error:", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())