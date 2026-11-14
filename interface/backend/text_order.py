"""Orders given as text, turned into a scenario the operator previews and applies.

    « De PD4 à PA4 par WP1 et WP3 dans n'importe quel ordre, le plus rapide, 60 km/h, éviter 2 km autour de WP2 »

Offline by design: a point is either a named point of the sheet's exercise PDF (PD1.., PA1.., WP1.., DA1..),
something drawn on the map (the point P clicked in hand mode, the departure, the arrival, waypoint n, zone to avoid n,
the selected rectangle), or coordinates written in the text (decimal degrees, or Lambert-93 metres). Place names ("Verdun") are listed as unresolved,
never guessed: there is no gazetteer on the machine and nothing is sent outside.

Two parsers fill the same structure:
- the local language model, the one that explains the orders, with grammar-constrained decoding: each point is an id
  taken from a closed list (the named points, and the coordinates the code found in the text: C1, C2, ...), so the
  model never writes a position; every number it returns must appear in the text;
- rules (regular expressions), used without the model or when the model's answer fails the checks.

Nothing is applied here: the result is a patch (scenario + options) and a list of changes for the interface's preview.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata

NAMED = re.compile(r"\b(PD|PA|WP|DA)\s?(\d{1,2})\b", re.I)
DEC = r"-?\d{1,3}(?:\.\d+)"
COORD = re.compile(rf"({DEC})\s*°?\s*([NSEOW])?\s*[,;/ ]\s*({DEC})\s*°?\s*([NSEOW])?(?![\d.])", re.I)
L93 = re.compile(r"(?<![\d.])(\d{6,7}(?:\.\d+)?)\s*[,;/ ]\s*(\d{6,7}(?:\.\d+)?)(?![\d.])")
KM = re.compile(r"(\d+(?:[.,]\d+)?)\s*km(?!\s*/?\s*h)", re.I)
SPEED = re.compile(r"(\d{2,3})\s*km\s*/?\s*h", re.I)
EXERCISE = re.compile(r"\bexerci[cs]e\s*(?:n[°o]\s*)?(\d{1,2})\b", re.I)
LAMBDA = re.compile(r"(?:λ|lambda|intensit[ée]\s+d[’'u]\s*(?:co[uû]t\s+d[’'])?incertitude)\s*(?:=|:|à|a|de)?\s*(\d+(?:[.,]\d+)?)", re.I)

CUES = [  # (role, pattern): the role of the points that follow, until the next cue
    ("avoid", r"[ée]vit\w*|contourn\w*|avoid\w*|sans passer (?:par|pr[eè]s de)|pas par|loin de|interdi\w*"),
    ("waypoint", r"en passant par|passant par|par|via|[ée]tapes?|through|passing|waypoints?"),
    ("start", r"depuis|partir de|d[ée]part(?: de| du| à| a)?|from|start(?:ing)? (?:at|from)|\bde|\bdu|\bd['’]"),
    ("end", r"jusqu['’]?(?:à|a|au)|vers|arriv[ée]e?(?: à| a| au)?|destination|\bto\b|\bà\b|\ba\b|\bau\b|\bva\b|\baller\b|\ballez\b|"
            r"rends[- ]toi|rejoin\w*|\bgo\b|drive"),
]
CUE_RE = re.compile("|".join(f"(?P<{r}>{p})" for r, p in CUES), re.I)
QUESTION_START = re.compile(r"^\s*(pourquoi|comment|quel(le)?s?|est[- ]ce|combien|explique|qu['’]|que\b|o[uù]\b|why|what|how|which|is\b|are\b|does\b|do\b|can you explain)", re.I)
COMMAND_START = re.compile(r"^\s*(va|aller|allez|rends[- ]toi|calcule\w*|planifie\w*|trace\w*|passe\w*|[ée]vite\w*|applique\w*|prends|charge\w*|utilise\w*|"
                           r"go|route|plan|avoid|use|load|peux[- ]tu (calculer|tracer|planifier|aller|passer)|"
                           r"itin[ée]raire|convoi|d[ée]part|de\s+(PD|PA|WP)|depuis)", re.I)
SYSTEM = (
    "Tu convertis un ordre de mouvement écrit en paramètres d'itinéraire pour un outil de planification.\n"
    "Règles : un point est TOUJOURS un identifiant de la liste fournie (points nommés et coordonnées repérées dans le texte) ; "
    "n'invente jamais de position. Un nom de lieu absent de la liste va dans unresolved_places, mot pour mot. "
    "Mets 'unspecified' (ou 0, ou 'none') pour tout ce que l'ordre ne dit pas. speed_kmh, radius_km et exercise ne peuvent être "
    "que des nombres écrits dans l'ordre. objective = 'time' pour « le plus rapide », 'length' pour « le plus court »."
)


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def named_points(exercises: list[dict]) -> dict[str, tuple[float, float]]:
    """Every named point of the sheet's exercise PDF (names are unique across its exercises)."""
    out: dict[str, tuple[float, float]] = {}
    for e in exercises:
        for k, v in e.get("points", {}).items():
            out.setdefault(k.upper(), (float(v[0]), float(v[1])))
    return out


