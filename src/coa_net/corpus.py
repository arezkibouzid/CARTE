"""S1 at the event: a set of doctrine documents -> one requirement base, with citations, conflicts and sign-off.

The organisers hand out the doctrine at the start of the hackathon as a set of
textual documents (STANAG and other NATO documents released to the public).
This module compiles the whole set in one pass:

1. every document is read with its provenance (``documents.py``): page,
   paragraph number, section;
2. each document is compiled sentence by sentence (``doctrine.py``: pattern
   compiler, or the language model L1 with the same checks); every instance
   cites its document, page and paragraph, and its quote is checked against
   the document it cites;
3. instances that state the same rule (same template, scope and kind of
   check) across documents are reconciled: equal values corroborate each
   other; different values are a conflict, settled by the precedence of the
   documents (the more specific first: movement standards, then general
   doctrine, then other documents; configurable), then by the stricter value,
   and always shown;
4. the seed base fills the gaps: a parameter that the doctrine states, in any
   scope, is no longer taken from the seed; seed instances the doctrine does
   not touch stay, flagged as not coming from the event's doctrine;
5. a sign-off sheet (CSV) lists the doctrine instances, mandatory ones first,
   with quote and citation, for the instructors to accept, edit or reject;
   until then the base is provisional (``coa-net plan --provisional``).

Coverage follows section 4.7 of the methodology over the normative sentences
of each document: coverage = (mapped + out of scope) / N, expressiveness =
mapped / (mapped + inexpressible).
"""

from __future__ import annotations

import csv
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .doctrine import SentenceResult, compile_items, is_normative
from .documents import Document, read_documents
from .requirements import TEMPLATES, Requirement, load_requirements

DISCRIMINATORS = ("check", "element", "attribute", "area")
TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
KIND_RANK = {"order": 0, "standard": 1, "doctrine": 2, "other": 3}

# document kinds from their identifiers: movement standards are more specific than general doctrine
_STANDARD = re.compile(r"\b(STANAG|STANREC|AMovP|AMOVP|ATP|AEP|AEngrP|AMedP|ALP|AAP)[\s_-]*\d", re.I)
_DOCTRINE = re.compile(r"\b(AJP|AJDP)[\s_-]*\d", re.I)


def doc_kind(doc: Document) -> str:
    probe = f"{doc.id} {doc.title} " + " ".join(b.text for b in doc.blocks[:12])
    if _STANDARD.search(probe):
        return "standard"
    if _DOCTRINE.search(probe):
        return "doctrine"
    return "other"


def rule_key(r: Requirement) -> tuple:
    """Two instances with the same key state the same rule (possibly with different values).

    A constraint (T0, T1) and an objective (T2, T3) of the same template are different rules: both are kept, and when they
    come from different documents the difference of force is listed with the conflicts."""
    mode = "constraint" if r.tier in ("T0", "T1") else "objective"
    return (r.template, mode, r.scope.model_dump_json(), *(json.dumps(r.params.get(d), sort_keys=True) for d in DISCRIMINATORS))


def _disc(r: Requirement) -> tuple:
    mode = "constraint" if r.tier in ("T0", "T1") else "objective"
    return (r.template, mode, *(json.dumps(r.params.get(d), sort_keys=True) for d in DISCRIMINATORS))


def cite_source(src) -> str:
    """'STANAG_2454, p. 12, para 0304' from a requirement's source."""
    out = [src.document]
    if src.page:
        out.append(f"p. {src.page}")
    if src.paragraph:
        out.append(src.paragraph if src.paragraph.startswith("sect.") else f"para {src.paragraph}")
    return ", ".join(out)


@dataclass
class DocStats:
    id: str
    title: str
    path: str
    sha1: str
    format: str
    pages: int
    language: str
    kind: str
    rank: int
    blocks: int
    sentences: int
    normative: int
    mapped: int
    inexpressible: int
    out_of_scope: int
    instances: int
    seconds: float
    warnings: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return (self.mapped + self.out_of_scope) / self.normative if self.normative else 0.0

    @property
    def expressiveness(self) -> float:
        return self.mapped / (self.mapped + self.inexpressible) if self.mapped + self.inexpressible else 0.0


