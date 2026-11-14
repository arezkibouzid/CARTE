"""Dependencies between operation orders: which order amends, replaces, cancels or completes which, and what is in force.

An order often depends on another one: a FRAGO amends an OPORD, an annex completes it, a new order cancels an old one.
Sent side by side to the language model, their values look equal ("60 km/h" and "40 km/h"). Here the code decides:

1. **Links**, read from the extracted fragments: each order's own reference (« OPORD 7/2026 », « FRAGO 1 », « Annexe B »)
   and issue date (DTG « 141400Z NOV 26 » or a date), then the sentences that name another order with a relation
   (« modifie », « annule et remplace », « annule », « complète », a FRAGO or an annex header). The operator can add a
   missed link or reject a wrong one (``outputs/orders/order_links.json``).
2. **Items**: comparable values of the fragments (maximum speed, spacing, load, height, separation of routes, halts),
   with their fragment id and page.
3. **What is in force**, per item, over the selected orders and every order linked to them: a value is replaced by an
   order that amends / replaces it (directly or along the chain), and every value of a cancelled or replaced order is
   void. Two values left in the same group of linked orders are settled by the later issue date when both are dated,
   otherwise reported as a **conflict** for the command to settle. Orders that are not linked at all are independent:
   their values are listed per order, not merged.

The result feeds the prompts (one block, ahead of the orders' fragments), the checks (an answer that gives a replaced
value as current is rejected) and the interface (chain, values in force with their history, conflicts).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

from interface.backend import orders as O

LINKS_FILE = O.DATA / "order_links.json"
_lock = threading.Lock()
_cache: dict = {"key": None, "headers": None}

KIND = r"(OPORD|OPLAN|FRAGO(?:RD)?|ANNEXE?|ORDRE D['’]OP[ÉE]RATION|ORDRE DE MOUVEMENT|ORDRE|MOVEMENT ORDER|OPERATION ORDER)"
REF = re.compile(KIND + r"\s*(?:N[°o]\.?\s*)?([A-Z]?\d{1,3}(?:/\d{2,4})?|[A-Z])\b", re.I)
REL = [  # most specific first
    ("replaces", re.compile(r"annule et remplace|remplace|supersed\w*|replac\w*", re.I)),
    ("cancels", re.compile(r"\bannul\w*|\bcancel\w*|rescind\w*", re.I)),
    ("amends", re.compile(r"\bmodifi\w*|\bamend\w*|\bmodif\w*|rectifi\w*", re.I)),
    ("complements", re.compile(r"compl[èée]t\w*|en compl[ée]ment|suppl[ée]ment\w*|further to|\bannexe?\s+(?:à|a|de|to)\b", re.I)),
]
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
MONTHS |= {"FEV": 2, "AVR": 4, "MAI": 5, "JUI": 6, "AOU": 8, "DEC": 12}
FR_MONTHS = {m: i for i, m in enumerate(["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
                                         "octobre", "novembre", "décembre"], 1)}
DTG = re.compile(r"\b(\d{2})(\d{2})(\d{2})\s?Z\s*([A-Z]{3})\w*\s*(\d{2,4})\b", re.I)
DATE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
DATE_FR = re.compile(r"\b(\d{1,2})\s+(" + "|".join(FR_MONTHS) + r")\s+(\d{4})\b", re.I)

ITEMS = {"speed_max": "vitesse maximale", "spacing": "espacement entre véhicules", "load_max": "charge maximale",
         "height_max": "hauteur maximale", "route_separation": "écart entre itinéraires", "halt_duration": "durée des haltes",
         "halt_interval": "intervalle entre haltes"}


def norm_ref(kind: str, num: str) -> str:
    k = kind.upper().replace("’", "'")
    k = {"FRAGORD": "FRAGO", "ANNEX": "ANNEXE", "OPERATION ORDER": "OPORD", "ORDRE D'OPÉRATION": "OPORD", "ORDRE D'OPERATION": "OPORD",
         "MOVEMENT ORDER": "ORDRE DE MOUVEMENT"}.get(k, k)
    return f"{k} {num.upper()}"


def _date(text: str) -> datetime | None:
    if (m := DTG.search(text)) and m.group(4).upper()[:3] in MONTHS:
        d, h, mi, mon, y = m.groups()
        y = int(y) + (2000 if len(y) == 2 else 0)
        try:
            return datetime(y, MONTHS[mon.upper()[:3]], int(d), int(h), int(mi))
        except ValueError:
            return None
    if m := DATE.search(text):
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    if m := DATE_FR.search(text):
        return datetime(int(m.group(3)), FR_MONTHS[m.group(2).lower()], int(m.group(1)))
    return None


# ----------------------------------------------------------------------------------------------------------- headers

def header(order_id: str) -> dict:
    """The order's own reference, kind, issue date, and the references it makes to other orders (with the relation)."""
    x = O.load_order(order_id)
    facts = O.facts_view(x)
    first = min((f["page"] for f in facts if f.get("page")), default=None)
    head_facts = [f for f in facts if f.get("page") == first][:6] or facts[:6]
    own = None
    for f in head_facts:
        m = REF.search(f["text"])
        if m:
            own = norm_ref(m.group(1), m.group(2))
            break
    date = next((d for f in head_facts if (d := _date(f["text"]))), None) or next((d for f in facts if (d := _date(f["text"]))), None)
    mentions = []
    for f in facts:
        refs = [norm_ref(m.group(1), m.group(2)) for m in REF.finditer(f["text"])]
        others = [r for r in refs if r != own]
        if not others:
            continue
        rel = next((name for name, rx in REL if rx.search(f["text"])), None)
        if rel is None and own:
            if own.startswith("FRAGO"):
                rel = "amends"  # a FRAGO that names an order modifies it
            elif own.startswith("ANNEXE"):
                rel = "complements"
        if rel:
            for r in dict.fromkeys(others):
                mentions.append({"ref": r, "relation": rel, "fact": f["id"], "page": f.get("page"), "text": f["text"][:200]})
    title = head_facts[0]["text"][:90] if head_facts else order_id
    return {"order": order_id, "ref": own, "kind": own.split()[0] if own else None, "date": date.isoformat() if date else None,
            "title": title, "mentions": mentions, "synthetic": bool((x.get("_amiad") or {}).get("synthetic"))}


