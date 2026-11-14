"""Reading reliability: how far the text we read from a document can be trusted.

Three levels (high, medium, low) at three places: a passage, a page, a document.
  * automatic signals: damaged text (OCR noise), passages made mostly of numbers (flattened tables), figures
  * manual evidence: reliability_overrides.json, where a person records what they saw comparing pages with the PDF
A manual entry always wins over the automatic level, and its note is kept, so the choice can be argued in the report.
The thresholds below are PROVISIONAL: calibrate them on pages a person has compared by hand.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

LEVELS = ("high", "medium", "low", "unknown")
WORD = re.compile(r"[a-z0-9]+")
DEFAULT_OVERRIDES = Path(__file__).resolve().parent.parent / "reliability_overrides.json"

# provisional thresholds
PAGE_NOISE_RATIO = 0.03       # odd tokens / tokens on a page
CHUNK_NOISE_RATIO = 0.05
CHUNK_NOISE_MIN = 2
NUMERIC_DENSE = 0.45          # share of number-only tokens that makes a passage look like a table
TABLE_LINES_PER_PAGE = 4
DOC_NOISY_LOW = 0.15          # share of noisy pages: document is low
DOC_NOISY_MEDIUM = 0.03
DOC_TABLE_MEDIUM = 0.15
LOSSY_RATIO = 0.05            # share of the words of a page that did not reach any passage
LOSSY_MIN_WORDS = 20
DOC_LOSSY_MEDIUM = 0.10

# A caption starts a line with "Figure 2-4" (the identifier holds a digit) and is followed by a full stop, a colon,
# a dash, the end of the line or a capitalised title. "Figure 2-4 shows the route..." is a reference in the text, not a caption.
_ID = r"(?=[\w.-]*\d)[A-Z]?[\dA-Za-z]{1,3}(?:[-.][\dA-Za-z]{1,3})*"
_AFTER = r"(?:[.:](?:\s|$)|\s*[\u2013\u2014]|\s+-\s|\s*$|\s+[A-Z][A-Za-z])"
FIG_CAPTION = re.compile(r"^\s*(?i:figure|fig\.)\s+" + _ID + _AFTER)
TABLE_CAPTION = re.compile(r"^\s*(?i:table)\s+" + _ID + _AFTER)
NUM_TOKEN = re.compile(r"^[\d.,/()%+-]+$")
INTRA_PUNCT = re.compile(r"\b[A-Za-z]{2,}[,;][A-Za-z]+\b")   # 'ma,p' : a letter replaced by punctuation


def odd_tokens(text: str) -> tuple[int, int]:
    """(number of odd tokens, number of tokens). Odd = replacement character, a comma inside a word, a stray lower-case letter."""
    toks = text.split()
    odd = text.count("�") + len(INTRA_PUNCT.findall(text))
    # capitals are legitimate (Annex B, A-Z lists); so is a unit letter after a number ("2.75 m", "9 t", "30 s")
    odd += sum(1 for i, t in enumerate(toks)
               if len(t) == 1 and t.islower() and t not in "ax" and not (i and re.search(r"\d$", toks[i - 1])))
    return odd, len(toks)


def looks_damaged(text: str, min_odd: int = CHUNK_NOISE_MIN, ratio: float = CHUNK_NOISE_RATIO) -> bool:
    odd, n = odd_tokens(text)
    letters = [c for c in text if not c.isspace()]
    strange = len(letters) >= 20 and sum(not (c.isalnum() or c in ".,;:()-/%'\"&+") for c in letters) / len(letters) > 0.25
    return strange or (odd >= min_odd and n > 0 and odd / n >= ratio)


def numeric_dense(text: str) -> bool:
    toks = text.split()
    return len(toks) >= 8 and sum(bool(NUM_TOKEN.match(t)) for t in toks) / len(toks) >= NUMERIC_DENSE


def chunk_quality(text: str) -> str:
    """ok | table | noisy, for one passage."""
    if looks_damaged(text):
        return "noisy"
    if numeric_dense(text):
        return "table"
    return "ok"


def table_like_line(line: str) -> bool:
    s = line.strip()
    return len(re.findall(r"\s{3,}|\t", s)) >= 2 or (len(re.findall(r"\b\d+(?:[.,]\d+)?\b", s)) >= 5 and len(s) < 120)


def pdf_figure_pages(path: Path, min_side: int = 120) -> tuple[set[int], bool]:
    """(pages that hold a real picture, document is a scan).
    A scanned manual has one big picture behind the text of every page: when most pages have one, the pictures are
    the scan itself, not figures, so they are ignored. Vector drawings are caught by their caption instead."""
    if Path(path).suffix.lower() != ".pdf":
        return set(), False
    try:
        from pypdf import PdfReader
        out = set()
        reader = PdfReader(str(path))
        for i, p in enumerate(reader.pages, 1):
            try:
                xo = (p.get("/Resources") or {}).get("/XObject") or {}
                for ref in xo.values():
                    o = ref.get_object()
                    if o.get("/Subtype") == "/Image" and min(int(o.get("/Width", 0)), int(o.get("/Height", 0))) >= min_side:
                        out.add(i)
                        break
            except Exception:
                continue
        if len(out) > 0.6 * len(reader.pages):
            return set(), True
        return out, False
    except Exception:
        return set(), False


def page_words(lines: list[str]) -> list[str]:
    return WORD.findall(re.sub(r"-\s*\n\s*(?=[a-z])", "", "\n".join(lines)).lower())


def page_loss(kept_lines: list[str], passage_texts: list[str], heading_lines: list[str]) -> tuple[int, int]:
    """(words of the page that reached no passage, words of the page). Short table cells and symbols are lost this way."""
    from collections import Counter
    kept = Counter(page_words(kept_lines))
    have = Counter()
    for t in passage_texts + heading_lines:
        have.update(WORD.findall(t.lower()))
    return sum((kept - have).values()), sum(kept.values())


def page_flags(pages_text: list[str], figure_pages: set[int] | None = None, loss: dict | None = None) -> list[dict]:
    """One dict per page: figure / table / noisy / lossy flags and the worst quality (noisy > lossy > table > figure > ok).
    loss: {page: (words lost, words)} from page_loss."""
    figure_pages = figure_pages or set()
    loss = loss or {}
    out = []
    for i, t in enumerate(pages_text, 1):
        lines = [l for l in t.splitlines() if l.strip()]
        odd, n = odd_tokens(t)
        fig = i in figure_pages or any(FIG_CAPTION.match(l) for l in lines)
        tab = sum(table_like_line(l) for l in lines) >= TABLE_LINES_PER_PAGE or any(TABLE_CAPTION.match(l) for l in lines)
        noisy = n >= 30 and odd / n >= PAGE_NOISE_RATIO
        lost, total = loss.get(i, (0, 0))
        lossy = total >= LOSSY_MIN_WORDS and lost / total > LOSSY_RATIO
        q = "noisy" if noisy else "lossy" if lossy else "table" if tab else "figure" if fig else "ok"
        out.append({"page": i, "figure": fig, "table": tab, "noisy": noisy, "lossy": lossy, "quality": q,
                    "odd_tokens": odd, "lost_pct": round(100 * lost / total, 1) if total else 0.0})
    return out


def load_overrides(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_OVERRIDES
    if not p.exists():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in d.items() if isinstance(v, dict) and not k.startswith("_")}


def _in_ranges(page: int, ranges) -> bool:
    return any(a <= page <= b for a, b in (ranges or []))


def document_report(doc: str, flags: list[dict], overrides: dict | None = None, scanned: bool = False) -> dict:
    manual = (overrides or {}).get(doc) or {}
    # pages a person looked at: graphic = symbols and figures, no rules expected; mixed = signs and text together
    for f in flags:
        if _in_ranges(f["page"], manual.get("graphic_pages")):
            f["quality"] = "graphic"
        elif _in_ranges(f["page"], manual.get("mixed_pages")):
            f["quality"] = "mixed"
    graphic = [f["page"] for f in flags if f["quality"] == "graphic"]
    mixed = [f["page"] for f in flags if f["quality"] == "mixed"]
    n = max(1, len(flags))
    noisy = [f["page"] for f in flags if f["noisy"]]
    tables = [f["page"] for f in flags if f["table"]]
    lossy = [f["page"] for f in flags if f.get("lossy")]
    figures = [f["page"] for f in flags if f["figure"]]
    reasons = []
    if graphic:
        reasons.append(f"{len(graphic)} pages are symbol tables or figures (marked by hand): no rule is extracted from them")
    if mixed:
        reasons.append(f"{len(mixed)} pages mix road signs and text (marked by hand): rules from them are low reliability")
    level = "high"
    if len(noisy) / n >= DOC_NOISY_LOW:
        level = "low"
    elif len(noisy) / n >= DOC_NOISY_MEDIUM or len(tables) / n >= DOC_TABLE_MEDIUM or len(lossy) / n >= DOC_LOSSY_MEDIUM:
        level = "medium"
    if scanned:
        reasons.append("pages are scans read through an OCR text layer")
    if noisy:
        reasons.append(f"{len(noisy)} of {len(flags)} pages show OCR-like noise")
    if lossy:
        reasons.append(f"{len(lossy)} pages lose more than {int(LOSSY_RATIO * 100)}% of their words when cut into passages (short table cells, symbols)")
    if tables:
        reasons.append(f"{len(tables)} pages hold tables (flattened into text, not extracted automatically)")
    if figures:
        reasons.append(f"{len(figures)} pages hold figures (not read)")
    manual = manual or None
    verify = level == "low"
    auto_level = level
    if manual:
        level = manual.get("level", level)
        verify = bool(manual.get("verify_against_pdf", verify))
        reasons.insert(0, "manual assessment: " + manual.get("note", "no note"))
    return {"document": doc, "pages": len(flags), "level": level, "automatic_level": auto_level,
            "verify_against_pdf": verify, "reasons": reasons, "noisy_pages": noisy, "lossy_pages": lossy, "graphic_pages": graphic, "mixed_pages": mixed, "table_pages": tables,
            "figure_pages": figures, "scanned": scanned, "manual": manual,
            "page_flags": [[f["page"], f["quality"], f["odd_tokens"], f.get("lost_pct", 0.0)] for f in flags]}


def rule_reliability(chunk_quality_: str, doc_info: dict | None, page_quality: str = "ok") -> tuple[str, str, bool]:
    """(level, note, must be checked against the PDF) for a rule drawn from one passage on one page."""
    if chunk_quality_ == "noisy":
        return "low", "this passage looks damaged (OCR noise)", True
    if chunk_quality_ == "table":
        return "low", "this passage looks like a flattened table", True
    if page_quality in ("noisy", "lossy", "mixed"):
        why = {"noisy": "shows OCR-like noise", "lossy": "loses text when read (short table cells, symbols)",
               "mixed": "mixes road signs and text"}[page_quality]
        return "low", f"this page {why}", True
    if not doc_info:
        return "unknown", "no reading report for this document", False
    note = "; ".join(doc_info.get("reasons", [])[:2])
    level, verify = doc_info["level"], bool(doc_info.get("verify_against_pdf"))
    if page_quality == "table":   # a table on the page: its numbers may be shifted
        level = "medium" if level == "high" else level
        verify = True
        note = "this page holds a table, numbers may be misplaced" + ("; " + note if note else "")
    return level, note, verify


def best_level(levels: list[str]) -> str:
    return min(levels, key=LEVELS.index)