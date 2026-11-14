"""Step 4: what a number MEANS, and whether the sentence alone is the whole rule."""
import json

import pytest

from docrag import semantics as SEM
from docrag.extract import extract_chunks, extract_pattern
from docrag.ingest import Chunk
from docrag.text import quantities


def read(sentence, which=0):
    qs = SEM.with_range_starts(sentence, SEM.drop_conversions(sentence, quantities(sentence)))
    q = qs[which]
    return SEM.analyse(sentence, (q["start"], q["end"]))


def chunk(text, n=0, page=1, para=1, doc="Doc", quality="ok"):
    return Chunk(n, f"{doc}:{page}:{para}", doc, page, para, "", "", "", text, quality)


# ---- the four examples from the review -----------------------------------------------------------------------

def test_desirable_minimum_is_a_recommendation_not_a_minimum():
    r = read("The desirable minimum headroom for bridges is 4.50 m.")
    assert r.cls == SEM.RECOMMENDATION and r.operator == ">="


def test_in_excess_of_mlc_triggers_an_assessment_it_is_not_an_mlc_limit():
    r = SEM.analyse("Crossing of bridges by extremely heavy vehicles (in excess of MLC 150) must be assessed on a case-by-case basis.",
                    (57, 64))
    s = "Crossing of bridges by extremely heavy vehicles (in excess of MLC 150) must be assessed on a case-by-case basis."
    i = s.index("MLC 150")
    r = SEM.analyse(s, (i, i + 7))
    assert r.cls == SEM.TRIGGER and r.operator == ">" and "assessed" in r.effect


def test_halts_of_at_least_30_minutes_create_a_reporting_duty_not_a_minimum_halt():
    r = read("Long halts made by columns for at least 30 minutes must always be specifically plotted on road movement graphs.")
    assert r.cls == SEM.TRIGGER and r.operator == ">=" and "plotted" in r.effect


def test_a_specific_trucks_capacity_is_not_a_road_limit():
    r = read("The PLS truck and trailer has an estimated capacity of 33 tons.")
    assert r.cls == SEM.DESCRIPTIVE


# ---- the 10 minute example: "must" is in the sentence, the number is still not a convoy gap rule -------------

def test_ten_minute_awareness_is_never_a_hard_gap():
    s = "Convoy commanders must be aware that another convoy could be scheduled as close as 10 minutes behind or in front of their convoy."
    r = read(s)
    assert r.cls == SEM.DESCRIPTIVE and r.role == "awareness"
    # even when a gap keyword is next to the number, the class stays non-hard
    s2 = "Convoy commanders must be aware that another convoy could follow with a gap as small as 10 minutes."
    rules, _ = extract_pattern(chunk(s2))
    assert rules and all(not x.hard for x in rules)
    # and it can not be exported as a constraint
    assert all(x.semantic_class == SEM.DESCRIPTIVE for x in rules)


# ---- operators -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sentence,op", [
    ("Vehicles must not exceed 40 km/h.", "<="),
    ("The road width must be at least 5.5 m.", ">="),
    ("Gaps shall be no less than 50 m.", ">="),
    ("The load shall not be less than 20 t.", ">="),
    ("A value which must never be lower than 30.5 m is used.", ">="),
    ("Vehicles heavier than 40 t shall not use the bridge.", "<="),
    ("Vehicles narrower than 3 m shall not use the road.", ">="),
    ("Vehicles must keep a spacing of 50 m.", "="),
    ("Vehicles must travel at speeds between 30 and 50 km/h.", ">="),
])
def test_operator(sentence, op):
    assert read(sentence).operator == op


def test_range_has_two_ends():
    s = "Vehicles must travel at speeds between 30 and 50 km/h."
    assert (read(s, 0).operator, read(s, 1).operator) == (">=", "<=")


def test_strictness_is_kept_for_strict_and_non_strict_forms():
    assert read("Vehicles exceeding 40 t must be escorted.").operator == ">"       # strictly above
    assert read("Vehicles of 40 t or more must be escorted.").operator == ">="     # at 40 and above


