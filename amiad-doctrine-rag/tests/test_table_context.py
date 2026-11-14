"""Reliability (is the text readable?) and PDF verification (did a person read the table?) are separate things.
A rule drawn from a table row, or from text with several measurements, always needs a person and written context."""
import argparse
import csv
import json
from pathlib import Path

from docrag import cli, pipeline as P
from docrag.contract import export_for_planner, validate_export
from docrag.extract import Rule, extract_chunks, table_shape
from docrag.ingest import Chunk
from docrag.signoff import COLUMNS, apply_edits, sheet_decisions

TABLE_SENTENCE = ("The minimum roadway width must be 2.75 m for class 4-12 and 3.35 m for class 13-30 "
                  "and 4.00 m for class 31-70.")
PLAIN_SENTENCE = "Convoys must keep a gap of at least 30 m between vehicles."


def test_table_shape_detection():
    assert table_shape(TABLE_SENTENCE)
    assert table_shape("Classification One-way Two-way 4-12 2.75 m 5.50 m 13-30 3.35 m 5.50 m")
    assert table_shape("axle distance 1.3 m to less than 1.80 m 18.0 t")
    assert not table_shape(PLAIN_SENTENCE)
    assert not table_shape("Vehicles must keep a minimum spacing of 30.5 meters and a maximum speed of 40 km/h.")


def _chunk(text, n=0):
    return Chunk(n, f"Doc:1:{n + 1}", "Doc", 1, n + 1, "", "", "", text)


HIGH = {"Doc": {"level": "high", "verify_against_pdf": False, "reasons": [], "page_flags": [[1, "ok", 0, 0.0]]}}


def test_a_readable_table_gets_high_reliability_but_still_needs_the_pdf_and_context():
    rules, _, _ = extract_chunks([_chunk(TABLE_SENTENCE), _chunk(PLAIN_SENTENCE, 1)], reliability=HIGH)
    table = [r for r in rules if r.quote == TABLE_SENTENCE]
    plain = [r for r in rules if r.quote == PLAIN_SENTENCE]
    assert table and plain
    for r in table:
        assert r.reliability == "high"                      # the text is readable ...
        assert r.table_derived and r.verify_against_pdf     # ... and a person must still read the table
        assert any("column header" in m for m in r.missing)
    assert all(not r.table_derived and not r.verify_against_pdf for r in plain)


