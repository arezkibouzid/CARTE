"""Operational orders: extraction (``extraction/``) -> applicable rules (``RAG/``) -> explanations and chat (local LLM).

    order PDF --extraction/run.py (Qwen3-VL-8B, vLLM)--> extraction 3.2 JSON          (or a stored / uploaded JSON)
              --to_rag_v2--> extraction 2.0 JSON --RAG/search/rank_extraction.js--> ranked rules
              --explain / chat (Qwen3-VL-4B, coa_net.llm client)--> explanations linked to the order's fragments

The two bundles are used as they are, through their own entry points: nothing here re-implements the extraction or
the rule search. Which rules apply is decided by the RAG's deterministic checks (``applicability_status``); the model
only explains, may cite only rules and fragments of the current order (enforced by the JSON schema and checked
again), and every role falls back to a deterministic text when no model answers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXTRACTION_DIR = REPO / "extraction"
# The doctrine rules and their search come from the reproducible pipeline (amiad-doctrine-rag/: PDFs -> passages ->
# candidates -> team review -> 214 rules, quote check; experiments/rag_reproduce.py reruns it). RAG/ is the earlier copy
# of the same files (identical rules), used when the pipeline folder is absent.
_PIPE = REPO / "amiad-doctrine-rag"
RAG_DIR = _PIPE if (_PIPE / "rules_final").exists() else REPO / "RAG"
RULES_CSV = RAG_DIR / ("rules_final" if RAG_DIR == _PIPE else "rules") / "doctrine_final_unique_rules.csv"
SEARCH_DIR = RAG_DIR / "search"
QUOTE_CHECK = _PIPE / "rules_final" / "quote_check.csv"  # each rule's quote searched in the source passages
QUOTE_FR = {"found_on_page": "citation retrouvée à la page indiquée", "found_next_page": "citation retrouvée à la page voisine",
            "start_and_end_found": "début et fin de la citation retrouvés", "not_found": "citation non retrouvée dans le texte lu (à comparer au PDF)"}
DATA = REPO / "outputs" / "orders"
STORE = DATA / "extractions"  # one extraction JSON per order (3.2 from the bundle, or 2.0 / uploaded)
PAGES = DATA / "pages"  # extraction/prepare.py output
PDF_DIR = REPO / "examples" / "orders"
ENDPOINTS = DATA / "llm_endpoints.json"  # written by deploy/serve_llm.sbatch

_lock = threading.Lock()
_rules_cache: dict[str, dict] = {}

LEVELS = {
    "concise": "une seule phrase courte (25 mots au plus) par règle",
    "standard": "deux ou trois phrases par règle : ce que dit la règle et pourquoi elle concerne cet ordre",
    "detailed": "quatre à six phrases par règle : ce que dit la règle, pourquoi elle concerne l'ordre, les éléments de "
                "l'ordre qui la déclenchent, ce qu'il reste à vérifier et ce que l'ordre ne dit pas",
}
STATUS_GROUP = {"applicable": "applicable", "potentially_applicable": "potential", "advisory_background": "potential",
                "unverified": "potential", "not_applicable": "not_applicable"}


# ----------------------------------------------------------------------------------------------- orders and storage

def corpus() -> dict:
    return json.loads((EXTRACTION_DIR / "corpus.json").read_text(encoding="utf-8"))


def _safe_id(s: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", s or ""):
        raise ValueError(f"identifiant d’ordre invalide : {s!r}")
    return s


def list_orders() -> list[dict]:
    """Every order of the corpus, plus stored extractions (uploads, reviewed versions), with what is available."""
    STORE.mkdir(parents=True, exist_ok=True)
    stored = {p.stem: p for p in STORE.glob("*.json")}
    out = []
    for c in corpus()["cases"]:
        p = stored.pop(c["case_id"], None)
        out.append({"id": c["case_id"], "label": f"Exercice {c['exercise_id']}", "exercise_id": c["exercise_id"],
                    "document_id": c["document_id"], "pages": c["main_pages"], "task_types": c["expected_task_types"],
                    "extracted": p is not None, **(_meta(p) if p else {})})
    for sid, p in sorted(stored.items()):
        x = _read(p)
        am = x.get("_amiad") or {}
        label = (f"TEST synthétique — {am.get('title', sid)}" if am.get("synthetic")
                 else f"Exercice {x.get('exercise_id', sid)} ({'revu' if 'reviewed' in sid else 'importé'})")
        out.append({"id": sid, "label": label, "synthetic": bool(am.get("synthetic")),
                    "exercise_id": str(x.get("exercise_id", sid)), "document_id": (x.get("document") or {}).get("id"),
                    "pages": x.get("main_pages"), "task_types": x.get("task_types"), "extracted": True, **_meta(p)})
    return out


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8-sig"))


def _meta(p: Path) -> dict:
    x = _read(p)
    return {"schema_version": x.get("schema_version"), "review_status": x.get("review_status"),
            "extraction_status": x.get("_amiad", {}).get("status")}


def load_order(order_id: str) -> dict:
    p = STORE / f"{_safe_id(order_id)}.json"
    if not p.exists():
        raise FileNotFoundError(f"aucune extraction pour {order_id} : lancez l’extraction ou importez un JSON")
    return _read(p)


def save_upload(data: dict) -> str:
    """An extraction JSON from elsewhere (schema 1.0, 2.0 or 3.2), kept under an id derived from its content."""
    if not isinstance(data, dict) or data.get("schema_version") not in ("1.0", "2.0", "3.2"):
        raise ValueError("JSON d’extraction attendu (schema_version 1.0, 2.0 ou 3.2)")
    if "exercise_id" not in data:
        raise ValueError("champ exercise_id manquant")
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
    oid = f"import_{re.sub(r'[^A-Za-z0-9]', '', str(data['exercise_id']))[:20]}_{hashlib.sha1(raw).hexdigest()[:8]}"
    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / f"{oid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return oid


# ----------------------------------------------------------------------------------------------- extraction (bundle)

def endpoints() -> dict:
    """Model endpoints: ``COA_LLM_BASE_URL`` / ``AMIAD_EXTRACTION_URL`` first, else the file the GPU job writes."""
    out = {}
    try:
        out = json.loads(ENDPOINTS.read_text())
    except (OSError, ValueError):
        pass
    if os.environ.get("COA_LLM_BASE_URL"):
        out["explanation"] = {"base_url": os.environ["COA_LLM_BASE_URL"], "model": os.environ.get("COA_LLM_MODEL", "")}
    if os.environ.get("AMIAD_EXTRACTION_URL"):
        out["extraction"] = {"base_url": os.environ["AMIAD_EXTRACTION_URL"],
                             "model": os.environ.get("AMIAD_EXTRACTION_MODEL", "Qwen/Qwen3-VL-8B-Instruct")}
    return out


def run_extraction(order_id: str) -> dict:
    """Run the bundle's ``extraction/run.py`` on one order of the corpus against the extraction model, and store its
    ``extraction.json`` (the bundle validates the schema and runs its own acceptance checks)."""
    case = next((c for c in corpus()["cases"] if c["case_id"] == _safe_id(order_id)), None)
    if case is None:
        raise ValueError(f"{order_id} n’est pas dans le corpus d’extraction")
    ep = endpoints().get("extraction")
    if not ep:
        raise RuntimeError("aucun modèle d’extraction en service (lancez deploy/serve_llm.sbatch ou réglez AMIAD_EXTRACTION_URL)")
    if not (PAGES / case["document_id"] / "manifest.json").exists():
        raise RuntimeError(f"pages non préparées : python extraction/prepare.py --source-dir {PDF_DIR} --out {PAGES}")
    out = DATA / "runs" / f"{order_id}_{time.strftime('%Y%m%d-%H%M%S')}"
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, str(EXTRACTION_DIR / "run.py"), "--case", order_id, "--pages-dir", str(PAGES),
                        "--endpoint", ep["base_url"].rstrip("/") + "/chat/completions", "--model", ep["model"], "--out", str(out)],
                       cwd=str(EXTRACTION_DIR), capture_output=True, text=True, timeout=600)
    meta = json.loads((out / "run.json").read_text()) if (out / "run.json").exists() else {"status": "failed"}
    if not (out / "extraction.json").exists():
        raise RuntimeError(f"extraction en échec ({meta.get('status')}) : {meta.get('error') or p.stderr[-400:]}")
    x = json.loads((out / "extraction.json").read_text())
    x["_amiad"] = {"status": meta["status"], "run": str(out.relative_to(REPO)), "model": ep["model"],
                   "seconds": round(time.perf_counter() - t0, 1), "usage": meta.get("usage")}
    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / f"{order_id}.json").write_text(json.dumps(x, ensure_ascii=False, indent=1), encoding="utf-8")
    _rules_cache.clear()
    return {"order": order_id, "status": meta["status"], "seconds": x["_amiad"]["seconds"],
            "acceptance": json.loads((out / "acceptance.json").read_text()) if (out / "acceptance.json").exists() else None}


# ----------------------------------------------------------------------------------------------- 3.2 -> RAG input 2.0

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def to_rag_v2(x: dict) -> dict:
    """The RAG reads extraction schema 1.0 and 2.0; the extraction bundle now writes 3.2. Same printed fragments,
    regrouped as 2.0 facts: nothing is reworded or added (one fact per 3.2 fragment, its own source id).

    constraint / objective -> ``constraint`` (an exercise requirement); ambiguous -> ``constraint`` with certainty
    ``uncertain``; requested_output -> ``requested_output``; input / reference / other -> ``unparsed`` (wording only).
    A single printed quantity becomes the fact's value; the description is the requested deliverable's text."""
    if x.get("schema_version") in ("1.0", "2.0"):
        return x
    if x.get("schema_version") != "3.2":
        raise ValueError(f"schema_version {x.get('schema_version')!r} non pris en charge (1.0, 2.0 ou 3.2)")
    kinds = {"constraint": "constraint", "objective": "constraint", "ambiguous": "constraint",
             "requested_output": "requested_output", "input": "unparsed", "reference": "unparsed", "other": "unparsed"}
    facts = []
    for f in x["facts"]:
        a = f["annotation"]
        q = f.get("quantities") or []
        facts.append({"id": f["id"], "kind": kinds.get(a["kind"], "unparsed"), "category": a["category"], "text": _clean(f["text"]),
                      "value": q[0]["value"] if len(q) == 1 else None, "unit": q[0]["unit"] if len(q) == 1 else None,
                      "scope": a.get("scope"), "certainty": "uncertain" if a["kind"] == "ambiguous" else "explicit",
                      "source_ids": [f["id"]]})
    outputs = [_clean(f["text"]) for f in x["facts"] if f["annotation"]["kind"] == "requested_output"]
    return {"schema_version": "2.0", "document": x.get("document"), "exercise_id": x["exercise_id"],
            "main_pages": x.get("main_pages"), "context_pages": x.get("context_pages"), "review_status": x.get("review_status"),
            "task_types": x.get("task_types"), "description": " ".join(outputs) or _clean(x.get("description", ""))[:600],
            "facts": facts, "issues": x.get("issues", []), "evidence": x.get("evidence", []),
            "printed_coordinates": x.get("printed_coordinates", []), "manual_correction": x.get("manual_correction", False)}


def facts_view(x: dict) -> list[dict]:
    """The order's fragments in one shape for the interface and the prompts, whatever the schema."""
    if x.get("schema_version") == "3.2":
        return [{"id": f["id"], "text": _clean(f["text"]), "page": f.get("page"), "kind": f["annotation"]["kind"],
                 "category": f["annotation"]["category"], "scope": f["annotation"].get("scope"),
                 "availability": f["annotation"].get("availability"),
                 "quantities": [q["printed_text"] for q in f.get("quantities") or []]} for f in x["facts"]]
    v2 = to_rag_v2(x) if x.get("schema_version") != "2.0" else x
    ev = {e["id"]: e for e in v2.get("evidence", [])}
    out = []
    for f in v2.get("facts", []):
        pages = sorted({ev[s]["page"] for s in f.get("source_ids", []) if s in ev})
        val = f.get("value")
        out.append({"id": f["id"], "text": _clean(f.get("text", "")), "page": pages[0] if pages else None, "kind": f.get("kind"),
                    "category": f.get("category"), "scope": f.get("scope"), "availability": None,
                    "quantities": [f"{val} {f.get('unit') or ''}".strip()] if isinstance(val, (int, float)) and val is not True else []})
    return out


