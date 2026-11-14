"""What does a number in a sentence MEAN? Read the wording around it before it becomes a planning rule.

A sentence with "must" and a number is not always a limit:
  "Halts of at least 30 minutes must be plotted"          the number says WHEN a reporting duty starts
  "Vehicles in excess of MLC 150 must be assessed"         the number says WHEN an assessment starts
  "The desirable minimum headroom is 4.50 m"               advice, not an obligation
  "Overhead clearance less than 4.3 m is considered an obstruction"   a classification threshold
  "Commanders must be aware that a convoy could be 10 minutes behind"  awareness, not a gap rule
So the reading looks at three things together: the force of the wording (obligation, prohibition, advice,
permission, description), what that wording is applied TO (a limit on the value itself, or some other action),
and the comparison the number takes part in (<, <=, >, >=, =).

Nothing here guesses a missing condition. When the sentence cannot say what it needs to say (it points back to an
earlier sentence, an exception follows, the page is about one country, the text is damaged), `completeness`
names the problem and the rule is marked incomplete_context.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

MANDATORY = "mandatory_constraint"
CONDITIONAL = "conditional_requirement"
RECOMMENDATION = "recommendation"
TRIGGER = "threshold_trigger"
DESCRIPTIVE = "descriptive_value"
AMBIGUOUS = "ambiguous_or_incomplete"
CLASSES = (MANDATORY, CONDITIONAL, RECOMMENDATION, TRIGGER, DESCRIPTIVE, AMBIGUOUS)
# A relevant-looking statement whose meaning nobody has established. Not one of the six meanings: a person gives it one
# (edit_class) after reading the PDF. It is never hard and never exported.
UNCLASSIFIED = "unclassified_evidence"
HARD = frozenset({MANDATORY, CONDITIONAL})          # the only classes that may leave as a constraint
OPERATORS = ("<", "<=", ">", ">=", "=")
PLAIN = {
    MANDATORY: "mandatory constraint", CONDITIONAL: "conditional requirement",
    RECOMMENDATION: "recommendation or normal practice", TRIGGER: "threshold that triggers an action or a class",
    DESCRIPTIVE: "descriptive value or equipment capability", AMBIGUOUS: "ambiguous or incomplete evidence",
    UNCLASSIFIED: "unclassified evidence (meaning not established)"}
COMPLETE, INCOMPLETE = "complete", "incomplete_context"


def is_hard(cls: str) -> bool:
    """'' is a rule written before classes existed (old files, hand-built test rules): treated as before."""
    return cls == "" or cls in HARD


# ---- comparison operator ---------------------------------------------------------------------------------------

# (pattern, operator, fixed, reach). fixed: the phrase already says its own direction. reach: how many words may sit
# between the phrase and the number ("maximum speed of 40": 'speed of' sits between).
_PHRASES = [
    (r"no\s+more\s+than|not\s+(?:to\s+)?exceed(?:ing)?|not\s+(?:more|greater|higher|taller|wider|longer|heavier)\s+than"
     r"|at\s+most|up\s+to|limited\s+to|not\s+over|not\s+above", "<=", True, 3),
    (r"maximum|max\.", "<=", True, 12),
    (r"no\s+less\s+than|not\s+(?:less|lower|smaller|shorter|narrower|fewer)\s+than|at\s+least|not\s+below|not\s+under", ">=", True, 3),
    (r"minimum|min\.", ">=", True, 12),
    (r"less\s+than|fewer\s+than|(?:narrower|lower|shorter|smaller)\s+than|short\s+of", "<", False, 3),
    (r"in\s+excess\s+of|exceed(?:s|ed|ing)?|more\s+than|greater\s+than|(?:wider|taller|higher|steeper|heavier|longer|larger)\s+than",
     ">", False, 3),
    (r"below|under", "<", False, 0),
    (r"above|over|beyond", ">", False, 0),
    (r"equal\s+to|exactly|set\s+at|established\s+at|fixed\s+at", "=", True, 1),
]
_PHRASES = [(re.compile(r"\b(?:" + p + r")(?![A-Za-z])", re.I), op, fixed, reach) for p, op, fixed, reach in _PHRASES]
_NEG = re.compile(r"\b(?:not|never|no)\b\s+(?:\w+\s+){0,2}$", re.I)
_AFTER_GE = re.compile(r"^\s*(?:\+|(?:or|and)\s+(?:more|greater|higher|above|over|longer|heavier|wider|taller)\b)", re.I)
_AFTER_LE = re.compile(r"^\s*(?:or|and)\s+(?:less|lower|below|under|shorter|smaller|fewer)\b", re.I)
_FILL = re.compile(r"^(?:\s+(?:an?|the|approximately|about|around|of|MLC|class)\b)*\s*$", re.I)
_INVERT = {"<": ">=", "<=": ">", ">": "<=", ">=": "<"}


def operator_of(sentence: str, start: int, end: int) -> tuple[str, bool, bool]:
    """(operator, fixed, stated) for the number at sentence[start:end]. stated=False: the text gives no direction ('=')."""
    before = sentence[max(0, start - 140):start]
    base = max(0, start - 140)
    best = None
    for pat, op, fixed, reach in _PHRASES:
        for m in pat.finditer(before):
            gap = before[m.end():]
            ok = bool(_FILL.match(gap)) if reach == 0 else bool(re.match(rf"^(?:\s+[\w\-/.()%]+){{0,{reach}}}\s*$", gap) or _FILL.match(gap))
            if not ok:
                continue
            rank = (len(gap), -(m.end() - m.start()))
            if best is None or rank < best[0]:
                best = (rank, op, fixed, base + m.start())
    after = sentence[end:end + 24]
    head = sentence[max(0, start - 40):start]
    if re.search(r"\b(?:between|from)\s+(?:an?\s+)?$", head, re.I):
        return ">=", True, True                  # "between 4 and 150": the lower end
    if re.search(r"\bbetween\b[^.;]{1,40}\band\s+(?:an?\s+)?$", head, re.I) or re.search(r"\bfrom\b[^.;]{1,40}\bto\s+(?:an?\s+)?$", head, re.I):
        return "<=", True, True                  # ... and the upper end
    if best is None:
        if _AFTER_GE.match(after):
            return ">=", True, True
        if _AFTER_LE.match(after):
            return "<=", True, True
        return "=", True, False
    _, op, fixed, pos = best
    if not fixed and _NEG.search(sentence[max(0, pos - 30):pos]):
        op = _INVERT[op]
    if op in (">", "<") and re.match(r"^\s*\+", after):
        op = ">="
    return op, fixed, True


# ---- force of the wording --------------------------------------------------------------------------------------

_CUES = [   # strongest first: when a clause has several, the first kind that is present governs
    ("aware", r"\b(?:be|is|are)\s+aware\b|\bbear\s+in\s+mind\b|\btake[sn]?\s+(?:into\s+)?account\b|\bnote\s+that\b"),
    ("classify", r"\b(?:is|are)\s+(?:also\s+)?(?:considered|classified|regarded|deemed|defined|termed|called|designated|known\s+as)\b"
                 r"|\bconstitutes?\b|\bcounts?\s+as\b"),
    ("prohibit", r"\b(?:shall|must|may)\s+not\b|\b(?:shall|must)\s+never\b|\bdo(?:es)?\s+not\s+(?:exceed|go|travel|drive|enter|cross|use|pass|stop|overtake|move|proceed)\b"
                 r"|\b(?:is|are)\s+(?:strictly\s+)?(?:prohibited|forbidden|not\s+(?:permitted|allowed|authori[sz]ed))\b|\bprohibited\b|\bforbidden\b|\bnever\b|\bavoid\w*"),
    ("oblige", r"\b(?:shall|must)\b|\b(?:is|are)\s+to\b|\b(?:is|are|be|been|being)\s+(?:also\s+|always\s+|normally\s+|strictly\s+)?(?:required|mandatory|imposed|compulsory|obligatory)\b"
               r"|\b(?:require[sd]?|requiring)\s+(?:that|to)\b|\b(?:has|have|need|needs)\s+to\b|\bonly\s+(?:with|if|when)\b"),
    ("authorize", r"\b(?:authori[sz]ed|permissible|admissible|allowed|permitted)\b"),
    ("recommend", r"\b(?:should|recommended|recommends?|ought|preferably|desirable|advisable|advised|suggested|preferred)\b"),
    ("practice", r"\b(?:normally|usually|generally|typically|customar(?:y|ily)|as\s+a\s+rule|in\s+general|where\s+possible|if\s+possible"
                 r"|whenever\s+possible|normal\s+practice|good\s+practice)\b"),
    ("permit", r"\b(?:may|optional|optionally)\b"),
    ("describe", r"\b(?:estimated|average|approximately|approx\.?|about|typical|capacity|payload|capable|able\s+to|can\s+(?:carry|reach|travel|attain|be)"
                 r"|will\s+be\s+slowed)\b"),
]
_CUES = [(k, re.compile(p, re.I)) for k, p in _CUES]
_RELATIVE = re.compile(r"\b(?:which|that|who|whom)\s+(?:\w+\s+)?$", re.I)
_STRENGTH = {"aware": "none", "classify": "none", "prohibit": "prohibition", "oblige": "obligation",
             "authorize": "authorization", "recommend": "recommendation", "practice": "recommendation",
             "permit": "permission", "describe": "none"}
_RANK = {k: i for i, (k, _) in enumerate(_CUES)}

# actions that use a number only to say WHEN they apply, versus verbs that put a requirement on the value itself
_ACTION = re.compile(r"\b(?:plot(?:ted|s)?|report(?:ed|s)?|record(?:ed|s)?|logged?|mark(?:ed|s)?|signposted|indicat(?:ed|es?)|display(?:ed)?"
                     r"|assess(?:ed|ment)?|analy[sz]ed?|inspect(?:ed)?|check(?:ed)?|verif(?:y|ied)|appl(?:y|ied)|assign(?:ed)?"
                     r"|notif(?:y|ied)|inform(?:ed)?|request(?:ed)?|obtain(?:ed)?|document(?:ed)?|calculat(?:e|ed)|determin(?:e|ed)"
                     r"|evaluat(?:e|ed)|review(?:ed)?|survey(?:ed)?|examin(?:e|ed)|test(?:ed)?|submit(?:ted)?|issu(?:e|ed)|approv(?:e|ed)"
                     r"|consider(?:ed)?|classif(?:y|ied)|treat(?:ed)?|carry|carries|bear|bears|fit(?:ted)?|equip(?:ped)?|escorted)\b", re.I)
_ACTION_FLAG = re.compile(_ACTION.pattern[:-3] + r"|provid(?:e|ed)|furnish(?:ed)?)\b", re.I)
_KEEP = re.compile(r"\b(?:maintain\w*|keep\w*|observ\w*|respect\w*|compl(?:y|ies|ied|ying)|remain\w*|limit(?:ed|s)?|restrict\w*|reduc\w+"
                   r"|travel\w*|driv(?:e|es|en|ing)|mov(?:e|es|ed|ing)|proceed\w*|spaced|separated|apart|stay\w*)\b", re.I)
_SUB = re.compile(r"\b(?:if|when|whenever|where|unless|once|provided\s+that|in\s+case|as\s+soon\s+as|whose)\b", re.I)
_SITUATION = re.compile(r"\b(?:crossings?|halts?|discipline|formations?|operations?|night|fog|rain|snow|expressways?|motorways?|built-up|convoys?\s+(?:movement|discipline))\b", re.I)
_INHERIT = re.compile(r"^\W*(?:that\s+is|namely|i\.e\.|viz\.|these\s+are|which\s+is)\b", re.I)


@dataclass
class Reading:
    cls: str
    operator: str = ""
    direction_stated: bool = True
    strength: str = "none"
    cue: str = ""
    applies_to: str = ""
    effect: str = ""
    role: str = ""
    notes: list[str] = field(default_factory=list)
    quals: list[str] = field(default_factory=list)


def _clauses(sentence: str) -> list[tuple[int, int]]:
    out, start = [], 0
    for m in re.finditer(r";", sentence):
        out.append((start, m.start()))
        start = m.end()
    out.append((start, len(sentence)))
    return [(a, b) for a, b in out if sentence[a:b].strip()]


def _cues_in(text: str):
    found = []
    for kind, pat in _CUES:
        for m in pat.finditer(text):
            if kind in ("oblige", "authorize") and _RELATIVE.search(text[:m.start()]):
                continue          # "vehicles which are required to ..." is not the rule's own verb
            found.append((kind, m.start(), m.end(), m.group(0)))
    return found


_QUAL = re.compile(r"\b(?:for|on|in|during|across|at)\s+(?:the\s+|a\s+|an\s+|all\s+|each\s+|any\s+)?[\w\-]+(?:\s+[\w\-]+){0,3}?"
                   r"(?=\s*[,.;:()]|\s+(?:and|or|if|when|which|that|unless|but|with|is|are|must|shall|should)\b|$)", re.I)
_NOT_QUAL = re.compile(r"^(?:for|on|in|at)\s+(?:at\s+least|least|most|the\s+case|case|order|general|particular|addition|accordance|this)", re.I)


def _qualifiers(clause: str, qs: tuple[int, int]) -> list[str]:
    """Phrases such as 'for two-way traffic' or 'on expressways', word for word. Shown so a person can check them."""
    out = []
    for m in _QUAL.finditer(clause):
        if m.start() < qs[1] and m.end() > qs[0]:
            continue
        t = m.group(0).strip()
        if _NOT_QUAL.match(t) or re.search(r"\d", t) or len(t) < 8:
            continue
        out.append(t)
    return out[:3]


def _trim(s: str, n: int = 110) -> str:
    s = re.sub(r"\s+", " ", s).strip(" ,.;:")
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def analyse(sentence: str, span: tuple[int, int] | None, flag: bool = False) -> Reading | None:
    """Meaning of the number at sentence[span] (or of the whole sentence for flag rules without a number).
    None: nothing in the wording makes it a requirement, advice or description worth keeping."""
    pos = span[0] if span else 0
    clauses = _clauses(sentence)
    ci = next((i for i, (a, b) in enumerate(clauses) if a <= pos <= b), 0)
    a, b = clauses[ci]
    clause = sentence[a:b]
    qs = (span[0] - a, span[1] - a) if span else (0, 0)
    cues = _cues_in(clause)
    inherited_from = None
    if not cues and ci > 0 and _INHERIT.match(clause):
        pa, pb = clauses[ci - 1]
        prev = sentence[pa:pb]
        pc = _cues_in(prev)
        if pc:
            inherited_from, cues = prev, [(k, -1, -1, t) for k, _, _, t in pc]
    notes: list[str] = []
    if not cues:
        if _CUES[-1][1].search(clause) and span:
            op, fixed, stated = operator_of(sentence, *span)
            return Reading(DESCRIPTIVE, op, stated, "none", "", "", "", "description",
                           ["describes a value (average, estimated, capacity ...); no requirement wording"])
        return None
    # the governing cue: strongest kind; among equals, the first one in front of the number
    best_kind = min((c[0] for c in cues), key=lambda k: _RANK[k])
    same = [c for c in cues if c[0] == best_kind]
    cue = next((c for c in same if c[2] <= qs[0]), same[0])
    kind, cs, ce, ctext = cue
    if kind in ("oblige", "prohibit", "authorize") and cs > 0:
        # "normally are to last", "usually must": a hedge right in front of the modal weakens it to normal practice
        hedge = re.search(r"\b(?:normally|usually|generally|typically|ordinarily)\s+(?:\w+\s+)?$", clause[:cs], re.I)
        if hedge:
            kind, ctext = "practice", hedge.group(0).split()[0] + " " + ctext
    hard_kinds = ("prohibit", "oblige", "authorize", "classify", "recommend", "practice", "permit", "aware")
    first = lambda text: min((c[1] for c in _cues_in(text) if c[0] in hard_kinds), default=0)
    reading_subject = inherited_from if inherited_from is not None else clause
    applies_to = _trim(reading_subject[:first(reading_subject)], 130)
    if not applies_to and ci > 0:       # "...; do not exceed 5 KPH": the subject was named in the clause before
        pa, pb = clauses[ci - 1]
        applies_to = _trim(sentence[pa:pb][:first(sentence[pa:pb])], 130)
    strength = _STRENGTH[kind]
    base = dict(strength=strength, cue=ctext, applies_to=applies_to)
    if flag:
        window = clause[max(ce, 0):max(ce, 0) + 70]      # what comes AFTER the modal verb
        act = _ACTION_FLAG.search(window)
        if kind in ("oblige", "prohibit", "authorize") and not act:
            return Reading(MANDATORY, "", True, role="limit", notes=[f"'{ctext}' applies to the requirement itself"], **base)
        if kind in ("recommend", "practice", "permit"):
            return Reading(RECOMMENDATION, role="advice", notes=[f"'{ctext}' is advice or permission, not an obligation"], **base)
        return Reading(AMBIGUOUS, role="unclear", notes=[f"'{ctext}' applies to an action ({act.group(0) if act else 'unclear'}), "
                                                     "not clearly to the requirement"], **base)
    op, fixed, stated = operator_of(sentence, *span)
    before_cue = cs >= 0 and qs[1] <= cs
    sub = None
    for m in _SUB.finditer(clause[:qs[0]]):
        sub = m
    in_cond = False
    if sub is not None:
        between = [c for c in cues if c[1] >= 0 and c[1] >= sub.end() and c[2] <= qs[0]]
        in_cond = not between
    vwin = clause[:max(qs[1], (ce if ce >= 0 else 0) + 45)]
    action = _ACTION.search(vwin)
    keeps = _KEEP.search(vwin)
    dist = (cs - qs[1]) if cs >= qs[1] else (qs[0] - ce) if cs >= 0 else 0
    table_like = len(re.findall(r"\d+(?:[.,]\d+)?", clause)) >= 6      # a lead-in followed by a table: stays, but is blocked later
    far = cs >= 0 and dist > 160 and not (cs < qs[0] and table_like)
    if far:
        return None            # the cue is so far from the number that it cannot govern it (a data listing)
    # --- decision -------------------------------------------------------------------------------------------
    quals = [q for q in _qualifiers(clause, qs) if clause.find(q) >= qs[1]]       # phrases AFTER the number ("for two-way traffic")
    if quals:
        notes.append("qualifiers in the sentence (check that scope/conditions reflect them): " + "; ".join(quals))
    if kind == "aware":
        return Reading(DESCRIPTIVE, op, stated, role="awareness",
                       notes=[f"'{ctext}': the sentence asks to be aware of a fact; the number is not a limit"], **base)
    if kind == "classify":
        eff = _trim(clause[ce:]) if ce >= 0 else ""
        return Reading(TRIGGER, op, stated, role="classification", effect=eff,
                       notes=[f"'{ctext}': the number is a threshold that defines a class or label, not a limit on movement"], **base)
    if kind in ("recommend", "practice", "permit"):
        n = [f"'{ctext}': advice or normal practice, not an obligation"]
        if in_cond or (before_cue and action):
            n.append("the number is the condition under which the advised action applies")
        return Reading(RECOMMENDATION, op, stated, role="advice", notes=n, **base)
    if kind == "describe":
        return Reading(DESCRIPTIVE, op, stated, role="description", notes=[f"'{ctext}': a description, not a requirement"], **base)
    # obligation, prohibition or authorisation
    if kind == "prohibit" and before_cue and not fixed and op != "=" and stated and not in_cond:
        op = _INVERT[op]            # "vehicles above 40 t shall not use it" forbids the range above 40: the limit is <= 40
        notes.append("the number names the forbidden range, so the limit is its opposite")
    if in_cond:
        eff = _trim(clause[cs:sub.start()]) if (cs >= 0 and cs < sub.start()) else _trim(clause[qs[1]:])
        return Reading(TRIGGER, op, stated, role="condition", effect=eff,
                       notes=[f"the number sits in a condition ('{sub.group(0)} ...'); it says when another provision applies"], **base)
    subset = (kind in ("oblige", "authorize") and before_cue and stated and not fixed and not keeps)
    if subset and not action:
        # "Vehicles over 3 t must carry a class number": a positive duty for the vehicles the number singles out
        return Reading(TRIGGER, op, stated, role="subject", effect=_trim(clause[ce:]),
                       notes=[f"'{ctext}' is a duty for the subjects the number singles out; the number says who it applies to"], quals=quals, **base)
    if action and not keeps:
        eff = _trim(clause[cs:qs[0]] if (cs >= 0 and not before_cue) else clause[(ce if ce >= 0 else 0):])
        return Reading(TRIGGER, op, stated, role="action", effect=eff,
                       notes=[f"'{ctext}' applies to an action ('{action.group(0)}'), not to the value: the number says when it applies"], **base)
    noun_of = bool(re.search(r"\b(?:of|at|is|are|to)\s*$", clause[max(0, qs[0] - 8):qs[0]]))
    if stated or keeps or noun_of:
        if not stated:
            notes.append("no comparison words: the value is stated, its direction comes from the template")
        cls = CONDITIONAL if _SITUATION.search(applies_to) else MANDATORY
        return Reading(cls, op, stated, role="limit", notes=[f"'{ctext}' applies to the value itself"] + notes, quals=quals, **base)
    return Reading(AMBIGUOUS, op, stated, role="unclear",
                   notes=[f"'{ctext}' found, but the sentence does not show that the number is a limit"], **base)


_RANGE_START = re.compile(r"(?:between|from)\s+(?P<num>\d+(?:[.,]\d+)?)\s+(?:and|to)\s+$", re.I)


def with_range_starts(sentence: str, qs: list[dict]) -> list[dict]:
    """'between 30 and 50 km/h': the 30 carries no unit of its own; it takes the unit of the 50."""
    from .text import parse_number
    out: list[dict] = []
    for q in qs:
        m = _RANGE_START.search(sentence[max(0, q["start"] - 30):q["start"]])
        if m and not any(o["end"] > q["start"] - 30 and o["unit"] == q["unit"] and o["end"] <= q["start"] - len(m.group(0)) + len(m.group("num")) for o in out):
            off = max(0, q["start"] - 30) + m.start("num")
            out.append({"value": parse_number(m.group("num")), "unit": q["unit"], "start": off, "end": off + len(m.group("num")),
                        "raw": m.group("num")})
        out.append(q)
    return out


# ---- conversions: "30.5 meters (100 feet)" is one measurement -----------------------------------------------------

_DIM = {"m": "len", "ft": "len", "km": "len", "km/h": "spd", "mph": "spd", "t": "mass", "min": "time", "h": "time", "s": "time"}


def drop_conversions(sentence: str, qs: list[dict]) -> list[dict]:
    """Remove a number that only restates the previous one in another unit ('12 KPH (8 MPH)', '30.5 m (100 ft)')."""
    out: list[dict] = []
    for q in qs:
        if out:
            p = out[-1]
            gap = sentence[p["end"]:q["start"]]
            if re.fullmatch(r"\s*[(\[/]\s*|\s*(?:or|=)\s*", gap) and _DIM.get(p["unit"]) == _DIM.get(q["unit"]) and p["unit"] != q["unit"]:
                continue
        out.append(q)
    return out


# ---- completeness -----------------------------------------------------------------------------------------------

ANAPHORA = re.compile(r"^\W*(?:this|these|that|those|such|it|they|the\s+latter|the\s+former|the\s+same)\b", re.I)
_EXCEPTION = re.compile(r"^\W*(?:however|exception|except|unless|otherwise|if\s+not|in\s+(?:this|that|such\s+a)\s+case)\b"
                        r"|\b(?:does|do)\s+not\s+apply\b|\bnot\s+applicable\b|\bexempt\w*", re.I)
_COUNTRY = re.compile(
    r"Special\s+Movement\s+Regulations\s+in\s+\w+|\bCountry\s*:\s*\w+"
    r"|\b(?:French|German|Belgian|Dutch|Italian|British|Spanish|Danish|Norwegian|Greek|Turkish|Canadian|Polish|Czech|Hungarian|Portuguese)\s+"
    r"(?:regulations?|law|laws|authorit\w+|rules?|territory)\b"
    r"|\b(?:in|of)\s+(?:France|Germany|Belgium|the\s+Netherlands|Italy|the\s+United\s+Kingdom|Spain|Denmark|Norway|Greece|Turkey|Canada|Poland|Luxembourg)\b",
    re.I)
_CAPTION = re.compile(r"\b(?:Figure|Table|Fig\.)\s+\d", re.I)
_DIST_NOUN = re.compile(r"\b(?:distance|spacing|width|length|height|gap|interval)\s+(?:is\s+)?(?:less\s+than\s+|more\s+than\s+)?$", re.I)


def page_markers(text: str) -> list[str]:
    return sorted({m.group(0) for m in _COUNTRY.finditer(text)})


def vocabulary(texts) -> Counter:
    c: Counter = Counter()
    for t in texts:
        c.update(w for w in re.findall(r"[a-z]{2,}", t.lower()))
    return c


def damaged_words(text: str, vocab: Counter) -> list[str]:
    """Words cut in two by the text layer ('brid ges'): both halves are rare in the whole corpus, their join is common."""
    words = re.findall(r"[A-Za-z]+", text)
    out = []
    for a, b in zip(words, words[1:]):
        j = (a + b).lower()
        if len(a) >= 2 and len(b) >= 2 and vocab.get(j, 0) >= 5 and vocab.get(a.lower(), 0) <= 2 and vocab.get(b.lower(), 0) <= 3:
            out.append(f"{a} {b}")
    return out


def completeness(quote: str, previous: str, nxt: str, lead_in: str, markers: list[str], vocab: Counter | None,
                 unit: str | None = None, value_pos: int | None = None) -> list[str]:
    """Problems that stop the quote alone from being the whole rule. Empty list: nothing found (not a proof)."""
    issues: list[str] = []
    if ANAPHORA.match(quote):
        issues.append("refers back: the quote starts with '" + " ".join(quote.split()[:3]) + "...'; what it refers to is in an earlier sentence")
    if quote[:1].islower():
        issues.append("the quote starts in the middle of a sentence")
    if lead_in:
        issues.append("list item: the applicability is stated in the lead-in '" + _trim(lead_in, 90) + "'")
    if nxt and _EXCEPTION.search(nxt):
        issues.append("the next sentence may state an exception: '" + _trim(nxt, 90) + "'")
    if previous and _EXCEPTION.search(previous) and not previous.rstrip().endswith("."):
        issues.append("the previous sentence may limit this one: '" + _trim(previous, 90) + "'")
    own = {m.group(0).lower() for m in _COUNTRY.finditer(quote)}
    if markers and not own:
        issues.append("country-specific page (" + "; ".join(markers[:3]) + "): the quote does not say which country it concerns")
    row = re.match(r"^\s*([A-Z]{3,}(?:\s+[A-Z]{3,})*)\s+[A-Z][a-z]+\s*:", quote)
    if row:
        issues.append(f"the quote is a table row labelled '{row.group(1)}': the label names the country or state it applies to")
    if len(own) >= 2 or _CAPTION.search(quote):
        issues.append("the quote joins separate provisions (figure or table caption, or several countries)")
    if vocab:
        bad = damaged_words(quote, vocab)
        if bad:
            issues.append("damaged text in the quote: " + ", ".join(f"'{x}'" for x in bad[:3]))
    if unit in ("t", "kg") and value_pos is not None and _DIST_NOUN.search(quote[max(0, value_pos - 30):value_pos]):
        issues.append("the unit does not fit the noun in front of the number (a distance given in tonnes)")
    return issues