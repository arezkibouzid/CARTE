"""Document intake for doctrine sets and order documents (S1 step 1, S2 step 1).

The organisers hand out doctrine at the event as a set of textual documents
(STANAG and other NATO documents released to the public, rules art. 10.4),
and orders as multimodal documents like ``exercise_for_preparation.pdf``: a
statement with constraints and desired outputs next to an illustration drawn
on the map. Both go through this reader, which keeps provenance:

* text is cut into blocks (heading, paragraph, list item, table row, caption),
  each with its document, page, paragraph number, section, list level and
  parent block, so that every requirement and every order item can be cited;
* page headers and footers that repeat (page numbers, classification lines)
  are dropped, line-end hyphenation is undone, ligatures, quotes, dashes and
  spaces are normalised, and a paragraph cut by a page break is joined again,
  so that a quote copied by a model can be found again in the source;
* figures (embedded images) are listed with their page position, their
  caption, whether vector drawings lie over them, and their pixels; the shown
  region can be rendered with those drawings (for the sketch reader, L9).

Formats: PDF with a text layer (pdfplumber; pypdf without positions as a
fallback), Word ``.docx``, plain text and Markdown, and images. A PDF page or
an image without text is read by OCR when RapidOCR is installed (flagged
``ocr``). Two-column layouts are not handled.
"""

from __future__ import annotations

import hashlib
import io
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

TEXT_EXT = {".txt", ".md", ".markdown", ".text"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
SUPPORTED = {".pdf", ".docx"} | TEXT_EXT | IMAGE_EXT

# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

_TR = str.maketrans({" ": " ", " ": " ", " ": " ", " ": " ", "​": "", "­": "",
                     "‘": "'", "’": "'", "ʼ": "'", "“": '"', "”": '"', "„": '"',
                     "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
                     "…": "...", "": "•", "": "•"})


def clean(s: str) -> str:
    """NFKC (ligatures), one kind of quote, dash and space, single spaces."""
    return " ".join(unicodedata.normalize("NFKC", s).translate(_TR).split())


def match_key(s: str) -> str:
    """Key for finding a quote in a source: cleaned, case-folded, no space before closing punctuation."""
    s = clean(s).casefold()
    s = re.sub(r"\s+([,.;:!?)\]»])", r"\1", s)
    return re.sub(r"([(\[«])\s+", r"\1", s)


EN_WORDS = {"the", "and", "of", "to", "shall", "should", "must", "is", "are", "be", "with", "for", "on", "by", "which"}
FR_WORDS = {"le", "la", "les", "des", "et", "du", "doit", "doivent", "est", "sont", "avec", "pour", "une", "dans", "par"}


def guess_language(text: str) -> str:
    w = re.findall(r"[a-zà-ÿ]+", text.lower())
    en, fr = sum(x in EN_WORDS for x in w), sum(x in FR_WORDS for x in w)
    if en + fr < 5:
        return "?"
    return "en" if en >= 1.3 * fr else "fr" if fr >= 1.3 * en else "mixed"


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

@dataclass
class Block:
    id: str
    doc: str
    page: int  # 1-based; 0 when the format has no pages
    kind: str  # heading | paragraph | item | table_row | caption
    text: str
    number: str = ""  # paragraph or item number as printed, without the final dot ("0304", "3.2", "a", "1")
    bullet: str = ""  # list mark ("-", "•") when the item is not numbered
    section: str = ""  # nearest heading above
    level: int = 0  # list depth on the page (0 = outermost)
    parent: str = ""  # id of the block that introduces this item
    bbox: tuple[float, float, float, float] | None = None  # x0, top, x1, bottom (PDF points)
    last_page: int = 0  # page where the block ends, when a page break cuts it
    ocr: bool = False

    def cite(self, title: str = "") -> str:
        where = [title or self.doc]
        if self.page:
            where.append(f"p. {self.page}" + (f"-{self.last_page}" if self.last_page and self.last_page != self.page else ""))
        if self.number:
            where.append(f"para {self.number}")
        return ", ".join(where)


@dataclass
class Figure:
    id: str
    doc: str
    page: int
    bbox: tuple[float, float, float, float] | None
    size_px: tuple[int, int] | None
    caption: str = ""
    caption_block: str = ""
    near_blocks: list[str] = field(default_factory=list)  # blocks on the same page, nearest first
    overlays: int = 0  # vector drawings (lines, curves, rectangles) over the image
    rotated: bool = False  # shown turned by 90 degrees (portrait pixels in a landscape box, or the reverse)
    name: str = ""
    data: bytes | None = field(default=None, repr=False)  # encoded image (JPEG, PNG...) when extractable
    ext: str = ".png"

    @property
    def shown_area(self) -> float:
        if self.bbox is None:
            return float(self.size_px[0] * self.size_px[1]) if self.size_px else 0.0
        x0, t, x1, b = self.bbox
        return max(0.0, x1 - x0) * max(0.0, b - t)


@dataclass
class Document:
    id: str
    path: str
    format: str
    sha1: str
    pages: int
    title: str
    language: str
    blocks: list[Block]
    figures: list[Figure]
    warnings: list[str] = field(default_factory=list)
    page_size: list[tuple[float, float]] = field(default_factory=list)
    _key: str | None = field(default=None, repr=False)

    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)

    def block(self, bid: str) -> Block | None:
        return next((b for b in self.blocks if b.id == bid), None)

    def children(self, bid: str) -> list[Block]:
        return [b for b in self.blocks if b.parent == bid]

    def locate(self, quote: str) -> Block | None:
        """The block that holds a quote (normalised match), or None. A quote across two blocks gives the first."""
        q = match_key(quote)
        if not q:
            return None
        for b in self.blocks:
            if q in match_key(b.text):
                return b
        for a, b in zip(self.blocks, self.blocks[1:]):
            if q in match_key(a.text + " " + b.text):
                return a
        return None

    def contains(self, quote: str) -> bool:
        if self._key is None:
            self._key = match_key(" ".join(b.text for b in self.blocks))
        return bool(quote.strip()) and match_key(quote) in self._key

    def sentences(self, kinds: Iterable[str] = ("paragraph", "item", "table_row")) -> list[tuple[Block, str]]:
        out = []
        for b in self.blocks:
            if b.kind in kinds:
                out += [(b, s) for s in split_text_sentences(b.text)]
        return out

    def summary(self) -> dict:
        kinds = Counter(b.kind for b in self.blocks)
        return {"id": self.id, "path": self.path, "format": self.format, "sha1": self.sha1, "pages": self.pages,
                "title": self.title, "language": self.language, "blocks": dict(kinds), "figures": len(self.figures),
                "ocr_blocks": sum(b.ocr for b in self.blocks), "warnings": self.warnings}