def find_coordinates(text: str) -> list[dict]:
    """Coordinates written in the text, in order: decimal degrees (lat/lon in either order, N/E letters optional) or
    Lambert-93 metres. Each gets an id C1, C2, ... (the only way the model can refer to a position)."""
    found = []
    for m in COORD.finditer(text):
        a, ha, b, hb = float(m.group(1)), (m.group(2) or "").upper(), float(m.group(3)), (m.group(4) or "").upper()
        lon = lat = None
        if ha in "NS" and ha and hb in "EOW":
            lat, lon = a, b
        elif ha in "EOW" and ha and hb in "NS" and hb:
            lon, lat = a, b
        elif 41 <= a <= 52 and -6 <= b <= 10:
            lat, lon = a, b
        elif 41 <= b <= 52 and -6 <= a <= 10:
            lon, lat = a, b
        if lat is None:
            continue
        if ha == "S":
            lat = -lat
        if hb in ("O", "W"):
            lon = -lon
        found.append({"span": m.span(), "text": m.group(0).strip(), "lonlat": (round(lon, 6), round(lat, 6))})
    for m in L93.finditer(text):
        if any(s <= m.start() < e for (s, e) in (f["span"] for f in found)):
            continue
        a, b = float(m.group(1)), float(m.group(2))
        x, y = (a, b) if 1e5 <= a <= 1.3e6 and 6e6 <= b <= 7.2e6 else (b, a) if 1e5 <= b <= 1.3e6 and 6e6 <= a <= 7.2e6 else (None, None)
        if x is None:
            continue
        from pyproj import Transformer

        lon, lat = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True).transform(x, y)
        found.append({"span": m.span(), "text": m.group(0).strip(), "lonlat": (round(lon, 6), round(lat, 6)), "lambert93": True})
    found.sort(key=lambda f: f["span"][0])
    for i, f in enumerate(found, 1):
        f["id"] = f"C{i}"
    return found


_NOT_CUE = r"(?!\s*(?:de|du|d['’]|à|a|au|en|:)?\s*(?:(?:PD|PA|WP|DA)\s?\d|-?\d{1,3}\.\d|\d{6,7}|ici\b))"
MAP_REFS = [  # (kind, regex with a "core" group, the span that names the object; words before it stay cues)
    ("P", r"\b(?P<core>ici|ce point|cet endroit|(?:le\s+)?point\s+(?:P\b|marqu[ée]|point[ée]|cliqu[ée])|here|this point|the (?:marked|clicked) point)"),
    ("D", rf"\b(?:le|du|au|mon|ton)\s+(?P<core>(?:point\s+de\s+)?d[ée]part(?:\s+(?:actuel|plac[ée]|de la carte|sur la carte))?)\b{_NOT_CUE}"),
    ("D", r"\b(?P<core>d[ée]part\s+(?:actuel|plac[ée]|de la carte|sur la carte)|the (?:current )?start)\b"),
    ("A", rf"\b(?P<core>(?:l['’]\s*|mon\s+|ton\s+)arriv[ée]e(?:\s+(?:actuelle|plac[ée]e|de la carte|sur la carte))?){_NOT_CUE}"),
    ("A", r"\b(?P<core>arriv[ée]e\s+(?:actuelle|plac[ée]e|de la carte|sur la carte)|the (?:current )?(?:end|arrival))\b"),
    ("E", r"\b(?:l['’]\s*)?(?P<core>[ée]tape\s+(?:n[°o]\s*)?(?P<n>\d{1,2})|waypoint\s+(?P<n2>\d{1,2}))\b"),
    ("Z", r"\b(?P<core>zone(?:\s+[àa]\s+[ée]viter)?\s+(?:n[°o]\s*)?(?P<n>\d{1,2})|Z(?P<n2>\d{1,2}))\b"),
    ("R", r"\b(?P<core>rectangle(?:\s+(?:dessin[ée]|trac[ée]|s[ée]lectionn[ée]))?|s[ée]lection)\b"),
]
MAP_LABEL = {"P": "point P cliqué sur la carte", "D": "départ placé sur la carte", "A": "arrivée placée sur la carte",
             "E": "étape {n} de la carte", "Z": "zone à éviter {n} de la carte", "R": "rectangle sélectionné sur la carte"}
