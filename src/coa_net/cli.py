"""Command line: ``coa-net demo | plan | exercise | read-order | compile-doctrine | signoff | audit | bench | fetch-osm | templates``."""

from __future__ import annotations

import argparse
import json
import sys
import time
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


def _base(args, reqs=None):
    """The requirement base of a run: the given one, or ``--requirements`` (default seed); drafts used as approved with
    ``--provisional`` (doctrine compiled at the event, before sign-off), with a note for the report."""
    reqs = reqs if reqs is not None else load_requirements(getattr(args, "requirements", None))
    notes = []
    if getattr(args, "provisional", False):
        from .corpus import provisional

        reqs, ids = provisional(reqs)
        if ids:
            notes.append(f"PROVISIONAL: {len(ids)} requirement instances compiled from the doctrine documents are used before "
                         f"sign-off ({', '.join(ids[:12])}{', ...' if len(ids) > 12 else ''})")
            print(notes[-1])
    return reqs, notes


def _order_text(path: str, out: Path | None = None):
    """Text of an order file, and its reading: plain text, or a document (PDF, Word, image) read by orderdoc."""
    from .orderdoc import read_order, reading_from_text

    p = Path(path)
    if p.suffix.lower() in (".txt", ".md", ".text", ""):
        try:
            text = p.read_text(encoding="utf-8")
            return text, reading_from_text(text, p.name)
        except UnicodeDecodeError:
            pass
    reading = read_order(p)
    print(f"read {p.name}: {reading.doc.format}, {len(reading.doc.blocks)} text blocks, {len(reading.doc.figures)} figure(s); "
          f"task read as '{reading.task}' ({reading.task_reason})")
    return reading.text, reading


def _session(G, args, max_rank: int | None = None, requirements=None, **kw) -> COANet:
    from .corridors import CorridorGraph

    reqs, notes = _base(args, requirements)
    if notes:
        kw["data_notes"] = list(kw.get("data_notes") or []) + notes
    return COANet(CorridorGraph(G, max_rank=max_rank), requirements=reqs, llm=_llm(args), use_cpsat=not args.no_cpsat,
                  sim_reps=args.reps, **kw)


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
              + (f" {o.playbook}" if o.playbook else "") + f" in {o.seconds:.2f} s"
              + (f" (restoration options {o.restore_seconds:.1f} s more)" if o.restore_seconds else ""))
        for line in o.notes + o.restoration:
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
    kw, reqs, data_notes = {}, None, []
    if args.graphml or args.place or args.bdtopo:
        from .loaders import add_places, load_bdtopo, load_osm

        places = {k: tuple(v) for k, v in json.loads(Path(args.places).read_text()).items()} if args.places else None
        if args.bdtopo and args.bdtopo.lower().endswith(".shp"):
            # organisers' Scenario data: BD TOPO v2 ROUTE shapefile without .prj (see realdata.py)
            from .realdata import load_scenario_bdtopo, roadside_halts, systemic_unknowns

            G, rep = load_scenario_bdtopo(args.bdtopo)
            print(f"BD TOPO shapefile: {rep.kept} of {rep.features} sections kept, {rep.nodes} junctions, {rep.km:.0f} km "
                  f"({rep.seconds:.0f} s)" + ("; CRS assumed EPSG:4326 from the bounds" if rep.crs_assumed else ""))
            reqs, notes = systemic_unknowns(G, load_requirements(args.requirements))
            notes += roadside_halts(G, max_rank=min(3, args.max_rank or 3))
            for n in notes:
                print("data-driven policy change: " + n)
            data_notes = [f"BD TOPO shapefile: {rep.kept} of {rep.features} sections kept, {rep.km:.0f} km"
                          + ("; CRS assumed EPSG:4326 from the bounds" if rep.crs_assumed else "")] + notes
            if places:
                # town labels sit on side streets and tracks: start and end on the nearest secondary road or better
                for name, info in add_places(G, places, snap_max_rank=3).items():
                    print(f"place {name}: junction {info['node']} on {', '.join(info['roads']) or 'an unnamed road'}, "
                          f"{info['distance_m']} m from the map position")
        elif args.bdtopo:
            G = load_bdtopo(args.bdtopo)
            if places:
                add_places(G, places)
        else:
            G = load_osm(place=args.place, graphml=args.graphml, places=places, dem=args.dem)
    else:
        G = synthetic_theatre(args.synthetic).G
    if args.recce_max is not None:
        kw["recce_max_elements"] = args.recce_max
    t0 = time.perf_counter()
    session = _session(G, args, max_rank=args.max_rank, requirements=reqs, data_notes=data_notes, **kw)
    H = session.H
    print(f"corridor graph: {len(H.corridors)} corridors, {len(H.dps)} decision points, {len(H.elements)} critical elements "
          f"({time.perf_counter() - t0:.0f} s)")
    incidents = []
    for spec in args.incident or []:
        t, _, text = spec.partition("@")
        if Path(text.strip()).is_file():  # an incident given as a document (format to confirm with the ETLO)
            text = _order_text(text.strip())[0]
        incidents.append((parse_time(t.strip()), text.strip()))
    order_text, reading = _order_text(args.order)
    if reading.task == "itineraries":
        print("note: this order asks for itineraries between drawn zones; `coa-net exercise --pdf` solves that task")
    _run(session, order_text, incidents, Path(args.out), args.place or args.graphml or args.bdtopo or "map", args.coa)
    from .orderdoc import movement_readback

    (Path(args.out) / "readback.md").write_text(movement_readback(reading, session.mission), encoding="utf-8")
    print(f"readback of the order: {Path(args.out) / 'readback.md'}")