def _key() -> tuple:
    files = sorted(O.STORE.glob("*.json"))
    return tuple((p.name, p.stat().st_mtime) for p in files) + ((LINKS_FILE.stat().st_mtime,) if LINKS_FILE.exists() else ())


def headers() -> dict[str, dict]:
    k = _key()
    with _lock:
        if _cache["key"] == k:
            return _cache["headers"]
    out = {}
    for o in O.list_orders():
        if o.get("extracted"):
            try:
                out[o["id"]] = header(o["id"])
            except (FileNotFoundError, ValueError, KeyError):
                continue
    with _lock:
        _cache.update(key=k, headers=out)
    return out


# ----------------------------------------------------------------------------------------------------------- links

def _manual() -> dict:
    if LINKS_FILE.exists():
        return json.loads(LINKS_FILE.read_text(encoding="utf-8"))
    return {"added": [], "rejected": []}


def set_link(src: str, dst: str, relation: str | None, action: str) -> dict:
    """``action`` add (relation required), reject (a detected link is wrong) or reset (back to what was detected)."""
    O._safe_id(src), O._safe_id(dst)
    if src == dst:
        raise ValueError("un ordre ne peut pas dépendre de lui-même")
    if action == "add" and relation not in ("amends", "replaces", "cancels", "complements"):
        raise ValueError("relation attendue : amends, replaces, cancels ou complements")
    m = _manual()
    m["added"] = [l for l in m["added"] if not (l["from"] == src and l["to"] == dst)]
    m["rejected"] = [l for l in m["rejected"] if not (l["from"] == src and l["to"] == dst)]
    if action == "add":
        m["added"].append({"from": src, "to": dst, "relation": relation, "at": datetime.now().isoformat(timespec="seconds")})
    elif action == "reject":
        m["rejected"].append({"from": src, "to": dst, "at": datetime.now().isoformat(timespec="seconds")})
    LINKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LINKS_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m