MAP_MISSING = {"P": "point P (cliquez d’abord sur la carte, outil main)", "D": "départ (pas encore placé sur la carte)",
               "A": "arrivée (pas encore placée sur la carte)", "E": "étape {n} (absente de la carte)",
               "Z": "zone à éviter {n} (absente de la carte)", "R": "rectangle (aucune sélection sur la carte)"}


def _centroid(ring) -> tuple[float, float]:
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return (round(sum(xs) / len(xs), 6), round(sum(ys) / len(ys), 6))


def map_refs(text: str, ctx: dict | None) -> tuple[list[dict], list[str]]:
    """What the order names on the map: tokens with an id (P, D, A, E2, Z1, R), a position and, for zones, a polygon.
    Returns (found, missing): a reference to something that is not on the map is reported, not guessed."""
    ctx = ctx or {}
    rect = ctx.get("rect")
    rect_ring = [[rect[0], rect[1]], [rect[2], rect[1]], [rect[2], rect[3]], [rect[0], rect[3]]] if rect else None
    found, missing, taken = [], [], []
    for kind, rx in MAP_REFS:
        for m in re.finditer(rx, text, re.I):
            a, b = m.span("core")
            if any(a < e and s < b for s, e in taken):
                continue
            n = int(m.groupdict().get("n") or m.groupdict().get("n2") or 0)
            ident = f"{kind}{n}" if kind in "EZ" else kind
            lonlat, poly = None, None
            if kind == "P":
                lonlat = ctx.get("pointed")
            elif kind == "D":
                lonlat = ctx.get("start")
            elif kind == "A":
                lonlat = ctx.get("end")
            elif kind == "E":
                w = ctx.get("waypoints") or []
                lonlat = w[n - 1] if 0 < n <= len(w) else None
            elif kind == "Z":
                z = ctx.get("avoid") or []
                poly = z[n - 1] if 0 < n <= len(z) else None
                lonlat = _centroid(poly) if poly else None
            elif kind == "R":
                poly = rect_ring
                lonlat = _centroid(poly) if poly else None
            taken.append((a, b))
            if lonlat is None:
                missing.append(MAP_MISSING[kind].format(n=n))
                continue
            found.append({"pos": a, "end": b, "id": ident, "text": m.group("core"), "lonlat": (float(lonlat[0]), float(lonlat[1])),
                          "polygon": poly, "label": MAP_LABEL[kind].format(n=n)})
    found.sort(key=lambda f: f["pos"])
    return found, list(dict.fromkeys(missing))


CONTINUE = re.compile(r"\b(?:puis|ensuite|apr[eè]s (?:ça|cela|quoi)|then|next|continu\w*|poursui\w*|repart\w*|[ée]tape suivante|next leg)\b", re.I)
HIST_FORBID = re.compile(r"sans (?:re)?passer par|sans reprendre|ne (?:pas )?(?:re)?passer par|sans repasser|pas (?:par )?le m[êe]me (?:chemin|trajet|itin[ée]raire)|"
                         r"par (?:un|une) autre (?:chemin|route|itin[ée]raire)|without (?:re)?using|not the same (?:road|path|route)|"
                         r"interdi\w* (?:le|les) (?:trajet|chemin)s? pr[ée]c[ée]dent|"
                         r"[ée]vit\w*(?: si possible| de pr[ée]f[ée]rence)? (?:le|les|l['’]) ?(?:trajet|chemin|itin[ée]raire)s? (?:pr[ée]c[ée]dent|de l['’]aller|d[ée]j[àa])", re.I)
HIST_PENAL = re.compile(r"(?:si possible|de pr[ée]f[ée]rence|autant que possible)\D{0,40}(?:trajet|chemin|itin[ée]raire)|"
                        r"(?:trajet|chemin)s? pr[ée]c[ée]dents?\D{0,30}(?:si possible|de pr[ée]f[ée]rence)|if possible", re.I)
HIST_FREE = re.compile(r"(?:par )?le m[êe]me (?:chemin|trajet|itin[ée]raire)|en repassant par|same (?:road|path|route)", re.I)


