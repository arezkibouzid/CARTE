"""Knowledge graph over chunks: domain entities, cross-references and neighbours.

Nodes   : chunks ("c<n>") and entities ("e:<name>").
Edges   : chunk -mentions-> entity (and back), chunk -ref-> chunk (cross-reference), chunk <-next-> chunk.
The entity lexicon is domain knowledge (synonyms of movement-doctrine terms). It is what lets a query
such as "headway between convoys" reach a passage that says "time gap between convoys".
Edit LEXICON to adapt it when the real documents arrive.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

import networkx as nx

LEXICON: dict[str, list[str]] = {
    # vehicles and cargo
    "heavy vehicle": [r"heavy (?:goods )?vehicles?", r"heavy trucks?", r"hgv", r"heavy equipment transporters?",
                      r"tank transporters?", r"\bhet\b"],
    "tanker": [r"tankers?", r"fuel (?:trucks?|bowsers?)"],
    "ambulance": [r"ambulances?", r"medical vehicles?"],
    "floating bridge": [r"floating bridges?", r"ribbon bridges?", r"pont flottant\w*", r"rafts?"],
    "convoy": [r"convoys?", r"columns?", r"serials?", r"march units?"],
    "hazardous cargo": [r"hazardous", r"dangerous goods", r"explosives?", r"ammunition", r"\bpol\b"],
    # infrastructure
    "bridge": [r"bridges?", r"viaducts?"],
    "tunnel": [r"tunnels?"],
    "level crossing": [r"level crossings?"],
    "ford": [r"fords?"],
    "built-up area": [r"built-up", r"urban areas?", r"towns?", r"villages?", r"cit(?:y|ies)", r"inhabitants",
                      r"populated areas?"],
    "junction": [r"junctions?", r"crossroads?", r"intersections?"],
    "road class": [r"road class(?:es)?", r"route class(?:es|ification)?", r"class [a-d] roads?"],
    # quantities and concepts
    "speed": [r"speed", r"km/h", r"velocity", r"rate of march"],
    "gap": [r"\bgaps?\b", r"spacing", r"intervals?", r"headway",
            r"distance between (?:vehicles|serials|convoys?)", r"time gaps?", r"following distance",
            r"vehicle intervals?"],
    "halt": [r"halts?", r"rest stops?", r"rest periods?", r"\bbreaks?\b", r"\bstops?\b"],
    "width": [r"width", r"\bwide\b", r"narrow\w*", r"lanes?"],
    "height": [r"height", r"clearance", r"headroom", r"overhead", r"taller", r"higher than"],
    "weight": [r"weight", r"load class", r"\bmlc\b", r"axle load", r"tonnes?", r"heavier"],
    "turn radius": [r"turn(?:ing)? radius", r"curvature", r"radius", r"sharp turns?", r"hairpin"],
    "gradient": [r"gradients?", r"slopes?", r"steep\w*", r"inclines?"],
    "escort": [r"escort\w*", r"force protection"],
    "traffic control": [r"traffic control", r"\btcps?\b", r"movement control"],
    "incident": [r"incidents?", r"ambush\w*", r"breakdowns?", r"road closures?", r"destroyed", r"obstruction"],
    "separation": [r"separated", r"\bapart\b", r"disjoint", r"not intersect", r"alternate routes?",
                   r"alternative routes?", r"\basr\b", r"\bmsr\b", r"main supply routes?", r"itinerar\w+"],
    "night": [r"night", r"darkness", r"blackout"],
    "weather": [r"rain", r"\bice\b", r"snow", r"\bfog\b", r"flood\w*", r"visibility"],
}
VEHICLE_GROUP = ["heavy vehicle", "tanker", "ambulance", "floating bridge", "convoy", "hazardous cargo"]

_COMPILED = {k: re.compile(r"\b(?:" + "|".join(v) + r")\b", re.I) for k, v in LEXICON.items()}
_DOCREF = re.compile(r"\b(?:STANAG\s*\d{3,4}|AMovP-\d+|ATP-\d+(?:\.\d+)*|AJP-\d+(?:\.\d+)*)\b", re.I)
_REF = re.compile(
    r"\b(?P<kind>paragraphs?|para\.?|sections?|chapters?|annex(?:es)?|appendix|table)\s+"
    r"(?P<id>[A-Z]\b|\d+(?:\.\d+)*)", re.I)


def entities_in(text: str) -> set[str]:
    out = {name for name, rx in _COMPILED.items() if rx.search(text)}
    out |= {"doc:" + re.sub(r"\s+", "", m.group(0)).upper() for m in _DOCREF.finditer(text)}
    return out


def ref_keys(text: str) -> list[str]:
    keys = []
    for m in _REF.finditer(text):
        kind, ident = m.group("kind").lower(), m.group("id").lower()
        if kind.startswith("annex"):
            keys.append(f"annex {ident}")
        elif kind.startswith("appendix"):
            keys.append(f"appendix {ident}")
        elif kind.startswith("chapter"):
            keys.append(f"chapter {ident}")
        else:
            keys.append(ident)
    return keys


class DocGraph:
    W_ENTITY, W_REF_OUT, W_REF_IN, W_NEXT = 0.5, 0.6, 0.3, 0.25

    def __init__(self, chunks):
        self.chunks = chunks
        self.G = nx.DiGraph()
        self.ents: dict[int, set[str]] = {}
        self._build()

    def _build(self):
        G, chunks = self.G, self.chunks
        sec_index: dict[str, list[int]] = defaultdict(list)
        for c in chunks:
            G.add_node(f"c{c.n}", kind="chunk")
            if c.section_id:
                sec_index[c.section_id.lower()].append(c.n)
                if c.label and c.label[0].isdigit():
                    sec_index[f"{c.section_id}.{c.label.strip('.')}".lower()].append(c.n)
        df: dict[str, int] = defaultdict(int)
        for c in chunks:
            es = entities_in(c.text)
            self.ents[c.n] = es
            for e in es:
                df[e] += 1
                G.add_edge(f"c{c.n}", f"e:{e}", kind="mentions")
                G.add_edge(f"e:{e}", f"c{c.n}", kind="mentions")
        N = max(1, len(chunks))
        self.idf = {e: math.log((N + 1) / (d + 1)) for e, d in df.items()}
        by_doc: dict[str, list] = defaultdict(list)
        for c in chunks:
            by_doc[c.doc].append(c)
        for cs in by_doc.values():
            for a, b in zip(cs, cs[1:]):
                if a.section_id == b.section_id:
                    G.add_edge(f"c{a.n}", f"c{b.n}", kind="next")
                    G.add_edge(f"c{b.n}", f"c{a.n}", kind="next")
        for c in chunks:
            for key in ref_keys(c.text):
                for t in sec_index.get(key, []):
                    t_chunk = chunks[t]
                    if t_chunk.doc == c.doc and t != c.n:
                        G.add_edge(f"c{c.n}", f"c{t}", kind="ref")

    def stats(self) -> dict:
        kinds = defaultdict(int)
        for _, _, d in self.G.edges(data=True):
            kinds[d["kind"]] += 1
        ent_nodes = sum(1 for n in self.G if n.startswith("e:"))
        return {"chunks": len(self.chunks), "entities": ent_nodes, **{f"edges_{k}": v for k, v in kinds.items()}}

    def expand(self, seeds: dict[int, float], query: str, top_entity: int = 25):
        """Return {chunk n: (extra score, [reasons])} from query entities, references and neighbours."""
        add: dict[int, float] = defaultdict(float)
        why: dict[int, list[str]] = defaultdict(list)
        qents = [e for e in entities_in(query) if f"e:{e}" in self.G]
        if qents:
            norm = sum(self.idf.get(e, 0.0) for e in qents) or 1.0
            cand: dict[int, float] = defaultdict(float)
            shared: dict[int, list[str]] = defaultdict(list)
            for e in qents:
                for nb in self.G.successors(f"e:{e}"):
                    n = int(nb[1:])
                    cand[n] += self.idf.get(e, 0.0)
                    shared[n].append(e)
            for n, s in sorted(cand.items(), key=lambda x: -x[1])[:top_entity]:
                add[n] += self.W_ENTITY * s / norm
                why[n].append("entity link: " + ", ".join(sorted(shared[n])))
        for n, s in seeds.items():
            node = f"c{n}"
            for nb in self.G.successors(node):
                if not nb.startswith("c"):
                    continue
                kind = self.G[node][nb]["kind"]
                m = int(nb[1:])
                if kind == "next":
                    add[m] += self.W_NEXT * s
                    why[m].append(f"neighbour of {self.chunks[n].id}")
                elif kind == "ref":
                    add[m] += self.W_REF_OUT * s
                    why[m].append(f"referenced by {self.chunks[n].id}")
            for nb in self.G.predecessors(node):
                if nb.startswith("c") and self.G[nb][node]["kind"] == "ref":
                    m = int(nb[1:])
                    add[m] += self.W_REF_IN * s
                    why[m].append(f"refers to {self.chunks[n].id}")
        return {n: (v, why[n]) for n, v in add.items()}
