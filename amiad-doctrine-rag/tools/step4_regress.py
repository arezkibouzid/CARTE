"""Step 4 regression set: the nine reference sentences and the reviewed sentences whose full text we hold.

Each case says where the candidate must END UP (destination) and its semantic class, independently of the reviewer's
accept/reject verdict: an accepted escort recommendation is valid evidence, but not a hard constraint.
A candidate must never vanish: it is in `constraint`, `advisory` or `blocked`."""
import pytest

from docrag.extract import extract_chunks
from docrag.ingest import Chunk

REF = "Only normal convoy discipline is imposed; that is, a minimum spacing of 30.5 meters (100 feet) between vehicles and a maximum speed of 40 KPH (25 MPH)."
CAUTION = "Caution crossings require that vehicles remain on the centerline of the bridge, maintain a spacing of 50 meters between vehicles, do not exceed a speed of 12 KPH (8 MPH) ; and do not stop, accelerate, or shift gears while crossing."
RISK = "Risk crossings require that vehicles remain over the centerline; do not exceed a speed of 5 KPH (3 MPH) ; and do not stop, accelerate, or shift gears on the bridge."

# (name, [passages before], sentence, value, unit, {allowed destinations}, {allowed classes})
CASES = [
    ("ref1 30.5 m spacing", [], REF, 30.5, "m", {"constraint"}, {"conditional_requirement", "mandatory_constraint"}),
    ("ref2 40 km/h", [], REF, 40.0, "km/h", {"constraint"}, {"conditional_requirement", "mandatory_constraint"}),
    ("ref3 50 m caution", [], CAUTION, 50.0, "m", {"constraint"}, {"conditional_requirement"}),
    ("ref4 12 km/h caution", [], CAUTION, 12.0, "km/h", {"constraint"}, {"conditional_requirement"}),
    ("ref5 5 km/h risk", [], RISK, 5.0, "km/h", {"constraint"}, {"conditional_requirement"}),
    ("ref6 50 mph", [], "The maximum speed authorized for military vehicles on expressways is 50 MPH.", 50.0, "mph",
     {"constraint"}, {"mandatory_constraint"}),
    ("ref8 4.3 m obstruction", [], "Overhead clearance less than 4.3 meters (14 ft) is considered an obstruction to traffic flow and is indicated by the symbol (OB) in the route classification formula (para 2-8).",
     4.3, "m", {"advisory"}, {"threshold_trigger"}),
    ("ref9 this value 30.5 m", ["Regarding floating bridges, several vehicles are allowed to cross only with a distance between vehicles greater than the one established during testing."],
     "This value (which must never be lower than 30.5 m) will not appear on the bridge but in the technical documents put at the disposal of the engineer in charge of the bridge.",
     30.5, "m", {"blocked"}, {"mandatory_constraint", "conditional_requirement"}),
    # reviewed examples
    ("4.50 m desirable headroom", [], "Headroom The desirable minimum headroom for bridges is 4.50 m.", 4.5, "m", {"advisory"}, {"recommendation"}),
    ("MLC 150 in excess", [], "Crossing of bridges by extremely heavy vehicles (in excess of MLC 150) must be assessed on a case-by-case basis even if the inequality MLCvehicle ≤ MLCbridge is respected.",
     150.0, "MLC", {"advisory"}, {"threshold_trigger"}),
    ("MLC 120 recommended", [], "This case-by-case assessment is highly recommended for vehicles exceeding MLC 120.", 120.0, "MLC", {"advisory", "blocked"}, {"recommendation"}),
    ("MLC 150+ should", [], "A detailed analysis procedure or the approach as outlined in Section 4.1.10 should be applied, as appropriate, when a bridge is crossed by an MLC 150+ vehicle.",
     150.0, "MLC", {"advisory"}, {"recommendation"}),
    ("30 minute halts plotted", [], "Long halts made by columns for at least 30 minutes must always be specifically plotted on road movement graphs.", 30.0, "min",
     {"advisory"}, {"threshold_trigger"}),
    ("10 minute halts normal practice", [], "Short halts made by columns normally are to last at least 10 minutes and should be taken when possible after every 2 hours of operation.",
     10.0, "min", {"advisory"}, {"recommendation"}),
    ("3 tons class number", [], "Standard military self-propelled vehicles with a gross weight over 3 tons and trailers with a rated payload of IV2 tons and over, must carry a load class number.",
     3.0, "t", {"advisory"}, {"threshold_trigger"}),
    ("3.50 m average lane", [], "The average width of a lane required for the movement of one column is established at 3.50 meters (ll1/^ ft) for wheeled vehicles and 4 meters (13 ft) for tracked vehicles.",
     3.5, "m", {"advisory"}, {"descriptive_value"}),
    ("5.50 m two-way rating", [], "Narrow bridges: Bridges narrower than 5.50 m shall not be rated for two-way traffic.", 5.5, "m", {"blocked"},
     {"mandatory_constraint", "conditional_requirement"}),
    ("5 km/h guided vehicles", ["Guided vehicles cross the bridge one at a time."], "They are guided and their speed must not exceed 5 km/h.", 5.0, "km/h",
     {"blocked"}, {"mandatory_constraint", "conditional_requirement"}),
]
FLAGS = [   # escort statements the reviewer accepted: valid evidence, destination by wording
    ("escort optional", "A convoy may be provided MP or combat force escorts.", {"advisory"}, {"recommendation"}),
    ("escort rear should", "If only one escort vehicle is provided, it should be placed to the rear of the convoy so it can be brought forward in the event of a tactical emergency.", {"advisory"}, {"recommendation"}),
    ("escort rear must", "Since it is easier for vehicles to move forward, some escort vehicles must be positioned in the rear of the march element to which they are attached.", {"blocked"}, {"mandatory_constraint"}),   # explicit duty, but no stated applicability: a person writes it
    ("escort prior approval", "Prior approval from higher headquarters must be received before task vehicles are converted into escort/gun trucks.", {"blocked"}, {"mandatory_constraint"}),
    ("escort tactical advice", "The security escort must take care in assaulting the main ambush force as mines and booby traps are commonly used to protect its flanks.", {"blocked"}, {"mandatory_constraint"}),
    ("escort chemical shipments", "According to AR 50-6, shipments of certain categories of chemical surety material must be accompanied by technical escorts who are qualified by the CPRP.", {"blocked"}, {"mandatory_constraint"}),
    ("escort lights", "When escort vehicles are required, they either must have warning lights or be driven with vehicle headlights on.", {"constraint"}, {"conditional_requirement", "mandatory_constraint"}),
    ("escort police", "If a civilian police escort is not available, MPs or other military personnel should provide escort service.", {"advisory"}, {"recommendation"}),
]