def history_of(t: str) -> tuple[bool, str]:
    """« puis va à C sans repasser par le trajet précédent »: continue from the previous arrival, and what to do with the
    earlier legs (forbid by default when it is said, penalize when « si possible », free for « par le même chemin »)."""
    cont = bool(CONTINUE.search(t))
    if HIST_PENAL.search(t) and (HIST_FORBID.search(t) or re.search(r"pr[ée]c[ée]dent|aller|d[ée]j[àa]", t, re.I)):
        return cont, "penalize"
    if HIST_FORBID.search(t):
        return cont, "forbid"
    if HIST_FREE.search(t) and not re.search(r"pas (?:par )?le m[êe]me", t, re.I):
        return cont, "free"
    return cont, "unspecified"


def looks_like_command(text: str) -> bool:
    """For the chat: an instruction to change the scenario, not a question about the route."""
    t = _norm(text).strip()
    has_target = bool(NAMED.search(t) or find_coordinates(t) or EXERCISE.search(t) or any(re.search(rx, t, re.I) for _, rx in MAP_REFS))
    has_setting = bool(SPEED.search(t) or re.search(r"plus (rapide|court)|fastest|shortest|aller[- ]retour|round[- ]trip|[ée]vit|avoid", t, re.I)
                       or HIST_FORBID.search(t))
    if not (has_target or has_setting):
        return False
    if COMMAND_START.search(t):
        return True
    return not (t.endswith("?") or QUESTION_START.search(t))


# ---------------------------------------------------------------------------------------------------------- rule parser

def _tokens(text: str, names: dict, coords: list[dict], mrefs: list[dict] | None = None) -> list[dict]:
    toks = [{"pos": m.start(), "end": m.end(), "id": (m.group(1) + m.group(2)).upper(), "text": m.group(0)} for m in NAMED.finditer(text)
            if (m.group(1) + m.group(2)).upper() in names]
    toks += [{"pos": c["span"][0], "end": c["span"][1], "id": c["id"], "text": c["text"]} for c in coords]
    for r in mrefs or []:
        if not any(r["pos"] < k["end"] and k["pos"] < r["end"] for k in toks):
            toks.append({"pos": r["pos"], "end": r["end"], "id": r["id"], "text": r["text"]})
    return sorted(toks, key=lambda t: t["pos"])


RECT_AREA = re.compile(r"(?:dans|à l['’]int[ée]rieur d[ue]|inside|within|limit[ée]e? (?:au|à la))\s+(?:le |la |du |de la )?(?:rectangle|s[ée]lection)"
                       r"|(?:rectangle|s[ée]lection)\s+(?:comme|=|en|pour)\s+(?:la\s+)?zone de d[ée]ploiement", re.I)


