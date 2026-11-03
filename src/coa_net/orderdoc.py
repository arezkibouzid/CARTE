"""S2 front end: a multimodal order document -> what it asks, with provenance, task routing and a readback.

The organisers' orders come as documents like ``exercise_for_preparation.pdf``:
a statement of the goal and the constraints, a list of desired outputs, and an
illustration drawn on the map (zones, start and finish points). The preliminary
description names the same three parts: "an illustration of a situation with
geographical objectives", "a set of constraints and a description of the goal",
"the desired outputs".

``read_order`` decomposes the document (``documents.py``) and labels every block:

* goal: the sentence that says what has to move where, or what to determine;
* constraint: the items under the goal, and sentences with a modal or a limit;
* output: the items under "Desired output" (or that name a file, a layer, a
  table); each is matched to the writer that produces it, or reported;
* context: the rest.

It then routes the task: ``itineraries`` (k itineraries between drawn zones,
``coa-net exercise``), ``movement`` (convoys with origins, destinations and
times, ``coa-net plan``) or ``unknown`` (left to the operator). The parsers
(pattern parsers, or the language model L2 with the same checks) read the text;
the illustration goes to the sketch reader (georeferencing and overlay marks by
code, mark naming by L9 or position words).

Two checks guard against silent errors: every constraint item must be
recognised by the parser, or it is listed as *not enforced*; and every number
the parser kept must appear in the document. Both go into the readback, the
one-page record of what was understood, which the operator confirms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .documents import Block, Document, Figure, match_key, numbers_in, read_document

TASK_ITINERARIES = "itineraries"
TASK_MOVEMENT = "movement"
TASK_UNKNOWN = "unknown"

_OUTPUT_HEAD = re.compile(r"^(desired|expected|required|requested)\s+outputs?\b|^outputs?\s*[:\-]|^deliverables?\b|^livrables?\b|"
                          r"^r[ée]sultats?\s+attendus|^productions?\s+attendues?|^produits?\s+attendus", re.I)
_OUTPUT_WORDS = re.compile(r"\b(layer|geojson|shapefile|kml|gpx|csv|file|fichier|table|tableau|map overlay|calque|couche|report|rapport)\b", re.I)
_GOAL = re.compile(r"\b(needs? to|has to|must)\s+(get|move|bring|deliver|transport|reach|go)\b|\bdetermine\b|\bfind\b|\bplan\b|"
                   r"\bacheminer\b|\bd[ée]terminer\b|\bdoit\s+(rejoindre|amener|acheminer|transporter)\b", re.I)
_CONSTRAINT = re.compile(r"\b(avoid|must|shall|should|do not|does not|cannot|never|no more than|at least|at most|maximum|minimum|"
                         r"only|before|after|prohibited|forbidden|within|apart|[ée]viter|interdit|doit|doivent|au moins|au plus|"
                         r"ne pas|ne doit|avant|apr[èe]s)\b", re.I)
_TASK_LINE = re.compile(r"^(convoy|convoi|serial|rame|march unit)\s+[\w-]+\s*:", re.I)
_INTENT_LINE = re.compile(r"^(commander'?s\s+)?(intent|intention)\s*:", re.I)
_ITINERARIES = [re.compile(p, re.I) for p in (  # in order of strength: the first that matches gives the reason
    r"\b(\d+|one|two|three|four|five|un|deux|trois|quatre|cinq)\s+(different\s+|distinct\s+)?(itinerar\w*|itin[ée]raires?|routes?\b)",
    r"\bitinerar\w*\s+(that|which)\s+link", r"\bfrom the \w+ zone to the \w+ zone\b")]
_MOVEMENT = re.compile(r"\b(convoy|convoi|serial|march unit|rame)\s+[\w-]+\s*:|\b(depart\w*|d[ée]part)\s*(at\s*|à\s*)?\d{1,2}[:h]\d{2}|"
                       r"\bmarch table\b|\btableau de marche\b|\border\s+[A-Z]{2,}-?\d+", re.I)

# requested outputs -> what produces them (task, regex, label, files)
DELIVERABLES = [
    (TASK_ITINERARIES, r"layer[^.;]*itinerar|itinerar[^.;]*layer|calque[^.;]*itin|couche[^.;]*itin", "itinerary layer",
     ["itineraries.geojson", "map_itineraries.png"]),
    (TASK_ITINERARIES, r"layer[^.;]*(maneuver|manoeuvr|difficult)|(difficult|maneuver|manoeuvr)[^.;]*layer|zones? difficiles",
     "difficult-manoeuvre layer", ["difficult_areas.geojson", "map_difficult_areas.png"]),
    (TASK_ITINERARIES, r"geojson[^.;]*(curvature|radius|turn)|(curvature|radius|rayon)[^.;]*geojson", "GeoJSON with turns and curvature radius",
     ["itineraries.geojson"]),
    (TASK_ITINERARIES, r"(text|texte)[^.;]*(decision|décision)|decision process|processus de d[ée]cision", "decision-process text",
     ["decision_process.txt"]),
    (TASK_MOVEMENT, r"march table|tableau de marche|timetable|horaires?", "march tables", ["report.html", "facts.md"]),
    (TASK_MOVEMENT, r"\bfrag(o|ord)s?\b|ordres? de conduite|fragmentary", "FRAGOs", ["fragos.txt"]),
    (TASK_MOVEMENT, r"courses? of action|\bcoas?\b|modes d'action", "COA comparison", ["report.html"]),
    (TASK_MOVEMENT, r"network|r[ée]seau|routes? layer|geojson", "route network layer", ["network.geojson", "report.html"]),
    (TASK_MOVEMENT, r"\breport\b|rapport|compte rendu", "planning report", ["report.html"]),
]


@dataclass
class OrderItem:
    role: str  # goal | task (a convoy line) | intent | constraint | output | context
    block: str
    text: str
    page: int


@dataclass
class Deliverable:
    label: str  # what COA-Net produces for it, or "" when nothing does
    text: str  # the order's words
    block: str
    files: list[str] = field(default_factory=list)

    @property
    def produced(self) -> bool:
        return bool(self.label)


@dataclass
class OrderReading:
    doc: Document
    task: str
    task_reason: str
    items: list[OrderItem]
    deliverables: list[Deliverable]
    illustration: Figure | None
    figures: list[Figure]
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The order's text in reading order, one item per line (input of the parsers)."""
        return "\n".join(i.text for i in self.items)

    def of(self, role: str) -> list[OrderItem]:
        return [i for i in self.items if i.role == role]