# ----------------------------------------------------------------------------------------------- rules (RAG bundle)

def _node() -> str:
    for c in (os.environ.get("AMIAD_NODE"), shutil.which("node"), str(Path.home() / ".local/node/bin/node")):
        if c and Path(c).exists():
            return c
    raise RuntimeError("Node.js 18+ introuvable : la recherche de règles (RAG/search/rank_extraction.js) en a besoin "
                       "(installez Node.js ou réglez AMIAD_NODE)")


def rank_rules(x: dict, policy: str = "lenient") -> dict:
    """The RAG's own searcher on the order (``node <RAG_DIR>/search/rank_extraction.js``), cached by input content."""
    v2 = to_rag_v2(x)
    raw = json.dumps(v2, ensure_ascii=False, sort_keys=True)
    key = hashlib.sha1((raw + policy + str(RULES_CSV.stat().st_mtime)).encode()).hexdigest()
    with _lock:
        if key in _rules_cache:
            return _rules_cache[key]
    work = DATA / "rag" / key[:16]
    work.mkdir(parents=True, exist_ok=True)
    inp = work / "rag_input.json"
    inp.write_text(raw, encoding="utf-8")
    p = subprocess.run([_node(), str(SEARCH_DIR / "rank_extraction.js"), str(RULES_CSV), str(inp), "--out", str(work),
                        "--policy", policy], capture_output=True, text=True, timeout=60)
    outs = list(work.glob("exercise_*_relevant_rules.json"))
    if p.returncode != 0 or not outs:
        raise RuntimeError(f"recherche de règles en échec : {(p.stderr or p.stdout).strip()[-400:]}")
    res = json.loads(outs[0].read_text(encoding="utf-8"))
    with _lock:
        _rules_cache[key] = res
    return res


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9:]+", " ", (s or "").lower()).strip()