def parse_rules(text: str, names: dict, coords: list[dict], mrefs: list[dict] | None = None) -> dict:
    t = _norm(text)
    out: dict = {"start": "none", "end": "none", "waypoints": [], "waypoint_order": "unspecified", "round_trip": "unspecified",
                 "objective": "unspecified", "speed_kmh": 0, "avoid": [], "deployment_area": "unspecified", "exercise": 0,
                 "lambda": -1.0, "uncertainty_cost": "unspecified", "area_ref": "none", "unresolved_places": []}
    if RECT_AREA.search(t) and any(r["id"] == "R" for r in mrefs or []):
        out["area_ref"] = "R"
    toks = [k for k in _tokens(t, names, coords, mrefs) if not k["id"].startswith("DA") and not (k["id"] == "R" and out["area_ref"] == "R")]
    roles: list[tuple[dict, str | None]] = []
    for i, k in enumerate(toks):
        prev_end = toks[i - 1]["end"] if i else 0
        window = re.sub(r"(autour|pr[eè]s|proche|loin|around|near)\s+(de|du|d['’]|of)?", " ", t[prev_end:k["pos"]], flags=re.I)
        cues = [m.lastgroup for m in CUE_RE.finditer(window)]
        cue = "avoid" if "avoid" in cues else (cues[-1] if cues else None)
        roles.append((k, cue, bool(re.fullmatch(r"\s*(?:[,;/&]|\bet\b|\band\b|\bou\b|\bor\b|\s)*\s*", window))))
    role = None
    for k, cue, in_list in roles:  # a cue keeps its role along a list ("par WP1, WP2 et WP3"), not past other words
        role = cue or (role if in_list else None)
        if role == "avoid":
            seg = t[max(0, k["pos"] - 40):k["end"] + 25]
            km = KM.findall(seg)
            out["avoid"].append({"point": k["id"], "radius_km": float(km[-1].replace(",", ".")) if km else 0.0})
        elif role == "waypoint":
            out["waypoints"].append(k["id"])
        elif role == "start" and out["start"] == "none":
            out["start"] = k["id"]
        elif role == "end":
            out["end"] = k["id"]
        else:
            out.setdefault("_free", []).append(k["id"])
    free = out.pop("_free", [])
    out["waypoints"] += [f for f in free if re.fullmatch(r"E\d+", f)]  # « l'étape 2 » without a cue is a waypoint
    free = [f for f in free if not re.fullmatch(r"E\d+", f)]
    if len(free) == 1 and out["start"] == "none" and re.match(r"^\s*(va|aller|allez|rends[- ]toi|rejoin\w*|go|drive)\b", t, re.I):
        out["end"] = free.pop()  # « va ici » : a destination
    if free:  # points without a cue: first = departure, last = arrival, the rest waypoints
        if out["start"] == "none":
            out["start"] = free.pop(0)
        if free and out["end"] == "none":
            out["end"] = free.pop()
        out["waypoints"] += free
    if re.search(r"(n['’]importe quel|tout|meilleur|libre)\s*ordre|ordre\s+(libre|quelconque|indiff[ée]rent)|any order|free order|best order", t, re.I):
        out["waypoint_order"] = "free"
    elif re.search(r"dans (cet|l['’]|le m[êe]me)\s*ordre|ordre (fixe|impos[ée])|in (this|that|the given) order", t, re.I):
        out["waypoint_order"] = "fixed"
    if re.search(r"aller[- ]retour|round[- ]trip|et retour|(?:puis|then) (?:revenir|retour|back)", t, re.I):
        out["round_trip"] = "yes"
    elif re.search(r"aller simple|sans retour|one[- ]way", t, re.I):
        out["round_trip"] = "no"
    if re.search(r"plus rapide|le plus vite|au plus vite|rapidement|fastest|quickest|minimi\w+ (le|la) (temps|dur[ée]e)", t, re.I):
        out["objective"] = "time"
    elif re.search(r"plus court|shortest|distance minimale|minimi\w+ (la )?distance", t, re.I):
        out["objective"] = "length"
    if (m := SPEED.search(t)):
        out["speed_kmh"] = int(m.group(1))
    if (m := EXERCISE.search(t)):
        out["exercise"] = int(m.group(1))
    if re.search(r"zone de d[ée]ploiement|deployment area|\bDA\d", t, re.I):
        out["deployment_area"] = "no" if re.search(r"(sans|hors|ignore\w*|without)\s+(la\s+)?zone de d[ée]ploiement", t, re.I) else "yes"
    if (m := LAMBDA.search(t)):
        out["lambda"] = float(m.group(1).replace(",", "."))
    if re.search(r"sans (le )?co[uû]t d['’]incertitude|sans incertitude|without (the )?uncertainty", t, re.I):
        out["uncertainty_cost"] = "no"
    known = {k["text"].lower() for k in toks} | {r["text"].lower() for r in mrefs or []}
    cap = r"[A-ZÉÈÀÂÎ][\w'’]+"
    for m in re.finditer(rf"\b(?:de|depuis|à|vers|par|via|près de|autour de|[ée]vit\w*|contourn\w*|from|to|through|around|near|avoid\w*)\s+"
                         rf"({cap}(?:(?:-|\s+(?:sur|en|le|la|les|de|du|aux?)\s+|-(?:sur|en|le|la|les|de|du|aux?)-|\s+){cap}){{0,3}})", t):
        w = m.group(1).strip(" -")
        if NAMED.match(w) or w.lower() in known:
            continue
        if w not in out["unresolved_places"]:
            out["unresolved_places"].append(w)
    return out


# ---------------------------------------------------------------------------------------------------------- model parser