def run(before, sentence):
    texts = before + [sentence]
    chunks = [Chunk(i, f"D:1:{i + 1}", "STANAG_2015", 1, i + 1, "", "", "", t, "ok") for i, t in enumerate(texts)]
    rules, _, _ = extract_chunks(chunks)
    return rules


@pytest.mark.parametrize("name,before,sentence,value,unit,dests,classes", CASES, ids=[c[0] for c in CASES])
def test_reviewed_and_reference_sentences(name, before, sentence, value, unit, dests, classes):
    got = [r for r in run(before, sentence) if r.value is not None and abs(r.value - value) < 1e-9 and r.unit == unit]
    assert got, f"{name}: the candidate disappeared"
    r = got[0]
    assert r.destination in dests, f"{name}: destination {r.destination} (class {r.semantic_class}), expected {dests}: {r.context_issues}"
    assert r.semantic_class in classes, f"{name}: class {r.semantic_class}, expected {classes}"
    assert r.quote in sentence             # the quotation is always word for word


@pytest.mark.parametrize("name,sentence,dests,classes", FLAGS, ids=[c[0] for c in FLAGS])
def test_accepted_escort_statements_are_kept_with_the_right_destination(name, sentence, dests, classes):
    got = [r for r in run([], sentence) if r.template == "PRO.ESCORT"]
    assert got, f"{name}: the candidate disappeared"
    assert got[0].destination in dests and got[0].semantic_class in classes, (name, got[0].destination, got[0].semantic_class)