def test_conversion_in_brackets_is_not_a_second_rule():
    s = "Vehicles must not exceed 12 KPH (8 MPH) on caution crossings."
    rules, _ = extract_pattern(chunk(s))
    assert [(r.value, r.unit) for r in rules] == [(12.0, "km/h")]


# ---- wording that is not the rule's own verb ---------------------------------------------------------------

def test_relative_clause_required_is_not_the_main_verb():
    r = read("Most vehicles which are required to negotiate slopes of 7 percent or greater will be slowed down.")
    assert r is None or r.cls == SEM.DESCRIPTIVE


def test_classification_threshold_is_not_a_limit():
    r = read("Overhead clearance less than 4.3 meters (14 ft) is considered an obstruction to traffic flow.")
    assert r.cls == SEM.TRIGGER and r.role == "classification" and r.operator == "<"


def test_require_that_and_do_not_exceed_are_read_as_conditional_requirements():
    s = "Caution crossings require that vehicles remain on the centerline, maintain a spacing of 50 meters between vehicles, do not exceed a speed of 12 KPH (8 MPH)."
    spacing, speed = read(s, 0), read(s, 1)
    assert spacing.cls == speed.cls == SEM.CONDITIONAL
    assert speed.operator == "<=" and speed.applies_to == "Caution crossings"
    assert not spacing.direction_stated        # "a spacing of 50 m": the text gives no direction, the sheet will say so


def test_clause_after_that_is_inherits_the_force_of_the_clause_before():
    s = "Only normal convoy discipline is imposed; that is, a minimum spacing of 30.5 meters (100 feet) between vehicles and a maximum speed of 40 KPH (25 MPH)."
    gap, speed = read(s, 0), read(s, 1)
    assert gap.cls == speed.cls == SEM.CONDITIONAL and (gap.operator, speed.operator) == (">=", "<=")


def test_authorized_maximum_is_a_limit():
    r = read("The maximum speed authorized for military vehicles on expressways is 50 MPH.")
    assert r.cls == SEM.MANDATORY and r.operator == "<="


def test_a_sentence_without_requirement_wording_is_not_a_candidate():
    rules, skipped = extract_pattern(chunk("The bridge is 40 m long and crosses the river at 5 km from the town."))
    assert not rules and skipped


# ---- context completeness --------------------------------------------------------------------------------------

def _run(texts, **kw):
    chunks = [chunk(t, i, page=1, para=i + 1) for i, t in enumerate(texts)]
    rules, _, _ = extract_chunks(chunks, **kw)
    return rules


def test_value_in_one_passage_and_its_antecedent_in_another():
    rules = _run(["Regarding floating bridges, vehicles may cross only with a distance between vehicles greater than the one established during testing.",
                  "This value (which must never be lower than 30.5 m) will not appear on the bridge."])
    r = next(x for x in rules if x.value == 30.5)
    assert r.context_status == SEM.INCOMPLETE
    assert "refers back" in r.context_issues[0]
    # the neighbouring passage is kept as evidence, separately from the quote, and is not applied to the rule
    assert "floating bridges" in r.context["previous_sentence"]
    assert "floating bridges" not in r.quote and r.conditions == [] and r.scope == []
    assert r.verify_against_pdf


def test_condition_in_the_next_passage_blocks_the_rule():
    rules = _run(["Vehicles must not exceed 40 km/h.", "However, this does not apply to vehicles of the engineers."])
    r = next(x for x in rules if x.value == 40.0)
    assert r.context_status == SEM.INCOMPLETE and "exception" in r.context_issues[0]


def test_a_rule_with_its_conditions_in_the_same_sentence_is_complete():
    rules = _run(["Vehicles must not exceed 40 km/h in built-up areas."])
    r = next(x for x in rules if x.value == 40.0)
    assert r.context_status == SEM.COMPLETE and r.hard and r.conditions


def test_no_condition_anywhere_stays_complete_and_nothing_is_invented():
    rules = _run(["The road width must be at least 5.5 m.", "The next sentence is about something else."])
    r = rules[0]
    assert r.context_status == SEM.COMPLETE and r.conditions == [] and r.scope == []


