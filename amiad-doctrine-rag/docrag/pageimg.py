"""Picture of one PDF page, shown next to a rule or a passage so a person can compare it with the original.
Uses pypdfium2 (installed with pdfplumber). No model is involved: it is the page exactly as printed."""
from __future__ import annotations

import io
import json
from pathlib import Path

MAX_SCALE = 3.0


def source_path(work: str | Path, doc: str) -> Path | None:
    """Original PDF of a document (file name without .pdf), only if sources.json lists it."""
    p = Path(work) / "sources.json"
    if not p.exists():
        return None
    for s in json.loads(p.read_text(encoding="utf-8")):
        path = s.get("path")
        if path and Path(s["name"]).stem == doc and s["name"].lower().endswith(".pdf") and Path(path).exists():
            return Path(path)
    return None


def render_page(pdf_path: str | Path, page: int, scale: float = 1.6) -> bytes:
    """PNG bytes of a page (1 = first page)."""
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        raise ValueError("page pictures need pypdfium2: py -3 -m pip install pdfplumber") from e
    scale = max(0.5, min(MAX_SCALE, float(scale)))
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        if not 1 <= page <= len(pdf):
            raise ValueError(f"page {page} is not in this document ({len(pdf)} pages)")
        img = pdf[page - 1].render(scale=scale).to_pil()
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    finally:
        pdf.close()