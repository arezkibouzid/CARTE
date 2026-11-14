"""Document intake: PDF and text files into cited chunks (document, page, paragraph, section)."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .reliability import chunk_quality, page_flags, page_loss, pdf_figure_pages
from .text import clean, quantities, repair_words

MAX_CHUNK_CHARS = 900
LIST_MAX_CHARS = 1500    # a lead-in sentence stays with its list items up to this size
MIN_CHUNK_CHARS = 20

# Headings: "3.1 Classes", "1 Scope", "0707 Traffic Control" (no period after the number, so that
# numbered paragraphs such as "2. Road transport is limited..." are not taken for headings).
H_NUM = re.compile(r"^(?P<id>\d{1,2}(?:\.\d{1,2})*|\d{4})\s+(?P<title>[A-Z][A-Za-z0-9 ,/&()'\-]{2,70})$")
H_ANNEX = re.compile(r"^(?P<id>(?:Annex|ANNEX|Appendix|APPENDIX|Chapter|CHAPTER)\s+[A-Z0-9]+)\b[.:]?\s*(?P<title>.*)$")
# "4.1.4. Vehicular live loadings", "1.2. AGREEMENT": at least two levels, a full stop after the number. Taken for a
# heading only if the next line does not continue a sentence (lower case) and the title holds no measurement.
H_NUM_DOT = re.compile(r"^(?P<id>(?:\d{1,2}|[A-Z])(?:\.\d{1,2})+)\.\s+(?P<title>[A-Z][A-Za-z0-9 ,/&()'\-]{2,70})$")
LABEL = re.compile(r"^\s*(?P<label>\d{1,2}\.|[a-z]\.|\([a-z0-9]{1,3}\))\s+(?=\S)")
PAGE_MARK = re.compile(r"^\s*\[page\s+(\d+)\]\s*$", re.I)
PAGE_NUM = re.compile(r"^((page\s+)?(\d{1,3}|[ivxlc]{1,6})|(?:[A-Za-z]|\d{1,2})\s?-\s?\d{1,3})$", re.I)
TOC_LINE = re.compile(r"[.\u2026]{5,}\s*(?:[A-Za-z]{1,2}\s?-\s?)?(?:\d{1,3}(?:\s?-\s?\d{1,3})?|[ivxlcIVXLC]{1,6})\s*$")
# a lead-in: the paragraph announces a list ("... as follows:", "These conditions include-")
LEAD_IN = re.compile(r"(?:[:\u2013\u2014-]|\b(?:as follows|the following|includes?|including))\s*$", re.I)
ITEM_START = re.compile(r"^\s*(?:[\u2022\u00b7\u25aa*\-\u2013]\s|\([a-z0-9]{1,3}\)\s|[a-z0-9]{1,2}[.)]\s|[a-z])")
ITEM_END = re.compile(r"(?:[;,]|\b(?:and|or))\s*$", re.I)


@dataclass
class Chunk:
    n: int
    id: str
    doc: str
    page: int
    para: int
    label: str
    section: str
    section_id: str
    text: str
    quality: str = "ok"   # ok | table | noisy (see reliability.py)
    breaks: list = field(default_factory=list)   # [[offset in text, page]] where a passage joined from the next page starts

    def page_at(self, offset: int) -> int:
        """The PDF page that holds the character at `offset` (a passage joined across a page break keeps its first page in `page`)."""
        page = self.page
        for off, pg in self.breaks:
            if offset >= off:
                page = pg
        return page

    def page_slices(self) -> list[tuple[int, str, str]]:
        """[(page, text that starts on that page, that text plus everything after it in the passage)]."""
        if not self.breaks:
            return [(self.page, self.text, self.text)]
        cuts = [(0, self.page)] + [(o, g) for o, g in self.breaks]
        ends = [o for o, _ in self.breaks] + [len(self.text)]
        return [(pg, self.text[a:b], self.text[a:]) for (a, pg), b in zip(cuts, ends)]

    def cite(self) -> str:
        label = f" ({self.label})" if self.label else ""
        sec = f", {self.section}" if self.section else ""
        return f"{self.doc} p.{self.page} para {self.para}{label}{sec}"

    def to_dict(self) -> dict:
        return asdict(self)


def _heading(line: str, nxt: str | None = None):
    s = line.strip()
    m = H_NUM.match(s)
    if m and len(m.group("title").split()) <= 8 and not s.endswith((".", ",")):
        return m.group("id"), m.group("title").strip()
    m = H_NUM_DOT.match(s)
    if (m and len(m.group("title").split()) <= 8 and not s.endswith((".", ",", ";", ":"))
            and not quantities(m.group("title"))
            and not (nxt and nxt.strip()[:1].islower())):
        return m.group("id"), m.group("title").strip()
    m = H_ANNEX.match(s)
    if m and len(s) < 90:
        return m.group("id"), m.group("title").strip()
    return None


def read_pages(path: Path) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = []
        for i, p in enumerate(reader.pages, 1):
            try:
                t = p.extract_text() or ""
            except Exception as e:  # damaged page: keep going
                t = ""
                warnings.append(f"{path.name} p.{i}: could not read ({e})")
            if len(t.strip()) < 5:
                warnings.append(f"{path.name} p.{i}: no text layer (scan?). OCR is not done here.")
            pages.append(t)
        return pages, warnings
    raw = path.read_text(encoding="utf-8", errors="replace")
    if "\f" in raw:
        return raw.split("\f"), warnings
    if any(PAGE_MARK.match(l) for l in raw.splitlines()):
        by_page: dict[int, list[str]] = {}
        current = 1  # text before the first marker belongs to page 1
        for l in raw.splitlines():
            m = PAGE_MARK.match(l)
            if m:
                current = int(m.group(1))
            else:
                by_page.setdefault(current, []).append(l)
        last = max(by_page, default=1)
        return ["\n".join(by_page.get(i, [])) for i in range(1, last + 1)], warnings
    return [raw], warnings


_ROMAN = re.compile(r"^[ivxlcdm]{1,7}$")


def _footer_key(s: str) -> str:
    """The line without its page label: words made of digits, letters+digits ('H-1', 'Ed7/5PDA'), Roman numerals and
    single letters are dropped, so 'VII Edition B Version 1' and 'H-1 Edition B Version 1' give the same key."""
    words = [w for w in re.findall(r"[^\s]+", s.lower())
             if not re.search(r"\d", w) and not _ROMAN.match(w) and len(re.sub(r"\W", "", w)) > 1]
    return " ".join(words) if len(words) >= 2 else ""


# A contents page without dotted leaders: "Section 1 - Planning 3-1", "Annex A 5". Judged page by page (many such lines
# on one page), never line by line, so a sentence that happens to end with a reference is safe.
CONTENTS_REF = re.compile(r"\s(?:[A-Z]{1,2}-\d{1,3}|\d{1,2}-\d{1,3}|[ivxlc]{2,6})\s*$")


def _contents_page(lines: list[str]) -> bool:
    full = [l.strip() for l in lines if l.strip()]
    hits = [l for l in full if len(l) < 160 and CONTENTS_REF.search(l) and not quantities(l) and not l.endswith((".", ";", ":"))]
    return len(hits) >= 6 and len(hits) / max(1, len(full)) >= 0.4


def _strip_running_lines(pages: list[list[str]]) -> list[list[str]]:
    """Drop repeated headers/footers, bare page numbers, dotted contents lines."""
    n = len(pages)
    counts: Counter = Counter()
    fcounts: Counter = Counter()
    if n >= 3:
        for lines in pages:
            counts.update({re.sub(r"\d+", "#", l.strip().lower()) for l in lines if l.strip()})
    if n >= 8:
        for lines in pages:
            fcounts.update({k for k in (_footer_key(l) for l in lines if l.strip() and len(l.strip()) < 80) if k})
    out = []
    for lines in pages:
        keep = []
        contents = _contents_page(lines)
        for l in lines:
            s = l.strip()
            if not s:
                keep.append("")
                continue
            if contents and CONTENTS_REF.search(s) and not quantities(s) and len(s) < 160:
                continue
            if PAGE_NUM.match(s) or TOC_LINE.search(s) or "intentionally blank" in s.lower():
                continue
            if n >= 3 and len(s) < 80 and counts[re.sub(r"\d+", "#", s.lower())] / n >= 0.4 and not quantities(s):
                continue   # (a line holding a measurement is data, whatever its repetition)
            fk = _footer_key(s) if n >= 8 and len(s) < 80 else ""
            if fk and fcounts[fk] / n >= 0.4 and not quantities(s):   # a running footer holds no measurement
                continue
            keep.append(l)
        out.append(keep)
    return out


_SENT = r"(?<=\.)\s+(?=[A-Z(])|(?<=;)\s+(?=[A-Z(])|\s+(?=[\u2022\u00b7\u25aa]\s)|(?<=[;.:])\s+(?=[-\u2013]\s)"
_SEMI = r"(?<=;)\s+(?=[a-z])"   # a clause of a long sentence: only when the sentence itself is far too long
SENTENCE_SLACK = 1.5          # a sentence may run to 1.5 x the limit before it is cut at a ';'
# Last resort for long unpunctuated runs (tables, forms): cut where a row ends (a number) and a NEW group starts
# ("... M211 6.79 | Truck, cargo, 5 T, 6x6: M41 9.55 ..."). Never right after a colon: a group header stays with its rows.
_ROW_END = r"(?<=\d)\s+(?=[A-Z][a-z]|\*[A-Z])"


def _pack(parts: list[str], limit: int) -> list[str]:
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > limit:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        out.append(cur)
    return out


def _split_long(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Cut a long passage at sentence ends and list bullets, so a number keeps its condition. A sentence is cut at a
    ';' only when it is longer than 1.5 x the limit, and a run with no boundary is cut between table groups.
    A numeric table stays whole."""
    if len(text) <= limit or chunk_quality(text) == "table":
        return [text]
    out: list[str] = []
    for piece in _pack(re.split(_SENT, text), limit):
        if len(piece) > limit * SENTENCE_SLACK:
            for sub in _pack(re.split(_SEMI, piece), limit):
                out += _pack(re.split(_ROW_END, sub), limit) if len(sub) > limit * SENTENCE_SLACK else [sub]
        else:
            out.append(piece)
    return out


