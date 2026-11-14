"""Quality check of the first step: reading a document and cutting it into passages.

It answers three questions:
  1. Is text lost?         (every word on the page must end up in a passage, a heading or a removed running line)
  2. Is text damaged?      (garbled characters, letters spaced out, broken words)
  3. Is the structure sane? (headings, contents pages, paragraphs cut at a page break)
and it prepares a page sample for a human to compare against the PDF, which is the check no program can replace
(reading order of columns, tables).
"""
from __future__ import annotations

import html
import random
import re
import statistics
from collections import Counter
from pathlib import Path

from .ingest import MAX_CHUNK_CHARS, TOC_LINE, _heading, _strip_running_lines, read_document, read_pages
from .text import clean

WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    return WORD.findall(clean(text).lower())


def dehyphenate(text: str) -> str:
    return re.sub(r"-\s*\n\s*(?=[a-z])", "", text)


def garbled(text: str) -> str | None:
    """Return why a passage looks damaged, or None."""
    if "�" in text:
        return "contains replacement characters (unreadable symbols)"
    letters = [c for c in text if not c.isspace()]
    if len(letters) >= 20 and sum(not (c.isalnum() or c in ".,;:()-/%'\"&+") for c in letters) / len(letters) > 0.25:
        return "many strange symbols"
    toks = text.split()
    if len(toks) >= 8 and statistics.mean(len(t) for t in toks) < 2.0:
        return "letters spaced apart (T h e  c o n v o y)"
    return None


def check_document(path: str | Path, sample_pages: int = 5, seed: int = 1) -> dict:
    path = Path(path)
    raw_pages, warnings = read_pages(path)
    chunks, _ = read_document(path)
    cleaned = [clean(p).splitlines() for p in raw_pages]
    kept = _strip_running_lines(cleaned)
    by_page: dict[int, list] = {}
    for c in chunks:
        by_page.setdefault(c.page, []).append(c)

    pages = []
    for i, (raw_lines, keep_lines) in enumerate(zip(cleaned, kept), 1):
        raw_words = words(dehyphenate("\n".join(raw_lines)))
        keep_words = Counter(words(dehyphenate("\n".join(keep_lines))))
        have = Counter()
        for c in by_page.get(i, []):
            have.update(words(c.text + " " + c.label))
        heads = [h for h in (_heading(l) for l in keep_lines) if h]
        for hid, title in heads:
            have.update(words(f"{hid} {title}"))
        # a sentence that ran over from the previous page now sits in that page's last passage
        prev_chunks = by_page.get(i - 1, [])
        first = " ".join(words(dehyphenate("\n".join(keep_lines)))[:6])
        if prev_chunks and first and first in " ".join(words(prev_chunks[-1].text)):
            have.update(words(prev_chunks[-1].text))
        lost = keep_words - have
        n_keep = sum(keep_words.values())
        removed = [l.strip() for l in raw_lines if l.strip() and l.strip() not in {k.strip() for k in keep_lines}]
        pages.append({
            "page": i, "raw_words": len(raw_words), "kept_words": n_keep, "lost_words": sum(lost.values()),
            "coverage": 1.0 if n_keep == 0 else round(1 - sum(lost.values()) / n_keep, 4),
            "lost_sample": [w for w, _ in lost.most_common(8)],
            "removed_lines": removed[:6], "n_removed": len(removed),
            "passages": len(by_page.get(i, [])), "empty": len(raw_words) == 0,
            "toc_page": sum(bool(TOC_LINE.search(l)) for l in raw_lines) >= 5})

    bad_chunks = [(c, g) for c in chunks if (g := garbled(c.text))]
    lens = [len(c.text) for c in chunks] or [0]
    # a paragraph cut by a page break: previous page ends without punctuation, next page starts in lower case
    broken = []
    pnums = sorted(by_page)
    for a, b in zip(pnums, pnums[1:]):
        if b == a + 1:
            last, first = by_page[a][-1].text.rstrip(), by_page[b][0].text.lstrip()
            if last and first and last[-1] not in ".:;!?)" and first[0].islower():
                broken.append({"page": a, "end": last[-60:], "next_start": first[:60]})
    heads = []
    seen = Counter()
    for c in chunks:
        if c.section and (not heads or heads[-1]["section"] != c.section):
            heads.append({"section": c.section, "page": c.page})
            seen[c.section] += 1
    repeated = [s for s, n in seen.items() if n > 1]

    text_pages = [p for p in pages if not p["empty"]]
    total_keep = sum(p["kept_words"] for p in pages) or 1
    coverage = round(1 - sum(p["lost_words"] for p in pages) / total_keep, 4)
    low = [p["page"] for p in text_pages if p["coverage"] < 0.95]
    empty = [p["page"] for p in pages if p["empty"]]
    rng = random.Random(seed)
    sample = sorted(rng.sample([p["page"] for p in text_pages], min(sample_pages, len(text_pages))))
    problems = []
    if len(empty) > len(pages) / 3:
        problems.append(f"{len(empty)} of {len(pages)} pages have no text: scanned PDF. Ask for a text version.")
    if coverage < 0.97:
        problems.append(f"{round((1 - coverage) * 100, 1)}% of the words did not reach a passage")
    if low:
        problems.append(f"pages with more than 5% words lost: {low[:15]}")
    if len(bad_chunks) > max(2, 0.02 * len(chunks)):
        problems.append(f"{len(bad_chunks)} passages look damaged")
    if not chunks:
        problems.append("no passage produced")
    if broken and len(broken) > 0.2 * len(pages):
        problems.append("many paragraphs are cut at page breaks")
    verdict = "BAD" if (not chunks or len(empty) > len(pages) / 3) else ("CHECK" if problems else "GOOD")
    return {"document": path.name, "pages": len(pages), "passages": len(chunks), "coverage": coverage,
            "verdict": verdict, "problems": problems, "empty_pages": empty,
            "toc_pages": [p["page"] for p in pages if p["toc_page"]], "low_pages": low,
            "damaged": [{"id": c.id, "why": g, "text": c.text[:120]} for c, g in bad_chunks[:20]],
            "broken_at_page_break": broken[:20], "headings": heads, "repeated_headings": repeated,
            "length": {"min": min(lens), "median": int(statistics.median(lens)), "max": max(lens),
                       "split_long": sum(l >= MAX_CHUNK_CHARS - 50 for l in lens),
                       "very_short": sum(l < 40 for l in lens)},
            "pages_detail": pages, "sample_pages": sample, "warnings": warnings,
            "_raw_pages": raw_pages, "_chunks": chunks}