def _sheet(work, rows):
    sheet = Path(work) / "signoff.csv"
    with open(sheet, newline="", encoding="utf-8-sig") as f:
        base = list(csv.DictReader(f))
    for r in base:
        r.update(rows.get(r["id"], {}))
    with open(sheet, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(base)
    return sheet


def _work(tmp_path, manual_row=None):
    doc = tmp_path / "Doc.txt"
    doc.write_text(f"[page 1]\n{PLAIN_SENTENCE}\n\n{TABLE_SENTENCE}\n", encoding="utf-8")
    work = tmp_path / "work"
    P.build([doc], work)
    manual = None
    if manual_row:
        manual = tmp_path / "manual.csv"
        manual.write_text("template,bound,value,unit,strictness,scope,conditions,document,page,quote,note\n" + manual_row,
                          encoding="utf-8")
    P.extract(work, manual=manual)
    return work, {r.id: r for r in P.load_draft(work)}


def test_table_rule_is_refused_without_pdf_check_and_without_context_then_accepted_with_both(tmp_path):
    work, rules = _work(tmp_path)
    t = next(r for r in rules.values() if r.table_derived)
    sheet = _sheet(work, {t.id: {"decision": "accept"}})
    dec, msgs = sheet_decisions(list(rules.values()), sheet, "T")
    assert t.id not in dec and any("PDF" in m for m in msgs)

    sheet = _sheet(work, {t.id: {"decision": "accept", "pdf_checked": "yes"}})
    dec, msgs = sheet_decisions(list(rules.values()), sheet, "T")
    assert t.id not in dec and any("edit_scope" in m for m in msgs)       # PDF compared, but no context written

    sheet = _sheet(work, {t.id: {"decision": "accept", "pdf_checked": "yes", "edit_scope": "fixed civilian bridges, class 4-12",
                                 "edit_conditions": "one-way traffic"}})
    dec, msgs = sheet_decisions(list(rules.values()), sheet, "T")
    assert dec[t.id]["edits"]["scope"] == ["fixed civilian bridges, class 4-12"]
    done = apply_edits(t, dec[t.id]["edits"])
    assert done.scope == ["fixed civilian bridges, class 4-12"] and not any("column header" in m for m in done.missing)


def test_approve_command_works_end_to_end_and_keeps_the_reviewers_context_and_pdf_tick(tmp_path):
    row = f'NET.GAP,min,30,m,must,vehicles,,Doc,1,"{PLAIN_SENTENCE}",typed\n'
    work, rules = _work(tmp_path, row)
    manual = next(r for r in rules.values() if r.origin == "manual")
    t = next(r for r in rules.values() if r.table_derived)
    sheet = _sheet(work, {manual.id: {"decision": "accept", "pdf_checked": "yes"},
                          t.id: {"decision": "accept", "pdf_checked": "yes", "edit_scope": "class 4-12 bridges"}})
    cli.cmd_approve(argparse.Namespace(sheet=str(sheet), work=str(work), reviewer="T"))   # used to stop at the first PDF rule
    dest = export_for_planner(work)
    out = {r["id"]: r for r in json.loads(dest.read_text(encoding="utf-8"))["rules"]}
    assert set(out) == {manual.id, t.id}
    assert out[manual.id]["verified_against_pdf"] is True and out[manual.id]["verify_required"] is True
    assert out[t.id]["scope"] == ["class 4-12 bridges"] and out[t.id]["verified_against_pdf"] is True
    assert validate_export(dest, work) == []


def test_edited_value_reaches_the_export_and_is_caught_if_the_quote_does_not_state_it(tmp_path):
    work, rules = _work(tmp_path)
    plain = next(r for r in rules.values() if not r.table_derived and r.value == 30)
    sheet = _sheet(work, {plain.id: {"decision": "edit", "edit_value": "40", "pdf_checked": "yes"}})
    cli.cmd_approve(argparse.Namespace(sheet=str(sheet), work=str(work), reviewer="T"))
    dest = export_for_planner(work)
    exported = json.loads(dest.read_text(encoding="utf-8"))["rules"][0]
    assert exported["value"] == 40.0                                        # the edit is not silently lost
    assert any("not stated in the quote" in p for p in validate_export(dest, work))   # and the validator refuses it


def test_unapproved_table_rule_is_never_exported(tmp_path):
    work, rules = _work(tmp_path)
    plain = next(r for r in rules.values() if not r.table_derived)
    sheet = _sheet(work, {plain.id: {"decision": "accept"}})
    cli.cmd_approve(argparse.Namespace(sheet=str(sheet), work=str(work), reviewer="T"))
    ids = {r["id"] for r in json.loads(export_for_planner(work).read_text(encoding="utf-8"))["rules"]}
    assert ids == {plain.id}


def test_review_screen_path_needs_the_context_too(tmp_path):
    work, rules = _work(tmp_path)
    t = next(r for r in rules.values() if r.table_derived)
    try:
        P.save_decision(work, t.id, "accept", "T", pdf_checked=True)
        assert False, "must refuse"
    except ValueError as e:
        assert "column header" in str(e)
    P.save_decision(work, t.id, "accept", "T", note="class 4-12, one-way", pdf_checked=True)
    assert P.load_decisions(work)[t.id]["edits"]["conditions"] == ["class 4-12, one-way"]


def test_a_draft_file_written_before_this_field_still_loads(tmp_path):
    d = Rule("NET.GAP", "min", 30.0, "m", "must", "q", "Doc:1:1", "Doc", 1, 1).to_dict()
    del d["table_derived"]
    (tmp_path / "rules_draft.json").write_text(json.dumps({"meta": {}, "rules": [d]}), encoding="utf-8")
    assert P.load_draft(tmp_path)[0].table_derived is False