"""Team review pack: export, round trip, import with conflicts, and the safety checks still apply to every imported row."""
import csv
import json
import shutil
from pathlib import Path

import pytest

from docrag import pipeline as P
from docrag import reviewpack as RP

CORPUS = Path(__file__).resolve().parent.parent / "sample_corpus"


@pytest.fixture()
def work(tmp_path):
    w = tmp_path / "work"
    P.build([CORPUS], w)
    P.extract(w)
    return w


def _files(pack):
    return sorted((pack / "by_document").glob("*.csv"))


def _fill(path, rid, **vals):
    rows = RP.read_csv(path)
    for r in rows:
        if r["review_id"] == rid:
            r.update(vals)
    RP.write_csv(path, rows, ";")


def test_pack_has_every_candidate_once_and_the_required_columns(work, tmp_path):
    pack = tmp_path / "pack"
    m = RP.make_pack(work, pack, max_rows=7)
    rules = P.load_draft(work)
    master = RP.read_csv(pack / "review_MASTER.csv")
    assert len(master) == len(rules) == m["candidates"]
    assert {r["review_id"] for r in master} == {r.id for r in rules}
    need = {"review_id", "origin_kind", "document", "pdf_page", "quote", "context_before", "context_after", "proposed_class", "proposed_value",
            "proposed_unit", "proposed_operator", "proposed_bound", "proposed_scope", "proposed_conditions", "trust_warnings", "block_reason",
            "destination", "decision", "pdf_checked", "corrected_class", "corrected_value", "corrected_unit", "corrected_operator",
            "corrected_scope", "corrected_conditions", "reviewer_name", "comments", "reviewed_at"}
    assert need <= set(master[0])
    split = [r for f in _files(pack) for r in RP.read_csv(f)]
    assert sorted(r["review_id"] for r in split) == sorted(r.id for r in rules)          # nothing lost, nothing twice
    assert {r["destination"] for r in master} >= {"constraint", "blocked", "advisory"}
    assert all(r["quote"] == next(x for x in rules if x.id == r["review_id"]).quote for r in master)   # quote word for word
    assert [int(r["priority"]) for r in master] == sorted(int(r["priority"]) for r in master)           # priority 1 first
    assert (pack / "REVIEW_PROGRESS.csv").exists() and (pack / "PACK_MANIFEST.json").exists()


def test_round_trip_unchanged_files_lose_and_alter_nothing(work, tmp_path):
    pack = tmp_path / "pack"
    RP.make_pack(work, pack, max_rows=7)
    before_draft = (work / P.DRAFT).read_bytes()
    before_dec = P.load_decisions(work)
    before = {f.name: RP.read_csv(f) for f in _files(pack)}
    rep = RP.import_reviews(work, [pack / "by_document"], apply=True, report_dir=tmp_path / "rep")
    assert rep["with_decision"] == 0 and rep["applied"] == 0
    assert (work / P.DRAFT).read_bytes() == before_draft                    # the draft is untouched
    assert P.load_decisions(work) == before_dec                              # no decision invented
    assert {f.name: RP.read_csv(f) for f in _files(pack)} == before         # the files are untouched
    prog = list(csv.DictReader(open(tmp_path / "rep" / "REVIEW_PROGRESS.csv", encoding="utf-8-sig"), delimiter=";"))
    assert prog[-1]["document"] == "ALL DOCUMENTS" and int(prog[-1]["total"]) == len(P.load_draft(work)) and int(prog[-1]["reviewed"]) == 0


def _first(work, pred):
    return next(r for r in P.load_draft(work) if pred(r))