def test_list_item_needs_its_lead_in():
    rules = _run(["For wheeled vehicles the following apply:", "The speed must not exceed 30 km/h."])
    r = next(x for x in rules if x.value == 30.0)
    assert r.context_status == SEM.INCOMPLETE and "lead-in" in r.context_issues[0]


def test_country_specific_page_blocks_a_value_that_names_no_country():
    rules = _run(["Special Movement Regulations in FR. For vehicles with pneumatic tyres the permissible axle load must not exceed 13 t.",
                  "Country: GE"])
    r = next(x for x in rules if x.value == 13.0)
    assert r.context_status == SEM.COMPLETE or r.context_issues     # a quote that names its own country is allowed
    rules = _run(["The permissible axle load must not exceed 8 t.", "Special Movement Regulations in GE, Country: GE"])
    r = next(x for x in rules if x.value == 8.0)
    assert r.context_status == SEM.INCOMPLETE and "country" in r.context_issues[0]


def test_text_split_inside_a_word_is_detected():
    texts = ["The bridges are narrow. Many bridges exist. Bridges, bridges and bridges.", "Bridges are checked. Bridges again. More bridges.",
             "A brid ges width must be at least 3 m."]
    r = next(x for x in _run(texts) if x.value == 3.0)
    assert r.context_status == SEM.INCOMPLETE and "damaged" in r.context_issues[0]


def test_distance_given_in_tonnes_is_flagged():
    issues = SEM.completeness("If the axle distance is 1.3 t the load is 22 t.", "", "", "", [], None, "t", 21)
    assert any("unit does not fit" in i for i in issues)


# ---- approval and export ------------------------------------------------------------------------------------

def _work(tmp_path, texts):
    from docrag import pipeline as P
    from docrag.retrieve import Index
    from docrag.embed import get_embedder
    chunks = [chunk(t, i, page=1, para=i + 1) for i, t in enumerate(texts)]
    w = tmp_path / "w"
    Index(chunks, get_embedder("hashing")).build().save(w)
    (w / "sources.json").write_text("[]")
    P.extract(w)
    return w, {r.value: r for r in P.load_draft(w)}


def test_incomplete_rule_cannot_be_accepted_without_context_and_pdf(tmp_path):
    from docrag import pipeline as P
    w, by = _work(tmp_path, ["Vehicles must not exceed 40 km/h.", "However, this does not apply to engineers."])
    r = by[40.0]
    assert r.context_status == SEM.INCOMPLETE
    with pytest.raises(ValueError):
        P.save_decision(w, r.id, "accept", "T", pdf_checked=False)
    with pytest.raises(ValueError):
        P.save_decision(w, r.id, "accept", "T", pdf_checked=True)         # PDF compared, but no context written
    P.save_decision(w, r.id, "accept", "T", note="engineers excepted", pdf_checked=True)
    from docrag.contract import export_for_planner, validate_export
    out = export_for_planner(w)
    assert validate_export(out, w) == []
    exported = json.loads(out.read_text(encoding="utf-8"))["rules"][0]
    assert exported["context_status"] == "completed_by_reviewer" and exported["conditions"] == ["engineers excepted"]
    assert exported["quote"] == "Vehicles must not exceed 40 km/h."          # the quote is untouched