def links(hs: dict[str, dict] | None = None) -> list[dict]:
    """Every link between the stored orders: detected from the fragments, plus the operator's, minus the rejected."""
    hs = hs if hs is not None else headers()
    by_ref: dict[str, list[str]] = {}
    for oid, h in hs.items():
        if h["ref"]:
            by_ref.setdefault(h["ref"], []).append(oid)
    m = _manual()
    rejected = {(l["from"], l["to"]) for l in m["rejected"]}
    out: dict[tuple, dict] = {}
    for oid, h in hs.items():
        for men in h["mentions"]:
            for tgt in by_ref.get(men["ref"], []):
                if tgt != oid and (oid, tgt) not in rejected:
                    out.setdefault((oid, tgt), {"from": oid, "to": tgt, "relation": men["relation"], "source": "detected",
                                                "fact": men["fact"], "page": men["page"], "evidence": men["text"]})
    for l in m["added"]:
        out[(l["from"], l["to"])] = {"from": l["from"], "to": l["to"], "relation": l["relation"], "source": "operator",
                                     "fact": None, "page": None, "evidence": "lien ajouté par l’opérateur"}
    return list(out.values())


def closure(order_ids: list[str], ls: list[dict]) -> list[str]:
    """The selected orders and every order linked to them, in both directions, transitively."""
    seen, todo = list(dict.fromkeys(order_ids)), list(order_ids)
    while todo:
        o = todo.pop()
        for l in ls:
            for a, b in ((l["from"], l["to"]), (l["to"], l["from"])):
                if a == o and b not in seen:
                    seen.append(b)
                    todo.append(b)
    return seen


# ----------------------------------------------------------------------------------------------------------- items

def items_of(order_id: str) -> list[dict]:
    """Comparable values of the order's fragments: (item key, value, unit, printed text, fragment, page, scope)."""
    x = O.load_order(order_id)
    out = []
    for f in O.facts_view(x):
        if f["kind"] in ("ignore", "input", "other", "reference") or not f["quantities"]:
            continue
        t = f["text"].lower()
        scope = f.get("scope") or ("nuit" if re.search(r"\bnuit\b|\bnight\b", t) else None)
        for q in f["quantities"]:
            m = re.match(r"\s*([\d.,]+)\s*(.*)$", q)
            if not m:
                continue
            val, unit = float(m.group(1).replace(",", ".")), m.group(2).strip().lower()
            key = None
            if unit in ("km/h", "kmh", "km / h"):
                key = "speed_max"
            elif unit in ("t", "tonnes", "tonne", "tons"):
                key = "load_max"
            elif unit == "m" and re.search(r"hauteur|height|gabarit|clearance", t):
                key = "height_max"
            elif unit == "m" and (f.get("category") == "spacing" or re.search(r"espacement|spacing|intervalle|distance entre (?:les )?v[ée]hicules", t)):
                key = "spacing"
            elif unit == "km" and re.search(r"apart|[ée]cart|s[ée]par|distance", t):
                key = "route_separation"
            elif unit in ("min", "minutes") and re.search(r"halte|halt|pause|arr[êe]t", t):
                key = "halt_duration"
            elif unit in ("h", "heures", "hours") and re.search(r"halte|halt|pause|toutes les|every", t):
                key = "halt_interval"
            if key:
                out.append({"key": key + (f":{scope}" if scope else ""), "label": ITEMS[key] + (f" ({scope})" if scope else ""),
                            "value": val, "unit": unit, "printed": q, "order": order_id, "fact": f["id"], "page": f.get("page")})
    return out


# ----------------------------------------------------------------------------------------------------------- resolve

