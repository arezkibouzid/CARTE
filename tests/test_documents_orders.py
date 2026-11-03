"""Document intake (doctrine sets and order documents), corpus compilation and sign-off, order reading and the sketch reader.

All inputs are synthetic and written by the tests; the stand-in doctrine sentences paraphrase common movement-control
practice and quote no NATO publication."""

import csv
import json

import numpy as np
import pytest

from coa_net.corpus import apply_signoff, compile_corpus, provisional, write_signoff_sheet
from coa_net.documents import clean, match_key, read_document, read_documents
from coa_net.llm import ScriptedLLM
from coa_net.orderdoc import (TASK_ITINERARIES, TASK_MOVEMENT, check_numbers, exercise_readback, numbers_in, read_order,
                              reading_from_text, uncovered)
from coa_net.requirements import load_requirements
from coa_net.scenarios import sample_text

STANDARD = """STANAG 9999 STAND-IN - synthetic test document, not a NATO text

0301. Headway. A minimum of 15 minutes headway between successive convoys shall be kept on the same road.

0302. Halts. A halt of 15 minutes is made at a designated halt area at least every 2 hours of driving.
Convoy speed shall not exceed 60 km/h.

0303. Radio. Each march unit commander must report the crossing of every control point by radio.
"""

DOCTRINE = """AJP-9.9 STAND-IN - synthetic test document, not a NATO text

1. Movement control. A minimum of 10 minutes headway between successive convoys shall be kept on the same road.
Convoy speed shall not exceed 60 km/h.

2. Protection. Columns shall not halt or wait within 800 m of a bridge or tunnel.
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_text_reader_keeps_paragraph_numbers_and_labelled_lines(tmp_path):
    d = read_document(_write(tmp_path, "STANAG_9999.txt", STANDARD))
    nums = [b.number for b in d.blocks if b.number]
    assert nums[:3] == ["0301", "0302", "0303"] and d.language == "en"
    assert any(s.startswith("A minimum of 15 minutes") for _, s in d.sentences())
    o = read_document(_write(tmp_path, "order.txt", sample_text("sample_order.txt")))
    assert [b.text.split(":")[0] for b in o.blocks[1:]] == ["Phase", "Intent", "Convoy C1", "Convoy C2", "Convoy C3"]


def test_quotes_are_found_through_typography_and_hyphenation():
    assert clean("ﬁve km – the column’s") == "five km - the column's"
    assert match_key("at least 5km apart) ;") == match_key("at least 5km apart);")
    from coa_net.documents import _join

    assert _join("the move-", "ment table") == "the movement table" and _join("Pont-", "Est") == "Pont- Est"


def test_docx_reader_headings_lists_tables_and_figures(tmp_path):
    docx = pytest.importorskip("docx")
    from PIL import Image

    img = tmp_path / "map.png"
    Image.new("RGB", (120, 80), (0, 0, 255)).save(img)
    d = docx.Document()
    d.add_heading("Chapter 3 - Road movement", level=1)
    d.add_paragraph("0304. Vehicle interval. The vehicle interval shall be 100 m by day.")
    d.add_paragraph("Convoys should avoid built-up areas.", style="List Bullet")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Vehicle", "Max speed", "heavy", "50 km/h"
    d.add_picture(str(img))
    d.add_paragraph("Figure 1: zones of the exercise", style="Caption")
    d.save(tmp_path / "doc.docx")
    doc = read_document(tmp_path / "doc.docx")
    kinds = [(b.kind, b.number) for b in doc.blocks]
    assert ("heading", "") in kinds and ("paragraph", "0304") in kinds and ("item", "") in kinds
    assert any(b.kind == "table_row" and "Max speed: 50 km/h" in b.text for b in doc.blocks)
    assert len(doc.figures) == 1 and doc.figures[0].caption.startswith("Figure 1") and doc.figures[0].data
    assert doc.blocks[1].section.startswith("Chapter 3")


def test_pdf_reader_drops_running_heads_and_joins_page_breaks(tmp_path):
    pytest.importorskip("pdfplumber")
    mpl = pytest.importorskip("matplotlib")
    mpl.use("Agg")
    mpl.rcParams["pdf.fonttype"] = 42
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    pages = [["0101. General. Convoys shall keep a headway of 12 minutes on the same road and the", "column commander"],
             ["is responsible for the march discipline of every vehicle.", "0102. Halts. A halt of 10 minutes is made every 2 hours."],
             ["0103. Speed. Convoy speed shall not exceed 70 km/h.", ""]]
    with PdfPages(tmp_path / "pub.pdf") as pdf:
        for i, lines in enumerate(pages, start=1):
            fig = plt.figure(figsize=(8.27, 11.69))
            fig.text(0.5, 0.97, "NATO UNCLASSIFIED - TEST STAND-IN", ha="center", fontsize=9)
            y = 0.85
            for ln in lines:
                if ln:
                    fig.text(0.1, y, ln, fontsize=10)
                y -= 0.02
            fig.text(0.5, 0.03, f"Page {i} of 3", ha="center", fontsize=9)
            pdf.savefig(fig)
            plt.close(fig)
    d = read_document(tmp_path / "pub.pdf")
    text = d.text()
    assert "UNCLASSIFIED" not in text and "Page 2" not in text
    assert any("dropped" in w for w in d.warnings)
    first = next(b for b in d.blocks if b.number == "0101")
    assert first.text.endswith("march discipline of every vehicle.") and first.last_page == 2  # joined across the break
    assert d.locate("the column commander is responsible").id == first.id and first.cite() .startswith(f"{d.id}, p. 1-2")


def test_corpus_reconciles_documents_with_precedence_and_seed(tmp_path):
    folder = tmp_path / "corpus"
    folder.mkdir()
    _write(folder, "STANAG_9999.txt", STANDARD), _write(folder, "AJP-9.9.txt", DOCTRINE), _write(folder, "notes.xyz", "x")
    docs, skipped = read_documents([folder])
    assert [d.id for d in docs] == ["AJP-9.9", "STANAG_9999"] and skipped == [f"{folder / 'notes.xyz'}: format not read"]
    base, rep = compile_corpus(docs)
    hw = [r for r in base if r.template == "SCH.HEADWAY"]
    assert len(hw) == 1 and hw[0].params["headway_min"] == 15.0 and hw[0].source.document == "STANAG_9999"  # standard first
    c = next(c for c in rep.conflicts if c.template == "SCH.HEADWAY")
    assert c.rule == "precedence" and c.other_value["headway_min"] == 10.0
    assert any(a.startswith("D") for a, b in rep.corroborated)  # 60 km/h stated in both documents
    seed_ids = {r.id for r in load_requirements()}
    assert not any(r.id in seed_ids and r.template == "SCH.HEADWAY" for r in base)  # the seed headway is replaced
    assert any(sid for sid, by, params in rep.seed_superseded)
    t = rep.totals()
    assert t["normative"] >= 6 and 0 < t["coverage"] <= 1
    assert rep.docs[1].kind == "standard" and rep.docs[0].kind == "doctrine"
    md = rep.markdown(base)
    assert "Conflicts" in md and "STANAG_9999" in md
    # same precedence: the stricter value is kept and the conflict listed
    base2, rep2 = compile_corpus(docs, precedence=["AJP-9.9", "STANAG_9999"])
    assert next(r for r in base2 if r.template == "SCH.HEADWAY").params["headway_min"] == 10.0  # order given by the operator


def test_signoff_round_trip_and_provisional(tmp_path):
    docs, _ = read_documents([_write(tmp_path, "STANAG_9999.txt", STANDARD)])
    base, rep = compile_corpus(docs)
    sheet = tmp_path / "signoff.csv"
    n = write_signoff_sheet(base, rep, sheet)
    rows = list(csv.DictReader(open(sheet, encoding="utf-8")))
    assert n == len(rows) > 0 and all(r["quote"] and r["document"] == "STANAG_9999" for r in rows)
    assert all(r.status == "draft" for r in base if r.source.document == "STANAG_9999")
    rows[0]["decision"] = "accept"
    rows[1]["decision"] = "reject"
    edit = next(r for r in rows if r["template"] == "SCH.HEADWAY")
    edit["decision"], edit["params"] = "edit", json.dumps({"headway_min": 20})
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    signed, notes = apply_signoff(base, sheet, reviewer="ETLO")
    by = {r.id: r for r in signed}
    assert by[rows[0]["id"]].status == "approved" and by[rows[1]["id"]].status == "rejected"
    assert by[edit["id"]].params == {"headway_min": 20} and by[edit["id"]].status == "approved"
    prov, ids = provisional(base)
    assert ids and all(r.status != "draft" for r in prov)


def test_order_reader_routes_and_checks_an_exercise(tmp_path):
    text = ("- The unit needs to get a fuel convoy from the West zone to the East zone. Determine 2 itineraries that link the two "
            "zones with the following constraints.\n  - Avoid getting in towns of more than 5 000 inhabitants;\n"
            "  - Do not use motorways;\n- Desired output :\n  - Layer on top of the map including the different itineraries;\n"
            "  - A KML file of the halts.\n")
    r = reading_from_text(text, "ex2.txt")
    assert r.task == TASK_ITINERARIES and "2 itineraries" in r.task_reason
    assert [d.produced for d in r.deliverables] == [True, False]  # KML: reported, not silently ignored
    from coa_net.exercise import parse_exercise

    spec, _ = parse_exercise(r.text)
    assert spec.k == 2 and spec.min_town_pop == 5000 and (spec.origin_zone, spec.destination_zone) == ("West", "East")
    missing = uncovered(r.items, [q for _, q in spec.recognised])
    assert [i.text for i in missing] == ["Do not use motorways;"]  # no parser handles it: NOT ENFORCED in the readback
    rb = exercise_readback(r, spec)
    assert "NOT ENFORCED: \"Do not use motorways;\"" in rb and "NOT PRODUCED" in rb
    assert check_numbers([("itineraries", 3.0)], r.doc.text()) == ["itineraries: 3 is not in the document"]
    assert {5000.0, 2.0}.issubset(numbers_in(text))
    m = reading_from_text(sample_text("sample_order.txt"))
    assert m.task == TASK_MOVEMENT and [i.role for i in m.items].count("task") == 3


def test_order_reader_on_a_slide_like_pdf(tmp_path):
    pytest.importorskip("pdfplumber")
    mpl = pytest.importorskip("matplotlib")
    mpl.use("Agg")
    mpl.rcParams["pdf.fonttype"] = 42
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(13.33, 7.5))
    ax = fig.add_axes([0.02, 0.1, 0.55, 0.8])
    img = np.full((300, 400, 3), 235, np.uint8)
    img[40:110, 60:200] = (90, 90, 230)  # two blue zones drawn on a grey map
    img[200:270, 220:360] = (90, 90, 230)
    ax.imshow(img)
    ax.axis("off")
    lines = ["- The unit needs to get a bridge from the South zone to the North zone.", "Determine 3 itineraries.",
             "    - Avoid towns of more than 10k inhabitants;", "- Desired output :",
             "    - A text file describing the decision process."]
    for i, ln in enumerate(lines):
        fig.text(0.6, 0.85 - 0.06 * i, ln, fontsize=12)
    fig.savefig(tmp_path / "ex.pdf")
    plt.close(fig)
    r = read_order(tmp_path / "ex.pdf")
    assert r.task == TASK_ITINERARIES and r.illustration is not None and r.illustration.data
    assert [d.label for d in r.deliverables] == ["decision-process text"]
    assert r.of("constraint") and r.of("goal")


def _two_zone_image():
    a = np.full((200, 300, 3), 240, int)
    yy, xx = np.mgrid[:200, :300]
    a[((xx - 80) / 50) ** 2 + ((yy - 50) / 25) ** 2 < 1] = (100, 100, 230)  # north-west zone
    a[((xx - 220) / 50) ** 2 + ((yy - 150) / 25) ** 2 < 1] = (100, 100, 230)  # south-east zone
    a[((xx - 150) / 12) ** 2 + ((yy - 100) / 12) ** 2 < 1] = (230, 30, 30)  # a red point
    return a


def test_marks_found_and_named_by_position():
    pytest.importorskip("scipy")
    from coa_net.figures import name_marks, names_in, overlay_marks, roles_in

    marks, masks = overlay_marks(_two_zone_image(), colours=("blue", "red"), min_px=200, per_colour=2, close_iter=1)
    assert [m.colour for m in marks] == ["blue", "blue", "red"]
    text = "Move the column from the South zone to the North zone, avoiding the red point."
    assert names_in(text) == ["South zone", "North zone"]
    assert roles_in(text, names_in(text)) == {"South zone": "origin", "North zone": "destination"}
    marks, notes, how = name_marks(marks, text)
    by = {m.label: m for m in marks if m.label}
    assert how == "position words" and by["North"].centroid[1] < by["South"].centroid[1]  # image y grows downwards
    assert by["South"].role == "origin" and any("mark 3" in n for n in notes)  # the red mark is shown, not guessed


def test_sketch_reader_answer_is_checked():
    pytest.importorskip("scipy")
    from PIL import Image

    from coa_net.figures import name_marks, overlay_marks

    text = "Move the column from the South zone to the North zone."
    good = ScriptedLLM(lambda stage, user: {"marks": [
        {"id": 1, "role": "destination", "name": "North zone", "evidence": "to the North zone"},
        {"id": 2, "role": "origin", "name": "South zone", "evidence": "from the South zone"}]})
    marks, masks = overlay_marks(_two_zone_image(), min_px=200, close_iter=1)
    marks, notes, how = name_marks(marks, text, llm=good, image=Image.new("RGB", (10, 10)))
    assert how == "model" and good.images_seen == 1 and {m.id: m.label for m in marks} == {1: "North", 2: "South"}
    bad = ScriptedLLM(lambda stage, user: {"marks": [{"id": 1, "role": "origin", "name": "Rennes airfield", "evidence": "x"},
                                                     {"id": 7, "role": "origin", "name": "South zone", "evidence": ""}]})
    marks, masks = overlay_marks(_two_zone_image(), min_px=200, close_iter=1)
    marks, notes, how = name_marks(marks, text, llm=bad, image=Image.new("RGB", (10, 10)))
    assert how == "position words" and any("rejected" in n for n in notes)  # names not in the order, unknown mark: fallback
    assert {m.label for m in marks if m.label} == {"North", "South"}


def test_corpus_with_a_model_unites_model_and_pattern_instances(tmp_path):
    q15 = "A minimum of 15 minutes headway between successive convoys shall be kept on the same road."

    def answer(stage, user):
        if stage == "doctrine" and user.startswith("Template card:\nSCH.HEADWAY"):
            return {"instances": [{"quote": q15, "tier": "T1", "params": {"headway_min": 15}}]}
        if stage == "doctrine" and user.startswith("Template card:\nSCH.HALT"):
            return {"instances": [{"quote": "Bridges are always fine.", "tier": "T1", "params": {}}]}  # invented quote
        return {"instances": []}

    llm = ScriptedLLM(answer)
    docs, _ = read_documents([_write(tmp_path, "STANAG_9999.txt", STANDARD), _write(tmp_path, "AJP-9.9.txt", DOCTRINE)])
    base, rep = compile_corpus(docs, llm=llm)
    hw = next(r for r in base if r.template == "SCH.HEADWAY")
    assert hw.params == {"headway_min": 15} and hw.source.document == "STANAG_9999"
    ten = next(s for s in rep.sentences if s.sentence.startswith("A minimum of 10 minutes"))
    assert ten.status == "mapped"  # the model left it out; the pattern compiler still maps it (union)
    assert any("SCH.HALT: model output rejected" in x for x in rep.log)
    assert any(r.template == "SCH.HALT" and r.params.get("halt_min") == 15 for r in base)  # pattern instance kept (union)
    assert 0 < llm.usage.calls <= 28 * 4


def test_model_numbers_must_be_stated_by_the_quote():
    from coa_net.doctrine import number_supported

    q = "A halt of 15 minutes is made at least every 2 hours; at night the march speed is reduced by 30 %."
    assert number_supported(120, q) and number_supported(15, q) and number_supported(0.7, q)  # 2 h, 15 min, -30 %
    assert not number_supported(20, q) and not number_supported(90, q)


def test_model_instance_with_an_invented_number_is_dropped(tmp_path):
    q15 = "A minimum of 15 minutes headway between successive convoys shall be kept on the same road."

    def answer(stage, user):
        if stage == "doctrine" and user.startswith("Template card:\nSCH.HEADWAY"):
            return {"instances": [{"quote": q15, "tier": "T1", "params": {"headway_min": 20}}]}  # 20 is not in the quote
        return {"instances": []}

    docs, _ = read_documents([_write(tmp_path, "STANAG_9999.txt", STANDARD)])
    base, rep = compile_corpus(docs, llm=ScriptedLLM(answer))
    assert any("headway_min=20 not stated" in x for x in rep.log)
    hw = [r for r in base if r.template == "SCH.HEADWAY"]
    assert hw and all(r.params.get("headway_min") != 20 for r in hw)  # the pattern instance (15 min) stands