_ABBREV = r"(?<!\be\.g)(?<!\bi\.e)(?<!\bcf)(?<!\bpara)(?<!\bNo)(?<!\bfig)(?<!\bp)(?<!\bart)(?<!\bvol)(?<!\bed)"


def split_text_sentences(text: str) -> list[str]:
    parts = re.split(_ABBREV + r"(?<=[.;!?])\s+(?=[A-ZÀ-Ý\"'(«])", text)
    return [p.strip() for p in parts if len(p.strip()) > 1]


# ---------------------------------------------------------------------------
# lines -> blocks (shared by PDF, OCR and text formats)
# ---------------------------------------------------------------------------

@dataclass
class _Line:
    text: str
    x0: float = 0.0
    top: float = 0.0
    x1: float = 0.0
    bottom: float = 0.0
    size: float = 0.0
    bold: bool = False
    ocr: bool = False


_NUM_MARK = r"(?:\d{1,4}\.(?:\d{1,3}\.)*\d{0,3}|\d{1,3}\)|[A-Za-z]\.|[a-z]\)|\((?:[a-z]|[ivxIVX]{1,4}|\d{1,2})\))"
_MARK = re.compile(r"^\s*(?P<m>" + _NUM_MARK + r"|[•\-*▪◦●·➢►■□✓])\s+(?=\S)(?!(?:m|km|t|kg|h|min|mm|cm|%|°)\b)")
_HEAD_WORDS = re.compile(r"^(chapter|chapitre|section|annex|annexe|appendix|article|part|partie|titre|lexique|glossary|"
                         r"preface|préface|introduction|references?|références)\b", re.I)
_CAPTION = re.compile(r"^(figure|fig\.|table|tableau|illustration|carte|croquis|map|sketch|schéma|graphique)\s*\d*\s*[:.\-]?\s*", re.I)
_LABEL = re.compile(r"^[A-Z][\w/'()-]*(?: [\w/'()-]+){0,3}\s?:\s")  # "Convoy C1: ...", "Phase: day", "Intent: ..." start a block
_PAGE_NO = re.compile(r"^(?:page\s*)?(?:[#]+|[ivxlc]+)(?:\s*(?:/|of|sur|de)\s*[#]+)?$|^-\s*[#]+\s*-$|^[#]+-[#]+$", re.I)


