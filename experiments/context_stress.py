"""Stress test of the language model's context window with many orders and dependencies between them.

The route chat packs, under a token budget, the route trace, the rules of the selected orders, their fragments and the
links between orders (values in force, conflicts). This measures how that holds when the input grows:

  orders     1..40 orders selected at once (the extracted exercises, then synthetic independent orders)
  chain      a chain of 1..20 FRAGOs, each amending the previous one; only the last is selected: the whole chain must come
             in by itself and the value in force must be the last one
  budget     1,500..100,000 tokens for 20 orders + a chain of 10: what is shortened or left out, whether the links survive
  conflicts  0..5 annexes contradicting the base order's load: the conflict must be reported, not settled

Per call: tokens per block, blocks shortened / omitted, time; with the model (``--llm``, default on): whether the answer
gives the value in force, never a replaced value as current, and reports the conflict. Synthetic orders (fictional, see
scripts/make_test_orders.py) are written in the run folder; the real orders are copied there, nothing is changed in
outputs/orders.

    sbatch -J context_stress experiments/run_cpu.sh experiments/context_stress.py [--no-llm] [--quick]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import time

from common import REPO, Run

spec = importlib.util.spec_from_file_location("mk", REPO / "scripts" / "make_test_orders.py")
mk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mk)


def dtg(h: int) -> str:
    d, hh = 14 + h // 24, h % 24
    return f"DTG {d:02d}{hh:02d}00Z NOV 26"


def chain_orders(k: int, prefix: str = "ch") -> list[tuple[str, dict]]:
    """OPORD 50/2026 at 60 km/h, then FRAGO 1..k, FRAGO i amending FRAGO i-1 (FRAGO 1 the OPORD), speed 60 - 2 i km/h."""
    out = [mk.order(f"{prefix}_opord", "OPORD 50/2026", [
        mk.fact(1, 1, "OPORD 50/2026 — ORDRE D'OPÉRATION (DONNÉES DE TEST SYNTHÉTIQUES)", "other", "other"),
        mk.fact(2, 1, dtg(0), "other", "other"),
        mk.fact(3, 1, "Vitesse maximale du convoi : 60 km/h", "constraint", "speed", [(60, "km/h")]),
        mk.fact(4, 1, "Espacement entre véhicules : 100 m", "constraint", "spacing", [(100, "m")]),
        mk.fact(5, 1, "Charge maximale des véhicules : 40 tonnes", "constraint", "load", [(40, "tonnes")]),
    ], "base")]
    for i in range(1, k + 1):
        tgt = "l'OPORD 50/2026" if i == 1 else f"le FRAGO {i - 1}"
        out.append(mk.order(f"{prefix}_frago{i:02d}", f"FRAGO {i}", [
            mk.fact(1, 1, f"FRAGO {i} (DONNÉES DE TEST SYNTHÉTIQUES)", "other", "other"),
            mk.fact(2, 1, dtg(i), "other", "other"),
            mk.fact(3, 1, f"Le présent FRAGO modifie {tgt}.", "constraint", "other"),
            mk.fact(4, 1, f"Vitesse maximale du convoi ramenée à {60 - 2 * i} km/h", "constraint", "speed", [(60 - 2 * i, "km/h")]),
        ], f"amendment {i}"))
    return out


def independent_orders(n: int) -> list[tuple[str, dict]]:
    return [mk.order(f"ind_{i:02d}", f"OPORD {100 + i}/2026", [
        mk.fact(1, 1, f"OPORD {100 + i}/2026 — ORDRE D'OPÉRATION (DONNÉES DE TEST SYNTHÉTIQUES)", "other", "other"),
        mk.fact(2, 1, dtg(30 + i), "other", "other"),
        mk.fact(3, 1, f"Le convoi {i} rejoint son point d'arrivée par l'itinéraire le plus court.", "objective", "route"),
        mk.fact(4, 1, f"Vitesse maximale du convoi : {50 + (i % 5) * 5} km/h", "constraint", "speed", [(50 + (i % 5) * 5, "km/h")]),
        mk.fact(5, 1, f"Espacement entre véhicules : {50 + (i % 4) * 25} m", "constraint", "spacing", [(50 + (i % 4) * 25, "m")]),
        mk.fact(6, 1, "Halte de 15 min toutes les 2 h", "constraint", "time", [(15, "min"), (2, "h")]),
    ], "independent") for i in range(1, n + 1)]


def conflict_orders(c: int) -> list[tuple[str, dict]]:
    out = chain_orders(1, "cf")
    for j in range(1, c + 1):
        out.append(mk.order(f"cf_annexe{j}", f"Annexe {chr(64 + j)}", [
            mk.fact(1, 1, f"ANNEXE {chr(64 + j)} à l'OPORD 50/2026 (DONNÉES DE TEST SYNTHÉTIQUES)", "other", "other"),
            mk.fact(2, 1, "Complète l'OPORD 50/2026.", "constraint", "other"),
            mk.fact(3, 1, f"Charge maximale des véhicules : {40 - 3 * j} tonnes", "constraint", "load", [(40 - 3 * j, "tonnes")]),
        ], "annex"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    use_llm = not a.no_llm
    N_ORDERS = [1, 5, 40] if a.quick else [1, 2, 5, 10, 20, 40]
    DEPTH = [1, 5] if a.quick else [1, 2, 5, 10, 20]
    BUDGETS = [1500, 6000] if a.quick else [1500, 3000, 6000, 12000, 100000]
    CONFL = [0, 3] if a.quick else [0, 1, 3, 5]
    cfg = {"script": "experiments/context_stress.py", "llm": use_llm, "quick": a.quick, "orders": N_ORDERS, "chain_depth": DEPTH,
           "budgets": BUDGETS, "conflicts": CONFL, "route": "exercise 7 (PD4 -> PA4, 4 waypoints free order), network nodice"}
    name = "context_stress" + ("_quick" if a.quick else "")
    with Run(name, cfg) as R:
        from interface.backend import amiad_api as A
        from interface.backend import order_links as L
        from interface.backend import orders as O
        from interface.backend import route_chat as RC

        store = R.dir / "orders_store"
        store.mkdir()
        real = sorted(p for p in O.STORE.glob("*.json") if not p.name.startswith("test_"))
        for p in real:
            shutil.copy(p, store / p.name)
        O.STORE = store
        O.corpus = lambda: {"cases": []}
        L.LINKS_FILE = R.dir / "order_links.json"
        client = O.llm_client() if use_llm else None
        R.log(f"{len(real)} real orders copied; model: {client.model if client else 'none'}")
        R.summary["model"] = client.model if client else None

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
            ex = {e["number"]: e for e in json.loads(json.dumps(A.pdf_from_sheet("hackathon")))["exercises"]}[7]
            P = ex["points"]
            r = A.route(A.RouteRequest(start=P[ex["start"]], end=P[ex["end"]], waypoints=[P[w] for w in ex["waypoints"]],
                                       waypoint_order="free", use_bdtopo=False))
            rid = json.loads(r.body)["route_id"]
            entry = A._traces[rid]

        def ask(question, ids, budget=None):
            out, m = R.measure(RC.chat, entry, [{"role": "user", "content": question}], "standard", ids, None, use_llm, budget)
            ctx = out["context"] or {}
            blocks = {b["block"]: b for b in ctx.get("blocks", [])}
            return out, m, ctx, blocks

        # ---------------------------------------------------------------------------------------- number of orders
        write(independent_orders(40))
        pool = [p.stem for p in real] + [f"ind_{i:02d}" for i in range(1, 41)]
        for n in N_ORDERS:
            ids = pool[:n]
            with R.phase("orders"):
                out, m, ctx, blocks = ask("Quelles règles de vitesse dois-je respecter sur ce trajet ?", ids)
            order_blocks = [b for k, b in blocks.items() if k.startswith("ordre ")]
            rec = {"test": "orders", "orders": n, "used_tokens": ctx.get("used_tokens"), "budget": ctx.get("budget_tokens"),
                   "rules_tokens": (blocks.get("règles des ordres") or {}).get("tokens"),
                   "order_blocks_full": sum(b["form"] == "full" for b in order_blocks),
                   "order_blocks_short": sum(b["form"] == "short" for b in order_blocks),
                   "order_blocks_omitted": sum(b["form"] == "omitted" for b in order_blocks),
                   "mode": out["mode"], "cited_rules": len(out["cited_rules"]), **m}
            R.record(rec)
            R.log(rec)
        clear(("ind_",))

        # ---------------------------------------------------------------------------------------- chain depth
        for k in DEPTH:
            write(chain_orders(k))
            last = f"ch_frago{k:02d}"
            res = L.resolve([last])
            eff = next((e for e in res["effective"] if e["key"] == "speed_max"), None)
            expected = f"{60 - 2 * k} km/h"
            with R.phase("chain"):
                out, m, ctx, blocks = ask("Quelle est la vitesse maximale en vigueur pour le convoi ?", [last])
            stale = L.stale_problems(out["answer"], res)
            rec = {"test": "chain", "depth": k, "orders_added": len(res["added"]), "expected": expected,
                   "computed_in_force": eff["in_force"]["printed"] if eff else None, "replaced_values": len(eff["replaced"]) if eff else 0,
                   "links_block_tokens": (blocks.get("liens entre ordres") or {}).get("tokens"),
                   "links_block_form": (blocks.get("liens entre ordres") or {}).get("form"),
                   "used_tokens": ctx.get("used_tokens"), "omitted": ctx.get("omitted"), "shortened": ctx.get("shortened"),
                   "mode": out["mode"], "answer_has_value": bool(re.search(rf"\b{60 - 2 * k}\s*km/h", out["answer"])),
                   "answer_stale": bool(stale), **m}
            R.record(rec)
            R.log({k_: v for k_, v in rec.items() if k_ not in ("omitted", "shortened")})
            clear(("ch_",))

        # ---------------------------------------------------------------------------------------- budget
        write(independent_orders(20) + chain_orders(10))
        ids = ["ch_frago10"] + [f"ind_{i:02d}" for i in range(1, 20)]
        for b in BUDGETS:
            res = L.resolve(ids)
            with R.phase("budget"):
                out, m, ctx, blocks = ask("Quelle est la vitesse maximale en vigueur pour le convoi de l'OPORD 50/2026 ?", ids, b)
            rec = {"test": "budget", "budget": b, "orders_selected": len(ids), "orders_total": len(ids) + len(res["added"]),
                   "used_tokens": ctx.get("used_tokens"), "links_block_form": (blocks.get("liens entre ordres") or {}).get("form"),
                   "links_block_tokens": (blocks.get("liens entre ordres") or {}).get("tokens"),
                   "omitted_blocks": len(ctx.get("omitted") or []), "shortened_blocks": len(ctx.get("shortened") or []),
                   "mode": out["mode"], "answer_has_value": bool(re.search(r"\b40\s*km/h", out["answer"])),
                   "answer_stale": bool(L.stale_problems(out["answer"], res)), **m}
            R.record(rec)
            R.log(rec)
        clear(("ind_", "ch_"))

        # ---------------------------------------------------------------------------------------- conflicts
        for c in CONFL:
            write(conflict_orders(c))
            res = L.resolve(["cf_frago01"])
            with R.phase("conflicts"):
                out, m, ctx, blocks = ask("Quelle charge maximale pour les véhicules du convoi ?", ["cf_frago01"])
            k = next((x for x in res["conflicts"] if x["key"] == "load_max"), None)
            rec = {"test": "conflicts", "annexes": c, "conflict_reported_by_code": bool(k), "conflict_values": len(k["values"]) if k else 0,
                   "used_tokens": ctx.get("used_tokens"), "mode": out["mode"],
                   "answer_flags_conflict": bool(re.search(r"conflit|contradict|trancher|commandement|diverg|incoh", out["answer"], re.I)),
                   **m}
            R.record(rec)
            R.log(rec)
            clear(("cf_",))

        recs = R.records
        R.summary = {"model": R.summary.get("model"),
                     "orders_max_used_tokens": max(r["used_tokens"] for r in recs if r["test"] == "orders"),
                     "chain_correct_in_force": all(r["computed_in_force"] == r["expected"] for r in recs if r["test"] == "chain"),
                     "chain_answers_with_value": sum(r["answer_has_value"] for r in recs if r["test"] == "chain"),
                     "chain_answers_stale": sum(r["answer_stale"] for r in recs if r["test"] == "chain"),
                     "chain_runs": sum(r["test"] == "chain" for r in recs),
                     "budget_links_never_omitted": all(r["links_block_form"] in ("full", "short") for r in recs if r["test"] == "budget"),
                     "conflicts_flagged": sum(r["answer_flags_conflict"] for r in recs if r["test"] == "conflicts" and r["annexes"] > 0),
                     "conflict_runs": sum(r["test"] == "conflicts" and r["annexes"] > 0 for r in recs)}
        R.log("summary", R.summary)

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(1, 3, figsize=(13, 3.6))
        o = [r for r in recs if r["test"] == "orders"]
        ax[0].plot([r["orders"] for r in o], [r["used_tokens"] for r in o], "o-", label="used")
        ax[0].axhline(o[0]["budget"], color="grey", ls="--", label="budget")
        ax[0].set_xscale("log")
        ax[0].set_title("context tokens vs orders selected")
        ax[0].legend()
        ch = [r for r in recs if r["test"] == "chain"]
        ax[1].plot([r["depth"] for r in ch], [r["used_tokens"] for r in ch], "o-", label="context")
        ax[1].plot([r["depth"] for r in ch], [r["links_block_tokens"] or 0 for r in ch], "s-", label="links block")
        ax[1].set_title("tokens vs chain depth")
        ax[1].legend()
        bu = [r for r in recs if r["test"] == "budget"]
        ax[2].bar([str(r["budget"]) for r in bu], [r["omitted_blocks"] for r in bu], label="omitted blocks")
        ax[2].bar([str(r["budget"]) for r in bu], [r["shortened_blocks"] for r in bu], bottom=[r["omitted_blocks"] for r in bu], label="shortened")
        ax[2].set_title("budget (20 orders + chain of 10)")
        ax[2].legend()
        fig.tight_layout()
        fig.savefig(R.dir / "context_stress.png", dpi=120)


if __name__ == "__main__":
    main()