def _subtree(doc: Document, root: Block) -> list[Block]:
    out, frontier = [], [root.id]
    while frontier:
        kids = [b for b in doc.blocks if b.parent in frontier]
        out += kids
        frontier = [b.id for b in kids]
    return out


def read_order(path: str | Path | Document, ocr: bool = True) -> OrderReading:
    doc = path if isinstance(path, Document) else read_document(path, ocr=ocr)
    blocks = [b for b in doc.blocks if b.kind != "caption" or b.text]
    roles: dict[str, str] = {}
    # outputs: the subtree under an outputs heading or introducing item
    for b in blocks:
        if _OUTPUT_HEAD.search(b.text.strip(" -:")) and len(b.text.split()) <= 8:
            roles[b.id] = "output_head"
            for c in _subtree(doc, b):
                roles[c.id] = "output"
            if not _subtree(doc, b):  # heading without parent links (plain text): the following items
                i = blocks.index(b)
                for c in blocks[i + 1:]:
                    if c.kind == "heading" or (c.kind == "paragraph" and not c.bullet):
                        break
                    roles[c.id] = "output"
    for b in blocks:
        if b.id not in roles and _TASK_LINE.match(b.text):
            roles[b.id] = "task"
        elif b.id not in roles and _INTENT_LINE.match(b.text):
            roles[b.id] = "intent"
    goal = next((b for b in blocks if b.id not in roles and b.kind in ("paragraph", "item") and _GOAL.search(b.text)), None)
    if goal is not None:
        roles[goal.id] = "goal"
        for c in _subtree(doc, goal):
            roles.setdefault(c.id, "constraint")
    for b in blocks:
        if b.id in roles or b.kind == "heading":
            continue
        if b.kind in ("item", "paragraph", "table_row") and _CONSTRAINT.search(b.text):
            roles[b.id] = "constraint"
        elif b.kind == "item" and _OUTPUT_WORDS.search(b.text) and not any(v == "output_head" for v in roles.values()):
            roles[b.id] = "output"
    items = []
    for b in blocks:
        r = roles.get(b.id, "context")
        if r == "output_head":
            continue
        items.append(OrderItem(r, b.id, b.text, b.page))
    full = " ".join(b.text for b in blocks)
    m_it = next((m for m in (p.search(full) for p in _ITINERARIES) if m), None)
    m_mv = _MOVEMENT.search(full)
    if m_it and not m_mv:
        task, why = TASK_ITINERARIES, m_it.group(0)
    elif m_mv and not m_it:
        task, why = TASK_MOVEMENT, m_mv.group(0)
    elif m_it and m_mv:
        task, why = TASK_MOVEMENT, f"{m_mv.group(0)} (also asks for itineraries: {m_it.group(0)})"
    else:
        task, why = TASK_UNKNOWN, "no itinerary request and no convoy with times found"
    deliverables = []
    for it in items:
        if it.role != "output":
            continue
        hit = next((d for d in DELIVERABLES if re.search(d[1], it.text, re.I) and (d[0] == task or task == TASK_UNKNOWN)), None)
        deliverables.append(Deliverable(hit[2], it.text, it.block, list(hit[3])) if hit else Deliverable("", it.text, it.block))
    figs = sorted(doc.figures, key=lambda f: -f.shown_area)
    illustration = figs[0] if figs else None
    notes = []
    if illustration is not None and illustration.overlays:
        notes.append(f"{illustration.overlays} vector drawings lie over the illustration: read the rendered region, not only the image")
    if illustration is not None and illustration.rotated:
        notes.append("the illustration is shown turned by 90 degrees in the page (the sketch reader tries the four orientations)")
    if not doc.blocks:
        notes.append("no text in the document")
    return OrderReading(doc, task, why, items, deliverables, illustration, figs[1:], notes + list(doc.warnings))


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_numbers(values: list[tuple[str, float]], text: str) -> list[str]:
    """Labels of the values that do not appear in the text (a number the parser kept must come from the order)."""
    have = numbers_in(text)
    return [f"{label}: {v:g} is not in the document" for label, v in values if v is not None and not any(abs(v - h) < 1e-9 for h in have)]


