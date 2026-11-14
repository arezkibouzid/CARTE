"""Text helpers: cleaning, tokens, quantities and unit handling."""
from __future__ import annotations

import re
import unicodedata

_MATH_ALNUM = re.compile("[\U0001D400-\U0001D7FF]")   # bold or italic letters used for formulas (MLC written in bold)
_SPACE_BEFORE_HYPHEN = re.compile(r"(?<=[A-Za-z]) -(?=[A-Za-z])")   # "case -by-case" -> "case-by-case"

_REPL = {
    "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", "­": "",
}


_PRIVATE_USE = re.compile(r"[\ue000-\uf8ff]")   # symbol-font bullets (Word "Symbol" glyph U+F0B7) come out as private-use characters


def clean(text: str) -> str:
    for a, b in _REPL.items():
        text = text.replace(a, b)
    text = _PRIVATE_USE.sub("\u2022", text)
    text = _MATH_ALNUM.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), text)
    return _SPACE_BEFORE_HYPHEN.sub("-", text)


_PAIR = re.compile(r"\b([A-Za-z]{2,}) ([b-hj-z])\b")   # "capacit y": a word cut before its last letter
_OPEN = re.compile(r"\(\s+(?=\w)")
_CLOSE = re.compile(r"(?<=\w)\s+\)")


def repair_words(pages: list[str]) -> list[str]:
    """Some PDFs put a space inside a word ("capacit y", "( e.g.,"). Join the cut only when the whole word
    is seen at least twice elsewhere in the same document, so real text is not touched."""
    count: dict[str, int] = {}
    for t in pages:
        for w in re.findall(r"[A-Za-z]+", t):
            count[w.lower()] = count.get(w.lower(), 0) + 1

    def join(m):
        whole = m.group(1) + m.group(2)
        return whole if count.get(whole.lower(), 0) >= 2 else m.group(0)

    return [_CLOSE.sub(")", _OPEN.sub("(", _PAIR.sub(join, t))) for t in pages]


def match_key(text: str) -> str:
    """Normal form used to check that a quote really appears in a passage."""
    return re.sub(r"\s+", " ", clean(text)).strip().lower()


STOP = set(
    "a an and are as at be been by for from has have in is it its of on or that the this to was were "
    "will with shall should must may not can".split()
)


def _stem(w: str) -> str:
    """Light stemming so that bridge/bridges, provide/provides/provided and contribute/contributes meet."""
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:[./-][a-z0-9]+)*", match_key(text))
    return [_stem(w) for w in words if w not in STOP]


# ---- quantities -----------------------------------------------------------------------------

_UNIT = (
    r"km\s*/\s*h|kph|mph|km|kilomet(?:re|er)s?|met(?:re|er)s?|m|tonnes?|tons?|t|"
    r"minutes?|mins?|hours?|hrs?|h|seconds?|secs?|s|%|per\s?cent|inhabitants?|residents?|ft|feet"
)
_NUM = r"\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?"
QTY = re.compile(
    rf"(?<![\w.])(?P<num>{_NUM})(?P<k>k)?\s*(?P<unit>{_UNIT})(?![A-Za-z0-9])", re.I
)
BARE_NUM = re.compile(rf"(?<![\w.])(?:{_NUM})(?![\w])")

_UNIT_NORM = {
    "km/h": "km/h", "kph": "km/h", "mph": "mph",
    "km": "km", "kilometre": "km", "kilometres": "km", "kilometer": "km", "kilometers": "km",
    "m": "m", "metre": "m", "metres": "m", "meter": "m", "meters": "m",
    "t": "t", "tonne": "t", "tonnes": "t", "ton": "t", "tons": "t",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "%": "%", "percent": "%",
    "inhabitant": "inhabitants", "inhabitants": "inhabitants",
    "resident": "inhabitants", "residents": "inhabitants",
    "ft": "ft", "feet": "ft",
}


def norm_unit(u: str) -> str:
    u = re.sub(r"\s+", "", u.lower())
    return _UNIT_NORM.get(u, u)


def parse_number(raw: str) -> float:
    s = raw.strip()
    if re.fullmatch(r"\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?", s):
        s = re.sub(r"[ ,]", "", s)
    elif re.fullmatch(r"\d+,\d{1,2}", s):
        s = s.replace(",", ".")
    return float(s)


def quantities(text: str) -> list[dict]:
    """Every number with a unit: {value, unit, start, end, raw}."""
    out = []
    for m in QTY.finditer(text):
        value = parse_number(m.group("num"))
        if m.group("k"):
            value *= 1000
        out.append(
            {"value": value, "unit": norm_unit(m.group("unit")), "start": m.start(),
             "end": m.end(), "raw": m.group(0)}
        )
    return out


def all_numbers(text: str) -> list[float]:
    nums = []
    for m in BARE_NUM.finditer(clean(text)):
        try:
            nums.append(parse_number(m.group(0)))
        except ValueError:
            pass
    for q in quantities(clean(text)):
        nums.append(q["value"])
    return nums


def number_supported(value: float, quote: str) -> bool:
    """A value is supported when the quote states it, possibly scaled (km/m, h/min)."""
    for n in all_numbers(quote):
        for cand in (n, n * 1000, n / 1000 if n else n, n * 60, n / 60 if n else n):
            if abs(cand - value) < 1e-6:
                return True
    return False


def sentences(text: str) -> list[tuple[int, int]]:
    """Sentence spans (start, end) in the original text, so quotes stay verbatim."""
    spans, start = [], 0
    for m in re.finditer(r"(?<=[.;])\s+(?=[A-Z(\d])", text):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]