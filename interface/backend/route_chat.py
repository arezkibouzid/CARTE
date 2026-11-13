"""Chat about a computed route (and, optionally, the operational orders it serves), grounded in the solver's trace.

Two answer paths, cheapest first (frugality):

1. **Trace answers, no language model.** Questions about one segment (« pourquoi le tronçon 3 ? », « L1S2 »), about every
   segment (« détaille l'itinéraire segment par segment »), about the alternatives, the limits / uncertainty, or « pourquoi
   cet itinéraire ? » are answered by templates filled from :func:`coa_net.route_explain.explain_route`. Instant, exact,
   reproducible.
2. **Language model, packed context.** Any other question goes to the local model with a context built by
   :class:`ContextPacker` under a token budget: the route trace (focused on the segments named in the question), then for
   every order the fragments and rules most related to the question, then the conversation (last turns in full, older
   ones shortened), then the map context. Blocks that do not fit are shortened, then left out, and the answer reports
   what was left out. The model may only cite rule, fragment and segment ids from the packed context, and every number
   it writes must appear in that context; otherwise the answer falls back to path 1.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata

from interface.backend import orders as O

BUDGET_TOKENS = int(os.environ.get("AMIAD_CHAT_BUDGET_TOKENS", "6000"))  # the model serves 32k; we stay well below
CHARS_PER_TOKEN = 3.0  # conservative for French text with ids and numbers (Qwen tokenizer: about 3.5)


def tokens(s: str) -> int:
    return int(len(s) / CHARS_PER_TOKEN) + 1


def _plain(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


class ContextPacker:
    """Blocks added by priority under a token budget; each block may give a shorter form used when the full one does not fit."""

    def __init__(self, budget: int):
        self.budget, self.parts, self.report = budget, [], {"budget_tokens": budget, "blocks": []}

    @property
    def used(self) -> int:
        return sum(tokens(p) for p in self.parts)

    def add(self, name: str, full: str, short: str | None = None, required: bool = False) -> None:
        for form, text in (("full", full), ("short", short)):
            if text and (required or self.used + tokens(text) <= self.budget):
                self.parts.append(text)
                self.report["blocks"].append({"block": name, "form": form, "tokens": tokens(text)})
                return
        self.report["blocks"].append({"block": name, "form": "omitted", "tokens": tokens(full)})

    def text(self) -> str:
        self.report["used_tokens"] = self.used
        self.report["omitted"] = [b["block"] for b in self.report["blocks"] if b["form"] == "omitted"]
        self.report["shortened"] = [b["block"] for b in self.report["blocks"] if b["form"] == "short"]
        return "\n\n".join(self.parts)


# ------------------------------------------------------------------------------------------------- path 1: trace

def _segments(tr: dict) -> list[dict]:
    return [s for l in tr["legs"] for s in l["segments"]]


def segment_answer(s: dict, unit: str, level: str) -> str:
    conf = f", confiance du modèle {s['confidence']:.2f} (minimum {s['min_confidence']:.2f})" if "confidence" in s else ""
    head = (f"**{s['id']}** — {s['road_class']}, {s['source']}{' : ' + ', '.join(s['names']) if s['names'] else ''}, "
            f"{s['km']} km en {s['min']:.0f} min{conf}.")
    why = {"unavoidable": "Passage obligé : ", "clearly best": "Choisi parce qu'il est nettement le moins coûteux : ",
           "narrow choice": "Choix serré : "}[s["verdict"]] + s["why"] + "."
    if level == "concise":
        return f"{head} {why}"
    det = [f"coût dans l'objectif : {s['cost_priced']} {unit} (dont {s.get('uncertainty_cost', 0)} {unit} de coût d'incertitude)",
           f"limites légales {', '.join(str(v) for v in s['legal_kmh'])} km/h ; {s['km_built_up']} km en agglomération (50 km/h)"]
    if s["bridges"]:
        det.append(f"{s['bridges']} pont(s) dessiné(s) sur la carte")
    a = s.get("alternative")
    if a and level == "detailed":
        det.append(f"alternative locale rejetée : {a['km']} km, {a['min']:.0f} min, coût {a['cost']} {unit} ({a['extra_cost_pct']:+.0f} %)"
                   + (f", confiance {a['confidence']:.2f}" if "confidence" in a else ""))
    return f"{head} {why}\n" + "\n".join(f"  - {d}" for d in det)


def trace_answer(tr: dict, question: str, level: str) -> tuple[str, list[str]] | None:
    """A template answer when the question is one the trace answers exactly; None otherwise."""
    q = _plain(question)
    segs = _segments(tr)
    by_id = {s["id"].lower(): s for s in segs}
    unit = tr["unit"]
    ids = [m.lower() for m in re.findall(r"\bl\d+s\d+\b", q)]
    for m in re.finditer(r"(?:segment|troncon|portion|section)s?\s*(?:n[o°]?\s*)?(\d+)", q):
        k = int(m.group(1))
        if 1 <= k <= len(segs):
            ids.append(segs[k - 1]["id"].lower())
    asked = [int(m.group(1)) for m in re.finditer(r"(?:segment|troncon|portion|section)s?\s*(?:n[o°]?\s*)?(\d+)", q)]
    if ids:
        picked = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
        if picked:
            return "\n\n".join(segment_answer(s, unit, level) for s in picked), [s["id"] for s in picked]
    if asked:
        return (f"Cet itinéraire compte {len(segs)} segment(s) ({', '.join(s['id'] for s in segs)}) : le segment {asked[0]} n'existe pas. "
                "Demandez par exemple « explique le segment 1 » ou « détaille l'itinéraire segment par segment »."), []
    if re.search(r"chaque (segment|troncon|portion)|tous les (segments|troncons)|segment par segment|troncon par troncon|detaill\w* (l.)?itineraire", q):
        crit = "Critères : " + " ; ".join(tr["criteria"]) + "."
        return crit + "\n\n" + "\n\n".join(segment_answer(s, unit, level) for s in segs), [s["id"] for s in segs]
    if re.search(r"alternativ|autres? (route|chemin|itineraire|trace)|pourquoi pas|rejet|ecart|plus court|plus rapide|evite", q):
        L = (["Alternatives comparées par le solveur :"] + [f"- {a['kind']} : {a['what']} — rejetée : {a['rejected_because']}" for a in tr["alternatives"]]
             if tr["alternatives"] else ["Le solveur n'a pas comparé d'autre itinéraire complet (un seul ordre de passage possible)."])
        L.append("Pour chaque segment, le meilleur contournement local et son surcoût sont dans « détaille l'itinéraire segment par segment ».")
        close = [s for s in segs if s["verdict"] == "narrow choice"]
        if close:
            L.append("Segments où une autre voie était presque aussi bonne : " + "; ".join(
                f"{s['id']} (contournement {s['alternative']['extra_cost_pct']:+.0f} %)" for s in close))
        return "\n".join(L), [s["id"] for s in close]
    if re.search(r"limite|incertitud|fiab|confiance|doute|risque|sur (de|que)|verifier", q):
        L = ["Limites et incertitudes :"] + [f"- {x}" for x in tr["limitations"] or ["aucune limite particulière relevée"]]
        weak = sorted([s for s in segs if "confidence" in s], key=lambda s: s["confidence"])[:3]
        if weak:
            L.append("Segments les moins sûrs : " + "; ".join(f"{s['id']} ({s['road_class']}, confiance {s['confidence']:.2f}, {s['km']} km)" for s in weak))
        if tr.get("uncertainty_strength"):
            L.append(f"Le coût d'incertitude (intensité {tr['uncertainty_strength']:g}) est inclus dans le choix du tracé.")
        return "\n".join(L), [s["id"] for s in weak]
    if re.search(r"pourquoi|comment .*(choisi|calcule)|justifi|explique", q) and re.search(r"itineraire|trajet|route|trace|chemin|choix|choisi", q):
        key = sorted(segs, key=lambda s: -(s.get("alternative") or {}).get("extra_cost", 0))[:3]
        L = ["Critères : " + " ; ".join(tr["criteria"]) + "."]
        L.append("Segments décisifs (l'alternative la plus chère) :")
        L += ["- " + segment_answer(s, unit, "concise") for s in key]
        must = [s["id"] for s in segs if s["verdict"] == "unavoidable"]
        if must:
            L.append(f"Passages obligés : {', '.join(must)}.")
        if tr["alternatives"]:
            L.append("Alternatives rejetées : " + "; ".join(f"{a['kind']} ({a['rejected_because']})" for a in tr["alternatives"][:3]) + ".")
        return "\n".join(L), [s["id"] for s in key]
    return None


# ------------------------------------------------------------------------------------------------- path 2: model

def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", _plain(s))}


def _order_facts(a: dict, qw: set, share: int) -> tuple[str, str, list[str]]:
    """Full and short text of one order (its fragments most related to the question first)."""
    facts = sorted([f for f in a["facts"] if f["kind"] != "ignore"], key=lambda f: -len(qw & _words(f["text"])))
    head = f"Ordre {a['order']} (exercice {a['exercise_id']}) — objectif : {a.get('objective')}"
    full, kept = [head], []
    for f in facts:
        line = f"{f['id']} (p. {f['page']}, {f['kind']}) : {f['text'][:300]}"
        if tokens("\n".join(full + [line])) > share:
            break
        full.append(line)
        kept.append(f["id"])
    short = "\n".join([head] + [f"{f['id']} : {f['text'][:140]}" for f in facts[:2]])
    return "\n".join(full), short, kept or [f["id"] for f in facts[:2]]


def _rules_block(analyses: list[dict], qw: set, room: int) -> tuple[str, list[str]]:
    """The rules of every order merged by rule id (the doctrine is shared: the same rule recurs across orders), ranked by
    relevance to the question; full text while it fits in ``room`` tokens, then ids only. Status per order kept."""
    merged: dict[str, dict] = {}
    for a in analyses:
        for r in a["rules"]:
            m = merged.setdefault(r["rule_id"], {"rule": r, "status": {}, "score": 0})
            m["status"][a["order"]] = r["group"]
            text = " ".join(str(r.get(k) or "") for k in ("rule_text", "rule_value", "rule_scope", "how_it_helps"))
            m["score"] = max(m["score"], len(qw & _words(text)) + (0.5 if r["group"] == "applicable" else 0))
    ranked = sorted(merged.values(), key=lambda m: (-m["score"], m["rule"]["rank"]))
    lines, full_ids, rest = [], [], []
    grp_fr = {"applicable": "applicable", "potential": "potentiellement pertinente", "not_applicable": "non applicable"}
    for m in ranked:
        st = "; ".join(f"{o} : {grp_fr[g]}" for o, g in m["status"].items())
        line = O._rule_line(m["rule"]) + f" | statut par ordre : {st}"
        if not rest and tokens("\n".join(lines + [line])) <= room:
            lines.append(line)
            full_ids.append(m["rule"]["rule_id"])
        else:
            rest.append(m["rule"]["rule_id"])
    txt = "Règles (texte complet ; statut fixé par le moteur de règles, par ordre) :\n" + "\n".join(lines)
    if rest:
        txt += f"\n({len(rest)} autre(s) règle(s) sans texte ici, NE PAS LES CITER : {', '.join(rest[:40])})"
    return txt, full_ids


def fallback_answer(tr: dict, question: str, analyses: list[dict], level: str) -> tuple[str, list[str]]:
    """Deterministic answer: the trace facts the question is about (bridges, built-up areas, speeds, confidence), the
    rules closest to it with their status per order, else the summary of why the route was chosen."""
    q = _plain(question)
    segs = _segments(tr)
    L, cited = [], []
    if re.search(r"pont", q):
        b = [s for s in segs if s["bridges"]]
        L.append("Ponts dessinés sur la carte le long du trajet : " + ("; ".join(f"{s['id']} ({s['bridges']})" for s in b) if b else "aucun") +
                 ". Leur classe (charge admissible, MLC) n'est pas lisible sur la carte : à vérifier.")
        cited += [s["id"] for s in b]
    if re.search(r"agglom|ville|urbain|village", q):
        b = [s for s in segs if s["km_built_up"] > 0]
        L.append("Traversées d'agglomération (50 km/h) : " + ("; ".join(f"{s['id']} ({s['km_built_up']} km)" for s in b) if b else "aucune") + ".")
        cited += [s["id"] for s in b]
    if re.search(r"vitesse|km/h|rapide|lent", q):
        lims = sorted({v for s in segs for v in s["legal_kmh"]})
        L.append(f"Limites légales rencontrées : {', '.join(str(v) for v in lims)} km/h ; " + tr["criteria"][1] + ".")
    if analyses:
        qw = _words(question) | {O.FR_EN[w] for w in _words(question) if w in O.FR_EN}
        merged = {}
        for a in analyses:
            for r in a["rules"]:
                text = " ".join(str(r.get(k) or "") for k in ("rule_text", "rule_value", "rule_scope", "how_it_helps"))
                m = merged.setdefault(r["rule_id"], {"r": r, "st": {}, "score": len(qw & _words(text))})
                m["st"][a["order"]] = r["group"]
        top = sorted([m for m in merged.values() if m["score"] > 0], key=lambda m: -m["score"])[:4]
        if top:
            fr = {"applicable": "explicitement applicable", "potential": "potentiellement pertinente", "not_applicable": "non applicable"}
            L.append("Règles les plus liées à la question (statut fixé par le moteur de règles) :")
            L += [f"- {m['r']['rule_id']} — {O.fallback_explanation(m['r'], 'concise')} ({'; '.join(f'{o} : {fr[g]}' for o, g in m['st'].items())})" for m in top]
    if not L:
        return trace_answer(tr, "pourquoi cet itinéraire", level)
    return "\n".join(L), list(dict.fromkeys(cited))


SYSTEM = """Tu expliques un itinéraire de convoi calculé par un solveur (Dijkstra sur un réseau routier lu sur une carte scannée)
et, s'il y en a, les règles doctrinales des ordres d'opération concernés.
- Utilise UNIQUEMENT la trace du solveur, les ordres et les règles fournis. N'invente ni raison, ni valeur, ni route.
- Une raison de choix d'un segment doit venir de la trace (coût, alternative locale, confiance, contraintes).
- Si le contexte ne permet pas de répondre (par exemple une question d'horaire, de nuit, de charge ou de largeur que ni la
  trace ni les règles ne traitent), commence par « Le contexte ne permet pas de conclure » et indique ce qu'il faudrait
  vérifier. Ne réponds jamais oui ou non sans un segment, une règle ou un fragment cité qui le justifie.