def _join(prev: str, line: str) -> str:
    if prev.endswith("-") and line[:1].islower():
        return prev[:-1] + line
    return f"{prev} {line}".strip()


def read_document(path: str | Path, start_n: int = 0, report: dict | None = None) -> tuple[list[Chunk], list[str]]:
    """report: optional dict, filled with the page flags (figure, table, noisy) of this document."""
    path = Path(path)
    doc = path.stem
    pages_raw, warnings = read_pages(path)
    pages = _strip_running_lines([t.splitlines() for t in repair_words([clean(p) for p in pages_raw])])
    chunks: list[Chunk] = []
    section, section_id = "", ""
    n = start_n
    for pno, lines in enumerate(pages, 1):
        para_no = 0
        buf, label = "", ""
        list_mode = False   # the passage holds a lead-in sentence and its list items
        pending = False     # a blank line followed a lead-in: the next line may be a list item
        item_kind = None    # "num" (1. 2.) or "alpha" (a. b. (a)) items of the list being collected

        def flush():
            nonlocal buf, label, para_no, n, list_mode, pending, item_kind
            text = re.sub(r"\s+", " ", buf).strip()
            if len(text) >= MIN_CHUNK_CHARS or quantities(text):   # a short sentence with a measurement is kept
                for piece in _split_long(text, LIST_MAX_CHARS if list_mode else MAX_CHUNK_CHARS):
                    para_no += 1
                    chunks.append(
                        Chunk(n, f"{doc}:{pno}:{para_no}", doc, pno, para_no, label, section,
                              section_id, piece, chunk_quality(piece))
                    )
                    n += 1
            buf, label, list_mode, pending, item_kind = "", "", False, False, None

        def announces_list() -> bool:
            return bool(buf) and (bool(LEAD_IN.search(buf.rstrip())) or (list_mode and bool(ITEM_END.search(buf.rstrip())))) \
                and len(buf) < LIST_MAX_CHARS

        for i, line in enumerate(lines):
            if not line.strip():
                if announces_list():
                    pending = True   # keep the lead-in with the items that follow
                else:
                    flush()
                continue
            nxt = next((x for x in lines[i + 1:] if x.strip()), None)
            h = _heading(line, nxt)
            if h:
                flush()
                section_id, title = h
                section = f"{section_id} {title}".strip()
                continue
            if pending:
                if ITEM_START.match(line) and len(buf) + len(line) <= LIST_MAX_CHARS:
                    lm = LABEL.match(line)
                    item_kind = item_kind or (("num" if lm.group("label")[:1].isdigit() else "alpha") if lm else "other")
                    buf = _join(buf, line.strip())
                    list_mode, pending = True, False
                    continue
                flush()
            m = LABEL.match(line)
            if m:
                kind = "num" if m.group("label")[:1].isdigit() else "alpha"
                # an item stays with its lead-in; later items of the same kind (a. b. c.) follow even when they end with
                # a full stop, but the next numbered paragraph (7.) after lettered items is a new paragraph
                if len(buf) + len(line) <= LIST_MAX_CHARS and (announces_list() or (list_mode and kind == item_kind)):
                    buf = _join(buf, line.strip())
                    list_mode, item_kind = True, item_kind or kind
                    continue
                flush()
                label = m.group("label")
                buf = line[m.end():].strip()
            else:
                buf = _join(buf, line.strip()) if buf else line.strip()
        flush()
    if report is not None:   # before pages are joined, so each passage still sits on its own page
        figs, scanned = pdf_figure_pages(path)
        by_page: dict[int, list[str]] = {}
        for c in chunks:
            by_page.setdefault(c.page, []).append(c.text + " " + c.label)
        loss = {}
        for pno, lines in enumerate(pages, 1):
            heads = [f"{h[0]} {h[1]}" for h in (_heading(l) for l in lines) if h]
            loss[pno] = page_loss(lines, by_page.get(pno, []), heads)
        report["flags"] = page_flags([clean(p) for p in pages_raw], figs, loss)
        report["scanned"] = scanned
    return _join_across_pages(chunks, start_n, doc), warnings


