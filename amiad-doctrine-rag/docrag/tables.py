"""Tables of a PDF with a text layer: one CSV per table, one JSON for all, plus a short searchable passage per table.

Tables are DATA, not rules: a row such as "MLC 50, span 10 m, 63.46 kNm/m" is looked up, it is not a "shall". So tables are
not pushed through the rule extractor. They are written as files the planner can read, and found by search through a
passage that names the table, its columns and the file.
Needs pdfplumber (pip install pdfplumber). Scanned manuals have no ruling lines to follow: they are skipped.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

NUM = re.compile(r"^[-+]?\d[\d.,]*%?$")


class TablesUnavailable(Exception):
    pass


def _cell(v) -> str:
    if v is None:
        return ""
    parts = str(v).split("\n")
    if len(parts) >= 5 and all(len(p.strip()) <= 2 for p in parts):   # text turned 90 degrees comes out one letter per line
        return "".join(p.strip() for p in parts)[::-1]
    return re.sub(r"\s+", " ", str(v)).strip()


def clean_rows(rows) -> list[list[str]]:
    rows = [[_cell(c) for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    keep = [j for j in range(max(len(r) for r in rows)) if any(j < len(r) and r[j] for r in rows)]
    return [[r[j] if j < len(r) else "" for j in keep] for r in rows]


def _caption(page, bbox, rows) -> str:
    x0, top, x1, _ = bbox
    try:
        above = page.crop((0, max(0, top - 45), page.width, top)).extract_text() or ""
    except Exception:
        above = ""
    lines = [l.strip() for l in above.splitlines() if l.strip()]
    for l in reversed(lines):
        if re.match(r"(?i)^(table|figure)\b", l):
            return l[:160]
    if rows and sum(bool(c) for c in rows[0]) == 1:   # a title row spanning the table
        return next(c for c in rows[0] if c)[:160]
    return ""


def _checks(rows, page_text: str) -> dict:
    cells = [c for r in rows for c in r if c]
    nums = [c for c in cells if NUM.match(c)]
    page_nums = set(re.findall(r"\d[\d.,]*", page_text))
    return {"cells": len(cells), "numeric_share": round(len(nums) / max(1, len(cells)), 2),
            "numbers_found_in_page_text": round(sum(n.rstrip("%") in page_nums for n in nums) / max(1, len(nums)), 3)}


def extract_tables(path: str | Path, pages: list[int] | None = None) -> list[dict]:
    try:
        import pdfplumber
    except ImportError as e:
        raise TablesUnavailable("pdfplumber is not installed: py -3 -m pip install pdfplumber") from e
    path = Path(path)
    out: list[dict] = []
    with pdfplumber.open(str(path)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            if pages and pno not in pages:
                continue
            try:
                found = page.find_tables()
                text = page.extract_text() or ""
            except Exception:
                continue
            for k, t in enumerate(found, 1):
                rows = clean_rows(t.extract())
                if len(rows) < 2 or len(rows[0]) < 2:
                    continue
                out.append({"document": path.stem, "page": pno, "index": k, "caption": _caption(page, t.bbox, rows),
                            "n_rows": len(rows), "n_cols": len(rows[0]), "checks": _checks(rows, text), "rows": rows})
    return out


def write_tables(tables: list[dict], folder: str | Path) -> list[dict]:
    """Write one CSV per table and tables.json. Returns the tables with their csv file name."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for t in tables:
        name = f"{t['document']}_p{t['page']}_t{t['index']}.csv"
        t["csv"] = name
        with open(folder / name, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerows(t["rows"])
    (folder / "tables.json").write_text(json.dumps(tables, ensure_ascii=False, indent=1), encoding="utf-8")
    return tables


def table_passage_text(t: dict) -> str:
    head = []
    for r in t["rows"][:2]:
        for c in r:
            if c and c not in head:
                head.append(c)
    cols = ", ".join(head)[:300]
    cap = t["caption"] or "untitled table"
    return (f"Table on page {t['page']}: {cap}. {t['n_rows']} rows, {t['n_cols']} columns. Header: {cols}. "
            f"Full table in tables/{t.get('csv', '')}.")