"""Chunking rules: noise is removed, but no number, unit or condition is lost, and cited pages stay exact."""
import re

from docrag.extract import Rule
from docrag.ingest import read_document
from docrag.reliability import looks_damaged
from docrag.text import quantities


def _doc(tmp_path, pages, name="Doc.txt"):
    f = tmp_path / name
    f.write_text("\n".join(f"[page {i}]\n{p}" for i, p in enumerate(pages, 1)), encoding="utf-8")
    cs, _ = read_document(f)
    return cs


def _all(cs):
    return " ".join(c.text for c in cs)


def test_footers_with_changing_labels_are_removed_but_body_numbers_stay(tmp_path):
    labels = ["VII", "H-1", "E-1", "xi", "2-3", "A-4", "iv", "B-7", "12", "ix"]
    pages = [f"{lab} Edition B Version 1\n\nConvoys of type {chr(65 + i)} keep a gap of {20 + i} m between vehicles." for i, lab in enumerate(labels)]
    cs = _doc(tmp_path, pages)
    assert "Edition B" not in _all(cs)
    got = sorted(q["value"] for q in quantities(_all(cs)))
    assert got == [float(20 + i) for i in range(10)]
    assert all(c.page == i + 1 for i, c in enumerate(cs))           # page references intact


def test_a_repeated_line_with_a_measurement_is_never_taken_for_a_footer(tmp_path):
    pages = [f"P{i} Edition B Max 40 km/h\n\nPlanning note {chr(65 + i)} concerns movement." for i in range(10)]
    assert _all(_doc(tmp_path, pages)).count("40 km/h") == 10


def test_contents_lines_with_dotted_leaders_are_removed(tmp_path):
    toc = "2.1. CLASSES OF VEHICLES ........................ 2-1\n5-7. Night Convoys ..................... VII\n"
    cs = _doc(tmp_path, [toc + "\nThe gap shall be 50 m at night."] + ["Filler sentence about nothing."] * 2)
    assert "......" not in _all(cs) and "50 m" in _all(cs)


def test_numbered_heading_with_full_stop_becomes_a_section_not_part_of_the_previous_paragraph(tmp_path):
    cs = _doc(tmp_path, ["All bridges shall be rated for both wheeled and tracked vehicles.\n4.1.4. Vehicular live loadings\n"
                         "The footprint width is 0.50 m on tracked vehicles."])
    assert "Vehicular live loadings" not in cs[0].text
    assert cs[-1].section.startswith("4.1.4") and "0.50 m" in cs[-1].text


def test_a_wrapped_numbered_sentence_is_not_a_heading_and_keeps_its_number_and_condition(tmp_path):
    cs = _doc(tmp_path, ["4.2. Vehicles must keep a distance of\nat least 30 m between them on bridges."])
    assert len(cs) == 1 and "at least 30 m" in cs[0].text and "Vehicles must keep" in cs[0].text


def test_a_numbered_title_that_holds_a_measurement_stays_in_the_text(tmp_path):
    cs = _doc(tmp_path, ["1.2. Speed limit 40 km/h\nApplies to all convoys."])
    assert "40 km/h" in _all(cs)


def test_lead_in_stays_with_its_list_items_even_across_blank_lines(tmp_path):
    cs = _doc(tmp_path, ["The following limits apply on this route:\n\nvehicles under 3.5 t may use route A;\n\n"
                         "vehicles over 3.5 t must use route B."])
    assert len(cs) == 1
    assert "following limits" in cs[0].text and "route A" in cs[0].text and "route B" in cs[0].text


def test_lead_in_stays_with_lettered_items(tmp_path):
    cs = _doc(tmp_path, ["Conditions are as follows:\na. speed at most 40 km/h;\nb. gap at least 50 m."])
    assert len(cs) == 1 and "40 km/h" in cs[0].text and "50 m" in cs[0].text and "as follows" in cs[0].text


def test_lead_in_joins_items_on_the_next_page(tmp_path):
    cs = _doc(tmp_path, ["Convoys must observe:", "- a gap of 50 m;\n- a speed of 40 km/h."])
    assert len(cs) == 1 and "50 m" in cs[0].text and "40 km/h" in cs[0].text and cs[0].page == 1


def test_a_normal_paragraph_after_a_sentence_is_not_merged(tmp_path):
    cs = _doc(tmp_path, ["The gap is 50 m.\n\nThe speed is 40 km/h."])
    assert len(cs) == 2


def test_long_list_is_cut_between_items_never_inside_one(tmp_path):
    items = [f"vehicle type {i} may carry {i + 1}.5 t on axle {i}" for i in range(60)]
    cs = _doc(tmp_path, ["Allowed loads include:\n" + "\n".join(f"- {t};" for t in items)])
    assert len(cs) > 1 and all(len(c.text) <= 1600 for c in cs)
    text = _all(cs)
    assert all(t in text for t in items)                            # no item cut, none lost


def test_numeric_table_is_kept_whole(tmp_path):
    table = " ".join(f"{i}.{i % 7}" for i in range(400))
    cs = _doc(tmp_path, [table])
    assert len(cs) == 1 and cs[0].quality == "table"