def cmd_compile(args) -> None:
    """Doctrine documents (files or folders: PDF, Word, text) -> requirement base, report, sign-off sheet, document log."""
    from .corpus import compile_corpus, compile_paths, document_log, write_signoff_sheet
    from .documents import read_document

    llm = _llm(args)
    out = Path(args.out)
    base_path = out if out.suffix == ".json" else out / "requirements.json"
    folder = base_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    precedence = [x.strip() for x in args.precedence.split(",")] if args.precedence else None
    if args.paths:
        docs, base, rep = compile_paths(args.paths, llm=llm, precedence=precedence, use_seed=not args.no_seed)
    else:  # bundled synthetic sample
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "sample_doctrine.txt"
            f.write_text(sample_text("sample_doctrine.txt"), encoding="utf-8")
            docs = [read_document(f)]
        base, rep = compile_corpus(docs, llm=llm, precedence=precedence, use_seed=not args.no_seed)
    save_requirements(base, base_path)
    save_requirements([r for r in base if not r.source.document.startswith("seed base")], folder / "requirements_doctrine.json")
    (folder / "corpus_report.md").write_text(rep.markdown(base), encoding="utf-8")
    n = write_signoff_sheet(base, rep, folder / "signoff.csv")
    (folder / "documents.json").write_text(json.dumps(document_log(docs), indent=1, ensure_ascii=False), encoding="utf-8")
    t = rep.totals()
    for d in rep.docs:
        print(f"  {d.id}: {d.kind}, {d.pages} pages, {d.normative} normative sentences, {d.instances} instances"
              + (f" ({'; '.join(d.warnings)})" if d.warnings else ""))
    for x in rep.skipped:
        print("  not read: " + x)
    print(f"{t['documents']} documents, {t['pages']} pages, {t['normative']} normative sentences; {t['instances']} instances "
          f"({n} to sign off), {t['conflicts']} conflicts; coverage {t['coverage']:.0%}, expressiveness {t['expressiveness']:.0%}; "
          f"seed: {len(rep.seed_superseded)} replaced, {len(rep.seed_kept)} kept; "
          f"{sum(rep.seconds.values()):.1f} s" + (f"; tokens in/out {llm.usage.input_tokens}/{llm.usage.output_tokens}" if llm else "; pattern compiler, no model"))
    print(f"wrote {base_path} (base: doctrine drafts + seed), {folder / 'signoff.csv'}, {folder / 'corpus_report.md'}, {folder / 'documents.json'}")
    print("next: fill the decision column of signoff.csv, then `coa-net signoff`; until then plan with --provisional")


def cmd_signoff(args) -> None:
    from .corpus import apply_signoff

    base, notes = apply_signoff(load_requirements(args.base), args.sheet, reviewer=args.reviewer or "")
    for n in notes:
        print("  " + n)
    save_requirements(base, args.out)
    print(f"signed base -> {args.out}")


