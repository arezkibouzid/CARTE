"""Rule extraction: pattern extractor (offline fallback) and checks shared with the LLM extractor.

A rule is only kept when its quote is verbatim in the source passage and its number is stated by the
quote. Sentences that contain a number but give no rule are logged in `skipped`, so a reviewer can
see what the extractor left out (recall matters more than noise).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

from .graph import VEHICLE_GROUP, entities_in, ref_keys
from .reliability import LEVELS, rule_reliability
from . import semantics as SEM
from .templates import KEYWORDS, PRIORITY, STRICTNESS_TIER, TEMPLATES
from .text import clean, match_key, number_supported, quantities, sentences

MUST = re.compile(r"\b(shall|must|is to|are to|required|mandatory|prohibited|forbidden|only (?:with|if|when))\b", re.I)
SHOULD = re.compile(r"\b(should|recommended|ought|preferably|desirable)\b", re.I)
MAY = re.compile(r"\b(may|can|permitted|optional)\b", re.I)
PROHIB = re.compile(r"\b(shall not|must not|should not|avoid\w*|prohibited|forbidden|not (?:be )?(?:use|used|enter|cross|halted|stopped|parked|pass|traverse))\b", re.I)
LE = re.compile(r"\b(?:not exceed|no more than|at most|maximum|max\.?|up to|not greater than|not more than)\b", re.I)
GE = re.compile(r"\b(?:at least|minimum|min\.?|no less than|not less than|or higher|or more)\b", re.I)
LT = re.compile(r"\b(?:below|less than|under|narrower than|lower than|shorter than|smaller than|fewer than)\b", re.I)
GT = re.compile(r"\b(?:above|more than|greater than|over|wider than|taller than|higher than|steeper than|heavier than|longer than)\b", re.I)
DELTA = re.compile(r"\b(reduced?|decreased?|lowered?|increased?|raised?)\b[^.;]*?\bby\b", re.I)
DEFN = re.compile(r"^\s*[\w\s-]+?\b(?:are|is)\s+(?:narrower|wider|lower|higher|shorter|longer|heavier|lighter|smaller|larger)\s+than\b", re.I)
EVERY = re.compile(r"\b(?:every|each)\b", re.I)
COND = re.compile(
    r"\b(?:when|if|unless|except|during|where|while)\s[^,.;]{3,80}"
    r"|\bat night\b|\bin (?:poor visibility|the dark|rain|snow|fog)\b|\bin built-up areas?\b"
    r"|\bon class [A-D] roads?\b|\bon open roads?\b", re.I)
HAZ_NOUN = re.compile(r"\b(explosives?|hazardous|dangerous goods|ammunition|\bpol\b)", re.I)
BUILT = re.compile(r"\b(built-up|towns?|urban|villages?|populated)\b", re.I)
ESCORT = re.compile(r"\bescort\w*", re.I)
MLC_NUM = re.compile(r"\bMLC\s*(\d+)\b", re.I)


@dataclass
class Rule:
    template: str
    bound: str
    value: float | None
    unit: str | None
    strictness: str
    quote: str
    chunk_id: str
    doc: str
    page: int
    para: int
    section: str = ""
    scope: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    origin: str = "pattern"
    status: str = "draft"
    checks: dict = field(default_factory=dict)
    also_in: list[str] = field(default_factory=list)
    conflict: str | None = None
    id: str = ""
    reliability: str = "unknown"        # high | medium | low | unknown (reading reliability of the source)
    reliability_note: str = ""
    verify_against_pdf: bool = False    # a person must compare with the PDF page before accepting
    # The quote looks like a table row or holds several measurements: the value may belong to another column or row.
    # Text quality (reliability) says nothing about this. A person must read the PDF and write the context.
    table_derived: bool = False
    # --- meaning of the number (docrag.semantics). The quote stays word for word; these three blocks are kept apart:
    #     quote (above) | context = evidence outside the quote | interpretation = the fields below
    semantic_class: str = ""            # mandatory_constraint | conditional_requirement | recommendation | threshold_trigger | descriptive_value | ambiguous_or_incomplete
    operator: str = ""                  # < <= > >= = : how the number compares with the quantity ("" for flags and relative changes)
    applies_to: str = ""                # the subject of the sentence, word for word (never inferred)
    effect: str = ""                    # for a trigger or a classification: what the threshold leads to, word for word
    interpretation: list = field(default_factory=list)   # why this class (one line each)
    context: dict = field(default_factory=dict)          # neighbouring sentences and passages, heading, page markers
    context_status: str = "complete"   # complete | incomplete_context | completed_by_reviewer
    context_issues: list = field(default_factory=list)
    qualifiers: list = field(default_factory=list)       # phrases after the number ('for two-way traffic'), word for word
    history: list = field(default_factory=list)          # audit trail: every change of class or destination (docrag.audit)

    @property
    def needs_context(self) -> bool:
        """A person must read the PDF and write the missing context before this rule can be approved."""
        return self.table_derived or self.context_status == SEM.INCOMPLETE

    @property
    def destination(self) -> str:
        """Where the candidate goes. Discovery and meaning decide it; nothing is dropped on the way.
        constraint: complete, can become a planning constraint after a normal accept.
        advisory:   valid evidence that is not a constraint (advice, trigger, description). Listed apart, never a constraint.
        blocked:    needs a person (context, PDF, ambiguous wording) before it can go anywhere.
        unclassified_evidence: looks relevant, but its meaning, scope and conditions are not established. Never exported."""
        if self.semantic_class == SEM.UNCLASSIFIED:
            return "unclassified_evidence"
        if self.semantic_class == SEM.AMBIGUOUS or self.needs_context:
            return "blocked"
        return "constraint" if self.hard else "advisory"

    @property
    def block_reason(self) -> str:
        """Why a blocked candidate is blocked, in one word, so the queue can be grouped. A country-specific rule is blocked
        as 'country_unresolved' (applicability not established), never rejected for being country-specific."""
        if self.destination != "blocked":
            return ""
        if self.semantic_class == SEM.AMBIGUOUS:
            return "ambiguous_wording"
        text = " | ".join(self.context_issues)
        for word, label in (("country-specific", "country_unresolved"), ("labelled", "country_unresolved"),
                            ("joins separate provisions", "merged_provisions"), ("damaged text", "text_damaged"),
                            ("does not fit the noun", "unit_mismatch"), ("refers back", "refers_back"),
                            ("without stated applicability", "missing_applicability"), ("lead-in", "list_lead_in"),
                            ("exception", "exception_nearby"), ("previous sentence may limit", "limited_by_previous"),
                            ("starts in the middle", "starts_mid_sentence"), ("not recorded as scope", "qualifier_not_recorded")):
            if word in text:
                return label
        return "table_row" if self.table_derived else "other_context"

    @property
    def hard(self) -> bool:
        return SEM.is_hard(self.semantic_class)

    @property
    def tier(self) -> str:
        return STRICTNESS_TIER.get(self.strictness, "T2")

    def signature(self) -> tuple:
        # Not the passage id: that changes whenever the passage cutting is improved. The id follows what the rule IS:
        # its document, page and sentence, so a reviewer's decision still applies after the index is rebuilt.
        sig = (self.template, self.bound, self.value, self.unit, self.doc, self.page, match_key(self.quote), tuple(self.scope))
        # A hand-typed rule and an automatic rule can have the same wording and scope but different conditions. They are two
        # different candidates, so they must not share one id (a decision would then apply to both). Automatic ids are unchanged.
        return sig + ("manual",) if str(self.origin).startswith("manual") else sig

    def assign_id(self):
        self.id = "R-" + hashlib.sha1(repr(self.signature()).encode()).hexdigest()[:6]

    def cite(self) -> str:
        return f"{self.doc} p.{self.page} para {self.para}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tier"] = self.tier
        return d


_STRICT_OF = {"obligation": "must", "prohibition": "must", "authorization": "must", "recommendation": "should",
              "permission": "may", "none": "none"}


def _direction(reading, tpl: str, sentence: str, window: str, after: str, every: bool) -> tuple[str, list[str]]:
    """Planner bound (max/min/exact/delta) from the operator. A value with no comparison words keeps the template's
    usual direction, and says so."""
    if DELTA.search(sentence):
        return bound_of(sentence, window, after, TEMPLATES[tpl]["default_bound"], every), []
    if reading.operator in ("<", "<="):
        return "max", []
    if reading.operator in (">", ">="):
        return "min", []
    default = TEMPLATES[tpl]["default_bound"]
    if every and default == "exact":
        return "exact", []
    note = [] if default == "exact" else [f"direction not stated in the sentence: the template's usual direction ({default}) is used. Check it"]
    return default, note


def strictness_of(sentence: str) -> str | None:
    if MUST.search(sentence):
        return "must"
    if SHOULD.search(sentence):
        return "should"
    if MAY.search(sentence):
        return "may"
    return None


def bound_of(sentence: str, window: str, after: str, default: str, every: bool) -> str:
    seg = window + " " + after
    if DELTA.search(sentence):
        return "delta-" if re.search(r"reduc|decreas|lower", sentence, re.I) else "delta+"
    if LE.search(seg):
        return "max"
    if GE.search(seg):
        return "min"
    prohib = bool(PROHIB.search(sentence))
    if LT.search(window):  # "narrower than X shall not be used" forbids the low side, so the rule is a minimum
        return "min" if prohib else "max"
    if GT.search(window):
        return "max" if prohib else "min"
    if every and default == "exact":
        return "exact"
    return default


def pick_template(unit: str, window: str) -> str | None:
    cands = [t for t in PRIORITY if unit in TEMPLATES[t]["units"]]
    for t in cands:
        if re.search(KEYWORDS[t], window, re.I):
            return t
    return None


def _scope(sentence: str) -> list[str]:
    return sorted(e for e in entities_in(sentence) if e in VEHICLE_GROUP)


def _conditions(sentence: str) -> list[str]:
    seen, out = set(), []
    for m in COND.finditer(sentence):
        c = m.group(0).strip().lower()
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _apply_reading(rule: Rule, reading) -> None:
    rule.semantic_class = reading.cls
    if reading.cls == SEM.MANDATORY and rule.conditions:
        rule.semantic_class = SEM.CONDITIONAL      # an explicit condition in the sentence
    rule.operator = reading.operator if not rule.bound.startswith("delta") else ""
    rule.applies_to, rule.effect = reading.applies_to, reading.effect
    rule.interpretation = list(reading.notes)
    rule.qualifiers = list(getattr(reading, 'quals', []))
    if reading.strength != "none" or rule.strictness not in ("must", "should", "may"):
        rule.strictness = _STRICT_OF.get(reading.strength, "none")


def _read_for(rule: Rule, sentence: str):
    """Reading for a rule that arrives without one (LLM extractor): find its number in the sentence."""
    if rule.value is None:
        return SEM.analyse(sentence, None, flag=True)
    for q in quantities(sentence):
        if abs(q["value"] - rule.value) < 1e-9:
            return SEM.analyse(sentence, (q["start"], q["end"]))
    for m in MLC_NUM.finditer(sentence):
        if float(m.group(1)) == rule.value:
            return SEM.analyse(sentence, (m.start(), m.end()))
    return None


def _finish(rule: Rule, sentence: str, reading=None, notes: list[str] | None = None) -> Rule:
    rule.scope = _scope(sentence)
    rule.conditions = _conditions(sentence)
    miss = []
    if TEMPLATES[rule.template]["needs_scope"] and not rule.scope:
        miss.append("vehicle type not stated")
    if rule.bound.startswith("delta"):
        miss.append("relative change: base value not stated here")
    refs = ref_keys(sentence)
    if refs:
        miss.append("may depend on: " + ", ".join(refs))
    miss += notes or []
    rule.missing = miss
    reading = reading or _read_for(rule, sentence)
    if reading is None:
        rule.semantic_class = SEM.AMBIGUOUS
        rule.interpretation = ["the wording around the number could not be read: not a usable constraint until a person checks it"]
    else:
        _apply_reading(rule, reading)
    rule.assign_id()
    return rule


_CAPTION = re.compile(r"^\s*(?:figure|fig\.|table|chart|annex|appendix)\b", re.I)


def _worth_review(sentence: str, span) -> bool:
    """Relevance filter for unclassified evidence. Not a keyword list: the number must carry a unit (page numbers and figure
    references never do), the sentence must not be a caption, must be of reading size, and must contain a modal verb (must, shall, should, may...)
    AND say something about the number as read by docrag.semantics. Plain descriptions of values stay in skipped.json."""
    s = sentence.strip()
    if _CAPTION.match(s) or not 30 <= len(s) <= 500:
        return False
    if span is None:   # escort / hazardous cargo subject: no number, so the modal verb is the only evidence of a requirement or advice
        return strictness_of(s) is not None
    return strictness_of(s) is not None and SEM.analyse(s, span) is not None


EXAMPLE = re.compile(r"\be\.g\.|\bfor example\b|\bfor instance\b|\bin this example\b|\bas an example\b|\bexample:", re.I)
NOT_A_GAP = re.compile(r"ground contact|side by side|\baxles?\b|\btyres?\b|\btires?\b|\bwheels?\b", re.I)


def extract_pattern(chunk, prev_hint: str = "") -> tuple[list[Rule], list[dict]]:
    """One rule per number that carries a requirement, an advice, a threshold or a described value. What the number MEANS
    is read by docrag.semantics; only hard classes can leave as constraints (see contract.py)."""
    text = chunk.text
    rules: list[Rule] = []
    skipped: list[dict] = []

    def base(sentence, template, bound, value, unit, strict):
        at = text.find(sentence.strip()) if getattr(chunk, "breaks", None) else -1
        page = chunk.page_at(at) if at >= 0 else chunk.page    # a sentence in the part that came from the next page is cited there
        return Rule(template, bound, value, unit, strict, sentence.strip(), chunk.id, chunk.doc,
                    page, chunk.para, chunk.section)

    def skip(sent, reason):
        skipped.append({"chunk_id": chunk.id, "doc": chunk.doc, "page": chunk.page, "para": chunk.para,
                        "sentence": sent.strip(), "reason": reason})

    def unclassified(sent, why, value=None, unit=None):
        """A statement that looks relevant but whose meaning is not known. It is kept, visible and never exportable."""
        r = base(sent, "EVID.UNCLASSIFIED", "flag", value, unit, strictness_of(sent) or "none")
        r.semantic_class = SEM.UNCLASSIFIED
        hint = SEM.analyse(sent, None, flag=True) if value is None else None
        r.interpretation = [why] + ([f"wording hint only, not used: {hint.cls}"] if hint is not None else [])
        r.missing = ["meaning, scope and conditions not established: a person must read the PDF"]
        r.verify_against_pdf = True
        r.assign_id()
        rules.append(r)

    spans = sentences(text)
    for si, (a, b) in enumerate(spans):
        sent = text[a:b]
        before = text[spans[si - 1][0]:spans[si - 1][1]] if si > 0 else prev_hint
        if DEFN.match(sent) and not PROHIB.search(sent):  # "Class C roads are narrower than X ..." defines a class
            skip(sent, "class definition, not a limit (review)")
            continue
        cut = EXAMPLE.search(sent)
        if cut:   # numbers inside an example ("e.g., MLC 56.2 rounded to 56") are not rules
            core = sent[:cut.start()]
            if not quantities(core) and not MLC_NUM.search(core):
                if quantities(sent) or MLC_NUM.search(sent):
                    skip(sent, "numbers only inside an example, not a rule")
                continue
            sent = core.rstrip(" ,;(")
        qs = SEM.with_range_starts(sent, SEM.drop_conversions(sent, quantities(sent)))
        found_numeric = False
        orphans: list[dict] = []
        why: list[str] = []
        prev_end = 0
        for q in qs:
            window = sent[prev_end:q["end"]]
            after = sent[q["end"]:q["end"] + 14]
            prev_end = q["end"]
            tpl = pick_template(q["unit"], window)
            hinted = False
            if tpl is None and before and SEM.ANAPHORA.match(sent):
                # "This value (...30.5 m)": the kind of value is named in the sentence before. Used only to open a
                # candidate; the rule stays incomplete_context (it refers back) until a person completes it.
                tpl = pick_template(q["unit"], before + " " + window)
                hinted = tpl is not None
            if tpl is None:
                orphans.append(q)
                continue
            if tpl == "NET.GAP" and NOT_A_GAP.search(sent):   # spacing between wheels or contact points, not between vehicles
                continue
            rd = SEM.analyse(sent, (q["start"], q["end"]))
            if rd is None:
                why.append("number found, but no requirement, advice or description wording (not a requirement)")
                continue
            bound, notes = _direction(rd, tpl, sent, window, after, bool(EVERY.search(window)))
            if tpl == "SEG.TOWN" and PROHIB.search(sent):
                bound = "max"
            if hinted:
                notes = notes + ["the kind of value (" + tpl + ") comes from the previous sentence, not from the quote: check it"]
            rules.append(_finish(base(sent, tpl, bound, q["value"], q["unit"], "must"), sent, rd, notes if rd.cls in SEM.HARD else []))
            found_numeric = True
        for m in MLC_NUM.finditer(sent):
            rd = SEM.analyse(sent, (m.start(), m.end()))
            if rd is None:
                why.append("MLC number found, but no requirement, advice or description wording (not a requirement)")
                continue
            bound, notes = _direction(rd, "SEG.MLC", sent, sent[:m.end()], sent[m.end():m.end() + 14], False)
            rules.append(_finish(base(sent, "SEG.MLC", bound, float(m.group(1)), "MLC", "must"), sent, rd, notes if rd.cls in SEM.HARD else []))
            found_numeric = True
        flag_hit = False
        if ESCORT.search(sent) or (HAZ_NOUN.search(sent) and PROHIB.search(sent) and BUILT.search(sent)):
            rd = SEM.analyse(sent, None, flag=True)
            if rd is not None:
                if HAZ_NOUN.search(sent) and PROHIB.search(sent) and BUILT.search(sent) and not any(
                        r.template == "SEG.TOWN" for r in rules if r.quote == sent.strip()):
                    rules.append(_finish(base(sent, "PRO.HAZMAT", "flag", None, None, "must"), sent, rd))
                    flag_hit = True
                if ESCORT.search(sent):
                    rules.append(_finish(base(sent, "PRO.ESCORT", "flag", None, None, "must"), sent, rd))
                    flag_hit = True
        topic = bool(ESCORT.search(sent) or (HAZ_NOUN.search(sent) and PROHIB.search(sent) and BUILT.search(sent)))
        if qs or MLC_NUM.search(sent):
            if not found_numeric:
                kept = [o for o in orphans if _worth_review(sent, (o["start"], o["end"]))] if not why else []
                if kept:   # a measurement with a unit in a sentence that says something about it, but no template fits
                    seen_q = set()
                    for o in kept:
                        if (o["value"], o["unit"]) in seen_q or len(seen_q) >= 3:
                            continue
                        seen_q.add((o["value"], o["unit"]))
                        unclassified(sent, "a measurement is stated, but no rule template fits it and the meaning is not established",
                                     o["value"], o["unit"])
                else:
                    skip(sent, why[0] if why else "requirement with a number but no rule template matched")
        elif topic and not flag_hit and _worth_review(sent, None):   # escort or hazardous cargo is the subject, with a modal verb, but the wording could not be read
            unclassified(sent, "escort or hazardous cargo is the subject, but the wording could not be read as a requirement")
        elif topic and not flag_hit:   # not queued (no modal verb), but never silent: it stays findable in skipped.json
            skip(sent, "escort or hazardous cargo is the subject, but no requirement or advice wording (not queued as evidence)")
        elif SEM.analyse(sent, None, flag=True) is not None and not flag_hit and ref_keys(sent):
            skip(sent, "requirement points to " + ", ".join(ref_keys(sent)) + " (value elsewhere)")
        elif strictness_of(sent) is not None and not flag_hit and entities_in(sent) and not any(r.quote == sent.strip() for r in rules):
            skip(sent, "normative sentence, no rule template matched (review for missed rules)")
    return rules, skipped


# ---- checks (shared by pattern and LLM rules) ---------------------------------------------------

_RANGE_LABEL = re.compile(r"\b\d{1,3}\s*[-\u2013]\s*\d{1,3}\b(?=\s+\d+(?:[.,]\d+)?\s*[A-Za-z%])")
CONTEXT_NOTE = ("table row or several measurements in one text: the column header and row conditions are not tied to this "
                "value. Read them in the PDF and write them in edit_scope / edit_conditions")


def table_shape(quote: str) -> bool:
    """True if the quote looks like a flattened table row, or holds so many measurements that a value may be tied to
    the wrong condition: three or more different measurements, or a class range ("4-12") followed by a measurement."""
    qs = SEM.drop_conversions(clean(quote), quantities(clean(quote)))      # "12 KPH (8 MPH)" is one measurement
    return len({(round(q["value"], 6), q["unit"]) for q in qs}) >= 3 or bool(_RANGE_LABEL.search(clean(quote)))


def check_rule(rule: Rule, chunk_text: str) -> dict:
    quote_ok = match_key(rule.quote) in match_key(chunk_text)
    number_ok = None if rule.value is None else number_supported(rule.value, rule.quote)
    if rule.template == "SEG.MLC" and rule.value is not None:
        number_ok = number_supported(rule.value, rule.quote)
    modal_ok = strictness_of(rule.quote) is not None or bool(rule.semantic_class)
    return {"quote_in_source": quote_ok, "number_in_quote": number_ok, "modal_in_quote": modal_ok,
            "all_ok": bool(quote_ok and modal_ok and number_ok is not False)}


def _last_sentence(c) -> str:
    if c is None:
        return ""
    sp = sentences(c.text)
    return c.text[sp[-1][0]:sp[-1][1]].strip() if sp else ""


def attach_context(rules: list[Rule], chunks) -> None:
    """Evidence from outside the quote: neighbouring sentences and passages, the heading, page-level markers.
    The quote is never changed and nothing is inferred; `completeness` only names what cannot be established."""
    by_doc: dict[str, list] = {}
    for c in chunks:
        by_doc.setdefault(c.doc, []).append(c)
    pos_in_doc = {c.id: (c.doc, i) for d, cs in by_doc.items() for i, c in enumerate(cs)}
    by_id = {c.id: c for c in chunks}
    page_text: dict[tuple, str] = {}
    for c in chunks:
        page_text[(c.doc, c.page)] = page_text.get((c.doc, c.page), "") + " " + c.text
    marks = {k: SEM.page_markers(v) for k, v in page_text.items()}
    vocab = SEM.vocabulary(c.text for c in chunks)

    def near(c, step):
        d, i = pos_in_doc[c.id]
        j = i + step
        cs = by_doc[d]
        if 0 <= j < len(cs) and abs(cs[j].page - c.page) <= 1:
            return cs[j]
        return None

    for r in rules:
        c = by_id.get(r.chunk_id)
        if c is None or r.origin == "manual":
            continue
        spans = sentences(c.text)
        at = c.text.find(r.quote)
        i = next((k for k, (a, b) in enumerate(spans) if at >= 0 and a <= at < b), None)
        prev_c, next_c = near(c, -1), near(c, 1)
        prev_s = nxt_s = ""
        if i is not None:
            if i > 0:
                prev_s = c.text[spans[i - 1][0]:spans[i - 1][1]].strip()
            elif prev_c is not None:
                ps = sentences(prev_c.text)
                prev_s = prev_c.text[ps[-1][0]:ps[-1][1]].strip() if ps else ""
            if i + 1 < len(spans):
                nxt_s = c.text[spans[i + 1][0]:spans[i + 1][1]].strip()
            elif next_c is not None:
                ns = sentences(next_c.text)
                nxt_s = next_c.text[ns[0][0]:ns[0][1]].strip() if ns else ""
        lead = prev_s if prev_s.rstrip().endswith(":") else ""
        mk = marks.get((r.doc, r.page), [])
        r.context = {"previous_sentence": prev_s[:300], "next_sentence": nxt_s[:300], "heading": c.section or "",
                     "previous_passage": (prev_c.text[-240:] if prev_c else ""), "next_passage": (next_c.text[:240] if next_c else ""),
                     "page_markers": mk,
                     "note": "evidence for the reviewer, not part of the quotation and not applied to the rule"}
        if r.table_derived or getattr(c, "quality", "ok") == "table":
            r.context["table_header"] = "not recoverable from the text layer: read the column and row headers in the PDF"
        qpos = next((q["start"] for q in quantities(r.quote) if r.value is not None and abs(q["value"] - r.value) < 1e-9), None)
        issues = SEM.completeness(r.quote, prev_s, nxt_s, lead, mk, vocab, r.unit, qpos)
        if r.hard:
            covered = " ".join(list(r.conditions) + list(r.scope) + [r.applies_to]).lower()
            loose = [q for q in r.qualifiers if q.lower() not in covered]
            if loose:
                issues.append("the sentence limits the value with " + ", ".join(f"'{q}'" for q in loose[:3])
                              + ", which is not recorded as scope or condition: write it in edit_scope / edit_conditions")
        if r.hard and r.value is None and r.template.startswith("PRO.") and not r.scope and not r.conditions:
            # an escort or hazardous-cargo duty with nothing saying WHICH convoys, vehicles or cargo it applies to: a person writes it
            issues.append("escort / hazardous cargo duty without stated applicability (which convoys, vehicles or cargo): "
                          "write it in edit_scope / edit_conditions")
        if issues:
            r.context_status, r.context_issues = SEM.INCOMPLETE, issues
            r.verify_against_pdf = True


def extract_chunks(chunks, llm=None, log: list | None = None, reliability: dict | None = None):
    """Pattern extraction on every chunk, plus the LLM extractor when a client is given.

    Returns (rules, skipped, problems). Pattern and LLM rules that agree are merged (origin both).
    """
    from .llm_extract import extract_llm  # local import: LLM part is optional

    rules: dict[tuple, Rule] = {}
    skipped: list[dict] = []
    problems: list[str] = []
    text_of = {c.id: c.text for c in chunks}
    prev_of: dict[str, object] = {}
    for i, c in enumerate(chunks):
        if i and chunks[i - 1].doc == c.doc and abs(chunks[i - 1].page - c.page) <= 1:
            prev_of[c.id] = chunks[i - 1]
    page_q = {(doc, row[0]): row[1] for doc, info in (reliability or {}).items() for row in info.get("page_flags", [])}
    for c in chunks:
        if page_q.get((c.doc, c.page)) == "graphic":   # symbol tables and figures: no rule is written there
            skipped.append({"chunk_id": c.id, "doc": c.doc, "page": c.page, "para": c.para,
                            "sentence": c.text[:200], "reason": "graphic page (symbols or figures), not extracted"})
            continue
        if getattr(c, "quality", "ok") == "table":   # flattened table: numbers lose their row and column
            skipped.append({"chunk_id": c.id, "doc": c.doc, "page": c.page, "para": c.para,
                            "sentence": c.text[:200], "reason": "table content, not extracted automatically (transcribe by hand)"})
            continue
        pr, sk = extract_pattern(c, _last_sentence(prev_of.get(c.id)))
        skipped += sk
        for r in pr:
            rules.setdefault(r.signature(), r)
        if llm is not None:
            lr, probs = extract_llm(c, llm)
            problems += probs
            for r in lr:
                key = r.signature()
                if key in rules:
                    rules[key].origin = "both"
                else:
                    rules[key] = r
    out = list(rules.values())
    quality_of = {c.id: getattr(c, "quality", "ok") for c in chunks}
    attach_context(out, chunks)
    for r in out:
        r.checks = check_rule(r, text_of[r.chunk_id])
        r.reliability, r.reliability_note, r.verify_against_pdf = rule_reliability(
            quality_of[r.chunk_id], (reliability or {}).get(r.doc), page_q.get((r.doc, r.page), "ok"))
        if r.context_status == SEM.INCOMPLETE:
            r.verify_against_pdf = True
        if table_shape(r.quote):   # readable text is not understood text: a person must read the table in the PDF
            r.table_derived = True
            r.verify_against_pdf = True
            if CONTEXT_NOTE not in r.missing:
                r.missing = list(r.missing) + [CONTEXT_NOTE]
    return out, skipped, problems


# ---- reconcile across documents -------------------------------------------------------------------

def _rank(doc: str, precedence: list[str]) -> int:
    for i, p in enumerate(precedence):
        if p.lower() in doc.lower():
            return i
    return len(precedence)


def reconcile(rules: list[Rule], precedence: list[str] | None = None) -> tuple[list[Rule], list[dict]]:
    """Merge rules that state the same thing. Equal values corroborate; different values are conflicts.

    Conflict handling: max -> smaller value kept, min -> larger value kept (the stricter one);
    exact or relative values keep the document with the highest precedence. Every conflict is listed.
    """
    precedence = precedence or []
    for r in rules:
        r.assign_id()
    groups: dict[tuple, list[Rule]] = {}
    kind = {"m": "distance", "km": "distance", "ft": "distance", "min": "time", "h": "time", "s": "time",
            "km/h": "speed", "mph": "speed", "t": "mass", "%": "percent", "inhabitants": "population",
            "MLC": "mlc", None: "flag"}
    for r in rules:
        key = (r.template, r.bound, tuple(r.scope), tuple(r.conditions), kind.get(r.unit, r.unit))
        if r.value is None:
            # yes/no rules (escort, hazardous cargo) have no number to tell them apart: two different sentences with the same scope
            # are two different rules. Only the identical sentence (same wording in two documents) is merged.
            key += (match_key(r.quote),)
        if not r.scope and not r.conditions:
            # Nothing says what the value applies to, so two different values are not a conflict: they may concern
            # different things. Only identical values are merged (they corroborate each other).
            key += (r.value, r.unit, r.hard, r.applies_to.strip().lower())   # same number, different sentence subject: not the same rule
        groups.setdefault(key, []).append(r)
    kept: list[Rule] = []
    conflicts: list[dict] = []
    for key, rs in groups.items():
        rs = sorted(rs, key=lambda r: (_rank(r.doc, precedence), r.doc, r.page, r.para))
        values = {(r.value, r.unit) for r in rs}
        if len(values) == 1:
            main = rs[0]
            best = min(rs, key=lambda r: LEVELS.index(r.reliability))
            if best is not main and best.reliability != main.reliability:   # same value stated by a more reliable source
                main.reliability = best.reliability
                main.verify_against_pdf = best.verify_against_pdf or main.needs_context   # corroboration never lifts the table check
                main.reliability_note = f"same value also in {best.cite()} (reliability {best.reliability})"
        else:
            bound = key[1]
            comparable = all(r.value is not None for r in rs) and len({r.unit for r in rs}) == 1
            if comparable and bound == "max":
                main = min(rs, key=lambda r: r.value)
            elif comparable and bound == "min":
                main = max(rs, key=lambda r: r.value)
            else:
                main = rs[0]
            desc = "; ".join(f"{r.value} {r.unit or ''} ({r.cite()})".strip() for r in rs)
            main.conflict = f"values differ: {desc}. Stricter value: {main.value} {main.unit or ''} ({main.cite()})".strip()
            # Nothing is dropped: a candidate must not disappear because another one with the same scope has another
            # value. Each one stays, with the conflict written on it, and the reviewer decides.
            for r in rs:
                if r is not main:
                    r.conflict = main.conflict
                    kept.append(r)
            conflicts.append({"template": key[0], "scope": list(key[2]), "conditions": list(key[3]),
                              "values": [{"value": r.value, "unit": r.unit, "cite": r.cite()} for r in rs],
                              "kept": "all (none dropped); stricter: " + main.id})
        main.also_in = [r.cite() for r in rs if r is not main and (r.value, r.unit) == (main.value, main.unit)]
        kept.append(main)
    # Values without context stay as separate rules (nothing is dropped), but different values of the same kind are
    # still reported, as possible conflicts for the reviewer.
    loose: dict[tuple, list[Rule]] = {}
    for r in kept:
        if not r.scope and not r.conditions and r.value is not None and not r.template.startswith("EVID."):
            loose.setdefault((r.template, r.bound, kind.get(r.unit, r.unit)), []).append(r)
    for (template, bound, _), rs in loose.items():
        if len({(r.value, r.unit) for r in rs}) > 1:
            desc = "; ".join(f"{r.value} {r.unit or ''} ({r.cite()})".strip() for r in rs)
            for r in rs:
                if not r.conflict:
                    r.conflict = f"possible conflict, context not stated: {desc}"
            conflicts.append({"template": template, "scope": [], "conditions": [], "context": "unknown",
                              "values": [{"value": r.value, "unit": r.unit, "cite": r.cite()} for r in rs],
                              "kept": "all (none dropped)"})
    kept.sort(key=lambda r: (r.template.startswith("EVID."), r.doc, r.page, r.para, r.template, r.id))   # unclassified evidence last; id makes the order total
    return kept, conflicts