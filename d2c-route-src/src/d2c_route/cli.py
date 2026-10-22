"""Command line: ``d2c-route demo | plan | compile-doctrine | bench | fetch-osm``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .constraints import load_rules, save_rules
from .explain import hhmm
from .llm import from_env
from .pipeline import D2CRoute
from .render import write_html
from .scenarios import sample_incidents, sample_text, synthetic_theatre


def _hm(s: str) -> float:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _llm(args):
    llm = from_env() if getattr(args, "llm", False) else None
    if getattr(args, "llm", False) and llm is None:
        sys.exit("--llm needs D2C_LLM_BASE_URL and D2C_LLM_MODEL (any OpenAI-compatible server: vLLM, llama.cpp, Ollama)")
    return llm


def _run_session(session: D2CRoute, order: str, incidents: list[tuple[float, str]], out: Path, title: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    plan, rep = session.plan_mission(order)
    print(f"[plan v0] {rep.summary()}")
    print("  " + json.dumps(rep.metrics))
    if session.mission.ambiguities:
        print("  ambiguities flagged: " + "; ".join(session.mission.ambiguities))
    panels = [(f"v0: initial plan ({title})", session.svg("v0 initial plan"))]
    explanations = [session.explain()]
    for t, text in incidents:
        plan, rr, rep = session.incident(text, t)
        inc = rr.incident
        print(f"[{hhmm(t)}] {inc.type.value} @ {inc.location}: {'; '.join(rr.messages)}")
        print(f"  repair {rr.latency_s * 1000:.0f} ms, replanned {rr.affected}, unchanged {rr.unchanged_share:.0%} -> {rep.summary()}")
        label = f"v{plan.version}: after {inc.id} ({inc.type.value}) at {hhmm(t)}"
        panels.append((label, session.svg(label)))
        explanations.append(session.explain())
    (out / "explanation.md").write_text("\n\n---\n\n".join(explanations), encoding="utf-8")
    (out / "plan.json").write_text(session.plan.model_dump_json(indent=1), encoding="utf-8")
    (out / "constraints.json").write_text(session.program.model_dump_json(indent=1), encoding="utf-8")
    metrics = {"final": session.report.metrics, "repairs": [
        {"incident": r.incident.model_dump(mode="json"), "latency_s": r.latency_s, "affected": r.affected, "unchanged_share": r.unchanged_share}
        for r in session.repairs
    ]}
    if session.llm is not None:
        u = session.llm.usage
        metrics["llm_usage"] = {"calls": u.calls, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "by_stage": u.by_stage}
    (out / "metrics.json").write_text(json.dumps(metrics, indent=1, default=str), encoding="utf-8")
    for i, (_, svg) in enumerate(panels):
        (out / f"plan_v{i}.svg").write_text(svg, encoding="utf-8")
    page = write_html(out / "report.html", panels, explanations[-1])
    print(f"\nwrote {page} (+ explanation.md, plan.json, constraints.json, metrics.json, plan_v*.svg)")


def cmd_demo(args) -> None:
    th = synthetic_theatre(args.seed)
    print("Synthetic theatre, planted constraints:\n  - " + "\n  - ".join(th.planted) + "\n")
    session = D2CRoute(th.G, rules=load_rules(args.rules), llm=_llm(args))
    incidents = [(_hm(i["time"]), i["text"]) for i in sample_incidents()]
    _run_session(session, sample_text("sample_order.txt"), incidents, Path(args.out), "synthetic theatre")


def cmd_plan(args) -> None:
    if args.graphml or args.place or args.bdtopo:
        from .loaders import load_bdtopo, load_osm

        places = {k: tuple(v) for k, v in json.loads(Path(args.places).read_text()).items()} if args.places else None
        if args.bdtopo:
            G = load_bdtopo(args.bdtopo)
            if places:
                from .loaders import add_places

                add_places(G, places)
        else:
            G = load_osm(place=args.place, graphml=args.graphml, places=places, dem=args.dem)
    else:
        G = synthetic_theatre(args.synthetic).G
    session = D2CRoute(G, rules=load_rules(args.rules), llm=_llm(args))
    incidents = []
    for spec in args.incident or []:
        t, _, text = spec.partition("@")
        incidents.append((_hm(t.strip()), text.strip()))
    _run_session(session, Path(args.order).read_text(encoding="utf-8"), incidents, Path(args.out), args.place or args.graphml or "map")


def cmd_compile(args) -> None:
    from .parsing import compile_doctrine

    text = Path(args.doctrine).read_text(encoding="utf-8") if args.doctrine else sample_text("sample_doctrine.txt")
    llm = _llm(args)
    rules, log = compile_doctrine(text, Path(args.doctrine).name if args.doctrine else "sample_doctrine", llm)
    for line in log:
        print("  " + line)
    for r in rules:
        expr = r.predicate or r.feature or r.params
        print(f"{r.id} [{r.type.value}] {expr}  scope={r.scope.model_dump(exclude_defaults=True)}")
    save_rules(rules, args.out)
    print(f"\n{len(rules)} rules -> {args.out}" + (f" (LLM tokens in/out: {llm.usage.input_tokens}/{llm.usage.output_tokens})" if llm else " (pattern fallback, no LLM)"))


def cmd_bench(args) -> None:
    from .bench import run_planning_bench, run_repair_bench, to_markdown

    seeds = range(args.seeds)
    md = [
        to_markdown(run_planning_bench(seeds, args.convoys), "Planning: baselines and ablations"),
        "",
        to_markdown(run_repair_bench(seeds), "Incident repair: local vs full re-plan"),
    ]
    text = "\n".join(md)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")


def cmd_fetch(args) -> None:
    from .loaders import save_graphml_osm

    save_graphml_osm(args.place, args.out)
    print(f"saved {args.out}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="d2c-route", description="Doctrine-to-Constraint routing for logistic convoys")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="end-to-end demo on the synthetic theatre")
    d.add_argument("--out", default="outputs/demo")
    d.add_argument("--seed", type=int, default=0)
    d.add_argument("--rules", help="rule base JSON (default: seed rules)")
    d.add_argument("--llm", action="store_true", help="use the LLM configured by D2C_LLM_* env vars")
    d.set_defaults(fn=cmd_demo)

    p = sub.add_parser("plan", help="plan a mission from an order file")
    p.add_argument("--order", required=True)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--place", help="OSM place name, downloaded with osmnx")
    src.add_argument("--graphml", help="cached OSM graph (see fetch-osm)")
    src.add_argument("--bdtopo", help="BD TOPO GeoPackage with a troncon_de_route layer")
    src.add_argument("--synthetic", type=int, default=0, help="synthetic theatre seed (default)")
    p.add_argument("--places", help='JSON {"name": [lon, lat]} gazetteer for the order')
    p.add_argument("--dem", help="DEM raster for slopes (needs rasterio)")
    p.add_argument("--rules")
    p.add_argument("--incident", action="append", help='"HH:MM@free-text report", repeatable')
    p.add_argument("--out", default="outputs/plan")
    p.add_argument("--llm", action="store_true")
    p.set_defaults(fn=cmd_plan)

    c = sub.add_parser("compile-doctrine", help="compile doctrine text into a rule base")
    c.add_argument("doctrine", nargs="?", help="text file (default: bundled synthetic sample)")
    c.add_argument("--out", default="outputs/compiled_rules.json")
    c.add_argument("--llm", action="store_true")
    c.set_defaults(fn=cmd_compile)

    b = sub.add_parser("bench", help="baselines, ablations and repair study on synthetic scenarios")
    b.add_argument("--seeds", type=int, default=10)
    b.add_argument("--convoys", type=int, default=None)
    b.add_argument("--out", default="outputs/bench.md")
    b.set_defaults(fn=cmd_bench)

    f = sub.add_parser("fetch-osm", help="download and cache an OSM road graph")
    f.add_argument("--place", required=True)
    f.add_argument("--out", required=True)
    f.set_defaults(fn=cmd_fetch)

    args = ap.parse_args(argv)
    if getattr(args, "out", None) and args.cmd == "compile-doctrine":
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    args.fn(args)


if __name__ == "__main__":
    main()
