import json
from pathlib import Path

import pytest

from docrag import pipeline as P
from docrag.contract import export_for_planner, validate_export
from docrag.extract import extract_chunks
from docrag.ingest import Chunk
from docrag.reliability import (chunk_quality, document_report, load_overrides, odd_tokens, page_flags, rule_reliability)
from docrag.signoff import apply_sheet, write_sheet

SENT = "Convoys shall not exceed 40 km/h in built-up areas."


def chunk(text, doc="d", n=1, quality=None):
    return Chunk(n=n, id=f"{doc}:1:{n}", doc=doc, page=1, para=n, label="1.", section="", section_id="", text=text,
                 quality=quality or chunk_quality(text))


def test_clean_text_is_ok_and_capital_letters_are_not_noise():
    assert chunk_quality(SENT) == "ok"
    assert odd_tokens("Annex B and Annex C list vehicles A B C D E F")[0] == 0   # capital letters are legitimate


def test_ocr_noise_is_detected():
    noisy = "The ma,p shows the ro,ute and the con,voy speed in the ar,ea of the m ap"
    assert chunk_quality(noisy) == "noisy"


def test_number_dense_passage_is_a_table():
    assert chunk_quality("12 14 16 18 20 22 24 26 30 35 40 45 50 55 60 65") == "table"


def test_page_flags_find_figure_table_and_noise():
    pages = ["Figure 2-4. Route plan\ntext text", "Table 2-1 Speeds\nA   1   2\nB   3   4",
             " ".join(["ma,p"] * 10 + ["word"] * 30), "plain text " * 20]
    q = [f["quality"] for f in page_flags(pages)]
    assert q == ["figure", "table", "noisy", "ok"]


def test_document_level_and_manual_override_wins():
    flags = page_flags(["plain text " * 20] * 10)
    assert document_report("x", flags)["level"] == "high"
    noisy = page_flags([" ".join(["ma,p"] * 10 + ["word"] * 30)] * 10)
    assert document_report("x", noisy)["level"] == "low"
    r = document_report("x", flags, {"x": {"level": "low", "verify_against_pdf": True, "note": "compared by hand"}})
    assert r["level"] == "low" and r["verify_against_pdf"] and "compared by hand" in r["reasons"][0]


def test_override_file_covers_the_two_old_manuals():
    ov = load_overrides()
    assert ov["STANAG_2015"]["verify_against_pdf"] and ov["STANAG_2174_alternative"]["verify_against_pdf"]


def test_table_passage_is_not_extracted_but_logged():
    c = chunk("shall not exceed 40 20 30 40 50 60 70 80 90 100 110 120 130 140", quality="table")
    rules, skipped, _ = extract_chunks([c])
    assert not rules and "table" in skipped[0]["reason"]


def test_rule_inherits_document_reliability():
    info = {"d": {"level": "medium", "verify_against_pdf": True, "reasons": ["scanned manual"]}}
    rules, _, _ = extract_chunks([chunk(SENT)], reliability=info)
    assert rules and rules[0].reliability == "medium" and rules[0].verify_against_pdf
    low = chunk(SENT.replace("shall", "sha,ll").replace("Convoys", "Con,voys"), quality="noisy")
    assert rule_reliability("noisy", info["d"])[0] == "low"
    assert low.quality == "noisy"


def _work(tmp_path, level="medium", verify=True):
    work = tmp_path / "w"
    work.mkdir()
    rules, _, _ = extract_chunks([chunk(SENT)], reliability={"d": {"level": level, "verify_against_pdf": verify, "reasons": ["why"]}})
    from docrag.signoff import write_json
    write_json(rules, work / P.DRAFT, {})
    return work, rules


