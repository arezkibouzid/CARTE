"""Compares language models of the same size on what CARTE asks of them, as the context grows.

Same code paths as the app (route chat with orders and links, written orders), against the model served at
``COA_LLM_BASE_URL`` (``experiments/run_llm_compare.sh`` starts it on one GPU and sets the variables). Tests:

  needle     n = 10..400 synthetic orders, each with a radio frequency only its own fragments carry; the question asks the
             frequency of one convoy (first, middle, last order): pure retrieval in a long context
  chain      a chain of k FRAGOs, each amending the previous one (k = 5..25): the value in force must be the last one
  conflicts  annexes contradicting the base order's load: the conflict must be reported, not settled
  text       written orders turned into scenario fields (start, end, waypoints, speed, objective) with the JSON grammar

each at several token budgets of the context packer (6,000 = the app's default). Per call: real prompt tokens (server
count), seconds, the model's FIRST answer before the app's checks (``raw_ok``), and the final answer after checks and one
retry (``final_ok``, ``mode``). Synthetic orders (fictional) are written in the run folder; outputs/orders is not changed.

    sbatch -J llm_compare experiments/run_llm_compare.sh <model dir> <served name> [--quick]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import time

from common import REPO, Run

spec = importlib.util.spec_from_file_location("cs", REPO / "experiments" / "context_stress.py")
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)
mk = cs.mk


def freq(i: int) -> str:
    return f"{30 + i * 0.025:.3f}"  # unique per order: 30.025, 30.050, ...


def needle_orders(n: int) -> list[tuple[str, dict]]:
    return [mk.order(f"nd_{i:03d}", f"OPORD {500 + i}/2026", [
        mk.fact(1, 1, f"OPORD {500 + i}/2026 — ORDRE D'OPÉRATION DU CONVOI {i} (DONNÉES DE TEST SYNTHÉTIQUES)", "other", "other"),
        mk.fact(2, 1, cs.dtg(30 + i % 50), "other", "other"),
        mk.fact(3, 1, f"Le convoi {i} rejoint son point d'arrivée par l'itinéraire le plus court.", "objective", "route"),
        mk.fact(4, 1, f"Vitesse maximale du convoi {i} : {50 + (i % 5) * 5} km/h", "constraint", "speed", [(50 + (i % 5) * 5, "km/h")]),
        mk.fact(5, 1, f"Espacement entre véhicules : {50 + (i % 4) * 25} m", "constraint", "spacing", [(50 + (i % 4) * 25, "m")]),
        mk.fact(6, 1, f"Fréquence radio du convoi {i} : {freq(i)} MHz, indicatif ORION {i}", "constraint", "other"),
        mk.fact(7, 1, "Halte de 15 min toutes les 2 h", "constraint", "time", [(15, "min"), (2, "h")]),
    ], "independent") for i in range(1, n + 1)]


# written orders on the points of the exercises and the fields expected
TEXT_CASES = [
    ("De PD1 à PA1, le plus rapide.", dict(start="PD1", end="PA1", objective="time")),
    ("Départ PD4, arrivée PA4, en passant par WP1 puis WP2, le plus court.", dict(start="PD4", end="PA4", waypoints=["WP1", "WP2"], objective="length")),
    ("Rejoindre PA1 depuis PD1 sans dépasser 60 km/h.", dict(start="PD1", end="PA1", speed_kmh=60)),
    ("Le convoi part de PD4 et doit être à PA4, vitesse maximale 70 km/h, itinéraire le plus rapide.", dict(start="PD4", end="PA4", speed_kmh=70, objective="time")),
    ("Aller de 48.957, 4.364 à 48.773, 5.160 par 49.090, 4.897, le plus court, 80 km/h.", dict(start="C1", end="C2", waypoints=["C3"], objective="length", speed_kmh=80)),
    ("Partir de PD2, passer par WP3 et finir à PA2.", dict(start="PD2", end="PA2", waypoints=["WP3"])),
    ("Le plus court de PD3 vers PA3 avec un aller-retour.", dict(start="PD3", end="PA3", objective="length", round_trip=True)),
    ("PD1 -> PA1, limite 50 km/h, en évitant DA2.", dict(start="PD1", end="PA1", speed_kmh=50)),
    ("Depuis PA4, rejoindre PD4 en passant par WP4, WP3, WP2 et WP1 dans cet ordre.", dict(start="PA4", end="PD4", waypoints=["WP4", "WP3", "WP2", "WP1"], waypoint_order="fixed")),
    ("Convoi de PD2 à PA2 : le plus rapide, 40 km/h maximum.", dict(start="PD2", end="PA2", objective="time", speed_kmh=40)),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--budgets", default="6000,32000,100000")
    a = ap.parse_args()
    budgets = [int(b) for b in a.budgets.split(",")]
    N = [10, 40] if a.quick else [10, 40, 100, 200, 400]
    DEPTH = [5, 15] if a.quick else [5, 15, 25]
    CONFL = [3]
    cfg = {"script": "experiments/llm_compare.py", "quick": a.quick, "budgets": budgets, "needle_orders": N, "chain_depth": DEPTH,
           "conflicts": CONFL, "text_cases": len(TEXT_CASES), "route": "exercise 7 (PD4 -> PA4), network nodice"}
    with Run("llm_compare" + ("_quick" if a.quick else ""), cfg) as R:
        from interface.backend import amiad_api as A
        from interface.backend import order_links as L
        from interface.backend import orders as O
        from interface.backend import route_chat as RC
        from interface.backend import text_order as T

        store = R.dir / "orders_store"
        store.mkdir()
        for p in sorted(O.STORE.glob("*.json")):
            if not p.name.startswith("test_"):
                shutil.copy(p, store / p.name)
        O.STORE = store
        O.corpus = lambda: {"cases": []}
        L.LINKS_FILE = R.dir / "order_links.json"
        c = O.llm_client()
        if c is None:
            raise SystemExit(f"no model: {O._llm['error']}")
        R.summary["model"] = c.model
        R.log(f"model {c.model} at {c.base_url}")
        raw: list = []
        inner = c.complete_json

        def spy(system, user, schema, stage=""):
            u0 = c.usage.input_tokens
            t0 = time.time()
            out = inner(system, user, schema, stage=stage)
            raw.append({"out": out, "prompt_tokens": c.usage.input_tokens - u0, "s": time.time() - t0})
            return out

        c.complete_json = spy

        def write(orders):
            for oid, x in orders:
                (store / f"{oid}.json").write_text(json.dumps(x, ensure_ascii=False), encoding="utf-8")
            L._cache.update(key=None, headers=None)

        def clear(prefixes):
            for p in store.glob("*.json"):
                if p.name.startswith(prefixes):
                    p.unlink()
            L._cache.update(key=None, headers=None)

        with R.phase("network + route"):
            exs = json.loads(json.dumps(A.pdf_from_sheet("hackathon")))["exercises"]
            ex = {e["number"]: e for e in exs}[7]
            P = ex["points"]
            r = A.route(A.RouteRequest(start=P[ex["start"]], end=P[ex["end"]], waypoints=[P[w] for w in ex["waypoints"]],
                                       waypoint_order="free", use_bdtopo=False))
            entry = A._traces[json.loads(r.body)["route_id"]]

        def ask(question, ids, budget):
            raw.clear()
            t0 = time.perf_counter()
            out = RC.chat(entry, [{"role": "user", "content": question}], "standard", ids, None, True, budget)
            ctx = out["context"] or {}
            first = raw[0] if raw else None
            return out, ctx, first, {"seconds": round(time.perf_counter() - t0, 2), "calls": len(raw),
                                     "prompt_tokens": first["prompt_tokens"] if first else None,
                                     "est_tokens": ctx.get("used_tokens"), "omitted_blocks": len(ctx.get("omitted") or [])}

        def has(text, rx):
            return bool(re.search(rx, text or "", re.I))

        # ------------------------------------------------------------------------------------------------ needle
        write(needle_orders(max(N)))
        for n in N:
            ids = [f"nd_{i:03d}" for i in range(1, n + 1)]
            for pos, t in (("first", 1), ("middle", (n + 1) // 2), ("last", n)):
                rx = re.escape(freq(t)).replace(r"\.", r"[.,]")
                for b in budgets:
                    with R.phase("needle"):
                        out, ctx, first, m = ask(f"D'après les ordres, quelle est la fréquence radio du convoi {t} ?", ids, b)
                    blk = next((x for x in ctx.get("blocks", []) if x["block"] == f"ordre nd_{t:03d}"), {})
                    rec = {"test": "needle", "orders": n, "position": pos, "budget": b, "target_in_context": blk.get("form") in ("full", "short"),
                           "raw_ok": has(json.dumps(first["out"], ensure_ascii=False) if first else "", rx), "final_ok": has(out["answer"], rx),
                           "mode": out["mode"], **m}
                    R.record(rec)
                    R.log(rec)
        clear(("nd_",))

        # ------------------------------------------------------------------------------------------------ chain
        for k in DEPTH:
            write(cs.chain_orders(k))
            res = L.resolve([f"ch_frago{k:02d}"])
            for b in budgets:
                with R.phase("chain"):
                    out, ctx, first, m = ask("Quelle est la vitesse maximale en vigueur pour le convoi ?", [f"ch_frago{k:02d}"], b)
                fa = first["out"].get("answer", "") if first else ""
                eff = next(e for e in res["effective"] if e["key"] == "speed_max")["in_force"]["printed"]
                rx = rf"\b{re.escape(eff.split()[0])}\s*km/h"
                rec = {"test": "chain", "depth": k, "budget": b, "expected": eff, "raw_ok": has(fa, rx), "raw_stale": bool(L.stale_problems(fa, res)),
                       "final_ok": has(out["answer"], rx), "final_stale": bool(L.stale_problems(out["answer"], res)), "mode": out["mode"], **m}
                R.record(rec)
                R.log(rec)
            clear(("ch_",))

        # ------------------------------------------------------------------------------------------------ conflicts
        cx = r"conflit|contradict|trancher|commandement|diverg|incoh"
        for cnum in CONFL:
            write(cs.conflict_orders(cnum))
            for b in budgets:
                with R.phase("conflicts"):
                    out, ctx, first, m = ask("Quelle charge maximale pour les véhicules du convoi ?", ["cf_frago01"], b)
                fa = first["out"].get("answer", "") if first else ""
                rec = {"test": "conflicts", "annexes": cnum, "budget": b, "raw_ok": has(fa, cx), "final_ok": has(out["answer"], cx),
                       "mode": out["mode"], **m}
                R.record(rec)
                R.log(rec)
            clear(("cf_",))

        # ------------------------------------------------------------------------------------------------ written orders
        for text, exp in TEXT_CASES:
            raw.clear()
            with R.phase("text"):
                t0 = time.perf_counter()
                res = T.parse(text, exs, "command", True)
                sec = time.perf_counter() - t0
            first = raw[0]["out"] if raw else {}
            names = {cc["id"]: cc for cc in T.find_coordinates(T._norm(text))}

            def check(o):
                bad = []
                for k_, want in exp.items():
                    got = o.get(k_)
                    if k_ == "round_trip":
                        got = got == "yes" if isinstance(got, str) else bool(got)
                    if k_ == "speed_kmh":
                        got = int(float(got or 0))
                    if got != want:
                        bad.append(f"{k_}: {got!r} != {want!r}")
                return bad

            raw_bad = check(first) if first else ["no model output"]
            rec = {"test": "text", "text": text, "raw_ok": not raw_bad, "raw_errors": raw_bad, "method": res.get("method"),
                   "final_ok": res.get("method") == "model" and not check(_fields(res, first)), "calls": len(raw),
                   "prompt_tokens": raw[0]["prompt_tokens"] if raw else None, "seconds": round(sec, 2), "coords": list(names)}
            R.record(rec)
            R.log(rec)

        recs = R.records

        def rate(test, key, **f):
            rs = [x for x in recs if x["test"] == test and all(x.get(k_) == v for k_, v in f.items())]
            return f"{sum(bool(x[key]) for x in rs)}/{len(rs)}"

        S = {"model": c.model}
        for b in budgets:
            S[f"needle_raw@{b}"] = rate("needle", "raw_ok", budget=b)
            S[f"needle_final@{b}"] = rate("needle", "final_ok", budget=b)
            S[f"chain_raw@{b}"] = rate("chain", "raw_ok", budget=b)
            S[f"chain_raw_stale@{b}"] = rate("chain", "raw_stale", budget=b)
            S[f"conflict_raw@{b}"] = rate("conflicts", "raw_ok", budget=b)
        S["text_raw"] = rate("text", "raw_ok")
        S["max_prompt_tokens"] = max((x.get("prompt_tokens") or 0) for x in recs)
        S["mean_seconds_chat"] = round(sum(x["seconds"] for x in recs if x["test"] != "text") / max(1, sum(x["test"] != "text" for x in recs)), 2)
        S["mean_seconds_text"] = round(sum(x["seconds"] for x in recs if x["test"] == "text") / max(1, sum(x["test"] == "text" for x in recs)), 2)
        R.summary = S
        R.log("summary", S)


def _fields(res: dict, first: dict) -> dict:
    """The fields the app applied, read back from the model output (the patch keeps the same values when accepted)."""
    return first or {}


if __name__ == "__main__":
    main()