def _mark(text: str) -> tuple[str, str, str]:
    """(number, bullet, rest) of a line that starts a numbered paragraph or a list item."""
    m = _MARK.match(text)
    if not m:
        return "", "", text
    tok, rest = m.group("m"), text[m.end():]
    if tok[0].isdigit() or tok.startswith("(") or (len(tok) == 2 and tok[1] in ".)"):
        if re.fullmatch(r"[A-Z]\.", tok) and re.match(r"[A-Z][a-z]", rest) and len(rest.split()) < 3:
            return "", "", text  # initial of a name ("J. Smith")
        return tok.strip("().").rstrip("."), "", rest
    return "", tok, rest


def _is_heading(text: str, size: float, body: float, bold: bool) -> bool:
    words = text.split()
    if not words or len(words) > 14 or text.endswith((".", ";", ",")):
        return bool(_HEAD_WORDS.match(text)) and len(words) <= 14 and not text.endswith((";", ","))
    letters = [c for c in text if c.isalpha()]
    caps = len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.8
    return bold or caps or (body > 0 and size >= body + 1.5) or bool(_HEAD_WORDS.match(text))


def _join(a: str, b: str) -> str:
    if re.search(r"[A-Za-zÀ-ÿ]-$", a) and re.match(r"[a-zà-ÿ]", b):
        return a[:-1] + b  # hyphenation at the end of a line
    return a + " " + b


def _blocks_from_lines(doc: str, page: int, lines: list[_Line], body: float, start: int, figures_bboxes=()) -> list[Block]:
    """Group the lines of one page into blocks."""
    if not lines:
        return []
    gaps = [b.top - a.bottom for a, b in zip(lines, lines[1:]) if 0 <= b.top - a.bottom < 40]
    gap = statistics.median(gaps) if gaps else 3.0
    marks = sorted({round(l.x0) for l in lines if _mark(clean(l.text))[0] or _mark(clean(l.text))[1]})
    levels: list[float] = []
    for x in marks:  # list levels: distinct indentations of the lines that start an item
        if not levels or x - levels[-1] > 6:
            levels.append(x)
    out: list[Block] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur and cur["text"].strip():
            text = clean(cur["text"])
            kind = cur["kind"]
            if kind == "paragraph" and not cur["number"] and _is_heading(text, cur["size"], body, cur["bold"]):
                kind = "heading"
            elif kind == "paragraph" and cur["number"] and len(text.split()) <= 8 and not text.endswith((".", ";", ",", ":")) \
                    and (cur["bold"] or (body > 0 and cur["size"] >= body + 1) or (text.isupper() and len(text) > 3)):
                kind = "heading"  # numbered title such as "2.1. Co-organisateurs" or "0301. GENERAL"
            if _CAPTION.match(text) and len(text.split()) <= 30 and (figures_bboxes or kind != "paragraph"):
                kind = "caption"
            out.append(Block(id=f"{doc}#p{page}.b{start + len(out) + 1}", doc=doc, page=page, kind=kind, text=text,
                             number=cur["number"], bullet=cur["bullet"], level=cur["level"],
                             bbox=(cur["x0"], cur["top"], cur["x1"], cur["bottom"]), ocr=cur["ocr"]))
        cur = None

    prev: _Line | None = None
    for ln in lines:
        text = clean(ln.text)
        if not text:
            continue
        number, bullet, rest = _mark(text)
        new = cur is None or bool(number or bullet) or bool(_LABEL.match(text))
        if prev is not None and cur is not None and not new:
            hanging = cur["kind"] == "item" and ln.x0 > cur["x0"] + 4  # continuation of a list item (hanging indent)
            if ln.top - prev.bottom > max(1.6 * gap, gap + 4):
                new = True
            elif abs(ln.size - prev.size) > 1.0:
                new = True
            elif cur["kind"] == "item" and ln.x0 < cur["x0"] - 8:
                new = True  # back to the margin: the list is over
            elif ln.bold != prev.bold and not hanging and (ln.top - prev.bottom > gap + 1.5 or len(cur["text"].split()) <= 8):
                new = True  # bold title above a plain paragraph (a bold phrase inside a paragraph does not split it)
            elif cur["kind"] == "paragraph" and not cur["number"] and len(cur["text"].split()) <= 8 \
                    and _is_heading(clean(cur["text"]), cur["size"], body, cur["bold"]) and not cur["text"].rstrip().endswith((",", ";")):
                new = True
        if new:
            flush()
            lvl = 0
            if number or bullet:
                lvl = max(0, sum(1 for x in levels if x <= ln.x0 + 6) - 1)
            kind = "paragraph"  # numbered paragraphs ("1.", "0304.", "5.2.") stay paragraphs
            if bullet or (number and (re.fullmatch(r"[A-Za-z]|[ivxIVX]{1,4}", number) or text.split()[0].endswith(")"))):
                kind = "item"  # bullets, lettered items "a.", "(1)", "1)"
            cur = {"text": rest if (number or bullet) else text, "kind": kind, "number": number, "bullet": bullet, "level": lvl,
                   "x0": ln.x0, "top": ln.top, "x1": ln.x1, "bottom": ln.bottom, "size": ln.size, "bold": ln.bold, "ocr": ln.ocr}
        else:
            cur["text"] = _join(cur["text"], text)
            cur["x0"], cur["x1"] = min(cur["x0"], ln.x0), max(cur["x1"], ln.x1)
            cur["bottom"] = ln.bottom
        prev = ln
    flush()
    return out