def uncovered(items: list[OrderItem], recognised_quotes: list[str]) -> list[OrderItem]:
    """Constraint items that no recognised quote covers: they would be dropped silently, so they are reported."""
    keys = [match_key(q) for q in recognised_quotes if q]

    def covered(t: str) -> bool:
        k = match_key(t)
        if any(k in q or q in k for q in keys):
            return True
        words = set(re.findall(r"\w{4,}", k))
        return any(words and len(words & set(re.findall(r"\w{4,}", q))) >= 0.6 * len(words) for q in keys)

    return [i for i in items if i.role == "constraint" and not covered(i.text)]


# ---------------------------------------------------------------------------
# readback
# ---------------------------------------------------------------------------

def readback_markdown(reading: OrderReading, understood: list[tuple[str, str, str]], not_enforced: list[OrderItem],
                      number_problems: list[str], figure_rows: list[tuple[str, str, str]] | None = None,
                      assumptions: list[str] | None = None, title: str = "") -> str:
    """The one-page record of what was read, with the source of every item, for the operator to confirm."""
    d = reading.doc
    out = [f"# Order readback: {title or Path(d.path).name}", "",
           f"Read as: **{reading.task}** task, because the order says \"{reading.task_reason}\". Document: {d.format}, "
           f"{d.pages or 1} page(s), language {d.language}, {len(d.blocks)} text blocks, {len(d.figures)} figure(s); sha1 {d.sha1}.", "",
           "## What was understood", "", "| Item | Value | Source |", "| --- | --- | --- |"]
    for item, value, source in understood:
        out.append(f"| {item} | {value} | {source} |")
    if figure_rows:
        out += ["", "## Read on the illustration", "", "| Mark | Read as | How |", "| --- | --- | --- |"]
        out += [f"| {a} | {b} | {c} |" for a, b, c in figure_rows]
    out += ["", "## Desired outputs", ""]
    if reading.deliverables:
        out += [f"- {'produced' if x.produced else 'NOT PRODUCED'}: \"{x.text}\"" + (f" -> {x.label} ({', '.join(x.files)})" if x.produced else "")
                for x in reading.deliverables]
    else:
        out.append("- none listed in the order")
    out += ["", "## Not understood or not enforced", ""]
    if not_enforced:
        out += [f"- NOT ENFORCED: \"{i.text}\" ({i.block.split('#')[-1]}): no parser recognised it; apply it by hand or rephrase" for i in not_enforced]
    else:
        out.append("- every constraint of the order was recognised")
    out += ["", "## Numbers checked against the document", ""]
    out.append("- " + ("; ".join(number_problems) if number_problems else "every number kept by the parser appears in the document"))
    if assumptions:
        out += ["", "## Assumptions (not in the order)", ""] + [f"- {a}" for a in assumptions]
    if reading.notes:
        out += ["", "## Notes on the document", ""] + [f"- {n}" for n in reading.notes]
    out += ["", "Confirm, or correct the order and run again. Text blocks keep their page and position, so every item above "
            "can be found in the source."]
    return "\n".join(out) + "\n"


def reading_from_text(text: str, name: str = "order.txt") -> OrderReading:
    """An order given as plain text goes through the same reader (blocks, roles, outputs, checks)."""
    import hashlib

    from .documents import _read_text

    data = text.encode("utf-8")
    doc = _read_text(Path(name), Path(name).stem or "order", hashlib.sha1(data).hexdigest()[:12], data)
    from .documents import guess_language

    doc.language = guess_language(doc.text())
    return read_order(doc)