def _join_across_pages(chunks: list[Chunk], start_n: int, doc: str) -> list[Chunk]:
    """A sentence that runs over a page break was cut in two passages. Join them (the passage keeps its first page)."""
    out: list[Chunk] = []
    for c in chunks:
        prev = out[-1] if out else None
        if (prev and c.page == prev.page + 1 and c.para == 1 and not c.label and c.section_id == prev.section_id
                and ((c.text[:1].islower() and prev.text.rstrip()[-1:] not in ".:;!?)")
                     or (LEAD_IN.search(prev.text.rstrip()) and ITEM_START.match(c.text)))):
            prev.text = _join(prev.text, c.text)
            prev.breaks.append([max(0, len(prev.text) - len(c.text)), c.page])
            prev.quality = chunk_quality(prev.text)
            continue
        out.append(c)
    counter: dict[int, int] = {}
    for n, c in enumerate(out, start_n):
        counter[c.page] = counter.get(c.page, 0) + 1
        c.n, c.para = n, counter[c.page]
        c.id = f"{doc}:{c.page}:{c.para}"
    return out


def read_documents(paths: list[str | Path]) -> tuple[list[Chunk], list[str]]:
    chunks, warnings, _ = read_documents_ex(paths)
    return chunks, warnings


def read_documents_ex(paths: list[str | Path]) -> tuple[list[Chunk], list[str], dict]:
    """Like read_documents, plus {document name: {"flags": page flags, "scanned": bool}} for the reliability report."""
    files: list[Path] = []
    warnings: list[str] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.suffix.lower() in {".pdf", ".txt", ".md"})
        elif p.suffix.lower() in {".pdf", ".txt", ".md"}:
            files.append(p)
        else:
            warnings.append(f"{p.name}: format not supported (use PDF, TXT or MD)")
    chunks: list[Chunk] = []
    flags: dict = {}
    for f in files:
        rep: dict = {}
        try:
            cs, w = read_document(f, start_n=len(chunks), report=rep)
        except Exception as e:
            warnings.append(f"{f.name}: skipped ({e})")
            continue
        chunks += cs
        warnings += w
        flags[f.stem] = {"flags": rep.get("flags", []), "scanned": rep.get("scanned", False)}
    return chunks, warnings, flags