def _link(blocks: list[Block]) -> None:
    """Sections (nearest heading) and parents (the block that introduces an item) for all blocks, in order."""
    section = ""
    stack: list[Block] = []  # open items and paragraphs by level
    for b in blocks:
        if b.kind == "heading":
            section, stack = (f"{b.number} {b.text}" if b.number else b.text), []
            b.section = section
            continue
        b.section = section
        if b.kind == "item" or (b.kind == "paragraph" and (b.bullet or b.number)):
            while stack and stack[-1].level >= b.level and stack[-1].page == b.page:
                stack.pop()
            while stack and stack[-1].page != b.page and stack[-1].level >= b.level:
                stack.pop()
            if stack:
                b.parent = stack[-1].id
            elif b.level > 0:
                prev = next((x for x in reversed(blocks[:blocks.index(b)]) if x.kind in ("paragraph", "heading", "item")), None)
                b.parent = prev.id if prev else ""
            stack.append(b)
        elif b.kind == "paragraph":
            stack = [b]


def _join_page_breaks(blocks: list[Block]) -> list[Block]:
    """A paragraph that ends a page without final punctuation continues in the first plain paragraph of the next page."""
    out: list[Block] = []
    for b in blocks:
        if out and b.kind == "paragraph" and not b.number and not b.bullet and out[-1].kind in ("paragraph", "item") \
                and b.page == (out[-1].last_page or out[-1].page) + 1 and not re.search(r"[.:;!?]$", out[-1].text) \
                and b.text[:1].islower() and _first_on_page(b, blocks):
            a = out[-1]
            a.text = _join(a.text, b.text)
            a.last_page = b.page
            continue
        out.append(b)
    return out


def _first_on_page(b: Block, blocks: list[Block]) -> bool:
    return next(x for x in blocks if x.page == b.page) is b


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