def link_rules(rules: list[dict], facts: list[dict], objective: str | None = None) -> None:
    """Each rule's ``linked_facts``: the order fragments behind the requirement line the RAG matched it on
    (a rule matched on the objective line is linked to the requested deliverable)."""
    nf = [(f["id"], _norm(f["text"])) for f in facts if f["text"]]
    outputs = [f["id"] for f in facts if f["kind"] in ("requested_output", "objective", "output")]
    obj = _norm(objective or "")
    for r in rules:
        req = _norm(r.get("matched_exercise_requirement") or "")
        hits = [fid for fid, t in nf if t and len(t) >= 8 and (t in req or (len(req) >= 8 and req in t))]
        if not hits and req and obj and (req == obj or req in obj or obj in req):
            hits = outputs
        r["linked_facts"] = list(dict.fromkeys(hits))[:4]


def quote_checks() -> dict[str, dict]:
    """rule_id -> the pipeline's quote check (``index_check``: found_on_page, found_next_page, start_and_end_found,
    not_found; and the team's ``quote_match_pdf``: yes, partly, or empty when not recorded)."""
    if not QUOTE_CHECK.exists():
        return {}
    key = QUOTE_CHECK.stat().st_mtime
    if _quotes.get("key") != key:
        import csv

        rows = csv.DictReader(QUOTE_CHECK.open(encoding="utf-8-sig"))
        _quotes.update(key=key, data={r["rule_id"]: {"index_check": r["index_check"], "quote_match_pdf": r.get("quote_match_pdf") or None}
                                      for r in rows})
    return _quotes["data"]