def test_completed_reviews_are_imported_with_the_safety_checks(work, tmp_path):
    pack = tmp_path / "pack"
    RP.make_pack(work, pack, max_rows=500)
    f = pack / "review_MASTER.csv"      # one file holding every row (the per-document files hold the same rows)
    good = _first(work, lambda r: r.destination == "constraint")
    blocked = _first(work, lambda r: r.destination == "blocked")
    unc = _first(work, lambda r: r.destination == "unclassified_evidence")
    adv = _first(work, lambda r: r.destination == "advisory")
    rej = _first(work, lambda r: r.destination == "advisory" and r.id != adv.id)
    # a legitimate ACCEPT, a REJECT, an UNSURE
    _fill(f, good.id, decision="ACCEPT", pdf_checked="yes", reviewer_name="Amel")
    _fill(f, rej.id, decision="REJECT", reviewer_name="Amel", comments="not a rule")
    _fill(f, adv.id, decision="UNSURE", reviewer_name="Amel", comments="cannot find the page")
    # must be refused: ACCEPT on blocked, ACCEPT on unclassified, ACCEPT without PDF check, CORRECT without PDF check, no reviewer name
    _fill(f, blocked.id, decision="ACCEPT", pdf_checked="yes", reviewer_name="Amel")
    _fill(f, unc.id, decision="ACCEPT", pdf_checked="yes", reviewer_name="Amel")
    rep = RP.import_reviews(work, [f], apply=True, report_dir=tmp_path / "rep")
    res = {r["review_id"]: r for r in csv.DictReader(open(rep["report"], encoding="utf-8-sig"), delimiter=";")}
    assert res[good.id]["result"] == "applied" and res[rej.id]["result"] == "applied" and res[adv.id]["result"] == "recorded_unsure"
    assert res[blocked.id]["result"] == "refused" and res[unc.id]["result"] == "refused"
    dec = P.load_decisions(work)
    assert set(dec) == {good.id, rej.id}                                     # nothing else was approved
    assert dec[good.id]["decision"] == "accept" and dec[good.id]["reviewer"] == "Amel" and dec[good.id]["fingerprint"]
    assert (work / "review_log.jsonl").exists()


def test_correct_needs_the_pdf_check_and_a_verified_correction_exports(work, tmp_path):
    from docrag.contract import export_for_planner, validate_export
    pack = tmp_path / "pack"
    RP.make_pack(work, pack, max_rows=500)
    f = pack / "review_MASTER.csv"      # one file holding every row (the per-document files hold the same rows)
    blocked = _first(work, lambda r: r.semantic_class == "ambiguous_or_incomplete")     # "Tank transporters ... shall use only bridges of MLC 100 or higher"
    _fill(f, blocked.id, decision="CORRECT", pdf_checked="no", reviewer_name="Bilel", corrected_class="mandatory_constraint", corrected_scope="tank transporters")
    r1 = RP.import_reviews(work, [f], apply=True, report_dir=tmp_path / "rep")
    assert r1["results"].get("refused") == 1 and not P.load_decisions(work)
    _fill(f, blocked.id, decision="CORRECT", pdf_checked="yes", reviewer_name="Bilel", corrected_class="mandatory_constraint", corrected_scope="tank transporters")
    r2 = RP.import_reviews(work, [f], apply=True, report_dir=tmp_path / "rep")
    assert r2["applied"] == 1
    dest = export_for_planner(work)
    out = next(x for x in json.loads(dest.read_text(encoding="utf-8"))["rules"] if x["id"] == blocked.id)
    assert out["scope"] == ["tank transporters"] and out["semantic_class"] == "mandatory_constraint" and out["verified_against_pdf"] and validate_export(dest, work) == []


def test_unclassified_must_be_corrected_with_a_class_and_scope_and_a_real_constraint_is_routed_to_manual_rules(work, tmp_path):
    pack = tmp_path / "pack"
    RP.make_pack(work, pack, max_rows=500)
    f = pack / "review_MASTER.csv"      # one file holding every row (the per-document files hold the same rows)
    unc = _first(work, lambda r: r.destination == "unclassified_evidence")
    _fill(f, unc.id, decision="CORRECT", pdf_checked="yes", reviewer_name="Cyrine", corrected_class="descriptive_value")      # no scope: refused
    assert RP.import_reviews(work, [f], apply=True, report_dir=tmp_path / "r1")["results"].get("refused") == 1
    # a real constraint among unclassified items: no planner template exists, so it is routed to manual_rules, never applied
    _fill(f, unc.id, decision="CORRECT", pdf_checked="yes", reviewer_name="Cyrine", corrected_class="mandatory_constraint", corrected_scope="all vehicles")
    rep = RP.import_reviews(work, [f], apply=True, report_dir=tmp_path / "r3")
    assert rep["results"].get("needs_manual_rule") == 1 and unc.id not in P.load_decisions(work)
    # a non-hard class with a scope is accepted
    _fill(f, unc.id, decision="CORRECT", pdf_checked="yes", reviewer_name="Cyrine", corrected_class="descriptive_value", corrected_scope="all vehicles")
    assert RP.import_reviews(work, [f], apply=True, report_dir=tmp_path / "r2")["applied"] == 1
    assert (work / "manual_rules_from_review.csv").exists()