def test_the_ten_minute_sentence_never_becomes_a_constraint():
    s = "Convoy commanders must be aware that another convoy could be scheduled as close as 10 minutes behind or in front of their convoy."
    assert not [r for r in run([], s) if r.destination == "constraint"]


# ---- unclassified evidence ------------------------------------------------------------------------------------

def _one(sentence, **kw):
    return [r for r in run(kw.get("before", []), sentence) if r.destination == "unclassified_evidence"]


def test_permission_to_provide_escorts_is_visible_with_its_quotation():
    s = "A convoy may be provided MP or combat force escorts."
    got = [r for r in run([], s) if r.template in ("PRO.ESCORT", "EVID.UNCLASSIFIED")]
    assert got and got[0].destination in ("advisory", "unclassified_evidence")
    assert got[0].quote == s and got[0].page == 1


def test_ten_minute_awareness_sentence_is_visible_and_not_exportable():
    s = "Convoy commanders must be aware that another convoy could be scheduled as close as 10 minutes behind or in front of their convoy."
    got = [r for r in run([], s) if r.value == 10.0 and r.unit == "min"]
    assert got, "the sentence disappeared"
    assert got[0].destination in ("advisory", "unclassified_evidence") and not got[0].hard
    assert got[0].quote == s and got[0].page == 1


def test_relevance_filter_keeps_page_numbers_and_figure_references_out():
    for s in ("See Figure 2-50, Typical multilane bridge classification and regulatory signs, on page 73.",
              "Figure 4 shows a convoy of 12 vehicles at 40 km/h.",
              "Annex B, paragraph 3 refers to table 12 and chapter 7.",
              "The meeting is on 12 November."):
        assert not _one(s), s


def test_unclassified_evidence_is_never_exported_and_never_a_rule(tmp_path):
    import json, shutil
    from pathlib import Path
    from docrag import pipeline as P
    from docrag.contract import export_for_planner, ExportError, validate_export
    corpus = Path(__file__).resolve().parent.parent / "sample_corpus"
    P.build([corpus], tmp_path); P.extract(tmp_path)
    draft = P.load_draft(tmp_path)
    unc = [r for r in draft if r.destination == "unclassified_evidence"]
    assert unc
    with pytest.raises(ValueError):
        P.save_decision(tmp_path, unc[0].id, "accept", "Molka", pdf_checked=True)     # not without edit_class via the sheet
    first_real = next(r for r in draft if r.destination != "unclassified_evidence")
    P.save_decision(tmp_path, first_real.id, "accept", "Molka", pdf_checked=True)
    dest = export_for_planner(tmp_path, {first_real.template: "PLAN." + first_real.template}, provisional=True)
    d = json.loads(Path(dest).read_text(encoding="utf-8"))
    ids = {r["id"] for r in d["rules"]} | {r["id"] for r in d.get("advisory", [])}
    assert not ids & {r.id for r in unc}


def test_two_identical_runs_give_identical_outputs_and_ids(tmp_path):
    from pathlib import Path
    from docrag import pipeline as P
    corpus = Path(__file__).resolve().parent.parent / "sample_corpus"
    P.build([corpus], tmp_path)
    P.extract(tmp_path)
    a = (tmp_path / P.DRAFT).read_text(encoding="utf-8"), (tmp_path / P.SKIPPED).read_text(encoding="utf-8")
    sheet_a = (tmp_path / P.SHEET).read_text(encoding="utf-8-sig")
    P.extract(tmp_path)
    b = (tmp_path / P.DRAFT).read_text(encoding="utf-8"), (tmp_path / P.SKIPPED).read_text(encoding="utf-8")
    assert a == b                                                      # byte for byte, ids included
    assert sheet_a == (tmp_path / P.SHEET).read_text(encoding="utf-8-sig")
    assert not (tmp_path / "audit_log.jsonl").exists()                 # nothing changed, so nothing is logged