def _schema(ids: list[str]) -> dict:
    pid = {"type": "string", "enum": ids + ["none"]}
    return {"type": "object", "additionalProperties": False,
            "required": ["start", "end", "waypoints", "waypoint_order", "round_trip", "objective", "speed_kmh", "avoid",
                         "deployment_area", "exercise", "unresolved_places"],
            "properties": {
                "start": pid, "end": pid,
                "waypoints": {"type": "array", "maxItems": 8, "items": {"type": "string", "enum": ids or ["none"]}},
                "waypoint_order": {"type": "string", "enum": ["fixed", "free", "unspecified"]},
                "round_trip": {"type": "string", "enum": ["yes", "no", "unspecified"]},
                "objective": {"type": "string", "enum": ["length", "time", "unspecified"]},
                "speed_kmh": {"type": "integer", "minimum": 0, "maximum": 130},
                "avoid": {"type": "array", "maxItems": 6, "items": {"type": "object", "additionalProperties": False,
                          "required": ["point", "radius_km"], "properties": {"point": {"type": "string", "enum": ids or ["none"]},
                                                                            "radius_km": {"type": "number", "minimum": 0, "maximum": 20}}}},
                "deployment_area": {"type": "string", "enum": ["yes", "no", "unspecified"]},
                "exercise": {"type": "integer", "minimum": 0, "maximum": 30},
                "unresolved_places": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 60}}}}