def cmd_read_order(args) -> None:
    """What an order document asks, as read, without the map: the readback and the command that solves it."""
    from .exercise import parse_exercise
    from .orderdoc import exercise_readback, movement_readback
    from .parsing import parse_order

    text, reading = _order_text(args.order)
    if reading.task == "itineraries":
        spec, _ = parse_exercise(text)
        rb = exercise_readback(reading, spec)
        nxt = f"coa-net exercise --data DATA --pdf {args.order} --out outputs/<exercise>"
    else:
        rb = movement_readback(reading, parse_order(text))
        nxt = f"coa-net plan --order {args.order} --bdtopo DATA/<...>_BDTopo.shp --places <places.json> --out outputs/<order>"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(rb, encoding="utf-8")
    print(rb)
    if reading.illustration is not None:
        f = reading.illustration
        print(f"illustration: {f.id}, {f.size_px} px" + (f", caption '{f.caption}'" if f.caption else "")
              + "; its marks are read with the map (georeferencing on the scans)")
    print(f"next: {nxt}")


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


def cmd_exercise(args) -> None:
    from .orderdoc import exercise_readback, reading_from_text
    from .scenario import EXERCISE_1, ScenarioPaths, run_exercise

    text, sketch, reading = EXERCISE_1, args.sketch, None
    if args.pdf:
        from .scenario import exercise_from_pdf

        text, pdf_sketch, reading = exercise_from_pdf(args.pdf, Path(args.out) / "input", with_reading=True)
        sketch = sketch or pdf_sketch
        print(f"read the statement and the illustration ({pdf_sketch}) from {args.pdf}: task '{reading.task}' ({reading.task_reason})")
    if not sketch:
        sys.exit("exercise needs the zones: give --pdf (exercise PDF) or --sketch (image)")
    paths = ScenarioPaths.from_dir(args.data, sketch=sketch)
    for k in ("bdtopo", "osm", "scan100", "scan250"):
        if getattr(args, k, None):
            setattr(paths, k, getattr(args, k))
    if args.exercise:
        text, reading = _order_text(args.exercise)
    if reading is None:
        reading = reading_from_text(text, "exercise.txt")
    reqs, notes = _base(args) if (args.requirements or args.provisional) else (None, [])
    run = run_exercise(paths, args.out, text, cache=args.cache, n_seeds=args.seeds, turn_aware=not args.no_turns,
                       compare=not args.no_compare, osm_check=not args.no_osm, llm=_llm(args), requirements=reqs)
    from .exercise import parse_exercise

    rb = exercise_readback(reading, parse_exercise(text)[0], run.facts)
    if notes:
        rb += "\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    (Path(args.out) / "readback.md").write_text(rb, encoding="utf-8")
    print((Path(args.out) / "decision_process.txt").read_text(encoding="utf-8"))
    print(f"outputs in {args.out}: itineraries.geojson, difficult_areas.geojson, zones.geojson, map_*.png, decision_process.txt, "
          "metrics.json, readback.md, sketch_marks.png")


def cmd_audit(args) -> None:
    from .audit import audit
    from .scenario import ScenarioPaths

    rep = audit(ScenarioPaths.from_dir(args.data), args.out)
    print(json.dumps({k: v for k, v in rep.items() if k != "files"}, indent=1))
    print(f"full report: {args.out}")