- Le statut des règles est fixé : ne le change jamais. « applicable » = obligatoire dès que sa situation se présente ;
  si la trace ne dit pas si la situation se présente (par exemple un pont), dis que c'est à vérifier, pas que la règle
  ne s'applique pas.
- Ne cite une règle (DR-xxxx) que si son texte complet figure dans le contexte.
- Cite les segments (L1S2…), règles (DR-xxxx) et fragments par leur identifiant. Réponds en français."""


def chat(entry: dict, messages: list[dict], level: str = "standard", order_ids: list[str] | None = None,
         ui: dict | None = None, use_llm: bool = True, budget: int | None = None) -> dict:
    from coa_net.llm import call_validated, unsupported
    from coa_net.route_explain import trace_text

    t0 = time.perf_counter()
    tr = entry["trace"]
    if tr is None:
        raise ValueError("cet itinéraire a été calculé sans trace d'explication")
    question = O._clean(messages[-1]["content"])
    # questions about the orders' rules (with orders selected) need the rules: model path, the trace stays in its context
    about_rules = bool(order_ids) and re.search(r"regle|doctrin|ordre|autoris|interdi|doit|peut|vitesse|reglement|DR-\d", _plain(question), re.I)
    direct = None if about_rules else trace_answer(tr, question, level)
    if direct is not None:
        text, segs = direct
        return {"answer": text, "mode": "trace", "cited_segments": segs, "cited_rules": [], "sources": [], "model": None,
                "context": None, "seconds": round(time.perf_counter() - t0, 3)}
    budget = budget or BUDGET_TOKENS
    P = ContextPacker(budget)
    P.add("question", f"Question : {question}", required=True)
    focus = next(iter(re.findall(r"\bL\d+S\d+\b", question, re.I)), None)
    summary = (f"Itinéraire : {entry['total']['length_km']} km, {entry['total']['duration_min']} min, réseau {entry['network']}, "
               f"objectif {tr['objective']}.")
    P.add("itinéraire (trace du solveur)", summary + "\n" + trace_text(tr, focus.upper() if focus else None, max_chars=int(budget * 0.45 * CHARS_PER_TOKEN)),
          summary + "\n" + trace_text(tr, None, max_chars=int(budget * 0.2 * CHARS_PER_TOKEN)), required=True)
    if entry.get("compare"):
        P.add("comparaisons", "Contrôles : " + "; ".join(f"{k} : écart de distance {v.get('length_gap')}, tracés communs {v.get('overlap')}"
                                                         for k, v in entry["compare"].items()))
    ids = {"rules": [], "facts": []}
    analyses = []
    order_ids = list(dict.fromkeys(order_ids or []))
    if order_ids:
        qw = _words(question) | {O.FR_EN[w] for w in _words(question) if w in O.FR_EN}
        for oid in order_ids:
            try:
                a = O.analysis(oid)
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                P.add(f"ordre {oid}", f"Ordre {oid} : indisponible ({exc})")
                continue
            a = dict(a, order=oid)
            analyses.append(a)
        if analyses:
            # rules first (they carry the answer to rule questions), then each order's fragments with what is left
            room = max(int((budget - P.used) * 0.55), 200)
            txt, full_ids = _rules_block(analyses, qw, room)
            P.add("règles des ordres", txt, required=True)
            ids["rules"] = full_ids
            share = max(int((budget - P.used) * 0.6 / len(analyses)), 80)
            for a in analyses:
                full, short, fids = _order_facts(a, qw, share)
                P.add(f"ordre {a['order']}", full, short)
                if P.report["blocks"][-1]["form"] != "omitted":
                    ids["facts"] += fids
    hist = [m for m in messages[:-1]]
    if hist:
        recent = "\n".join(f"{'Utilisateur' if m['role'] == 'user' else 'Assistant'} : {O._clean(m['content'])[:600]}" for m in hist[-2:])
        older = "\n".join(f"{'U' if m['role'] == 'user' else 'A'} : {O._clean(m['content']).split('.')[0][:120]}" for m in hist[:-2][-10:])
        P.add("conversation récente", "Conversation récente :\n" + recent, "Conversation récente :\n" + recent[-600:])
        if older:
            P.add("conversation ancienne (résumée)", "Plus tôt :\n" + older)
    if ui:
        P.add("contexte de la carte", O._ui_block(ui))
    context = P.text()
    seg_ids = [s["id"] for s in _segments(tr)]
    out, problems, model = None, [], None
    c = O.llm_client() if use_llm else None
    if c is not None:
        length = {"concise": "Réponds en 2 phrases au plus.", "standard": "Réponds en un court paragraphe (6 phrases au plus).",
                  "detailed": "Réponds de façon détaillée et structurée, segment par segment si utile."}[level]
        schema = {"type": "object", "additionalProperties": False, "required": ["answer", "cited_segments", "cited_rules"],
                  "properties": {"answer": {"type": "string"},
                                 "cited_segments": {"type": "array", "maxItems": 20, "items": {"type": "string", "enum": seg_ids or ["none"]}},
                                 "cited_rules": {"type": "array", "maxItems": 10, "items": {"type": "string", "enum": ids["rules"] or ["none"]}}}}
        rules_all = [r for a in analyses for r in a["rules"]]

        def validate(o):
            if not 2 <= len(o["answer"].strip()) <= 4000:
                return o, ["réponse vide ou trop longue"]
            extra = unsupported(o["answer"], context)
            bad = [f"nombres ou identifiants absents du contexte : {extra[:4]}"] if extra else []
            no_text = sorted(set(re.findall(r"DR-\d{4}", o["answer"])) - set(ids["rules"]))
            if no_text:
                bad.append(f"règles citées sans leur texte dans le contexte : {', '.join(no_text[:5])} — ne les cite pas")
            grp = {r["rule_id"]: r["group"] for r in rules_all}
            for sent in re.findall(r"[^.;!?\n]+", o["answer"]):
                rid = set(re.findall(r"DR-\d{4}", sent))
                if rid and all(grp.get(r) == "applicable" for r in rid) and re.search(r"ne s.appliqu\w* pas|non applicable|n.est pas applicable|ne sont pas applicables", sent, re.I):
                    bad.append(f"{', '.join(sorted(rid))} : règle(s) explicitement applicable(s), pas « ne s'applique pas » (dire « à vérifier »)")
            if not o["cited_segments"] and not o["cited_rules"] and not re.search(r"ne permet pas|pas d.information|aucune information|a verifier|à vérifier", o["answer"], re.I):
                bad.append("affirmation sans segment ni règle cités : cite tes sources ou dis que le contexte ne permet pas de conclure")
            return o, bad + O.status_problems(o["answer"], rules_all)

        out, problems = call_validated(c, SYSTEM, f"{context}\n\n{length}", schema, "route.chat", validate, retries=1)
        model = c.model if out is not None else None
    if out is None:
        # no model, or the model's answer failed the checks: what the trace and the rule engine say about the question
        note = "(Réponse sans modèle de langue" + (f" : {problems[0]}" if problems else "") + ". Voici ce que disent la trace du solveur et le moteur de règles.)"
        text, segs = fallback_answer(tr, question, analyses, level)
        out = {"answer": note + "\n" + text, "cited_segments": segs, "cited_rules": []}
    by_rule = {r["rule_id"]: r for a in analyses for r in a["rules"]}
    cited = [r for r in out["cited_rules"] if r in by_rule]
    sources = [{"rule_id": r, "status": by_rule[r]["applicability_status"], "group": by_rule[r]["group"],
                "pdf_document": (by_rule[r].get("source") or {}).get("pdf_document"), "pdf_page": (by_rule[r].get("source") or {}).get("pdf_page"),
                "verification_status": by_rule[r].get("verification_status")} for r in cited]
    return {"answer": out["answer"], "mode": "model" if model else "trace (repli)", "cited_segments": [s for s in out["cited_segments"] if s in seg_ids],
            "cited_rules": cited, "sources": sources, "model": model, "context": P.report, "seconds": round(time.perf_counter() - t0, 2)}