def resolve(order_ids: list[str]) -> dict:
    """Links, values in force with their history, conflicts and void orders over the selected orders and their chain."""
    hs = headers()
    order_ids = [o for o in order_ids if o in hs]
    ls_all = links(hs)
    ids = closure(order_ids, ls_all)
    added = [o for o in ids if o not in order_ids]
    ls = [l for l in ls_all if l["from"] in ids and l["to"] in ids]
    over = {(l["from"], l["to"]) for l in ls if l["relation"] in ("amends", "replaces", "cancels")}

    def overrides(a: str, b: str) -> bool:  # a path a -> ... -> b of amending links
        seen, todo = set(), [a]
        while todo:
            x = todo.pop()
            for s, t in over:
                if s == x and t not in seen:
                    if t == b:
                        return True
                    seen.add(t)
                    todo.append(t)
        return False

    void = {}  # order -> (by, relation)
    for l in ls:
        if l["relation"] in ("cancels", "replaces"):
            void[l["to"]] = (l["from"], l["relation"])
    comp: dict[str, int] = {}  # groups of linked orders
    for i, o in enumerate(ids):
        comp.setdefault(o, i)
    changed = True
    while changed:
        changed = False
        for l in ls:
            a, b = comp[l["from"]], comp[l["to"]]
            if a != b:
                lo = min(a, b)
                for k in comp:
                    if comp[k] in (a, b) and comp[k] != lo:
                        comp[k] = lo
                        changed = True
    items: dict[str, list[dict]] = {}
    for o in ids:
        try:
            for it in items_of(o):
                items.setdefault(it["key"], []).append(it)
        except (FileNotFoundError, ValueError):
            continue
    date = {o: hs[o]["date"] for o in ids}
    effective, conflicts, per_order = [], [], []
    for key, cands in items.items():
        alive = [c for c in cands if c["order"] not in void]
        dead = [c for c in cands if c["order"] in void]
        top = [c for c in alive if not any(overrides(d["order"], c["order"]) for d in alive if d["order"] != c["order"])]
        replaced = [c for c in alive if c not in top] + dead
        groups: dict[int, list[dict]] = {}
        for c in top:
            groups.setdefault(comp[c["order"]], []).append(c)
        for g, cs in groups.items():
            vals = {(c["value"], c["unit"]) for c in cs}
            hist = [c for c in replaced if comp[c["order"]] == g]
            if len(vals) == 1:
                effective.append({"key": key, "label": cs[0]["label"], "in_force": cs[0], "also_stated_by": cs[1:], "replaced": hist,
                                  "reason": "chaîne de modifications" if hist else "seule valeur"})
                continue
            dated = [c for c in cs if date.get(c["order"])]
            if len(dated) == len(cs) and len({date[c["order"]] for c in cs}) == len(cs) and len({c["order"] for c in cs}) == len(cs):
                cs2 = sorted(cs, key=lambda c: date[c["order"]])
                effective.append({"key": key, "label": cs2[-1]["label"], "in_force": cs2[-1], "also_stated_by": [],
                                  "replaced": hist + cs2[:-1], "reason": "ordre le plus récent (date d’émission)"})
            else:
                conflicts.append({"key": key, "label": cs[0]["label"], "values": cs,
                                  "why": "valeurs différentes dans des ordres liés (ou un même ordre), sans modification ni date pour trancher"})
    by_key: dict[str, list[dict]] = {}
    for e in effective:
        by_key.setdefault(e["key"], []).append(e)
    for key, es in by_key.items():  # unlinked orders with different values: independent, each for its own order
        if len({(e["in_force"]["value"], e["in_force"]["unit"]) for e in es}) > 1:
            per_order.append({"key": key, "label": es[0]["label"], "values": [e["in_force"] for e in es]})
    return {"selected": order_ids, "added": added, "orders": [hs[o] for o in ids], "links": ls,
            "void": [{"order": o, "by": b, "relation": r} for o, (b, r) in void.items()],
            "effective": effective, "conflicts": conflicts, "per_order": per_order}


REL_FR = {"amends": "modifie", "replaces": "annule et remplace", "cancels": "annule", "complements": "complète"}


def _name(hs: dict, o: str) -> str:
    h = hs.get(o) or {}
    return f"{o} ({h.get('ref') or 'sans référence'}{', ' + h['date'][:16].replace('T', ' ') if h.get('date') else ''})"