def test_unit_letters_after_numbers_are_not_damage_but_real_garbage_still_is():
    assert not looks_damaged("The following are the minimum widths: 4-12 2.75 m 5.50 m 13-30 3.35 m 5.50 m 31-70 4.00 m 7.30 m")
    assert looks_damaged("m- «c > ?? A & yêk •> f \"à s % «e ■rs St m H m 3*.* #s Figure 2-18.")


def _rule(chunk_id, para):
    r = Rule("NET.GAP", "min", 30.5, "m", "must", "Vehicles must keep 30.5 m.", chunk_id, "Doc", 7, para)
    r.assign_id()
    return r


def test_rule_id_does_not_change_when_passages_are_renumbered():
    assert _rule("Doc:7:3", 3).id == _rule("Doc:7:1", 1).id


def test_annex_style_headings_are_recognised(tmp_path):
    cs = _doc(tmp_path, ["Spacing is given in the tables below.\nB.1. Wheeled vehicles\nThe nominal width is 2.55 m for this class."])
    assert "Wheeled vehicles" not in _all(cs) and cs[-1].section.startswith("B.1") and "2.55 m" in cs[-1].text


def test_a_short_sentence_with_a_measurement_is_not_dropped(tmp_path):
    assert "50 m" in _all(_doc(tmp_path, ["The gap is 50 m."]))


def test_lettered_items_ending_with_a_full_stop_all_stay_with_the_lead_in_but_not_the_next_paragraph(tmp_path):
    cs = _doc(tmp_path, ["6. Vehicles to be marked, except that the marking of the following is optional:\n"
                         "a. Vehicles of a gross weight of 3 tons or less.\n"
                         "b. Baggage trailers with a rated capacity of 1.5 tons or less.\n"
                         "7. Signs must be at least 20 cm wide."])
    assert len(cs) == 2
    assert "optional" in cs[0].text and "3 tons" in cs[0].text and "1.5 tons" in cs[0].text
    assert cs[1].text.startswith("Signs must be") and "7." not in cs[0].text


def test_contents_page_without_dots_is_dropped_but_prose_kept():
    from docrag.ingest import _strip_running_lines
    toc = ["Contents", "Section 1 - General 1-1", "Section 2 - Planning 2-1", "Sustain 3-2", "Deployment of forces 3-2",
           "Redeployment of forces 3-4", "Section 4 - Reporting 4-1", "Annex A 5-1"]
    prose = ["Vehicles keep a gap of 50 m.", "See paragraph 3-1", "The convoy halts every 2 h."]
    other = ["A different page about bridges.", "Spans over 30 m need a check."]
    out = _strip_running_lines([toc, prose, other])
    assert not any(l.startswith("Section") for l in out[0])
    assert out[1] == prose            # an ordinary page is never touched


def test_symbol_font_bullets_keep_the_list_with_its_lead_in(tmp_path):
    """Word lists come out of the PDF as U+F0B7. The glyph must read as a bullet so items stay with the lead-in."""
    p1 = ("4.14 Road transportation planning. The following factors affect the suitability of road transportation "
          "and should be considered when planning:\n\n availability of secured routes;\n\n"
          " speed of road movement will be influenced by the types of vehicles (wheeled or\ntracked) and their size;\n\n"
          " density of civilian traffic;")
    cs = _doc(tmp_path, [p1])
    assert "" not in _all(cs)
    assert len(cs) == 1 and "The following factors" in cs[0].text and "speed of road movement" in cs[0].text


def test_table_group_header_stays_with_its_rows(tmp_path):
    """STANAG_2015 p222: the cut must fall BETWEEN vehicle groups, never right after 'Truck, cargo, 5 T, 6x6:'."""
    groups = []
    for g in range(12):
        groups.append(f"Truck, cargo, {g + 2} T, 6x6:\nM{g}1   {9 + g}.55\nM{g}4   {10 + g}.62\nM{g}5A2   {11 + g}.46")
    cs = _doc(tmp_path, ["Table D-3. Wheeled Vehicles\n\n" + "\n".join(groups)])
    assert len(cs) >= 2
    for c in cs:
        assert not c.text.rstrip().endswith(":"), c.text[-60:]       # a header is never the last thing in a passage
        assert c.text.lstrip().split()[0] in {"Truck,", "Wheeled", "Table"} or c.text.startswith("Table"), c.text[:40]


def test_a_sentence_with_semicolon_clauses_stays_whole_when_not_far_too_long(tmp_path):
    """STANAG_2015 p33: five clauses of one sentence about grade arrows, 1000 characters, must stay in one passage."""
    sent = ("Reconnaissance symbols have been adopted to portray gradients on maps. A single headed arrow along the trace "
            "of a route pointing in the uphill direction indicates a 5 but less than 7 percent grade; two arrowheads "
            "represent a grade 7 or greater but less than 10 percent ; three arrowheads represent a grade 10 or greater "
            "but less than 14 percent; and four arrowheads represent a grade 14 percent and over (fig. 2-10). "
            + "The figure shows an example of the method used on a typical map sheet. " * 6)
    cs = _doc(tmp_path, [sent])
    whole = [c for c in cs if "single headed arrow" in c.text and "four arrowheads" in c.text]
    assert whole, [c.text[-80:] for c in cs]