import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("pypdfium2")
pytest.importorskip("reportlab")

from docrag import pipeline as P
from docrag.contract import export_for_planner
from docrag.pageimg import render_page, source_path


def make_pdf(path):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    texts = ["1. Convoys shall not exceed 40 km/h in built-up areas.",
             "2. Tracked vehicles shall keep a gap of 100 m on bridges.",
             "3. Hazardous cargo is prohibited in tunnels longer than 500 m."]
    for t in texts:   # different text on each page: a line repeated on every page would be dropped as a running header
        c.drawString(72, 750, t)
        c.showPage()
    c.save()


def test_render_page_gives_a_png_and_checks_the_page_number(tmp_path):
    pdf = tmp_path / "D1.pdf"
    make_pdf(pdf)
    assert render_page(pdf, 2)[:8] == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(ValueError):
        render_page(pdf, 9)


def test_sources_keep_the_local_path_but_the_export_does_not(tmp_path):
    pdf = tmp_path / "D1.pdf"
    make_pdf(pdf)
    work = tmp_path / "w"
    P.build([pdf], work)
    P.extract(work)
    assert source_path(work, "D1") == pdf.resolve()
    assert source_path(work, "other") is None
    rid = P.load_draft(work)[0].id
    P.save_decision(work, rid, "accept", "Molka", pdf_checked=True)
    out = json.loads(export_for_planner(work).read_text(encoding="utf-8"))
    assert out["source_documents"] and all(set(s) <= {"name", "sha256", "bytes"} for s in out["source_documents"])


def test_page_endpoint(tmp_path):
    from docrag.ui import make_handler
    pdf = tmp_path / "D1.pdf"
    make_pdf(pdf)
    work = tmp_path / "w"
    P.build([pdf], work)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(work))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        r = urllib.request.urlopen(base + "/api/page?doc=D1&page=1")
        assert r.headers["Content-Type"] == "image/png" and r.read(8) == b"\x89PNG\r\n\x1a\n"
        for bad in ("/api/page?doc=D1&page=99", "/api/page?doc=nope&page=1", "/api/page?doc=..%2F..%2Fetc%2Fpasswd&page=1",
                    "/api/page?doc=D1&page=abc"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(base + bad)
        hits = json.load(urllib.request.urlopen(base + "/api/search?q=convoys+built-up&k=2"))
        assert hits[0]["doc"] == "D1" and isinstance(hits[0]["page"], int)
    finally:
        srv.shutdown()