def context_block(res: dict) -> tuple[str, str]:
    """Full and short text for the prompts; empty when the orders have no link and nothing to settle."""
    if not res["links"] and not res["per_order"] and not res["conflicts"] and not res["void"]:
        return "", ""
    hs = {h["order"]: h for h in res["orders"]}
    L = ["Liens entre ordres (établis par le code ; à respecter) :"]
    for l in res["links"]:
        L.append(f"- {_name(hs, l['from'])} {REL_FR[l['relation']]} {_name(hs, l['to'])}"
                 + (f" (fragment {l['fact']}, p. {l['page']})" if l.get("fact") else " (lien ajouté par l’opérateur)"))
    for v in res["void"]:
        L.append(f"- {v['order']} n’est plus en vigueur ({REL_FR[v['relation']]} par {v['by']}).")
    changed = [e for e in res["effective"] if e["replaced"]]
    if changed:
        L.append("Valeurs en vigueur (à citer telles quelles ; les anciennes valeurs ne s’appliquent plus) :")
        for e in changed:
            c = e["in_force"]
            old = "; ".join(f"{r['printed']} ({r['order']}, {r['fact']})" for r in e["replaced"])
            L.append(f"- {e['label']} : {c['printed']} — {c['order']} ({c['fact']}, p. {c['page']})" + (f" ; remplace : {old}" if old else ""))
    if res["conflicts"]:
        L.append("Conflits non résolus (ne pas trancher : signaler au commandement) :")
        for k in res["conflicts"]:
            L.append(f"- {k['label']} : " + " / ".join(f"{c['printed']} ({c['order']}, {c['fact']})" for c in k["values"]))
    if res["per_order"]:
        L.append("Valeurs propres à des ordres indépendants (non liés, chacune pour son ordre) :")
        for k in res["per_order"]:
            L.append(f"- {k['label']} : " + " / ".join(f"{c['printed']} ({c['order']})" for c in k["values"]))
    full = "\n".join(L)
    short = "\n".join([l for l in L if not l.startswith("- ") or "remplace" in l or "Conflit" in l or "n’est plus" in l][:14])
    return full, short


def superseded(res: dict) -> list[dict]:
    """Values that are no longer in force (for the check): printed value, item, the value that replaces it."""
    out = []
    for e in res["effective"]:
        for r in e["replaced"]:
            if r["printed"] != e["in_force"]["printed"]:
                out.append({"printed": r["printed"], "label": e["label"], "in_force": e["in_force"]["printed"], "order": r["order"]})
    return out


OLD_WORDS = re.compile(r"remplac|ancien|initial|auparavant|pr[ée]c[ée]dent|modifi|n.est plus|ne s.applique plus|abrog|annul|was|previous|superseded|replaced", re.I)


def stale_problems(answer: str, res: dict) -> list[str]:
    """An answer that states a replaced value without saying it was replaced gives an outdated order as current."""
    probs = []
    for s in superseded(res):
        num = re.escape(s["printed"].split()[0])
        for m in re.finditer(rf"(?<![\d.,]){num}(?![\d.,])\s*{re.escape(' '.join(s['printed'].split()[1:]))}", answer, re.I):
            window = answer[max(0, m.start() - 120):m.end() + 120]
            if not OLD_WORDS.search(window):
                probs.append(f"{s['printed']} ({s['label']}) n’est plus en vigueur : la valeur actuelle est {s['in_force']}")
                break
    return probs


def fallback_lines(res: dict, question: str) -> list[str]:
    """Deterministic lines on what is in force, for the answer without a model (items the question mentions first)."""
    q = question.lower()
    rel = lambda lab: any(w in q for w in lab.split() if len(w) > 4)
    L = []
    for e in sorted(res["effective"], key=lambda e: not rel(e["label"])):
        if e["replaced"]:
            L.append(f"- {e['label']} en vigueur : {e['in_force']['printed']} ({e['in_force']['order']}, {e['in_force']['fact']}) ; "
                     f"remplace {', '.join(r['printed'] + ' (' + r['order'] + ')' for r in e['replaced'])}.")
    for k in res["conflicts"]:
        L.append(f"- Conflit à faire trancher : {k['label']} = " + " / ".join(f"{c['printed']} ({c['order']})" for c in k["values"]) + ".")
    for v in res["void"]:
        L.append(f"- {v['order']} n’est plus en vigueur ({REL_FR[v['relation']]} par {v['by']}).")
    return L