def cmd_templates(args) -> None:
    from .requirements import catalogue_markdown

    print(catalogue_markdown())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="coa-net", description="COA-Net: survivable route networks and courses of action for logistic convoys")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, reps: int = 100):
        p.add_argument("--requirements", help="requirement base JSON (default: bundled seed base)")
        p.add_argument("--provisional", action="store_true", help="use draft instances (doctrine not signed off yet), flagged in the report")
        p.add_argument("--llm", action="store_true", help="use the model configured by COA_LLM_* environment variables")
        p.add_argument("--reps", type=int, default=reps, help="Monte Carlo replications per COA (0: none)")
        p.add_argument("--no-cpsat", action="store_true", help="greedy design and scheduling (no OR-Tools)")
        p.add_argument("--coa", help="COA to execute (default: the one built from the confirmed intent)")

    d = sub.add_parser("demo", help="end-to-end demo on the synthetic theatre (order LOG-01, three incidents)")
    d.add_argument("--out", default="outputs/demo")
    d.add_argument("--seed", type=int, default=0)
    common(d)
    d.set_defaults(fn=cmd_demo)

    p = sub.add_parser("plan", help="plan a mission from an order (text, PDF, Word)")
    p.add_argument("--order", required=True, help="order file: text, or a document read by orderdoc (PDF, .docx, image)")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--place", help="OSM place name, downloaded with osmnx")
    src.add_argument("--graphml", help="cached OSM graph (see fetch-osm)")
    src.add_argument("--bdtopo", help="BD TOPO GeoPackage with a troncon_de_route layer, or the organisers' v2 ROUTE shapefile (.shp)")
    src.add_argument("--synthetic", type=int, default=0, help="synthetic theatre seed (default)")
    p.add_argument("--places", help='JSON {"name": [lon, lat]} gazetteer for the order')
    p.add_argument("--dem", help="DEM raster for slopes (needs rasterio)")
    p.add_argument("--incident", action="append", help='"HH:MM@free-text report", repeatable')
    p.add_argument("--max-rank", type=int, help="corridor backbone: roads of rank <= N (1 motorway .. 5 minor) plus critical "
                   "elements; 4 is a good value on a whole BD TOPO extract (default: every road)")
    p.add_argument("--recce-max", type=int, help="value of information: evaluate at most N unknown elements per COA, "
                   "the closest to the designated network (large maps)")
    p.add_argument("--out", default="outputs/plan")
    common(p)
    p.set_defaults(fn=cmd_plan)

    c = sub.add_parser("compile-doctrine", help="compile a set of doctrine documents (PDF, Word, text; files or folders) into a "
                       "requirement base with citations, conflicts, coverage and a sign-off sheet")
    c.add_argument("paths", nargs="*", help="documents or folders (default: bundled synthetic sample)")
    c.add_argument("--out", default="outputs/doctrine", help="output folder (or the base's .json path)")
    c.add_argument("--precedence", help="comma-separated document ids or title words, strongest first (default: movement "
                   "standards, then general doctrine, then other documents)")
    c.add_argument("--no-seed", action="store_true", help="do not fill the gaps with the seed base")
    c.add_argument("--llm", action="store_true")
    c.set_defaults(fn=cmd_compile)

    so = sub.add_parser("signoff", help="apply a filled sign-off sheet (accept / edit / reject) to a compiled base")
    so.add_argument("--base", required=True)
    so.add_argument("--sheet", required=True)
    so.add_argument("--out", required=True)
    so.add_argument("--reviewer")
    so.set_defaults(fn=cmd_signoff)

    ro = sub.add_parser("read-order", help="read an order document (PDF, Word, text, image): task, constraints, desired "
                        "outputs, checks; prints the readback and the command that solves it")
    ro.add_argument("order")
    ro.add_argument("--out", help="write the readback (Markdown) here")
    ro.set_defaults(fn=cmd_read_order)

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

    x = sub.add_parser("exercise", help="solve an exercise on the organisers' Scenario data (shapefiles, scans, sketch)")
    x.add_argument("--data", required=True, help="folder with Scenario*_BDTopo.shp/.dbf/.shx, *_OSM.*, *100K.tif, *250K.tif")
    x.add_argument("--pdf", help="exercise document (PDF, Word or image): statement, constraints, desired outputs and the "
                   "illustration (largest figure) are read from it, with a readback")
    x.add_argument("--sketch", help="exercise illustration (image) with the zones drawn on it (overrides the PDF's)")
    x.add_argument("--exercise", help="exercise text or document (overrides the PDF's text; default: the preparation exercise)")
    x.add_argument("--out", default="outputs/exercise1")
    x.add_argument("--cache", help="cache folder for OCR, built-up areas and zones (default: OUT/cache)")
    x.add_argument("--seeds", type=int, default=20, help="first-itinerary seeds for NET.DISTINCT")
    x.add_argument("--no-turns", action="store_true", help="search on the junction graph without turn costs")
    x.add_argument("--no-compare", action="store_true", help="skip the comparison with the search without turn costs")
    x.add_argument("--no-osm", action="store_true", help="skip the OSM cross-check")
    x.add_argument("--requirements", help="requirement base JSON (default: seed base; e.g. the base compiled from the doctrine)")
    x.add_argument("--provisional", action="store_true", help="use draft instances (doctrine not signed off yet), flagged")
    x.add_argument("--llm", action="store_true", help="name the sketch's marks with the model (L9, needs a vision-language model)")
    for k in ("bdtopo", "osm", "scan100", "scan250"):
        x.add_argument(f"--{k}", help=f"explicit path for {k}")
    x.set_defaults(fn=cmd_exercise)

    a = sub.add_parser("audit", help="audit a Scenario delivery: files, CRS, encodings, OSM/BD TOPO agreement, scan alignment")
    a.add_argument("--data", required=True)
    a.add_argument("--out", default="outputs/audit.json")
    a.set_defaults(fn=cmd_audit)

    t = sub.add_parser("templates", help="print the requirement template catalogue")
    t.set_defaults(fn=cmd_templates)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