_quotes: dict = {}


def analysis(order_id: str, policy: str = "lenient") -> dict:
    """Extraction + ranked rules, grouped as explicitly applicable / potentially relevant / not applicable."""
    x = load_order(order_id)
    facts = facts_view(x)
    res = rank_rules(x, policy)
    rules = res["rules"]
    link_rules(rules, facts, res.get("exercise_objective"))
    qc = quote_checks()
    for r in rules:
        r["group"] = STATUS_GROUP.get(r["applicability_status"], "potential")
        r["quote_check"] = qc.get(r["rule_id"])
    return {"order": order_id, "exercise_id": str(x.get("exercise_id")), "schema_version": x.get("schema_version"),
            "review_status": x.get("review_status"), "extraction": x.get("_amiad"), "task_types": x.get("task_types"),
            "objective": res.get("exercise_objective"), "facts": facts, "issues": x.get("issues", []),
            "visual_notes": x.get("visual_notes", []), "coordinates": x.get("printed_coordinates", []),
            "map_inputs": map_inputs(x), "known_facts": res.get("known_facts"), "rules": rules,
            "rag": {k: res.get(k) for k in ("rules_file", "total_rules_in_csv", "retrieved_candidates", "selected_rules_count",
                                             "search_mode", "policy", "score_note")}}


def map_inputs(x: dict) -> dict:
    """Points of the order that the map's own inputs can take: start, end, deployment area (printed coordinates only)."""
    ev = {e["id"]: e for e in x.get("evidence", [])}
    out: dict = {"area": []}
    for c in x.get("printed_coordinates", []):
        q = _clean((ev.get(c["source_id"]) or {}).get("quote") or "").lower()
        p = [c["longitude"], c["latitude"]]
        if re.search(r"\bda\s*\d", q) or "deployment" in q:
            out["area"].append(p)
        elif "start" in q or "departure" in q or re.search(r"\bpd\b", q):
            out.setdefault("start", p)
        elif "end point" in q or "finish" in q or "arrival" in q or re.search(r"\bpa\b", q):
            out.setdefault("end", p)
        elif re.search(r"\bwp\s*\d|waypoint", q):
            out.setdefault("waypoints", []).append(p)
    if len(out["area"]) < 3:
        out.pop("area")
    return out


# ----------------------------------------------------------------------------------------------- language model

_llm = {"client": None, "url": None, "checked": 0.0, "error": None}


def llm_client():
    """The explanation model (``coa_net.llm`` client: local endpoints only, fails fast, records usage)."""
    from coa_net.llm import LLMConfigError, LLMUnavailable, OpenAICompatibleClient, is_local_url

    ep = endpoints().get("explanation")
    if not ep:
        _llm.update(client=None, url=None, error="aucun modèle en service")
        return None
    now = time.time()
    if _llm["url"] == ep["base_url"] and (_llm["client"] is not None or now - _llm["checked"] < 20):
        c = _llm["client"]
        if c is None or not c.down:
            return c
    _llm.update(url=ep["base_url"], checked=now)
    try:
        if not is_local_url(ep["base_url"]) and os.environ.get("COA_LLM_ALLOW_REMOTE", "0") != "1":
            raise LLMConfigError(f"{ep['base_url']} n’est pas local")
        c = OpenAICompatibleClient(ep["base_url"], ep.get("model") or "", timeout=float(os.environ.get("COA_LLM_TIMEOUT", "90")),
                                   connect_timeout=3.0, max_retries=1, log_path=str(DATA / "llm_calls.jsonl"), max_tokens=3000)
        served = c.models()
        if not c.model:
            c.model = served[0]
        _llm.update(client=c, error=None)
    except (LLMUnavailable, LLMConfigError) as exc:
        _llm.update(client=None, error=str(exc))
    return _llm["client"]


