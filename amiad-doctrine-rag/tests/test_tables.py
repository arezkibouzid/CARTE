import json

import pytest

pytest.importorskip("pdfplumber")
reportlab = pytest.importorskip("reportlab")

from docrag import pipeline as P
from docrag.tables import clean_rows, extract_tables, table_passage_text, write_tables


def make_pdf(path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet
    data = [["MLC", "4", "8", "12"], ["10", "7.99", "14.39", "22.33"], ["20", "9.01", "17.18", "27.84"], ["50", "9.62", "18.88", "31.16"]]
    t = Table(data)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    st = getSampleStyleSheet()
    SimpleDocTemplate(str(path), pagesize=A4).build(
        [Paragraph("Table 1: UNIT BENDING MOMENTS (kNm/m)", st["Normal"]), t,
         Paragraph("Convoys shall not exceed 40 km/h in built-up areas.", st["Normal"])])


def test_rotated_cell_and_empty_columns_are_cleaned():
    rotated = "\n".join(")S R E T E M ( H T G N E L".split())   # text turned 90 degrees: one letter per line, bottom to top
    rows = clean_rows([[rotated, None, "5.57"], [None, None, "6.01"]])
    assert rows[0][0] == "LENGTH(METERS)"
    assert all(len(r) == 2 for r in rows)   # the empty column is dropped


def test_table_is_extracted_with_values_and_checks(tmp_path):
    pdf = tmp_path / "AEPX.pdf"
    make_pdf(pdf)
    ts = extract_tables(pdf)
    assert len(ts) == 1
    t = ts[0]
    assert t["rows"][1][2] == "14.39" and t["n_rows"] == 4 and t["n_cols"] == 4
    assert "UNIT BENDING MOMENTS" in t["caption"]
    assert t["checks"]["numbers_found_in_page_text"] == 1.0
    write_tables(ts, tmp_path / "out")
    assert (tmp_path / "out" / "AEPX_p1_t1.csv").read_text(encoding="utf-8-sig").splitlines()[1] == "10,7.99,14.39,22.33"
    assert "Full table in tables/AEPX_p1_t1.csv" in table_passage_text(ts[0])


def test_index_gets_a_passage_per_table_and_keeps_rules_out_of_tables(tmp_path):
    pdf = tmp_path / "AEPX.pdf"
    make_pdf(pdf)
    P.build([pdf], tmp_path / "w")
    assert (tmp_path / "w" / "tables" / "tables.json").exists()
    from docrag.retrieve import Index
    idx = Index.load(tmp_path / "w")
    hits = idx.search("unit bending moments table", k=3)
    assert any(h.chunk.label == "TABLE" for h in hits)
    assert P.load_reading(tmp_path / "w")["AEPX"]["tables_extracted"] == 1
    P.extract(tmp_path / "w")
    rules = P.load_draft(tmp_path / "w")
    assert all(r.template and r.quote for r in rules) and not any(r.para >= 900 for r in rules)   # no rule from the table passage