@dataclass
class Conflict:
    template: str
    kept: str
    kept_source: str
    kept_value: dict
    other: str
    other_source: str
    other_value: dict
    rule: str  # "precedence" | "stricter value" | "stricter tier"


@dataclass
class CorpusReport:
    docs: list[DocStats]
    sentences: list[SentenceResult]
    conflicts: list[Conflict] = field(default_factory=list)
    corroborated: list[tuple[str, str]] = field(default_factory=list)  # (kept id, confirming id)
    seed_superseded: list[tuple[str, str, list[str]]] = field(default_factory=list)  # (seed id, by ids, params)
    seed_kept: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)

    def totals(self) -> dict:
        n = sum(d.normative for d in self.docs)
        m = sum(d.mapped for d in self.docs)
        o = sum(d.out_of_scope for d in self.docs)
        i = sum(d.inexpressible for d in self.docs)
        return {"documents": len(self.docs), "pages": sum(d.pages for d in self.docs), "sentences": sum(d.sentences for d in self.docs),
                "normative": n, "mapped": m, "inexpressible": i, "out_of_scope": o,
                "coverage": round((m + o) / n, 3) if n else 0.0, "expressiveness": round(m / (m + i), 3) if m + i else 0.0,
                "instances": sum(d.instances for d in self.docs), "conflicts": len(self.conflicts)}

    def markdown(self, base: list[Requirement] | None = None) -> str:
        t = self.totals()
        out = ["# Doctrine set: compilation report", "",
               f"{t['documents']} documents, {t['pages']} pages, {t['sentences']} sentences, {t['normative']} normative "
               f"(modal of obligation or recommendation). {t['instances']} requirement instances passed the checks; "
               f"{t['conflicts']} conflicts between instances. Coverage {t['coverage']:.0%}, expressiveness {t['expressiveness']:.0%} "
               "(methodology 4.7). Every instance is a draft until signed off (`signoff.csv`).", "",
               "| Document | Kind (precedence) | Pages | Language | Normative | Mapped | Inexpressible | Out of scope | Instances | Seconds | Warnings |",
               "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for d in self.docs:
            out.append(f"| {d.title} (`{d.id}`) | {d.kind} ({d.rank}) | {d.pages} | {d.language} | {d.normative} | {d.mapped} | "
                       f"{d.inexpressible} | {d.out_of_scope} | {d.instances} | {d.seconds:.2f} | {'; '.join(d.warnings) or '-'} |")
        if self.skipped:
            out += ["", "Files not read: " + "; ".join(self.skipped)]
        if self.conflicts:
            out += ["", "## Conflicts", "", "| Template | Kept | Value kept | Over | Value set aside | Rule |", "| --- | --- | --- | --- | --- | --- |"]
            for c in self.conflicts:
                out.append(f"| {c.template} | {c.kept} ({c.kept_source}) | `{json.dumps(c.kept_value)}` | {c.other} ({c.other_source}) | "
                           f"`{json.dumps(c.other_value)}` | {c.rule} |")
        if self.corroborated:
            out += ["", "Rules stated with the same values in several places: " + ", ".join(f"{a} = {b}" for a, b in self.corroborated)]
        if base is not None:
            doc_reqs = [r for r in base if not r.source.document.startswith("seed base")]
            out += ["", "## Requirement instances from the documents", "", "| Id | Template | Tier | Parameters | Source | Quote |", "| --- | --- | --- | --- | --- | --- |"]
            for r in sorted(doc_reqs, key=lambda r: (TIER_ORDER[r.tier], r.template, r.id)):
                out.append(f"| {r.id} | {r.template} | {r.tier} | `{json.dumps(r.params)}` | {cite_source(r.source)} | {r.source.quote[:160]} |")
        out += ["", "## Seed base", "",
                f"{len(self.seed_superseded)} seed instances replaced in whole or in part by the documents; "
                f"{len(self.seed_kept)} kept unchanged (method defaults and stand-in rules, not from the event's doctrine: to confirm)."]
        for sid, by, params in self.seed_superseded:
            out.append(f"- {sid}: {'parameters ' + ', '.join(params) if params else 'whole instance'} now from {by}")
        gaps = [s for s in self.sentences if s.status == "inexpressible" and s.normative]
        if gaps:
            out += ["", "## Normative statements no template states (catalogue gaps, applied by hand)", ""]
            out += [f"- {s.doc}" + (f", p. {s.page}" if s.page else "") + (f", {s.paragraph if s.paragraph.startswith('sect.') else 'para ' + s.paragraph}"
                    if s.paragraph else "") + f": {s.sentence[:220]}" for s in gaps[:200]]
        if self.log:
            out += ["", "## Log", ""] + [f"- {x}" for x in self.log[:300]]
        return "\n".join(out) + "\n"


def doc_items(doc: Document) -> list[tuple[str, str, int]]:
    """(paragraph number or section, sentence, page) for every sentence of a document's paragraphs, items and table rows."""
    items = []
    for b, s in doc.sentences():
        para = b.number or ""
        if not para and b.parent:
            parent = doc.block(b.parent)
            para = parent.number if parent is not None else ""
        items.append((para or (f"sect. {b.section[:40]}" if b.section else ""), s, b.page))
    return items


def compile_corpus(docs: list[Document], llm=None, seed: list[Requirement] | None = None, precedence: list[str] | None = None,
                   use_seed: bool = True) -> tuple[list[Requirement], CorpusReport]:
    """Doctrine documents -> one requirement base (doctrine drafts + the seed instances they leave) and a report."""
    t0 = time.perf_counter()
    stats: list[DocStats] = []
    sentences: list[SentenceResult] = []
    log: list[str] = []
    drafts: list[Requirement] = []
    rank_of: dict[str, int] = {}
    n = 0
    llm_raw = None
    if llm is not None:  # one retrieval per template over the whole set (L1), then per-document compilation with the answers
        from .doctrine import _llm_instances

        llm_raw = _llm_instances([s for doc in docs for _p, s, _pg in doc_items(doc)], llm, log)
    for k, doc in enumerate(docs, start=1):
        t = time.perf_counter()
        kind = doc_kind(doc)
        rank = KIND_RANK[kind]
        if precedence:
            hit = next((i for i, p in enumerate(precedence) if p.lower() in f"{doc.id} {doc.title}".lower()), None)
            rank = hit if hit is not None else len(precedence) + KIND_RANK[kind]
        rank_of[doc.id] = rank
        items = doc_items(doc)
        reqs, res, lg = compile_items(items, doc.text(), doc.id, llm, id_prefix=f"D{k}", start=0, llm_raw=llm_raw)
        n += len(reqs)
        drafts += reqs
        sentences += res
        log += lg
        norm = [r for r in res if r.normative]
        stats.append(DocStats(doc.id, doc.title, doc.path, doc.sha1, doc.format, doc.pages, doc.language, kind, rank, len(doc.blocks),
                              len(res), len(norm), sum(r.status == "mapped" for r in norm), sum(r.status == "inexpressible" for r in norm),
                              sum(r.status == "out_of_scope" for r in norm), len(reqs), round(time.perf_counter() - t, 3), list(doc.warnings)))
    t_comp = time.perf_counter()
    base, conflicts, corroborated = reconcile(drafts, rank_of)
    seed = load_requirements() if seed is None and use_seed else (seed or [])
    seed_out, superseded, kept = merge_seed(seed, base) if use_seed else ([], [], [])
    rep = CorpusReport(stats, sentences, conflicts, corroborated, superseded, kept, [], log,
                       {"compile": round(t_comp - t0, 3), "reconcile": round(time.perf_counter() - t_comp, 3)})
    return base + seed_out, rep


def reconcile(drafts: list[Requirement], rank_of: dict[str, int]) -> tuple[list[Requirement], list[Conflict], list[tuple[str, str]]]:
    """One instance per rule: corroborations merged, conflicts settled by precedence, then stricter value, and listed."""
    groups: dict[tuple, list[Requirement]] = defaultdict(list)
    for r in drafts:
        groups[rule_key(r)].append(r)
    kept_all: list[Requirement] = []
    conflicts: list[Conflict] = []
    corroborated: list[tuple[str, str]] = []

    def src(r: Requirement) -> str:
        return cite_source(r.source)

    for key, rs in groups.items():
        rs = sorted(rs, key=lambda r: (rank_of.get(r.source.document, 9), TIER_ORDER[r.tier], r.id))
        kept = rs[0]
        for r in rs[1:]:
            if r.params == kept.params and r.tier == kept.tier:
                corroborated.append((kept.id, r.id))
                continue
            same_rank = rank_of.get(r.source.document, 9) == rank_of.get(kept.source.document, 9)
            if same_rank and r.params != kept.params:
                merged = dict(kept.params)
                for p in TEMPLATES[r.template].params:
                    a, b = kept.params.get(p.name), r.params.get(p.name)
                    if a is None or b is None or a == b:
                        merged[p.name] = a if a is not None else b
                    elif p.stricter == "max":
                        merged[p.name] = max(a, b)
                    elif p.stricter == "min":
                        merged[p.name] = min(a, b)
                before = dict(kept.params)
                kept.params = {k: v for k, v in merged.items() if v is not None}
                conflicts.append(Conflict(r.template, kept.id, src(kept), before, r.id, src(r), dict(r.params), "stricter value (same precedence)"))
            elif same_rank:
                conflicts.append(Conflict(r.template, kept.id, src(kept), dict(kept.params), r.id, src(r), dict(r.params), "stricter tier"))
            else:
                conflicts.append(Conflict(r.template, kept.id, src(kept), dict(kept.params), r.id, src(r), dict(r.params), "precedence"))
        kept_all.append(kept)
    by_rule: dict[tuple, list[Requirement]] = defaultdict(list)  # same rule stated as a constraint here and an objective there
    for r in kept_all:
        k = rule_key(r)
        by_rule[(k[0],) + k[2:]].append(r)
    for rs in by_rule.values():
        docs = {r.source.document for r in rs}
        if len(rs) > 1 and len(docs) > 1:
            a, b = sorted(rs, key=lambda r: TIER_ORDER[r.tier])[:2]
            conflicts.append(Conflict(a.template, a.id, src(a), dict(a.params), b.id, src(b), dict(b.params),
                                      f"different force ({a.tier} here, {b.tier} there): both kept, the constraint applies"))
    return kept_all, conflicts, corroborated


def merge_seed(seed: list[Requirement], doctrine: list[Requirement]) -> tuple[list[Requirement], list[tuple[str, str, list[str]]], list[str]]:
    """Seed instances minus what the doctrine states: a parameter stated by the doctrine (any scope) is not taken from the seed."""
    stated: dict[tuple, set[str]] = defaultdict(set)
    by: dict[tuple, list[str]] = defaultdict(list)
    for r in doctrine:
        stated[_disc(r)] |= {k for k, v in r.params.items() if v is not None and k not in DISCRIMINATORS}
        by[_disc(r)].append(r.id)
    out, superseded, kept = [], [], []
    for s in seed:
        k = _disc(s)
        if k not in stated:
            out.append(s)
            kept.append(s.id)
            continue
        own = {p for p in s.params if p not in DISCRIMINATORS}
        drop = own & stated[k]
        rest = own - drop
        if not own or not rest:
            superseded.append((s.id, ", ".join(by[k]), []))
            continue
        if drop:
            s = s.model_copy(deep=True)
            s.params = {p: v for p, v in s.params.items() if p not in drop}
            superseded.append((s.id, ", ".join(by[k]), sorted(drop)))
        else:
            kept.append(s.id)
        out.append(s)
    return out, superseded, kept


# ---------------------------------------------------------------------------
# sign-off (S1 step 9): instructors accept, edit or reject each instance
# ---------------------------------------------------------------------------

SHEET_FIELDS = ["id", "decision", "template", "tier", "params", "scope", "on_unknown", "document", "page", "paragraph", "quote", "notes"]


def write_signoff_sheet(base: list[Requirement], report: CorpusReport, path: str | Path) -> int:
    """CSV of the doctrine instances (mandatory first); fill ``decision`` with accept, edit (change params or tier) or reject."""
    notes: dict[str, list[str]] = defaultdict(list)
    for c in report.conflicts:
        notes[c.kept].append(f"kept over {c.other} ({c.other_source}: {json.dumps(c.other_value)}) by {c.rule}")
    for a, b in report.corroborated:
        notes[a].append(f"also stated by {b}")
    for sid, by, params in report.seed_superseded:
        for rid in by.split(", "):
            notes[rid].append(f"replaces seed {sid}" + (f" ({', '.join(params)})" if params else ""))
    rows = [r for r in base if not r.source.document.startswith("seed base")]
    rows.sort(key=lambda r: (TIER_ORDER[r.tier], r.template, r.id))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({"id": r.id, "decision": "", "template": r.template, "tier": r.tier, "params": json.dumps(r.params, ensure_ascii=False),
                        "scope": r.scope.model_dump_json(), "on_unknown": r.on_unknown, "document": r.source.document,
                        "page": r.source.page or "", "paragraph": r.source.paragraph, "quote": r.source.quote, "notes": "; ".join(notes[r.id])})
    return len(rows)


def apply_signoff(base: list[Requirement], path: str | Path, reviewer: str = "") -> tuple[list[Requirement], list[str]]:
    """Statuses from a filled sheet: accept -> approved, reject -> rejected, edit -> approved with the edited params and tier."""
    by_id = {r.id: r for r in base}
    notes: list[str] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            r = by_id.get(row.get("id", ""))
            d = (row.get("decision") or "").strip().lower()
            if r is None:
                notes.append(f"{row.get('id')}: not in the base")
                continue
            if d in ("accept", "accepted", "ok", "yes", "approve"):
                r.status = "approved"
            elif d in ("reject", "rejected", "no"):
                r.status = "rejected"
            elif d in ("edit", "edited", "change"):
                try:
                    new = r.model_copy(update={"params": json.loads(row["params"]), "tier": row["tier"].strip(), "status": "approved"})
                    by_id[r.id] = Requirement.model_validate(new.model_dump())
                    notes.append(f"{r.id}: edited ({row['tier']}, {row['params']})")
                except (ValueError, KeyError) as exc:
                    notes.append(f"{r.id}: edit rejected ({exc}); left as draft")
            elif d:
                notes.append(f"{r.id}: decision {d!r} not understood; left as draft")
    out = [by_id[r.id] for r in base]
    n_draft = sum(r.status == "draft" for r in out)
    notes.append(f"{sum(r.status == 'approved' for r in out)} approved, {sum(r.status == 'rejected' for r in out)} rejected, {n_draft} still draft"
                 + (f" (reviewer {reviewer})" if reviewer else ""))
    return out, notes


def provisional(base: Iterable[Requirement]) -> tuple[list[Requirement], list[str]]:
    """Drafts used as if approved, until sign-off; returns the base and the ids used that way (for the plan's warning)."""
    out, ids = [], []
    for r in base:
        if r.status == "draft":
            r = r.model_copy(update={"status": "approved"})
            ids.append(r.id)
        out.append(r)
    return out, ids


def document_log(docs: list[Document]) -> list[dict]:
    """Data log entries (rules art. 9.4: source, date, method) for the documents read."""
    return [{**d.summary(), "read_with": "coa_net.documents"} for d in docs]


def compile_paths(paths: list[str], llm=None, precedence: list[str] | None = None, use_seed: bool = True):
    t = time.perf_counter()
    docs, skipped = read_documents(paths)
    t_read = time.perf_counter() - t
    base, rep = compile_corpus(docs, llm=llm, precedence=precedence, use_seed=use_seed)
    rep.skipped = skipped
    rep.seconds["read"] = round(t_read, 3)
    return docs, base, rep


__all__ = ["Conflict", "CorpusReport", "DocStats", "apply_signoff", "cite_source", "compile_corpus", "compile_paths", "doc_items", "doc_kind",
           "document_log", "merge_seed", "provisional", "reconcile", "rule_key", "write_signoff_sheet"]