def llm_status() -> dict:
    c = llm_client()
    ep = endpoints()
    return {"explanation": {"available": c is not None, "model": c.model if c else (ep.get("explanation") or {}).get("model"),
                            "error": None if c else _llm["error"]},
            "extraction": {"configured": "extraction" in ep, "model": (ep.get("extraction") or {}).get("model")},
            "node": _has_node(), "pages_ready": all((PAGES / d["id"] / "manifest.json").exists() for d in corpus()["documents"])}


def _has_node() -> bool:
    try:
        _node()
        return True
    except RuntimeError:
        return False


SYSTEM = """Tu es un assistant d'état-major qui explique des règles doctrinales applicables à un ordre d'opération.
Règles strictes :
- Utilise UNIQUEMENT l'ordre extrait et les règles fournies. N'invente ni règle, ni valeur, ni source.
- Le statut de chaque règle est déjà fixé par une vérification automatique : ne le change jamais, même si une
  condition reste à vérifier.
  « explicitement applicable » = règle obligatoire pour cet ordre : elle doit être respectée dès que sa situation se
  présente (par exemple « en traversant un pont ») ; si l'ordre ne dit pas si cette situation se présente, dis que la
  règle reste applicable et que ce point est à vérifier — ne dis pas qu'elle ne s'applique pas.
  « potentiellement pertinente » = recommandation ou lien à confirmer par une personne, jamais une obligation.
  « non applicable » = une condition de la règle est contredite par l'ordre.
- Ce que l'ordre ne dit pas est inconnu, jamais faux : signale-le comme point à vérifier.
- Cite les règles par leur identifiant (DR-xxxx) et les éléments de l'ordre par leur identifiant de fragment.
- Réponds en français, de façon claire et factuelle."""


def _rule_line(r: dict) -> str:
    grp = {"applicable": "applicable", "potential": "potentiellement pertinente", "not_applicable": "non applicable"}[r["group"]]
    src = r.get("source") or {}
    q = QUOTE_FR.get((r.get("quote_check") or {}).get("index_check"), "citation non contrôlée")
    return (f"{r['rule_id']} [{grp}; {r.get('rule_class')}; vérifiée: {r.get('verification_status')}; {q}] "
            f"valeur: {r.get('rule_value')} | portée: {r.get('rule_scope')} | texte: {_clean(r.get('rule_text'))[:400]} | "
            f"lien avec l'ordre: {_clean(r.get('matched_exercise_requirement'))[:200]} (fragments {', '.join(r.get('linked_facts') or []) or 'aucun'}) | "
            f"utilité: {r.get('how_it_helps')} | à vérifier: {'; '.join(r.get('check') or [])[:300]} | "
            f"contrôle chiffré: {(r.get('number_check') or {}).get('flag')} | source: {src.get('pdf_document')} p. {src.get('pdf_page')}")


def _status_block(rules: list[dict]) -> str:
    g = {k: [r["rule_id"] for r in rules if r["group"] == k] for k in ("applicable", "potential", "not_applicable")}
    return (f"Statuts fixés (à recopier tels quels) — explicitement applicables : {', '.join(g['applicable']) or 'aucune'} ; "
            f"potentiellement pertinentes : {', '.join(g['potential']) or 'aucune'} ; non applicables : {', '.join(g['not_applicable']) or 'aucune'}.")


_SENT = re.compile(r"[^.;!?\n]+")
_NEG_APPLICABLE = re.compile(r"ne s.appliqu\w* pas|non applicable|n.est pas applicable|ne sont pas applicables|potentiellement pertinente|"
                             r"pas obligatoire|non obligatoire|ne sont pas activ|n.est pas activ", re.I)
_POS_MANDATORY = re.compile(r"absolument|obligatoire|explicitement applicable|doit être respectée|doivent être respectées", re.I)


def status_problems(text: str, rules: list[dict]) -> list[str]:
    """Sentences that give a rule another status than the one the rule engine fixed (one rule named in the sentence)."""
    grp = {r["rule_id"]: r["group"] for r in rules}
    out = []
    for s in _SENT.findall(text):
        ids = set(re.findall(r"DR-\d{4}", s))
        groups = {grp.get(i) for i in ids}
        if not ids or len(groups) != 1:  # several statuses in one sentence: cannot tell which one a phrase is about
            continue
        rid = ", ".join(sorted(ids))
        g = groups.pop()
        neg = re.sub(r"pas obligatoires? mais", "", s)
        if g == "applicable" and _NEG_APPLICABLE.search(s):
            out.append(f"{rid} est explicitement applicable (pas « {_NEG_APPLICABLE.search(s).group()} »)")
        elif g == "potential" and _POS_MANDATORY.search(neg) and not re.search(r"pas obligatoire|non obligatoire|n.est pas", neg, re.I):
            out.append(f"{rid} est seulement potentiellement pertinente (pas « {_POS_MANDATORY.search(neg).group()} »)")
        elif g == "not_applicable" and re.search(r"(?<!non )(?<!n.est pas )applicable|obligatoire", s, re.I) and not re.search(r"non applicable|ne s.appliqu|pas applicable", s, re.I):
            out.append(f"{rid} est non applicable")
    return out