def render_html(r: dict) -> str:
    e = html.escape
    color = {"GOOD": "#1a7f37", "CHECK": "#9a6700", "BAD": "#b42318"}[r["verdict"]]
    out = [f"""<!doctype html><meta charset="utf-8"><title>Reading check</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:1000px;margin:20px auto;padding:0 16px;color:#1c1f23}}
table{{border-collapse:collapse}}td,th{{border:1px solid #d9dde2;padding:3px 8px;font-size:13px;text-align:left}}
pre{{white-space:pre-wrap;background:#f6f8fa;border:1px solid #d9dde2;padding:8px;border-radius:6px;font-size:13px}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.bad{{color:#b42318}}.mut{{color:#5d6670}}</style>
<h1>Reading check: {e(r['document'])}</h1>
<h2 style="color:{color}">{r['verdict']}</h2>
<p>{r['pages']} pages, {r['passages']} passages, <b>{r['coverage'] * 100:.1f}%</b> of the words kept.</p>"""]
    if r["problems"]:
        out.append("<ul>" + "".join(f"<li class='bad'>{e(p)}</li>" for p in r["problems"]) + "</ul>")
    out.append("<h2>What to do by hand (5 minutes)</h2><p>Open the PDF at each page below and compare with the two columns: "
               "left is the text read from the PDF, right is how it was cut into passages. Look for: wrong order of columns, "
               "tables turned into mixed text, missing lines, headings cut wrongly.</p>")
    for pno in r["sample_pages"]:
        raw = r["_raw_pages"][pno - 1]
        ps = [c for c in r["_chunks"] if c.page == pno]
        right = "\n\n".join(f"[para {c.para}{' ' + c.label if c.label else ''}] {c.text}" for c in ps)
        out.append(f"<h3>PDF page {pno}</h3><div class='cols'><div><b>read from the PDF</b><pre>{e(raw)}</pre></div>"
                   f"<div><b>passages ({len(ps)})</b><pre>{e(right)}</pre></div></div>")
    out.append(f"<h2>Other checks</h2><ul><li>Pages without text: {r['empty_pages'] or 'none'}</li>"
               f"<li>Contents pages skipped on purpose: {r['toc_pages'] or 'none'}</li>"
               f"<li>Passage length: min {r['length']['min']}, median {r['length']['median']}, max {r['length']['max']} characters; "
               f"{r['length']['split_long']} long paragraphs were split, {r['length']['very_short']} passages under 40 characters</li>"
               f"<li>Headings found: {len(r['headings'])}; headings seen twice: {r['repeated_headings'][:8] or 'none'}</li>"
               f"<li>Paragraphs possibly cut at a page break: {len(r['broken_at_page_break'])}</li></ul>")
    if r["damaged"]:
        out.append("<h3>Passages that look damaged</h3><ul>" + "".join(
            f"<li>{e(d['id'])}: {e(d['why'])}: <span class='mut'>{e(d['text'])}</span></li>" for d in r["damaged"]) + "</ul>")
    worst = sorted((p for p in r["pages_detail"] if not p["empty"]), key=lambda p: p["coverage"])[:10]
    out.append("<h3>Pages with the lowest word coverage</h3><table><tr><th>page</th><th>coverage</th><th>words lost</th>"
               "<th>lost words (sample)</th><th>lines removed on purpose (running headers, page numbers, contents)</th></tr>")
    for p in worst:
        out.append(f"<tr><td>{p['page']}</td><td>{p['coverage'] * 100:.1f}%</td><td>{p['lost_words']}</td>"
                   f"<td>{e(', '.join(p['lost_sample']))}</td><td>{e(' | '.join(p['removed_lines'][:3]))}</td></tr>")
    out.append("</table>")
    return "".join(out)


def public(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_") and k != "pages_detail"}