def _doc_id(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("_")[:60] or "document"


def read_document(path: str | Path, doc_id: str | None = None, ocr: bool = True) -> Document:
    p = Path(path)
    data = p.read_bytes()
    sha = hashlib.sha1(data).hexdigest()[:12]
    did = doc_id or _doc_id(p)
    ext = p.suffix.lower()
    if not ext or ext not in SUPPORTED:  # uploads come without extension: sniff the content
        ext = ".pdf" if data[:5] == b"%PDF-" else ".docx" if data[:2] == b"PK" else ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" \
            else ".jpg" if data[:3] == b"\xff\xd8\xff" else ".txt"
    if ext == ".pdf":
        doc = _read_pdf(p, did, sha, ocr)
    elif ext == ".docx":
        doc = _read_docx(p, did, sha)
    elif ext in IMAGE_EXT:
        doc = _read_image(p, did, sha, data, ext, ocr)
    else:
        doc = _read_text(p, did, sha, data)
    doc.language = guess_language(doc.text())
    if not doc.blocks:
        doc.warnings.append("no text found")
    return doc


def read_documents(paths: Iterable[str | Path], ocr: bool = True) -> tuple[list[Document], list[str]]:
    """Every supported file in the given files and folders (recursively, sorted), with unique ids."""
    files: list[Path] = []
    skipped: list[str] = []
    for x in paths:
        q = Path(x)
        if q.is_dir():
            for f in sorted(q.rglob("*")):
                if f.is_file() and not f.name.startswith("."):
                    (files.append(f) if f.suffix.lower() in SUPPORTED else skipped.append(f"{f}: format not read"))
        elif q.is_file():
            files.append(q)
        else:
            skipped.append(f"{q}: not found")
    docs, seen = [], Counter()
    for f in files:
        did = _doc_id(f)
        seen[did] += 1
        if seen[did] > 1:
            did = f"{did}_{seen[did]}"
        try:
            docs.append(read_document(f, did, ocr=ocr))
        except Exception as exc:  # noqa: BLE001 - one unreadable file must not stop the corpus
            skipped.append(f"{f}: unreadable ({type(exc).__name__}: {exc})")
    return docs, skipped


def _title(meta_title: str | None, blocks: list[Block], fallback: str) -> str:
    t = clean(meta_title or "")
    if t and not re.search(r"pr[ée]sentation powerpoint|^microsoft|untitled|^document\d*$", t, re.I):
        return t
    head = next((b.text for b in blocks[:15] if b.kind == "heading"), "")
    return head or fallback


def _drop_margins(pages: list[list[_Line]], heights: list[float]) -> tuple[list[list[_Line]], int]:
    """Remove page numbers and the header and footer lines that repeat on at least half of the pages."""
    def key(l: _Line) -> str:
        return re.sub(r"\d+", "#", clean(l.text).lower())

    def in_margin(l: _Line, h: float) -> bool:
        return l.top < 0.09 * h or l.bottom > 0.91 * h

    n = len(pages)
    counts: Counter = Counter()
    for lines, h in zip(pages, heights):
        counts.update({key(l) for l in lines if in_margin(l, h)})
    repeated = {k for k, c in counts.items() if n >= 3 and c >= max(2, 0.5 * n)}
    out, dropped = [], 0
    for lines, h in zip(pages, heights):
        keep = []
        for l in lines:
            if in_margin(l, h) and (key(l) in repeated or _PAGE_NO.match(key(l).strip())):
                dropped += 1
                continue
            keep.append(l)
        out.append(keep)
    return out, dropped


def _read_pdf(p: Path, did: str, sha: str, ocr: bool) -> Document:
    try:
        import pdfplumber
    except ImportError:
        return _read_pdf_plain(p, did, sha)
    warnings: list[str] = []
    pages_lines, heights, sizes, raw_figs = [], [], [], []
    with pdfplumber.open(str(p)) as pdf:
        meta_title = (pdf.metadata or {}).get("Title")
        for i, page in enumerate(pdf.pages, start=1):
            heights.append(float(page.height))
            sizes.append((float(page.width), float(page.height)))
            lines = []
            for l in page.extract_text_lines(x_tolerance=1.5, y_tolerance=3, return_chars=True):
                chars = [c for c in l["chars"] if c["text"].strip()]
                if not chars:
                    continue
                size = statistics.median(c["size"] for c in chars)
                bold = sum(bool(re.search(r"bold|black|heavy|semibold", c.get("fontname", ""), re.I)) for c in chars) > 0.6 * len(chars)
                lines.append(_Line(l["text"], l["x0"], l["top"], l["x1"], l["bottom"], round(size, 1), bold))
            page_area = float(page.width * page.height)
            drawings = [o for o in (page.rects + page.curves + page.lines)
                        if (o["x1"] - o["x0"]) * (o["bottom"] - o["top"]) < 0.9 * page_area]
            for im in page.images:
                x0, t, x1, b = (float(im["x0"]), float(im["top"]), float(im["x1"]), float(im["bottom"]))
                if (x1 - x0) * (b - t) < 0.004 * page_area:
                    continue  # bullets, rules, tiny logos
                over = sum(1 for o in drawings if o["x0"] < x1 and o["x1"] > x0 and o["top"] < b and o["bottom"] > t)
                raw_figs.append((i, (x0, t, x1, b), tuple(im.get("srcsize") or ()) or None, str(im.get("name", "")), over))
            if len(" ".join(l.text for l in lines)) < 20 and (page.images or not lines):
                if ocr:
                    got = _ocr_page(p, i - 1, float(page.width))
                    if got is not None:
                        lines = got
                        warnings.append(f"page {i}: no text layer, read by OCR")
                    else:
                        warnings.append(f"page {i}: no text layer and no OCR available (pip install rapidocr_onnxruntime)")
                else:
                    warnings.append(f"page {i}: no text layer")
            pages_lines.append(lines)
    pages_lines, dropped = _drop_margins(pages_lines, heights)
    if dropped:
        warnings.append(f"{dropped} header, footer or page-number lines dropped")
    all_sizes = [l.size for lines in pages_lines for l in lines if l.size]
    body = statistics.median(all_sizes) if all_sizes else 0.0
    blocks: list[Block] = []
    for i, lines in enumerate(pages_lines, start=1):
        lines = sorted(lines, key=lambda l: (round(l.top), l.x0))
        figs = [f[1] for f in raw_figs if f[0] == i]
        blocks += _blocks_from_lines(did, i, lines, body, len(blocks), figs)
    blocks = _merge_split_captions(blocks)
    blocks = _join_page_breaks(blocks)
    _link(blocks)
    figures = _pdf_figures(p, did, raw_figs, blocks)
    return Document(did, str(p), "pdf", sha, len(heights), _title(meta_title, blocks, did), "", blocks, figures, warnings, sizes)


def _merge_split_captions(blocks: list[Block]) -> list[Block]:
    """'Figure' on one line and '1: Illustration of...' on the next are one caption."""
    out: list[Block] = []
    for b in blocks:
        if out and re.fullmatch(r"(figure|fig\.|table|tableau)", out[-1].text, re.I) and out[-1].page == b.page \
                and re.match(r"\d+\s*[:.\-]", b.text):
            a = out[-1]
            a.text, a.kind = f"{a.text} {b.text}", "caption"
            a.bbox = (min(a.bbox[0], b.bbox[0]), a.bbox[1], max(a.bbox[2], b.bbox[2]), b.bbox[3]) if a.bbox and b.bbox else a.bbox
            continue
        out.append(b)
    return out


def _pdf_figures(p: Path, did: str, raw_figs, blocks: list[Block]) -> list[Figure]:
    data: dict[tuple[int, str], tuple[bytes, str]] = {}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(p))
        for i in sorted({f[0] for f in raw_figs}):
            for im in reader.pages[i - 1].images:
                data[(i, Path(im.name).stem)] = (im.data, Path(im.name).suffix or ".png")
    except Exception:  # noqa: BLE001 - pixels are optional: figures stay listed without them
        pass
    figs: list[Figure] = []
    for n, (page, bbox, size, name, over) in enumerate(raw_figs, start=1):
        x0, t, x1, b = bbox
        same = [bl for bl in blocks if bl.page == page and bl.bbox]
        cap = None
        for bl in same:
            if bl.kind != "caption":
                continue
            bx0, bt, bx1, bb = bl.bbox
            if bx0 < x1 + 20 and bx1 > x0 - 20 and (0 <= bt - b + 15 <= 60 or 0 <= t - bb <= 45):
                cap = bl
                break

        def dist(bl: Block) -> float:
            bx0, bt, bx1, bb = bl.bbox
            dx = max(0.0, bx0 - x1, x0 - bx1)
            dy = max(0.0, bt - b, t - bb)
            return (dx * dx + dy * dy) ** 0.5

        near = [bl.id for bl in sorted(same, key=dist)]
        rotated = bool(size) and ((size[0] > size[1]) != ((x1 - x0) > (b - t))) and abs(size[0] - size[1]) > 0.1 * max(size)
        raw, ext = data.get((page, name), (None, ".png"))
        figs.append(Figure(f"{did}#p{page}.f{n}", did, page, bbox, size, cap.text if cap else "", cap.id if cap else "", near,
                           over, rotated, name, raw, ext))
    return figs