def _order_block(a: dict) -> str:
    lines = [f"Ordre : exercice {a['exercise_id']} ; tâche : {', '.join(a.get('task_types') or [])} ; objectif : {a.get('objective')}"]
    for f in a["facts"]:
        if f["kind"] == "ignore":
            continue
        lines.append(f"{f['id']} (p. {f['page']}, {f['kind']}/{f['category']}{', portée ' + f['scope'] if f.get('scope') else ''}) : {f['text'][:300]}")
    for n in a.get("visual_notes") or []:
        lines.append(f"Note visuelle {n['source_id']} (à vérifier) : {_clean(n['text'])[:300]}")
    for i in a.get("issues") or []:
        lines.append(f"Incertitude : {i.get('kind')} {i.get('source_id', '')} {_clean(i.get('text', ''))[:200]}")
    kf = a.get("known_facts") or {}
    lines.append("Faits connus du moteur de règles : " + ", ".join(f"{k}={v}" for k, v in kf.items()))
    return "\n".join(lines)


def _numbers_ok(text: str, context: str) -> list[str]:
    from coa_net.llm import unsupported

    return unsupported(text, context)


def fallback_explanation(r: dict, level: str) -> str:
    grp = {"applicable": "Règle explicitement applicable", "potential": "Règle potentiellement pertinente, à confirmer",
           "not_applicable": "Règle non applicable à cet ordre"}[r["group"]]
    s = f"{grp} : {r.get('how_it_helps') or _clean(r.get('rule_text'))[:160]}"
    if level == "concise":
        return s
    s += f" Elle a été retenue pour : « {_clean(r.get('matched_exercise_requirement'))[:160]} »."
    if level == "detailed":
        if r.get("check"):
            s += " À vérifier : " + "; ".join(c.replace("check: ", "") for c in r["check"][:4]) + "."
        if (r.get("number_check") or {}).get("flag") not in (None, "no value"):
            nc = r["number_check"]
            s += f" Contrôle chiffré : {nc.get('flag')} (limite {nc.get('rule_limit')}, ordre : {', '.join(nc.get('exercise_values') or [])})."
    return s


def explain(order_id: str, level: str = "standard", policy: str = "lenient", use_llm: bool = True) -> dict:
    """Every retrieved rule explained at the requested level, in one model call (fallback: the RAG's own wording)."""
    from coa_net.llm import call_validated

    level = level if level in LEVELS else "standard"
    a = analysis(order_id, policy)
    rules = [r for r in a["rules"] if r["group"] != "not_applicable"]
    t0 = time.perf_counter()
    c = llm_client() if use_llm else None
    expl, problems = None, []
    before = dict((c.usage.by_stage.get("orders.explain") or {}) if c is not None else {})
    if c is not None and rules:
        ids, fids = [r["rule_id"] for r in rules], [f["id"] for f in a["facts"]]
        schema = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {
            "type": "array", "minItems": len(ids), "maxItems": len(ids), "items": {
                "type": "object", "additionalProperties": False, "required": ["rule_id", "explanation", "order_elements"],
                "properties": {"rule_id": {"type": "string", "enum": ids},
                               "explanation": {"type": "string"},
                               "order_elements": {"type": "array", "maxItems": 4, "items": {"type": "string", "enum": fids}}}}}}}
        context = _order_block(a) + "\n\nRègles retenues :\n" + "\n".join(_rule_line(r) for r in rules) + "\n" + _status_block(a["rules"])
        user = (f"{context}\n\nPour CHAQUE règle retenue (une entrée par règle, dans le même ordre), explique en français "
                f"pourquoi elle concerne cet ordre : {LEVELS[level]}. Commence par son statut (explicitement applicable ou "
                "potentiellement pertinente). Dans order_elements, donne les fragments de l'ordre qui la justifient.")

        def validate(out):
            items = {i["rule_id"]: i for i in out["items"]}
            bad = [f"{rid} manquante" for rid in ids if rid not in items]
            limit = {"concise": 300, "standard": 700, "detailed": 1400}[level]
            bad += [f"{i['rule_id']} : explication vide ou trop longue" for i in out["items"] if not 10 <= len(i["explanation"]) <= limit]
            for i in out["items"]:
                extra = _numbers_ok(i["explanation"], context)
                if extra:
                    bad.append(f"{i['rule_id']} : nombres absents du contexte {extra[:3]}")
                bad += status_problems(i["explanation"].replace("Cette règle", i["rule_id"], 1) if i["rule_id"] not in i["explanation"] else i["explanation"], rules)
            return items, bad
        expl, problems = call_validated(c, SYSTEM, user, schema, "orders.explain", validate, retries=1)
    for r in rules:
        it = (expl or {}).get(r["rule_id"])
        r["explanation"] = it["explanation"] if it else fallback_explanation(r, level)
        r["explained_by"] = c.model if it else "règles (sans modèle)"
        if it and it.get("order_elements"):
            r["linked_facts"] = list(dict.fromkeys(it["order_elements"] + (r.get("linked_facts") or [])))[:5]
    for r in a["rules"]:
        if r["group"] == "not_applicable":
            r["explanation"] = fallback_explanation(r, "concise")
            r["explained_by"] = "règles (sans modèle)"
    a["explanation"] = {"level": level, "model": c.model if (c and expl) else None, "seconds": round(time.perf_counter() - t0, 1),
                        "fallback_reason": None if expl else (problems[:2] if c else (_llm["error"] or "modèle désactivé")),
                        "usage": {k: round(v - before.get(k, 0), 1) if isinstance(v, float) else v - before.get(k, 0)
                                  for k, v in (c.usage.by_stage.get("orders.explain") or {}).items()} if (c and expl) else None}
    return a