def test_advice_is_never_exported_as_a_constraint(tmp_path):
    from docrag import pipeline as P
    from docrag.contract import export_for_planner, validate_export
    w, by = _work(tmp_path, ["The desirable minimum headroom for bridges is 4.50 m.", "Vehicles must not exceed 40 km/h."])
    advice, hard = by[4.5], by[40.0]
    assert advice.semantic_class == SEM.RECOMMENDATION
    P.save_decision(w, advice.id, "accept", "T")
    P.save_decision(w, hard.id, "accept", "T")
    out = export_for_planner(w)
    body = json.loads(out.read_text(encoding="utf-8"))
    assert [r["id"] for r in body["rules"]] == [hard.id]
    assert [r["id"] for r in body["advisory"]] == [advice.id] and body["advisory"][0]["binding"] is False
    assert validate_export(out, w) == []
    # tampering: moving the advice into the binding list is caught
    body["rules"].append(body["advisory"][0])
    body["rules_sha256"] = __import__("hashlib").sha256(json.dumps(body["rules"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    out.write_text(json.dumps(body), encoding="utf-8")
    assert any("not a constraint" in e or "non-binding" in e for e in validate_export(out, w))


def test_ambiguous_rule_needs_a_person_to_classify_it(tmp_path):
    from docrag import pipeline as P
    w, by = _work(tmp_path, ["Vehicles must comply with the following 40 km/h."])
    ambiguous = [r for r in P.load_draft(w) if r.semantic_class == SEM.AMBIGUOUS]
    for r in ambiguous:
        with pytest.raises(ValueError):
            P.save_decision(w, r.id, "accept", "T", pdf_checked=True, note="x")


# ---- errors found on the reviewed examples (Step 4 evaluation) ---------------------------------------------------

def test_a_hedge_in_front_of_the_modal_makes_it_normal_practice():
    r = read("Short halts made by columns normally are to last at least 10 minutes and should be taken after every 2 hours.")
    assert r.cls == SEM.RECOMMENDATION


def test_a_duty_for_the_vehicles_a_number_singles_out_is_a_trigger():
    r = read("Standard military vehicles with a gross weight over 3 tons must carry a load class number.")
    assert r.cls == SEM.TRIGGER and r.operator == ">"


def test_must_be_provided_is_still_a_limit():
    r = read("A minimum clear width of 5.5 m must be provided.")
    assert r.cls == SEM.MANDATORY and r.operator == ">="


def test_qualifier_after_the_number_must_be_recorded_before_the_rule_can_be_approved():
    rules = _run(["Bridges narrower than 5.50 m shall not be rated for two-way traffic."])
    r = next(x for x in rules if x.value == 5.5)
    assert r.context_status == SEM.INCOMPLETE and "two-way traffic" in r.context_issues[0]
    # when the sentence carries the condition itself, nothing is missing
    r2 = next(x for x in _run(["Vehicles must not exceed 40 km/h in built-up areas."]) if x.value == 40.0)
    assert r2.context_status == SEM.COMPLETE


def test_same_number_with_different_subjects_is_not_merged():
    texts = ["Only normal convoy discipline is imposed; that is, a minimum spacing of 30.5 meters between vehicles.",
             "Floating bridges: vehicles must keep a spacing of at least 30.5 meters between vehicles."]
    chunks = [chunk(t, i, page=1 + i, para=1, doc=f"D{i}") for i, t in enumerate(texts)]
    from docrag.extract import reconcile
    rules, _, _ = extract_chunks(chunks)
    kept, _ = reconcile(rules)
    assert len([r for r in kept if r.value == 30.5]) == 2


def test_a_capitalised_row_label_means_a_table_row_for_one_state():
    rules = _run(["OKLAHOMA Width: No access provisions but allowed on any road having surface width of 20 feet or more."])
    r = next(x for x in rules if x.value == 20.0)
    assert r.context_status == SEM.INCOMPLETE and "OKLAHOMA" in r.context_issues[0]


def test_a_unit_conversion_does_not_make_a_sentence_a_table():
    from docrag.extract import table_shape
    assert not table_shape("Caution crossings require a spacing of 50 meters and do not exceed 12 KPH (8 MPH).")
    assert table_shape("Class 4-12 2.75 m 5.50 m 13-30 3.35 m 5.50 m 31-70 4.10 m")


def test_escort_sentences_with_a_plain_duty_are_not_ambiguous():
    for t in ["Some escort vehicles must be positioned in the rear of the march element.",
              "When escort vehicles are required, they either must have warning lights or be driven with headlights on."]:
        rules, _ = extract_pattern(chunk(t))
        esc = [r for r in rules if r.template == "PRO.ESCORT"]
        assert esc and all(r.hard for r in esc), t