def test_sheet_refuses_accept_without_pdf_check(tmp_path):
    work, rules = _work(tmp_path)
    sheet = tmp_path / "s.csv"
    write_sheet(rules, sheet)
    text = sheet.read_text(encoding="utf-8-sig")
    assert "REQUIRED" in text
    import csv
    rows = list(csv.DictReader(open(sheet, encoding="utf-8-sig")))
    rows[0]["decision"] = "accept"
    with open(sheet, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    approved, msgs = apply_sheet(rules, sheet)
    assert not approved and "pdf_checked" in msgs[0]
    rows[0]["pdf_checked"] = "yes"
    with open(sheet, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    approved, _ = apply_sheet(rules, sheet)
    assert len(approved) == 1


def test_ui_decision_and_export_enforce_pdf_check(tmp_path):
    work, rules = _work(tmp_path)
    rid = rules[0].id
    with pytest.raises(ValueError):
        P.save_decision(work, rid, "accept", "Molka")
    P.save_decision(work, rid, "accept", "Molka", pdf_checked=True)
    (work / "sources.json").write_text("[]")
    dest = export_for_planner(work)
    out = json.loads(dest.read_text())["rules"][0]
    assert out["reliability"] == "medium" and out["verify_required"] and out["verified_against_pdf"]
    assert validate_export(dest) == []
    # a file where the check was skipped must fail validation
    body = json.loads(dest.read_text())
    body["rules"][0]["verified_against_pdf"] = False
    import hashlib
    body["rules_sha256"] = hashlib.sha256(json.dumps(body["rules"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    dest.write_text(json.dumps(body))
    assert any("not compared with the PDF" in e for e in validate_export(dest))


def test_build_writes_reading_report(tmp_path):
    doc = tmp_path / "STANAG_2015.txt"
    doc.write_text("1 Scope\n\n1. Convoys shall not exceed 40 km/h in built-up areas.\n\nFigure 1-1 Route plan\n", encoding="utf-8")
    ov = tmp_path / "ov.json"
    ov.write_text(json.dumps({"STANAG_2015": {"level": "low", "verify_against_pdf": True, "note": "test"}}))
    P.build([doc], tmp_path / "w", overrides=ov)
    info = P.load_reading(tmp_path / "w")["STANAG_2015"]
    assert info["level"] == "low" and info["verify_against_pdf"] and info["figure_pages"] == [1]
    P.extract(tmp_path / "w")
    assert all(r.verify_against_pdf for r in P.load_draft(tmp_path / "w"))


def test_scanned_manual_pictures_are_not_figures(tmp_path):
    """Every page of a scan has a big picture behind the text: they must not all be reported as figures."""
    import io
    pytest.importorskip("reportlab")   # a test tool, not needed to run docrag
    pytest.importorskip("PIL")
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image
    from docrag.reliability import pdf_figure_pages
    img = ImageReader(Image.new("RGB", (400, 500), "white"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for _ in range(5):
        c.drawImage(img, 0, 0, 400, 500)
        c.drawString(50, 50, "text behind a scan")
        c.showPage()
    c.save()
    f = tmp_path / "scan.pdf"
    f.write_bytes(buf.getvalue())
    figs, scanned = pdf_figure_pages(f)
    assert scanned and not figs


def test_reliability_page_command_and_csv(tmp_path, capsys):
    from docrag.cli import main
    doc = tmp_path / "D1.txt"
    doc.write_text("1 Scope\n\n1. Convoys shall not exceed 40 km/h in built-up areas.\n\nFigure 1-1 Route plan\n", encoding="utf-8")
    P.build([doc], tmp_path / "w")
    assert (tmp_path / "w" / "reading_pages.csv").exists()
    main(["reliability", "-w", str(tmp_path / "w"), "--doc", "D1", "--pages", "1", "9"])
    out = capsys.readouterr().out
    assert "D1 p.1: figure" in out and "not in this document" in out


def test_captions_are_told_from_references_in_the_text():
    from docrag.reliability import FIG_CAPTION, TABLE_CAPTION
    for t in ["Figure 2-4. Route plan", "FIGURE 2-43. TRAILER", "Fig. 3 Speeds", "Figure 2-b8. Layout", "Figure B-1. Plan"]:
        assert FIG_CAPTION.match(t), t
    for t in ["Figure 2-4 shows the route", "figure is shown below", "Figure 3 is", "Figure 2-4, the route"]:
        assert not FIG_CAPTION.match(t), t
    assert TABLE_CAPTION.match("Table 2-4. Speeds") and TABLE_CAPTION.match("TABLE 2-1. MLC")
    assert not TABLE_CAPTION.match("Table of contents") and not TABLE_CAPTION.match("Table 2-4 shows the")


def test_page_that_loses_short_cells_is_lossy_and_its_rules_are_low(tmp_path):
    """A table of short cells separated by blank lines is dropped by the chunker: no noise, but text is lost."""
    cells = "\n\n".join(f"Axle {i}" for i in range(60))
    doc = tmp_path / "T1.txt"
    doc.write_text("1 Scope\n\n1. Convoys shall not exceed 40 km/h in built-up areas.\n\n" + cells + "\n\f"
                   "2. Convoys shall not exceed 50 km/h on open roads.\n", encoding="utf-8")
    P.build([doc], tmp_path / "w")
    info = P.load_reading(tmp_path / "w")["T1"]
    flags = {p: q for p, q, *_ in info["page_flags"]}
    assert flags[1] == "lossy" and flags[2] == "ok"
    P.extract(tmp_path / "w")
    by_page = {r.page: r for r in P.load_draft(tmp_path / "w")}
    assert by_page[1].reliability == "low" and by_page[1].verify_against_pdf
    # the clean page is not forced to a PDF check (the document is only medium because half of its pages are lossy)
    assert by_page[2].reliability != "low" and not by_page[2].verify_against_pdf


def test_hand_marked_pages_graphic_skipped_and_mixed_low(tmp_path):
    pages = ["1. Convoys shall not exceed 40 km/h in built-up areas.",
             "2. Convoys shall not exceed 50 km/h on open roads.",
             "3. Convoys shall not exceed 30 km/h on bridges."]
    doc = tmp_path / "S1.txt"
    doc.write_text("\f".join(pages), encoding="utf-8")
    ov = tmp_path / "ov.json"
    ov.write_text(json.dumps({"S1": {"level": "medium", "graphic_pages": [[1, 1]], "mixed_pages": [[2, 3]], "note": "by hand"}}))
    P.build([doc], tmp_path / "w", overrides=ov)
    info = P.load_reading(tmp_path / "w")["S1"]
    assert info["graphic_pages"] == [1] and info["mixed_pages"] == [2, 3]
    P.extract(tmp_path / "w")
    rules = {r.page: r for r in P.load_draft(tmp_path / "w")}
    assert 1 not in rules                                  # graphic page: nothing extracted
    assert rules[2].reliability == "low" and rules[2].verify_against_pdf
    skipped = json.loads((tmp_path / "w" / P.SKIPPED).read_text())
    assert any("graphic page" in x["reason"] for x in skipped)