# ----------------------------------------------------------------------------------------------- chatbot

def _ui_block(ui: dict | None) -> str:
    if not ui:
        return "Contexte de la carte : aucun."
    keep = {k: v for k, v in ui.items() if v not in (None, [], {}, "")}
    return "Contexte actuel de l'interface (points et zones saisis par l'utilisateur, en lon/lat) :\n" + json.dumps(keep, ensure_ascii=False)[:2500]


FR_EN = {"vitesse": "speed", "vitesses": "speed", "nuit": "night", "nocturne": "night", "halte": "halt", "haltes": "halt",
         "pause": "halt", "repos": "rest", "pont": "bridge", "ponts": "bridge", "intervalle": "gap", "distance": "distance",
         "espacement": "gap", "vehicule": "vehicle", "vehicules": "vehicle", "convoi": "convoy", "colonne": "column",
         "rame": "serial", "rames": "serial", "agglomeration": "urban", "ville": "urban", "autoroute": "expressway",
         "charge": "load", "poids": "weight", "hauteur": "clearance", "largeur": "width", "itineraire": "route",
         "route": "route", "horaire": "time", "depart": "departure", "arrivee": "arrival", "graphique": "chart",
         "marche": "march", "tunnel": "tunnel", "carrefour": "junction", "croisement": "junction", "pente": "slope"}


def _retrieve(question: str, a: dict, k: int = 4) -> list[dict]:
    """Fallback retrieval: rules whose wording shares the most words with the question (French words mapped to the
    rules' English wording)."""
    import unicodedata

    plain = unicodedata.normalize("NFKD", question).encode("ascii", "ignore").decode()
    words = _norm(plain).split()
    q = (set(words) | {FR_EN[w] for w in words if w in FR_EN}) - {"the", "de", "la", "le", "les", "des", "et", "a", "est",
                                                                  "quelle", "quelles", "pour", "une", "un", "sur", "du"}
    scored = []
    for r in a["rules"]:
        words = set(_norm(" ".join(str(r.get(k_) or "") for k_ in ("rule_text", "rule_value", "rule_scope", "how_it_helps", "rule_id"))).split())
        scored.append((len(q & words), -r["rank"], r))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for s, _, r in scored[:k] if s > 0] or [r for r in a["rules"] if r["group"] == "applicable"][:k]


