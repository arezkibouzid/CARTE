"""Command line: ``coa-net demo | plan | compile-doctrine | bench | fetch-osm | templates``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .llm import from_env
from .parsing import parse_time
from .pipeline import COANet
from .requirements import load_requirements, save_requirements
from .scenarios import sample_incidents, sample_text, synthetic_theatre
from .schedule import hhmm


def _llm(args):
    llm = from_env() if getattr(args, "llm", False) else None
    if getattr(args, "llm", False) and llm is None:
        sys.exit("--llm needs COA_LLM_BASE_URL and COA_LLM_MODEL (any OpenAI-compatible server: vLLM, llama.cpp, Ollama)")
    return llm


def _session(G, args) -> COANet:
    return COANet.from_graph(G, requirements=load_requirements(args.requirements), llm=_llm(args), use_cpsat=not args.no_cpsat,
                             sim_reps=args.reps)


def _run(session: COANet, order: str, incidents: list[tuple[float, str]], out: Path, title: str, coa: str | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    coas = session.plan(order)
    if session.mission.ambiguities:
        print("ambiguities flagged: " + "; ".join(session.mission.ambiguities))
    print(session.intent.describe())
    print()
    for c in coas:
        tag = " (default: confirmed intent order)" if c.name == session.default else ""
        print(f"COA {c.label}{tag}: {c.design.description}; {c.report.summary()}; {len(c.playbooks)} playbooks; "
              f"{len(c.recce)} reconnaissance task(s); design {c.design.solver} {c.design.seconds:.2f} s")
        for gid, r in sorted(c.design.msr.items()):
            nm = c.ref.name_of(r)
            asr = ", ".join(c.ref.name_of(a) or "ASR" for a in c.design.asrs.get(gid, [])) or "none"
            print(f"   {gid}: MSR {nm} {r.length_m / 1000:.1f} km {r.time_min:.1f} min; ASR {asr}")
    print()
    print(session.comparison)
    for x in session.concerns:
        print("  concern: " + x)
    chosen = session.choose(coa)
    print(f"\nchosen: COA {chosen.label}")
    for c in chosen.sched.credits.values():
        print(f"   {c.unit}: depart {hhmm(c.depart)}, arrive {hhmm(c.arrive)}" + (f" (deadline {hhmm(c.deadline)})" if c.deadline is not None else ""))
    for t, text in incidents:
        o = session.incident(text, t)
        print(f"\n[{hhmm(t)}] {o.incident.id} {o.incident.type.value}: {o.summary} -> {o.mode}"
              + (f" {o.playbook}" if o.playbook else "") + f" in {o.seconds:.2f} s")
        for line in o.restoration:
            print("   " + line)
        print(o.frago)
    page = session.report(out, f"COA-Net: {title}")
    print(f"\nwrote {page} (+ panel_*.svg, network.geojson, facts.md, fragos.txt, metrics.json)")
    t = session.metrics()["timings_s"]
    print("timings (s): " + ", ".join(f"{k} {v:.2f}" for k, v in t.items()))


def cmd_demo(args) -> None:
    th = synthetic_theatre(args.seed)
    print("Synthetic theatre, planted constraints:\n  - " + "\n  - ".join(th.planted) + "\n")
    session = _session(th.G, args)
    incidents = [(parse_time(i["time"]), i["text"]) for i in sample_incidents()]
    _run(session, sample_text("sample_order.txt"), incidents, Path(args.out), "synthetic theatre", args.coa)


def cmd_plan(args) -> None:
    if args.graphml or args.place or args.bdtopo:
        from .loaders import add_places, load_bdtopo, load_osm

        places = {k: tuple(v) for k, v in json.loads(Path(args.places).read_text()).items()} if args.places else None
        if args.bdtopo:
            G = load_bdtopo(args.bdtopo)
            if places:
                add_places(G, places)
        else:
            G = load_osm(place=args.place, graphml=args.graphml, places=places, dem=args.dem)
    else:
        G = synthetic_theatre(args.synthetic).G
    session = _session(G, args)
    incidents = []
    for spec in args.incident or []:
        t, _, text = spec.partition("@")
        incidents.append((parse_time(t.strip()), text.strip()))
    _run(session, Path(args.order).read_text(encoding="utf-8"), incidents, Path(args.out), args.place or args.graphml or args.bdtopo or "map", args.coa)


def cmd_compile(args) -> None:
    from .doctrine import compile_doctrine

    text = Path(args.doctrine).read_text(encoding="utf-8") if args.doctrine else sample_text("sample_doctrine.txt")
    llm = _llm(args)
    reqs, rep = compile_doctrine(text, Path(args.doctrine).name if args.doctrine else "sample_doctrine", llm)
    print(rep.markdown())
    for line in rep.log:
        print("  " + line)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    save_requirements(reqs, args.out)
    print(f"\n{len(reqs)} draft requirement instances -> {args.out} (status draft: review, then approve)"
          + (f"; tokens in/out {llm.usage.input_tokens}/{llm.usage.output_tokens}" if llm else "; pattern fallback, no model"))


def cmd_bench(args) -> None:
    from .bench import incidents_markdown, run_incident_bench, run_planning_bench, to_markdown

    seeds = range(args.seeds)
    text = "\n\n".join([
        to_markdown(run_planning_bench(seeds, args.convoys, reps=args.reps, use_cpsat=not args.no_cpsat), "Planning: baselines and COA-Net"),
        incidents_markdown(run_incident_bench(seeds, use_cpsat=not args.no_cpsat), "Incident loop: with and without playbooks"),
    ])
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")


def cmd_fetch(args) -> None:
    from .loaders import save_graphml_osm

    save_graphml_osm(args.place, args.out)
    print(f"saved {args.out}")


def cmd_templates(args) -> None:
    from .requirements import catalogue_markdown

    print(catalogue_markdown())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="coa-net", description="COA-Net: survivable route networks and courses of action for logistic convoys")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, reps: int = 100):
        p.add_argument("--requirements", help="requirement base JSON (default: bundled seed base)")
        p.add_argument("--llm", action="store_true", help="use the model configured by COA_LLM_* environment variables")
        p.add_argument("--reps", type=int, default=reps, help="Monte Carlo replications per COA (0: none)")
        p.add_argument("--no-cpsat", action="store_true", help="greedy design and scheduling (no OR-Tools)")
        p.add_argument("--coa", help="COA to execute (default: the one built from the confirmed intent)")

    d = sub.add_parser("demo", help="end-to-end demo on the synthetic theatre (order LOG-01, three incidents)")
    d.add_argument("--out", default="outputs/demo")
    d.add_argument("--seed", type=int, default=0)
    common(d)
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
    p.add_argument("--incident", action="append", help='"HH:MM@free-text report", repeatable')
    p.add_argument("--out", default="outputs/plan")
    common(p)
    p.set_defaults(fn=cmd_plan)

    c = sub.add_parser("compile-doctrine", help="compile doctrine text into draft requirement instances, with coverage")
    c.add_argument("doctrine", nargs="?", help="text file (default: bundled synthetic sample)")
    c.add_argument("--out", default="outputs/compiled_requirements.json")
    c.add_argument("--llm", action="store_true")
    c.set_defaults(fn=cmd_compile)

    b = sub.add_parser("bench", help="baselines B0-B3 vs COA-Net and the incident loop on synthetic scenarios")
    b.add_argument("--seeds", type=int, default=20)
    b.add_argument("--convoys", type=int, default=None)
    b.add_argument("--reps", type=int, default=50)
    b.add_argument("--no-cpsat", action="store_true")
    b.add_argument("--out", default="outputs/bench.md")
    b.set_defaults(fn=cmd_bench)

    f = sub.add_parser("fetch-osm", help="download and cache an OSM road graph")
    f.add_argument("--place", required=True)
    f.add_argument("--out", required=True)
    f.set_defaults(fn=cmd_fetch)

    t = sub.add_parser("templates", help="print the requirement template catalogue")
    t.set_defaults(fn=cmd_templates)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