def exercise_readback(reading: OrderReading, spec, facts: dict | None = None) -> str:
    """Readback of an itineraries task: what parse_exercise kept, the marks read on the sketch, and the checks."""
    labels = {
        "number of itineraries": lambda: str(spec.k),
        "separation between itineraries": lambda: f"at least {spec.min_sep_m / 1000:g} km",
        "itineraries must not intersect": lambda: "yes" if spec.no_intersect else "no",
        "towns to avoid (population threshold)": lambda: f"more than {spec.min_town_pop:,} inhabitants" if spec.min_town_pop else "-",
        "convoy: motorised floating bridge (PFM)": lambda: f"{spec.n_vehicles} x {spec.vehicle} (count and type assumed)",
        "origin and destination zones": lambda: f"{spec.origin_zone} zone -> {spec.destination_zone} zone",
    }
    understood = [(item, labels.get(item, lambda: "recognised")(), cite(reading, q)) for item, q in spec.recognised
                  if not item.startswith("output:")]
    values = [("number of itineraries", float(spec.k)), ("separation (km)", spec.min_sep_m / 1000)]
    if spec.min_town_pop:
        values.append(("town population threshold", float(spec.min_town_pop)))
    rows = None
    if facts and facts.get("marks"):
        g = facts.get("georef", {})
        rows = [(f"mark {m['id']} ({m['colour']}, {m['area_km2']} km2)", f"{m['name'] or 'unnamed'}" + (f", {m['role']}" if m.get("role") else ""),
                 f"{m['named_by'] or 'not named'}; sketch georeferenced on {g.get('inliers', '?')} place names, median residual "
                 f"{g.get('median_residual_m', '?')} m") for m in facts["marks"]]
        rows += [("note", n, "sketch reader") for n in facts.get("mark_notes", [])]
    return readback_markdown(reading, understood, uncovered(reading.items, [q for _, q in spec.recognised]),
                             check_numbers(values, reading.doc.text()), rows, spec.assumptions)


def movement_readback(reading: OrderReading, mission) -> str:
    """Readback of a movement order: convoys, places, times and intent as parsed, with the checks."""
    from .schedule import hhmm

    understood, values = [], []
    for c in mission.convoys:
        veh = ", ".join(f"{v.count} x {v.type}" for v in c.vehicles)
        when = f"depart {hhmm(c.earliest_departure_min)}" + (f", arrive before {hhmm(c.latest_arrival_min)}" if c.latest_arrival_min is not None else "")
        place = lambda loc: loc.name or (f"node {loc.node}" if loc.node is not None else f"{loc.xy}")  # noqa: E731
        b = next((x for x in reading.doc.blocks if re.search(rf"\b{re.escape(c.id)}\b", x.text)), None)
        src = (f"\"{b.text[:120]}\" (" + (f"p. {b.page}" if b.page else b.id.split("#")[-1]) + ")") if b else "not found in the document"
        understood.append((f"convoy {c.id}", f"{veh}; {place(c.origin)} -> {place(c.destination)}"
                           + (f" via {', '.join(place(v) for v in c.via)}" if c.via else "") + f"; {when}; priority {c.priority}", src))
        values += [(f"{c.id} vehicles", float(v.count)) for v in c.vehicles]
        values.append((f"{c.id} departure", float(c.earliest_departure_min)))
        if c.latest_arrival_min is not None:
            values.append((f"{c.id} arrival deadline", float(c.latest_arrival_min)))
    if mission.intent:
        understood.append(("intent", "kept verbatim for the priority order", cite(reading, mission.intent)))
    for loc in mission.avoid:
        understood.append(("place to avoid", loc.name or str(loc.xy), cite(reading, loc.name or "")))
    rec = [mission.intent or ""] + [x.text for x in reading.doc.blocks if any(re.search(rf"\b{re.escape(c.id)}\b", x.text) for c in mission.convoys)]
    rec += [f"avoid {loc.name}" for loc in mission.avoid if loc.name]
    amb = [f"ambiguity: {a}" for a in mission.ambiguities]
    return readback_markdown(reading, understood, uncovered(reading.items, rec), check_numbers(values, reading.doc.text()) + amb)


def cite(reading: OrderReading, quote: str) -> str:
    b = reading.doc.locate(quote)
    if b is None:
        return f"\"{quote[:120]}\" (not found in the document)"
    where = f"p. {b.page}" if b.page else b.id.split("#")[-1]
    return f"\"{quote[:120]}\" ({where})"


__all__ = ["DELIVERABLES", "Deliverable", "OrderItem", "OrderReading", "TASK_ITINERARIES", "TASK_MOVEMENT", "TASK_UNKNOWN",
           "check_numbers", "cite", "exercise_readback", "movement_readback", "numbers_in", "read_order", "reading_from_text",
           "readback_markdown", "uncovered"]