def chat(order_id: str, messages: list[dict], level: str = "standard", ui: dict | None = None, policy: str = "lenient") -> dict:
    """One answer about the order and its rules; ``messages`` is the conversation so far (the last one is the question)."""
    from coa_net.llm import call_validated

    level = level if level in LEVELS else "standard"
    a = analysis(order_id, policy)
    question = _clean(messages[-1]["content"]) if messages else ""
    if not question:
        raise ValueError("question vide")
    rules = a["rules"]
    by_id = {r["rule_id"]: r for r in rules}
    fids = [f["id"] for f in a["facts"]]
    t0 = time.perf_counter()
    from interface.backend import order_links as OL

    try:  # orders linked to this one: what is in force along the chain, decided by code
        lres = OL.resolve([order_id])
        if not (lres["links"] or lres["void"] or lres["conflicts"]):
            lres = None
    except (FileNotFoundError, ValueError, KeyError):
        lres = None
    c = llm_client()
    out, problems = None, []
    if c is not None:
        length = {"concise": "Réponds en 2 phrases au plus.", "standard": "Réponds en un court paragraphe (5 phrases au plus).",
                  "detailed": "Réponds de façon détaillée et structurée (listes si utile), en restant fidèle au contexte."}[level]
        # context under a token budget: the order, the rules closest to the question in full and the others by id only,
        # the latest turns in full and older ones shortened (see route_chat.ContextPacker)
        from interface.backend.route_chat import BUDGET_TOKENS, ContextPacker, tokens

        P = ContextPacker(BUDGET_TOKENS)
        ob = _order_block(a)
        P.add("ordre", ob, "\n".join(ob.split("\n")[:16]) + "\n[… fragments suivants omis faute de place]", required=True)
        if lres:
            lf, ls_ = OL.context_block(lres)
            if lf:
                P.add("liens entre ordres", lf + "\nSeules les valeurs en vigueur s’appliquent ; un conflit se signale, il ne se tranche pas.", ls_, required=True)
                P.report["linked_orders_added"] = lres["added"]
        ranked = _retrieve(question, a, k=len(rules)) if rules else []
        ranked += [r for r in rules if r not in ranked]
        room = max(BUDGET_TOKENS - P.used - 900, 300)
        lines, rest = [], []
        for r in ranked:  # by relevance: full lines while they fit, then ids only
            ln = _rule_line(r)
            if not rest and tokens("\n".join(lines + [ln])) <= room:
                lines.append(ln)
            else:
                rest.append(r["rule_id"])
        P.add("règles", "Règles retenues par la recherche (avec leur statut) :\n" + "\n".join(lines)
              + (f"\n(non détaillées faute de place : {', '.join(rest)})" if rest else "") + "\n" + _status_block(rules), required=True)
        hist = messages[:-1]
        history = "\n".join(f"{'Utilisateur' if m['role'] == 'user' else 'Assistant'} : {_clean(m['content'])[:600]}" for m in hist[-2:])
        older = "\n".join(f"{'U' if m['role'] == 'user' else 'A'} : {_clean(m['content']).split('.')[0][:120]}" for m in hist[:-2][-8:])
        if older:
            history = "(plus tôt) " + older + "\n" + history
        P.add("carte", _ui_block(ui))
        context = P.text()
        packed = P.report
        schema = {"type": "object", "additionalProperties": False, "required": ["answer", "cited_rules", "cited_facts"],
                  "properties": {"answer": {"type": "string"},
                                 "cited_rules": {"type": "array", "maxItems": 10, "items": {"type": "string", "enum": list(by_id) or ["none"]}},
                                 "cited_facts": {"type": "array", "maxItems": 10, "items": {"type": "string", "enum": fids or ["none"]}}}}
        user = (f"{context}\n\nConversation précédente :\n{history or '(aucune)'}\n\nQuestion : {question}\n\n{length} "
                "Si la question porte sur des règles, distingue les règles explicitement applicables des règles potentiellement "
                "pertinentes et dis pourquoi. Si la réponse n'est pas dans le contexte, dis-le. Liste dans cited_rules et "
                "cited_facts les identifiants utilisés.")

        def validate(o):
            if not 2 <= len(o["answer"].strip()) <= 3000:
                return o, ["réponse vide ou trop longue"]
            extra = _numbers_ok(o["answer"], context + "\n" + history + "\n" + question)
            bad = [f"nombres ou identifiants absents du contexte : {extra[:4]}"] if extra else []
            if lres:
                bad += OL.stale_problems(o["answer"], lres)
            return o, bad + status_problems(o["answer"], rules)
        out, problems = call_validated(c, SYSTEM, user, schema, "orders.chat", validate, retries=1)
    if out is None:
        hits = _retrieve(question, a)
        lines = [f"(Réponse sans modèle de langue{' : ' + problems[0] if problems else ''}.)"]
        fl = OL.fallback_lines(lres, question) if lres else []
        if fl:
            lines += ["Ordres liés (valeurs en vigueur calculées par le code) :"] + fl
        lines.append("Règles les plus proches de la question :")
        for r in hits:
            lines.append(f"- {r['rule_id']} — {fallback_explanation(r, level)}")
        out = {"answer": "\n".join(lines), "cited_rules": [r["rule_id"] for r in hits], "cited_facts": []}
    cited = [rid for rid in out["cited_rules"] if rid in by_id]
    # sources added by code from the RAG's records, never by the model
    sources = [{"rule_id": rid, "status": by_id[rid]["applicability_status"], "group": by_id[rid]["group"],
                "pdf_document": (by_id[rid].get("source") or {}).get("pdf_document"), "pdf_page": (by_id[rid].get("source") or {}).get("pdf_page"),
                "verification_status": by_id[rid].get("verification_status")} for rid in cited]
    facts = [f for f in a["facts"] if f["id"] in set(out["cited_facts"])]
    return {"answer": out["answer"], "cited_rules": cited, "sources": sources,
            "cited_facts": [{"id": f["id"], "page": f["page"], "text": f["text"][:200]} for f in facts],
            "model": c.model if (c is not None and not problems and out is not None and "sans modèle" not in out["answer"][:40]) else None,
            "context": locals().get("packed"), "seconds": round(time.perf_counter() - t0, 1),
            "links": {"added": lres["added"], "conflicts": len(lres["conflicts"]), "links": len(lres["links"])} if lres else None}