def test_audit_trail_records_class_changes_and_removals(tmp_path):
    from dataclasses import replace
    from docrag.audit import audit_changes
    old = run([], "Vehicles must not exceed a speed of 40 km/h on expressways.")
    new = [replace(r, semantic_class="recommendation") for r in old]
    out = audit_changes(old, new, tmp_path)
    assert out["changed"] == len(old) and new[0].history[-1]["class"][1] == "recommendation"
    assert (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8").count("\n") == len(old)
    gone = audit_changes(new, [], tmp_path)
    assert gone["removed"] == len(old)
    again = audit_changes(new, [replace(r) for r in new], tmp_path)    # identical: nothing added
    assert again == {"changed": 0, "removed": 0}


def test_unclassified_cannot_be_accepted_by_sheet_without_a_class_and_pdf_check():
    from docrag.signoff import sheet_decisions, write_sheet
    import csv, tempfile, pathlib
    rules = run([], "Commanders must report significant delays to the next higher headquarters within 2 hours.")
    unc = [r for r in rules if r.destination == "unclassified_evidence"]
    assert unc
    d = pathlib.Path(tempfile.mkdtemp())
    write_sheet(unc, d / "s.csv")
    rows = list(csv.DictReader(open(d / "s.csv", encoding="utf-8-sig")))
    def decide(**kw):
        rows[0].update({"decision": "accept", "edit_class": "", "pdf_checked": "", "edit_scope": "", "edit_conditions": "", **kw})
        with open(d / "t.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
        return sheet_decisions(unc, d / "t.csv", "Molka")
    assert decide()[0] == {}
    assert decide(edit_class="mandatory_constraint", pdf_checked="yes", edit_scope="all commanders")[0] == {}   # no planner template
    assert decide(edit_class="recommendation", pdf_checked="yes")[0] == {}                                     # context missing
    ok, _ = decide(edit_class="descriptive_value", pdf_checked="yes", edit_scope="all commanders")
    assert unc[0].id in ok


def test_hand_typed_and_automatic_rule_never_share_an_id(tmp_path):
    from docrag.manual import load_manual
    from docrag.extract import reconcile
    sentence = "This value (which must never be lower than 30.5 m) will not appear on the bridge but in the technical documents."
    auto = run(["Floating bridges: vehicles keep a distance greater than the one established during testing."], sentence)
    csv_path = tmp_path / "m.csv"
    csv_path.write_text("template,bound,value,unit,strictness,scope,conditions,document,page,quote,note\n"
                        f'NET.GAP,min,30.5,m,must,,"floating bridge",STANAG_2015,1,"{sentence}",\n', encoding="utf-8")
    chunks = [Chunk(0, "D:1:1", "STANAG_2015", 1, 1, "", "", "", "Floating bridges: vehicles keep a distance greater than the one established during testing.", "ok"),
              Chunk(1, "D:1:2", "STANAG_2015", 1, 2, "", "", "", sentence, "ok")]
    by_hand, _ = load_manual(csv_path, chunks)
    kept, _ = reconcile(auto + by_hand)
    ids = [r.id for r in kept]
    assert len(ids) == len(set(ids)), ids


def test_apply_review_never_guesses_between_automatic_and_hand_typed(tmp_path):
    import csv, subprocess, sys
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    cols = ["id", "decision", "template", "bound", "value", "unit", "doc", "page", "quote", "origin"]
    q = "This value (which must never be lower than 30.5 m) will not appear."
    def sheet(path, rows):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
            for r in rows: w.writerow({**dict.fromkeys(cols, ""), **r})
    common = dict(template="NET.GAP", bound="min", value="30.5", unit="m", doc="AEP", page="83", quote=q)
    sheet(tmp_path / "old.csv", [dict(common, id="R-old001", origin="manual")])
    sheet(tmp_path / "new.csv", [dict(common, id="R-new001", origin="pattern"), dict(common, id="R-new002", origin="manual")])
    (tmp_path / "rev.txt").write_text("R-old001 | ACCEPT | reference\n", encoding="utf-8")
    subprocess.run([sys.executable, str(root / "tools" / "apply_review.py"), str(tmp_path / "new.csv"), str(tmp_path / "rev.txt"),
                    "--old-sheet", str(tmp_path / "old.csv")], check=True, capture_output=True)
    rows = {r["id"]: r["decision"] for r in csv.DictReader(open(tmp_path / "new.csv", encoding="utf-8-sig"))}
    assert rows == {"R-new001": "", "R-new002": "accept"}      # the verdict goes to the hand-typed rule only


def test_two_escort_sentences_with_the_same_scope_are_not_merged():
    """Seen on the corpus: reconcile merged all PRO.ESCORT rules of scope 'convoy' into one, silently dropping the others."""
    from docrag.extract import reconcile
    a = run([], "A convoy may be provided MP or combat force escorts.")
    b = run([], "If only one escort vehicle is provided, it should be placed to the rear of the convoy.")
    c = run([], "Convoys carrying ammunition must be escorted when they cross a built-up area.")
    allr = [r for r in a + b + c if r.template == "PRO.ESCORT"]
    assert len(allr) == 3
    kept, _ = reconcile(allr)
    assert len(kept) == 3 and all(r.quote for r in kept)


def test_escort_sentences_without_a_modal_verb_are_not_queued_as_unclassified():
    """Seen on the corpus: 18 escort descriptions ('Fewer guides, escorts, and route markers are needed') filled the queue."""
    for s in ("Fewer guides, escorts, and route markers are needed.",
              "The advance party officer, trail party officer, pacesetter, and escorts assist the convoy commander in controlling the movement."):
        assert not [r for r in run([], s) if r.destination == "unclassified_evidence"], s
    # a modal verb that the reading cannot place keeps the sentence visible
    kept = [r for r in run([], "Escorts will, whenever the tactical situation permits, be provided by the supported unit.")]
    assert all(r.destination != "constraint" for r in kept)


def test_escort_sentences_left_out_are_still_logged_with_their_reason():
    from docrag.extract import extract_pattern
    c = Chunk(0, "D:1:1", "STANAG_2174", 78, 1, "", "", "", "However, military escorts have no authority to instruct military drivers to disregard traffic laws.", "ok")
    rules, skipped = extract_pattern(c)
    assert not [r for r in rules if r.destination != "advisory"]
    assert any("escort" in x["reason"] and "military escorts" in x["sentence"] and x["page"] == 78 for x in skipped)


def test_validator_rejects_a_flipped_operator_or_bound_even_with_a_valid_hash(tmp_path):
    """Step 5 gap found by tools/step5_safety.py: a flipped operator or minimum/maximum used to pass validation."""
    import copy, hashlib, json
    from pathlib import Path
    from docrag import pipeline as P
    from docrag.contract import export_for_planner, validate_export
    corpus = Path(__file__).resolve().parent.parent / "sample_corpus"
    P.build([corpus], tmp_path); P.extract(tmp_path)
    r = next(x for x in P.load_draft(tmp_path) if x.destination == "constraint" and x.value is not None and x.operator and x.template != "SEG.TOWN")
    P.save_decision(tmp_path, r.id, "accept", "Tester", "", True)
    good = json.loads(Path(export_for_planner(tmp_path)).read_text(encoding="utf-8"))
    assert validate_export(export_for_planner(tmp_path), tmp_path) == []
    def damaged(fn):
        d = copy.deepcopy(good); fn(d["rules"][0])
        d["rules_sha256"] = hashlib.sha256(json.dumps(d["rules"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        f = tmp_path / "bad.json"; f.write_text(json.dumps(d), encoding="utf-8")
        return validate_export(f, tmp_path)
    flip = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "<"}
    assert damaged(lambda x: x.update(operator=flip[x["operator"]]))
    assert damaged(lambda x: x.update(bound="min" if x["bound"] == "max" else "max"))