def render_region(path: str | Path, page: int, bbox: tuple[float, float, float, float] | None, scale: float = 2.0):
    """The region of a PDF page as shown (image and the vector drawings over it), as a PIL image (needs pypdfium2)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        im = pdf[page - 1].render(scale=scale).to_pil()
    finally:
        pdf.close()
    if bbox is None:
        return im
    x0, t, x1, b = bbox
    return im.crop((max(0, int(x0 * scale)), max(0, int(t * scale)), min(im.width, int(x1 * scale)), min(im.height, int(b * scale))))


def _ocr_lines(img) -> list[_Line] | None:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    import numpy as np

    res, _ = RapidOCR()(np.asarray(img.convert("RGB")))
    lines = []
    for box, txt, conf in res or []:
        if float(conf) < 0.5 or not txt.strip():
            continue
        xs, ys = [pt[0] for pt in box], [pt[1] for pt in box]
        lines.append(_Line(txt, min(xs), min(ys), max(xs), max(ys), round(max(ys) - min(ys), 1), False, True))
    return sorted(lines, key=lambda l: (round(l.top / 4), l.x0))


def _ocr_page(p: Path, index: int, width_pt: float) -> list[_Line] | None:
    try:
        img = render_region(p, index + 1, None, scale=2.0)
    except Exception:  # noqa: BLE001
        return None
    lines = _ocr_lines(img)
    if lines is None:
        return None
    for l in lines:  # back to PDF points
        l.x0, l.top, l.x1, l.bottom, l.size = l.x0 / 2, l.top / 2, l.x1 / 2, l.bottom / 2, l.size / 2
    return lines


def _read_pdf_plain(p: Path, did: str, sha: str) -> Document:
    from pypdf import PdfReader

    reader = PdfReader(str(p))
    pages = [[_Line(t) for t in (pg.extract_text() or "").splitlines()] for pg in reader.pages]
    blocks: list[Block] = []
    for i, lines in enumerate(pages, start=1):
        blocks += _blocks_from_lines(did, i, lines, 0.0, len(blocks))
    _link(blocks)
    figs = []
    for i, pg in enumerate(reader.pages, start=1):
        for im in pg.images:
            try:
                size = im.image.size
            except Exception:  # noqa: BLE001
                size = None
            figs.append(Figure(f"{did}#p{i}.f{len(figs) + 1}", did, i, None, size, name=Path(im.name).stem, data=im.data,
                               ext=Path(im.name).suffix or ".png"))
    return Document(did, str(p), "pdf", sha, len(pages), _title(reader.metadata.title if reader.metadata else None, blocks, did), "",
                    blocks, figs, ["pdfplumber not installed: no positions, captions or vector drawings"])


def _read_docx(p: Path, did: str, sha: str) -> Document:
    import docx
    from docx.oxml.ns import qn

    d = docx.Document(str(p))
    blocks: list[Block] = []
    figs: list[Figure] = []
    rels = d.part.rels

    def add(kind: str, text: str, number: str = "", bullet: str = "", level: int = 0):
        text = clean(text)
        if text:
            blocks.append(Block(f"{did}#b{len(blocks) + 1}", did, 0, kind, text, number, bullet, level=level))

    for el in d.element.body.iterchildren():
        if el.tag == qn("w:p"):
            para = docx.text.paragraph.Paragraph(el, d)
            style = (para.style.name if para.style is not None else "") or ""
            text = para.text
            for blip in el.iter(qn("a:blip")):
                rid = blip.get(qn("r:embed"))
                part = rels[rid].target_part if rid in rels else None
                if part is not None:
                    figs.append(Figure(f"{did}#f{len(figs) + 1}", did, 0, None, None, name=Path(part.partname).name, data=part.blob,
                                       ext=Path(part.partname).suffix or ".png", near_blocks=[blocks[-1].id] if blocks else []))
            if not text.strip():
                continue
            numbered = el.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is not None
            number, bullet, rest = _mark(clean(text))
            lvl_el = el.find(f"{qn('w:pPr')}/{qn('w:numPr')}/{qn('w:ilvl')}")
            level = int(lvl_el.get(qn("w:val"))) if lvl_el is not None else 0
            if re.match(r"(heading|titre|title)", style, re.I):
                add("heading", text)
            elif re.match(r"(caption|légende)", style, re.I) or _CAPTION.match(clean(text)) and len(text.split()) <= 30 and figs \
                    and figs[-1].near_blocks == ([blocks[-1].id] if blocks else []):
                add("caption", text)
                if figs and not figs[-1].caption:
                    figs[-1].caption, figs[-1].caption_block = blocks[-1].text, blocks[-1].id
            elif numbered or bullet or re.search(r"list|liste", style, re.I) or (number and re.fullmatch(r"[a-z]|[ivx]+", number)):
                add("item", rest if (number or bullet) else text, number, bullet or ("-" if not number else ""), level)
            else:
                b = len(blocks)
                add("paragraph", rest if number else text, number)
                if b < len(blocks) and _is_heading(blocks[-1].text, 0, 0, bool(para.runs) and all(r.bold for r in para.runs if r.text.strip())):
                    blocks[-1].kind = "heading"
        elif el.tag == qn("w:tbl"):
            table = docx.table.Table(el, d)
            rows = [[clean(c.text) for c in r.cells] for r in table.rows]
            if not rows:
                continue
            head = rows[0]
            for r in rows[1:] or rows:
                cells = [f"{h}: {v}" if h and h != v else v for h, v in zip(head, r) if v] if len(rows) > 1 else [v for v in r if v]
                add("table_row", "; ".join(dict.fromkeys(cells)))
    _link(blocks)
    title = clean(d.core_properties.title or "") or next((b.text for b in blocks[:10] if b.kind == "heading"), did)
    return Document(did, str(p), "docx", sha, 0, title, "", blocks, figs, [])


def _read_text(p: Path, did: str, sha: str, data: bytes) -> Document:
    warnings = []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
        warnings.append("not UTF-8: read as Windows-1252")
    blocks: list[Block] = []
    for chunk in re.split(r"\n\s*\n", text):
        lines = [l for l in chunk.splitlines() if l.strip()]
        if not lines:
            continue
        if re.match(r"^#{1,6}\s", lines[0]) and len(lines) == 1:
            blocks.append(Block(f"{did}#b{len(blocks) + 1}", did, 0, "heading", clean(lines[0].lstrip("#"))))
            continue
        lns = [_Line(re.sub(r"^#{1,6}\s+", "", l), x0=float(len(l) - len(l.lstrip()) * 5), top=float(i * 12), bottom=float(i * 12 + 10), size=10.0)
               for i, l in enumerate(lines)]
        got = _blocks_from_lines(did, 0, lns, 10.0, len(blocks))
        for b in got:
            b.bbox = None
            if b.kind == "heading" and b.text == clean(lines[0]) and len(lines) > 1 and not re.match(r"^#", lines[0]):
                b.kind = "paragraph" if len(b.text.split()) > 6 else "heading"
        blocks += got
    for i, b in enumerate(blocks, start=1):
        b.id = f"{did}#b{i}"
    _link(blocks)
    title = next((b.text for b in blocks[:5] if b.kind == "heading"), did)
    return Document(did, str(p), p.suffix.lower().lstrip(".") or "txt", sha, 0, title, "", blocks, [], warnings)


def _read_image(p: Path, did: str, sha: str, data: bytes, ext: str, ocr: bool) -> Document:
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    fig = Figure(f"{did}#f1", did, 1, None, im.size, name=p.name, data=data, ext=ext)
    warnings, blocks = [], []
    if ocr:
        lines = _ocr_lines(im)
        if lines is None:
            warnings.append("image without text layer and no OCR available")
        else:
            blocks = _blocks_from_lines(did, 1, lines, statistics.median([l.size for l in lines]) if lines else 0.0, 0)
            _link(blocks)
            warnings.append("text read by OCR")
    fig.near_blocks = [b.id for b in blocks]
    return Document(did, str(p), "image", sha, 1, did, "", blocks, [fig], warnings, [(float(im.width), float(im.height))])


_NUM = re.compile(r"(?<![\w.,])(\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d+)?)\s*(k\b|000\b)?")


def numbers_in(text: str) -> set[float]:
    """Every number of a text, with thousands written 10k, 10 000, 10,000 or 10.000, and times hh:mm as minutes."""
    out: set[float] = set()
    t = text.replace(" ", " ").replace(" ", " ")
    for m in re.finditer(r"\b(\d{1,2})\s*[:h]\s*(\d{2})\b", t):
        out.add(float(int(m.group(1)) * 60 + int(m.group(2))))
    for m in _NUM.finditer(t):
        raw, suf = m.group(1), m.group(2)
        if re.fullmatch(r"\d{1,3}(?:[ .,]\d{3})+", raw):
            vals = [float(re.sub(r"[ .,]", "", raw))]
            if "," in raw or "." in raw:
                vals.append(float(raw.replace(",", ".")) if raw.count(",") + raw.count(".") == 1 else vals[0])
        else:
            vals = [float(raw.replace(",", "."))]
        for v in vals:
            out.add(v * 1000 if suf in ("k", "000") and v < 1000 else v)
    return out


__all__ = ["Block", "Document", "Figure", "SUPPORTED", "clean", "guess_language", "match_key", "numbers_in", "read_document", "read_documents",
           "render_region", "split_text_sentences"]
