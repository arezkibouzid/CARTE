"""Builds the jury report: inlines the measured charts of the deck (docs/presentation/charts, drawn by build_deck.py
from the experiment runs) into report.src.html and prints CARTE_technical_report.pdf (A4) with Chromium.

    .venv/bin/python docs/presentation/build_deck.py        # charts first (once)
    LD_LIBRARY_PATH=... .venv/bin/python docs/jury_report/build_report.py
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHARTS = HERE.parent / "presentation" / "charts"


def main():
    src = (HERE / "report.src.html").read_text(encoding="utf-8")

    def chart(m):
        p = CHARTS / f"{m.group(1)}.svg"
        if not p.exists():
            raise SystemExit(f"missing chart {p}: run docs/presentation/build_deck.py first")
        svg = p.read_text(encoding="utf-8")
        return svg[svg.index("<svg"):]

    html = re.sub(r"<!--chart:(\w+)-->", chart, src)
    out = HERE / "report.html"
    out.write_text(html, encoding="utf-8")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        pg.goto(out.as_uri())
        pg.wait_for_timeout(500)
        pg.pdf(path=str(HERE / "CARTE_technical_report.pdf"), format="A4", print_background=True, prefer_css_page_size=True)
        n = pg.evaluate("Math.ceil(document.body.scrollHeight / 1050)")
        b.close()
    print(f"written {HERE / 'CARTE_technical_report.pdf'} (about {n} pages)")


if __name__ == "__main__":
    main()