def _numbers(text: str) -> set[float]:
    return {float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", text)}


SETTING_WORDS = {"objective": r"\b(rapide\w*|vite|court|fast\w*|quick\w*|short\w*|temps|dur[ée]e|distance)\b",
                 "round_trip": r"\b(retour|revenir|aller simple|round|back|one[- ]way)\b",
                 "waypoint_order": r"\b(ordre|order)\b"}


def parse_model(client, text: str, names: dict, coords: list[dict], mrefs: list[dict] | None = None):
    from coa_net.llm import call_validated

    t = _norm(text)
    mentioned = {(m.group(1) + m.group(2)).upper() for m in NAMED.finditer(t)}
    mrefs = mrefs or []
    mids = list(dict.fromkeys(r["id"] for r in mrefs))
    ids = sorted(n for n in names if not n.startswith("DA")) + [c["id"] for c in coords] + mids
    listing = "\n".join([f"- {n} : point nommé du PDF{' (cité dans l’ordre)' if n in mentioned else ''}" for n in ids if n in names]
                        + [f"- {c['id']} : coordonnées « {c['text']} » écrites dans l’ordre" for c in coords]
                        + [f"- {r['id']} : {r['label']} (« {r['text']} » dans l’ordre)" for r in {r['id']: r for r in mrefs}.values()])
    user = f"Ordre :\n« {t} »\n\nPoints disponibles :\n{listing}"
    nums = _numbers(t)
    usable = mentioned | {c["id"] for c in coords} | set(mids)

    def validate(o: dict):
        problems = []
        used = [o["start"], o["end"], *o["waypoints"], *[a["point"] for a in o["avoid"]]]
        for p in used:
            if p != "none" and p not in usable:
                problems.append(f"{p} n'est pas cité dans l'ordre")
        if o["speed_kmh"] and float(o["speed_kmh"]) not in nums:
            problems.append(f"vitesse {o['speed_kmh']} absente de l'ordre")
        for a in o["avoid"]:
            if a["radius_km"] and float(a["radius_km"]) not in nums:
                problems.append(f"rayon {a['radius_km']} km absent de l'ordre")
        if o["exercise"] and float(o["exercise"]) not in nums:
            problems.append(f"exercice {o['exercise']} absent de l'ordre")
        for key, rx in SETTING_WORDS.items():  # a setting no word of the order says is dropped (the model's default, not the order's)
            if o[key] != "unspecified" and not re.search(rx, t, re.I):
                o[key] = "unspecified"
        if o["start"] != "none" and o["start"] == o["end"] and not re.search(SETTING_WORDS["round_trip"], t, re.I):
            problems.append("départ et arrivée identiques sans aller-retour demandé")
        o["unresolved_places"] = [u for u in o["unresolved_places"] if u and u.lower() in t.lower()]
        return o, problems

    return call_validated(client, SYSTEM, user, _schema(ids), "order.text", validate, retries=1)


# ---------------------------------------------------------------------------------------------------------- patch

def _square(lonlat, r_km: float) -> list[list[float]]:
    lon, lat = lonlat
    dy, dx = r_km / 110.574, r_km / (111.320 * math.cos(math.radians(lat)))
    return [[round(lon - dx, 6), round(lat + dy, 6)], [round(lon + dx, 6), round(lat + dy, 6)],
            [round(lon + dx, 6), round(lat - dy, 6)], [round(lon - dx, 6), round(lat - dy, 6)]]


_LABELS: dict[str, str] = {}


def _fmt(pid: str, ll) -> str:
    if pid in _LABELS:
        return f"{_LABELS[pid]} ({ll[1]:.4f} N, {ll[0]:.4f} E)"
    return f"{pid} ({ll[1]:.4f} N, {ll[0]:.4f} E)" if not pid.startswith("C") else f"{ll[1]:.4f} N, {ll[0]:.4f} E"


def build_patch(p: dict, names: dict, coords: list[dict], exercises: list[dict], mrefs: list[dict] | None = None,
                ctx: dict | None = None) -> dict:
    mrefs, ctx = mrefs or [], ctx or {}
    _LABELS.clear()
    _LABELS.update({r["id"]: r["label"] for r in mrefs})
    polys = {r["id"]: r["polygon"] for r in mrefs if r.get("polygon")}
    pos = dict(names) | {c["id"]: c["lonlat"] for c in coords} | {r["id"]: r["lonlat"] for r in mrefs}
    sc: dict = {}
    op: dict = {}
    changes: list[str] = []
    notes: list[str] = []
    ex = next((e for e in exercises if e.get("number") == p.get("exercise")), None) if p.get("exercise") else None
    if p.get("exercise") and not ex:
        notes.append(f"Exercice {p['exercise']} inconnu sur cette feuille : ignoré.")
    if ex:
        P = {k.upper(): v for k, v in ex["points"].items()}
        sc |= {"label": f"Exercice {ex['number']} (modifié par un ordre écrit)", "start": P[ex["start"].upper()], "end": P[ex["end"].upper()],
               "waypoints": [P[w.upper()] for w in ex["waypoints"]], "waypointOrder": "free" if ex["waypoint_order"] == "free" else "fixed",
               "roundTrip": bool(ex["round_trip"]), "area": [P[a.upper()] for a in ex["area"]] if len(ex["area"]) >= 3 else None, "avoid": [],
               "requirements": ex.get("requirements", [])}
        op |= {"objective": ex["objective"], "speedCap": ex["speed_cap_kmh"]}
        changes.append(f"Exercice {ex['number']} chargé : {ex['start']} → {ex['end']}"
                       + (f" par {', '.join(ex['waypoints'])}" if ex["waypoints"] else ""))
    if p["start"] != "none":
        sc["start"] = list(pos[p["start"]])
        changes.append(f"Départ : {_fmt(p['start'], pos[p['start']])}")
    if p["end"] != "none":
        sc["end"] = list(pos[p["end"]])
        changes.append(f"Arrivée : {_fmt(p['end'], pos[p['end']])}")
    if p["waypoints"]:
        wps = [w for w in dict.fromkeys(p["waypoints"]) if w not in (p["start"], p["end"])]
        sc["waypoints"] = [list(pos[w]) for w in wps]
        changes.append("Étapes : " + ", ".join(_fmt(w, pos[w]) for w in wps))
    if p["waypoint_order"] != "unspecified":
        sc["waypointOrder"] = p["waypoint_order"]
        changes.append("Ordre des étapes : " + ("libre (le meilleur ordre est cherché)" if p["waypoint_order"] == "free" else "imposé"))
    if p["round_trip"] != "unspecified":
        sc["roundTrip"] = p["round_trip"] == "yes"
        changes.append("Aller-retour par une autre route" if sc["roundTrip"] else "Aller simple")
    if p["objective"] != "unspecified":
        op["objective"] = p["objective"]
        changes.append("Objectif : " + ("le plus rapide" if p["objective"] == "time" else "le plus court"))
    if p["speed_kmh"]:
        v = int(p["speed_kmh"])
        if 30 <= v <= 130:
            op["speedCap"] = v
            changes.append(f"Vitesse maximale du convoi : {v} km/h")
        else:
            notes.append(f"Vitesse {v} km/h hors de la plage 30–130 : ignorée.")
    if p["avoid"]:
        zones = []
        for a in p["avoid"]:
            if a["point"] == "none" or a["point"] not in pos:
                continue
            if a["point"] in polys and not a["radius_km"]:
                if a["point"].startswith("Z"):
                    notes.append(f"La {_LABELS[a['point']]} est déjà évitée.")
                    continue
                zones.append([list(x) for x in polys[a["point"]]])
                changes.append(f"Zone à éviter : le {_LABELS[a['point']]}")
                continue
            r = float(a["radius_km"]) or 1.0
            zones.append(_square(pos[a["point"]], r))
            changes.append(f"Zone à éviter : {r:g} km autour de {_fmt(a['point'], pos[a['point']])}"
                           + ("" if a["radius_km"] else " (rayon non précisé : 1 km par défaut)"))
        if zones:
            sc["avoid"] = zones
            sc["_avoid_mode"] = "add"
    if p.get("area_ref") == "R" and "R" in polys:
        sc["area"] = [list(x) for x in polys["R"]]
        changes.append("Zone de déploiement : le rectangle sélectionné sur la carte")
    elif p["deployment_area"] == "yes" and not ex and ctx.get("area") and not p.get("_da_named"):
        notes.append("Zone de déploiement : celle dessinée sur la carte est conservée.")
    elif p["deployment_area"] == "yes" and not ex:
        da = [names[k] for k in ("DA1", "DA2", "DA3", "DA4") if k in names]
        if len(da) >= 3:
            sc["area"] = [list(x) for x in da]
            changes.append("Zone de déploiement : DA1–DA4 du PDF")
    elif p["deployment_area"] == "no":
        sc["area"] = None
        changes.append("Zone de déploiement retirée")
    if p.get("lambda", -1) >= 0:
        lam = min(max(float(p["lambda"]), 0.0), 4.0)
        op |= {"lambda": lam, "uncertaintyCost": lam > 0}
        changes.append(f"Coût d’incertitude : λ = {lam:g}")
    if p.get("uncertainty_cost") == "no":
        op["uncertaintyCost"] = False
        changes.append("Coût d’incertitude désactivé")
    cont, hmode = p.get("_continue", False), p.get("_history", "unspecified")
    if cont and "start" not in sc:
        sc["_continue"] = True
        changes.append("Étape suivante : départ = arrivée de l’étape précédente (le trajet calculé entre dans le journal de mission)")
    if hmode != "unspecified":
        sc["historyMode"] = hmode
        changes.append({"forbid": "Trajets des étapes précédentes : interdits (rouverts seulement autour du point partagé s’il le faut)",
                        "penalize": "Trajets des étapes précédentes : pénalisés (évités si possible)",
                        "free": "Trajets des étapes précédentes : autorisés"}[hmode])
    if "start" in sc and "end" in sc and sc["start"] == sc["end"] and not sc.get("roundTrip"):
        notes.append("Départ et arrivée identiques : précisez « aller-retour » si c’est voulu.")
    return {"scenario": sc, "options": op, "changes": changes, "notes": notes}


def parse(text: str, exercises: list[dict], mode: str = "command", use_llm: bool = True, mapctx: dict | None = None) -> dict:
    """``mode="auto"`` (chat): returns ``is_command: False`` at once for a question. Otherwise parses and builds the patch."""
    t0 = time.perf_counter()
    text = _norm(text).strip()
    if mode == "auto" and not looks_like_command(text):
        return {"is_command": False}
    names = named_points(exercises)
    coords = find_coordinates(text)
    mrefs, missing = map_refs(text, mapctx)
    rules = parse_rules(text, names, coords, mrefs)
    method, model, problems = "rules", None, []
    parsed = rules
    if use_llm:
        from interface.backend import orders as O

        c = O.llm_client()
        if c is not None:
            out, problems = parse_model(c, text, names, coords, mrefs)
            if out is not None:
                for k in ("lambda", "uncertainty_cost", "area_ref"):  # rare settings: rules only
                    out[k] = rules[k]
                parsed, method, model = out, "model", c.model
    unknown = [n for n in {(m.group(1) + m.group(2)).upper() for m in NAMED.finditer(text)} if n not in names]
    parsed["_da_named"] = bool(re.search(r"\bDA\s?\d", text, re.I))
    parsed["_continue"], parsed["_history"] = history_of(text)
    patch = build_patch(parsed, names, coords, exercises, mrefs, mapctx)
    places = [u for u in parsed.get("unresolved_places", []) if not any(re.search(rx, u, re.I) for _, rx in MAP_REFS)]
    unresolved = list(dict.fromkeys(places + [f"{u} (point inconnu sur cette feuille)" for u in sorted(unknown)]
                                    + missing))
    disagreements = []
    if method == "model":
        for k in ("start", "end", "objective", "speed_kmh", "round_trip", "waypoint_order"):
            if rules[k] not in ("none", "unspecified", 0) and parsed[k] in ("none", "unspecified", 0):
                disagreements.append(k)
    return {"is_command": bool(patch["changes"]) or bool(unresolved), "method": method, "model": model,
            "seconds": round(time.perf_counter() - t0, 2), **patch, "unresolved": unresolved,
            "coordinates": [{"id": c["id"], "text": c["text"], "lonlat": c["lonlat"]} for c in coords],
            "map_refs": [{"id": r["id"], "text": r["text"], "label": r["label"]} for r in mrefs],
            "model_problems": problems if method == "rules" and use_llm else [],
            "rules_found_more": disagreements}