def test_duplicates_and_conflicts_are_detected_and_nothing_is_overwritten(work, tmp_path):
    pack = tmp_path / "pack"
    RP.make_pack(work, pack, max_rows=500)
    f1 = pack / "review_MASTER.csv"
    f2 = tmp_path / "copy.csv"; shutil.copy(f1, f2)
    f3 = tmp_path / "copy2.csv"; shutil.copy(f1, f3)
    a, b = [r for r in P.load_draft(work) if r.destination == "advisory"][:2]
    for f in (f1, f2):                                               # identical in both files: counted once
        _fill(f, a.id, decision="ACCEPT", pdf_checked="yes", reviewer_name="Amel")
    _fill(f1, b.id, decision="ACCEPT", pdf_checked="yes", reviewer_name="Amel")       # contradictory
    _fill(f2, b.id, decision="REJECT", reviewer_name="Bilel")
    rep = RP.import_reviews(work, [f1, f2], apply=True, report_dir=tmp_path / "rep")
    res = [r for r in csv.DictReader(open(rep["report"], encoding="utf-8-sig"), delimiter=";")]
    assert any(r["review_id"] == b.id and r["result"] == "conflict" for r in res)
    assert any(r["review_id"] == a.id and r["result"] == "duplicate_identical" for r in res)
    dec = P.load_decisions(work)
    assert a.id in dec and b.id not in dec                           # the conflict applied neither side
    # a later file disagreeing with a decision already recorded does not overwrite it
    _fill(f3, a.id, decision="REJECT", reviewer_name="Cyrine")
    rep2 = RP.import_reviews(work, [f3], apply=True, report_dir=tmp_path / "rep2")
    assert rep2["results"].get("conflict_existing") == 1 and P.load_decisions(work)[a.id]["decision"] == "accept"
    rep3 = RP.import_reviews(work, [f3], apply=True, allow_replace=True, report_dir=tmp_path / "rep3")
    assert P.load_decisions(work)[a.id]["decision"] == "reject"
    assert (work / "decisions_before_review_import.json").exists()


def test_altered_evidence_stale_and_unknown_rows_are_refused(work, tmp_path):
    pack = tmp_path / "pack"
    RP.make_pack(work, pack, max_rows=500)
    f = pack / "review_MASTER.csv"      # one file holding every row (the per-document files hold the same rows)
    r1, r2 = [r for r in P.load_draft(work) if r.destination == "advisory"][:2]
    rows = RP.read_csv(f)
    for r in rows:
        if r["review_id"] == r1.id:
            r.update(decision="REJECT", reviewer_name="Amel", quote="Convoys shall always travel at 42 km/h.")   # evidence altered
        if r["review_id"] == r2.id:
            r.update(decision="REJECT", reviewer_name="Amel")
    rows.append({**rows[0], "review_id": "R-nothing", "decision": "REJECT", "reviewer_name": "Amel"})
    RP.write_csv(f, rows, ";")
    # the rule changed after the pack was made
    d = json.loads((work / P.DRAFT).read_text(encoding="utf-8"))
    next(x for x in d["rules"] if x["id"] == r2.id)["scope"] = ["changed since"]
    (work / P.DRAFT).write_text(json.dumps(d), encoding="utf-8")
    rep = RP.import_reviews(work, [f], apply=True, report_dir=tmp_path / "rep")
    res = {r["review_id"]: r["result"] for r in csv.DictReader(open(rep["report"], encoding="utf-8-sig"), delimiter=";")}
    assert res[r1.id] == "refused" and res[r2.id] == "stale" and res["R-nothing"] == "refused"
    assert not P.load_decisions(work)


def test_progress_counts_per_document(work, tmp_path):
    pack = tmp_path / "pack"
    RP.make_pack(work, pack, max_rows=500)
    f = pack / "review_MASTER.csv"      # one file holding every row (the per-document files hold the same rows)
    adv = [r for r in P.load_draft(work) if r.destination == "advisory"][:3]
    for r, d in zip(adv, ("ACCEPT", "REJECT", "UNSURE")):
        _fill(f, r.id, decision=d, pdf_checked="yes" if d == "ACCEPT" else "", reviewer_name="Amel")
    rep = RP.import_reviews(work, [f], apply=False, report_dir=tmp_path / "rep")
    prog = list(csv.DictReader(open(tmp_path / "rep" / "REVIEW_PROGRESS.csv", encoding="utf-8-sig"), delimiter=";"))
    allrow = prog[-1]
    assert (int(allrow["reviewed"]), int(allrow["accepted"]), int(allrow["rejected"]), int(allrow["unsure"])) == (3, 1, 1, 1)
    assert not P.load_decisions(work)                                 # a dry run writes nothing