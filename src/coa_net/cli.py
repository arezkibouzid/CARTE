"""Command line: ``coa-net demo | plan | exercise | scan-roads | scan-learn | scan-unet | real-bench | read-order | compile-doctrine | signoff | llm-check | audit | bench | fetch-osm |
templates``."""

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
    """The locally deployed model for ``--llm`` (``llm.from_env``), checked before the run; None without ``--llm``."""
    if not getattr(args, "llm", False):
        return None
    from .llm import LLMConfigError, LLMUnavailable

    try:
        llm = from_env()
    except LLMConfigError as exc:
        sys.exit(f"--llm refused: {exc}")
    except LLMUnavailable as exc:
        sys.exit(f"--llm: {exc}. Start the local model server (deploy/README.md) and check it with `coa-net llm-check`, "
                 "or run without --llm: every model role has a deterministic fallback")
    d = llm.describe()
    print(f"language model: {d['model']} at {d['base_url']} ({'local' if d['local'] else 'remote, accepted with COA_LLM_ALLOW_REMOTE=1'})")
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
    from .scanlearn import write_plan_checks

    n_checks = write_plan_checks(session.H.G, chosen, out)
    if n_checks:
        print(f"   map checks before the move (image network): {n_checks} stretches on the MSRs and ASRs, in {out / 'map_checks.md'}")
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
    if args.graphml or args.place or args.bdtopo or args.scan:
        from .loaders import add_places, load_bdtopo, load_osm

        places = {k: tuple(v) for k, v in json.loads(Path(args.places).read_text(encoding="utf-8")).items()} if args.places else None
        if args.scan:
            # image input: the road network is read from the scanned map (see scanroads.py)
            from .realdata import roadside_halts, systemic_unknowns
            from .scenario import load_graph_cached

            cache = Path(args.out) / "cache"
            cache.mkdir(parents=True, exist_ok=True)
            G, rep, _ = load_graph_cached(args.scan, cache, kind="scan")
            print(f"road network read from the scan {args.scan}: {rep.nodes} junctions, {rep.km:.0f} km ({rep.seconds:.0f} s)")
            if args.corrector:
                from .scanlearn import correct_graph

                print(f"learned corrector: {correct_graph(G, args.scan, args.corrector)}")
            reqs, notes = systemic_unknowns(G, load_requirements(args.requirements))
            notes += roadside_halts(G, max_rank=min(3, args.max_rank or 3))
            for n in notes:
                print("data-driven policy change: " + n)
            data_notes = [f"road network read from the scanned map {Path(args.scan).name}: {rep.km:.0f} km; widths, slopes and "
                          "load classes unknown (not on the map)"] + notes
            if places:
                for name, info in add_places(G, places, snap_max_rank=3).items():
                    print(f"place {name}: junction {info['node']}, {info['distance_m']} m from the map position")
        elif args.bdtopo and args.bdtopo.lower().endswith(".shp"):
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
    _run(session, order_text, incidents, Path(args.out), args.place or args.graphml or args.bdtopo or args.scan or "map", args.coa)
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


def _probe_image():
    """A 256-pixel PNG data URL of a red square on white (the image probe of llm-check), or None without Pillow."""
    try:
        import base64
        import io

        from PIL import Image, ImageDraw
    except ImportError:
        return None
    im = Image.new("RGB", (256, 256), "white")
    ImageDraw.Draw(im).rectangle((64, 64, 192, 192), fill=(220, 30, 30))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def cmd_llm_check(args) -> None:
    """Checks the local model server before a run: where it is, which model, JSON answers, images (L9), and speed."""
    from .llm import LLMConfigError, LLMUnavailable, call_validated

    try:
        llm = from_env()
    except LLMConfigError as exc:
        sys.exit(f"refused: {exc}")
    except LLMUnavailable as exc:
        sys.exit(f"{exc}\nstart the local model server (deploy/README.md), or run without --llm: every model role has a fallback")
    d = llm.describe()
    served = llm.models()
    rows: list[tuple[str, str, str]] = []
    report: dict = {"server": d, "served_models": served, "checks": {}}
    print(f"server {d['base_url']} ({'local' if d['local'] else 'REMOTE, accepted with COA_LLM_ALLOW_REMOTE=1'}); model {d['model']}; "
          f"served: {', '.join(served) or 'none listed'}")

    def probe(name: str, system: str, user: str, schema: dict, ok, images: list[str] | None = None):
        t0 = time.perf_counter()
        before = llm.usage.output_tokens
        try:
            out = (llm.complete_json_images(system, user, schema, images, stage="check") if images
                   else llm.complete_json(system, user, schema, stage="check"))
            good, detail = ok(out)
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not stop the check
            good, detail = False, f"{type(exc).__name__}: {str(exc)[:160]}"
        dt = time.perf_counter() - t0
        rows.append((name, ("ok" if good else "FAILED") + f": {detail}", f"{dt:.1f}"))
        report["checks"][name] = {"ok": good, "detail": detail, "seconds": round(dt, 2), "output_tokens": llm.usage.output_tokens - before}
        return good, dt, llm.usage.output_tokens - before

    def sum_ok(o):
        return o.get("sum") == 42, f"sum = {o.get('sum')}"

    json_ok, _, _ = probe("JSON answer", "You are a careful calculator.", "What is 17 + 25? Put the result in the field sum.",
                          {"type": "object", "properties": {"sum": {"type": "integer"}}, "required": ["sum"]}, sum_ok)
    schema_ok = llm.use_json_schema and llm.schema_rejected == 0
    rows[-1] = (rows[-1][0] + (" (schema-constrained)" if schema_ok else " (plain JSON prompt)"), *rows[-1][1:])
    report["checks"]["JSON answer"]["schema_constrained"] = schema_ok
    if not args.no_image:
        img = _probe_image()
        if img is None:
            rows.append(("image (L9)", "skipped: Pillow is not installed", "-"))
        else:
            def colour_ok(o):
                c = str(o.get("colour", "")).lower()
                return "red" in c, f"colour = {o.get('colour')}"

            probe("image (L9)", "You describe images exactly.", "What colour is the square in the image? Put one word in the field colour.",
                  {"type": "object", "properties": {"colour": {"type": "string"}}, "required": ["colour"]}, colour_ok, [img])
    _, dt, n = probe("speed", "You write plain English.", "Write about 120 words on why convoys keep a fixed distance between "
                     "vehicles. Put the text in the field text.",
                     {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                     lambda o: (bool(str(o.get("text", "")).strip()), f"{len(str(o.get('text', '')).split())} words"))
    if n and dt > 0:
        rows[-1] = (rows[-1][0], rows[-1][1] + f", {n} tokens, {n / dt:.1f} tokens/s", rows[-1][2])
        report["checks"]["speed"]["tokens_per_s"] = round(n / dt, 1)
    t0 = time.perf_counter()
    _, problems = call_validated(llm, "Reply with JSON.", "Return {\"ok\": true}.", {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                 "required": ["ok"]}, "check", lambda o: (o, [] if o.get("ok") is True else ["ok is not true"]))
    dt = time.perf_counter() - t0
    rows.append(("validated call (the path every role uses)", "ok" if not problems else "FAILED: " + "; ".join(problems)[:160], f"{dt:.1f}"))
    report["checks"]["validated call"] = {"ok": not problems, "detail": "; ".join(problems), "seconds": round(dt, 2)}
    w = max(len(r[0]) for r in rows)
    for name, result, sec in rows:
        print(f"  {name:<{w}}  {result}  ({sec} s)")
    if not report["checks"].get("image (L9)", {"ok": True})["ok"]:
        print("no image answer: the sketch reader (L9) will name the marks by the order's position words; "
              "with llama.cpp, give the vision projector (--mmproj)")
    report["down"] = llm.describe()["down"]
    print(f"model time {llm.usage.seconds:.1f} s, {llm.usage.input_tokens} input and {llm.usage.output_tokens} output tokens over "
          f"{llm.usage.calls} calls" + (f"; the server stopped answering ({llm.down})" if llm.down else ""))
    print("for the runs: export COA_LLM_BASE_URL=" + d["base_url"] + " COA_LLM_MODEL=" + d["model"]
          + ("" if schema_ok else " COA_LLM_JSON_SCHEMA=0"))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    if not json_ok or llm.down:
        sys.exit(1)


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


def cmd_real_bench(args) -> None:
    import pickle

    from . import realbench as rb
    from .bench import to_markdown
    from .loaders import add_places
    from .realdata import roadside_halts, systemic_unknowns
    from .scenario import ScenarioPaths, load_graph_cached

    paths = ScenarioPaths.from_dir(args.data)
    out = Path(args.out)
    cache = out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    if args.network == "bdtopo":
        G, _, _ = load_graph_cached(paths.bdtopo, cache)
        max_rank = args.max_rank or 4
    else:
        G, _, _ = load_graph_cached(paths.scan100, cache, kind="scan")
        max_rank = args.max_rank or 5
        if args.corrector:
            from .scanlearn import correct_graph

            print(f"learned corrector: {correct_graph(G, paths.scan100, args.corrector)}")
    reqs, notes = systemic_unknowns(G, load_requirements(args.requirements))
    roadside_halts(G, max_rank=3)
    places = {k: tuple(v) for k, v in json.loads(Path(args.places).read_text()).items()}
    add_places(G, places, snap_max_rank=3)
    scorer = rb.BDScorer(paths.bdtopo) if args.network != "bdtopo" and paths.bdtopo else None
    res = rb.run(G, None, range(args.seeds), max_rank, reqs, reps=args.reps, scorer=scorer, places_xy=places)
    with (out / "results.pkl").open("wb") as fh:
        pickle.dump(res, fh)
    text = to_markdown(res, f"{args.network}{' + corrector' if args.corrector else ''}: baselines and COA-Net, random missions")
    (out / "bench.md").write_text(text + "\n", encoding="utf-8")
    print(text)


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
    extra = None
    if args.graphhopper:
        from .gh import gh_variant

        extra = {"GraphHopper (same roads and costs)": gh_variant(Path(args.out) / "graphhopper", home=args.gh_home,
                                                                  n_seeds=args.seeds)}
        if args.gh_turns:
            extra["GraphHopper with turn costs"] = gh_variant(Path(args.out) / "graphhopper_turns", home=args.gh_home,
                                                              port=8991, n_seeds=args.seeds, turns=True)
    run = run_exercise(paths, args.out, text, cache=args.cache, n_seeds=args.seeds, turn_aware=not args.no_turns,
                       compare=not args.no_compare, osm_check=not args.no_osm, llm=_llm(args), requirements=reqs,
                        network=args.network, corrector=args.corrector, extra_solvers=extra)
    if extra:
        print("router comparison:")
        for name, c in run.facts["router_comparison"].items():
            print(f"  {name}: {c['itineraries']} itineraries, cost {c['total_cost_min']} min "
                  f"({c['total_cost_with_turns_min']} min with turns), time {c['total_time_min']} min, {c['total_km']} km, "
                  f"min separation {c['min_separation_m']} m, {c['critical_turns']} critical turns, {c['seconds']} s")
    from .exercise import parse_exercise

    rb = exercise_readback(reading, parse_exercise(text)[0], run.facts)
    if notes:
        rb += "\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    (Path(args.out) / "readback.md").write_text(rb, encoding="utf-8")
    print((Path(args.out) / "decision_process.txt").read_text(encoding="utf-8"))
    print(f"outputs in {args.out}: itineraries.geojson, difficult_areas.geojson, zones.geojson, map_*.png, decision_process.txt, "
          "metrics.json, readback.md, sketch_marks.png")


def cmd_scan_roads(args) -> None:
    from .scanroads import write_network_outputs
    from .scenario import ScenarioPaths, load_graph_cached

    paths = ScenarioPaths.from_dir(args.data)
    tif = args.tif or (paths.scan100 if args.scale == "100k" else paths.scan250)
    if not tif:
        sys.exit(f"no {args.scale} scan in {args.data}")
    out = Path(args.out)
    cache = Path(args.cache) if args.cache else out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    G, rep, cached = load_graph_cached(tif, cache, kind="scan")
    print(f"road network read from {tif}{' (cache)' if cached else ''}: {rep.nodes} junctions, {rep.km:.0f} km "
          f"({rep.largest_scc_share:.0%} of the junctions in the largest connected network), {time.perf_counter() - t0:.0f} s")
    if args.corrector:
        from .scanlearn import correct_graph

        print(f"learned corrector: {correct_graph(G, tif, args.corrector)}")
    write_network_outputs(G, tif, out)
    if not args.no_check and paths.bdtopo:
        from .scancheck import validate

        G_ref, _, _ = load_graph_cached(paths.bdtopo, cache)
        res = validate(G, paths.bdtopo, paths.osm, G_ref=G_ref, out=out, n_pairs=args.pairs)
        print((out / "validation.md").read_text(encoding="utf-8"))
        print(f"correctness vs BD TOPO (25 m) {res['correctness_vs_bdtopo']['25m']:.1%}")
    print(f"outputs in {out}: network.geojson, scan_report.md, map_network.png" + ("" if args.no_check else ", validation.md/.json"))


def cmd_scan_learn(args) -> None:
    import copy

    from . import scanlearn as sl
    from .scenario import ScenarioPaths, load_graph_cached

    paths = ScenarioPaths.from_dir(args.data)
    tif = args.tif or (paths.scan100 if args.scale == "100k" else paths.scan250)
    if not (tif and paths.bdtopo):
        sys.exit("scan-learn needs the scan and the BD TOPO shapefile (labels)")
    out = Path(args.out)
    cache = Path(args.cache) if args.cache else out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    G, rep, cached = load_graph_cached(tif, cache, kind="scan")
    print(f"road network read from {tif}{' (cache)' if cached else ''}: {rep.km:.0f} km")
    unet = None
    if args.unet:  # out-of-fold U-Net probabilities (coa-net scan-unet) as features of every task
        import shutil

        from . import scanunet

        unet = scanunet.load_raster(Path(args.unet) / "unet_oof.tif")
        out.mkdir(parents=True, exist_ok=True)
        for f in ("unet_final.pt", "unet_scores.json"):
            if (Path(args.unet) / f).exists():
                shutil.copy(Path(args.unet) / f, out / f)
    ds = sl.build_dataset(G, tif, paths.bdtopo, paths.osm, grid=args.grid, unet=unet)
    cv = sl.cross_validate(ds)
    models = sl.train_final(ds)
    out.mkdir(parents=True, exist_ok=True)
    extra, extra_meta = set(), {}
    if not args.no_cnn:
        try:
            from . import scancnn
        except ImportError:
            scancnn = None
            print("crop network skipped: PyTorch is not installed")
        if scancnn is not None:
            try:
                import torch  # noqa: F401

                res = scancnn.cross_validate_cnn(ds, cv, G, tif)
                extra_meta["cnn"] = res
                scancnn.train_final_cnn(ds, G, tif, out, [t for t, r in res.items() if r["used"]])
            except ImportError:
                print("crop network skipped: PyTorch is not installed")
    P = sl.predictions_from_oof(ds, cv)
    gate = None
    if not args.no_route_gate:
        from .scanroute import fold_pairs, route_gate

        G_bd, _, _ = load_graph_cached(paths.bdtopo, cache)
        pairs = fold_pairs(ds["bounds"], ds["grid"], cv["folds_of_blocks"], per_fold=args.route_pairs)
        prec = lambda dec, key, v: sl.decision_scores(ds, cv, {key: v})[dec]["precision"]
        gate = route_gate(G, P, sl.road_gate_enabled(cv, args.allow_removal) - sl.REMOVALS, sl.THRESH, G_bd, pairs, precision_of=prec)
        print(f"route gate: kept {gate['enabled']}; routes against BD TOPO, time ratio median "
              f"{gate['no_correction']['time_ratio_median']:.3f} -> {gate['corrected']['time_ratio_median']:.3f}")
    if not args.no_geometry:
        from . import scangeom

        gmodel, offsets, gsc = scangeom.learn(G, ds["roads"], tif, paths.bdtopo, ds["bounds"], ds["grid"], cv["folds_of_blocks"], unet=unet)
        scangeom.save(out, gmodel, offsets, gsc)
        extra_meta["geometry"] = gsc
        if gsc["enabled"]:
            extra.add("move_lines")
    sl.save(out, models, P, cv, ds, tif, sl.scan_graph_key(tif), allow_removal=args.allow_removal, route_gate=gate,
            extra_enabled=extra, extra_meta=extra_meta)
    Gc = copy.deepcopy(G)
    changes = sl.correct_graph(Gc, tif, out)
    md = sl.markdown(cv, sl.feature_importance(models), changes, ds)
    if gate:
        from .scanroute import markdown as route_md

        md += "\n" + route_md(gate)
    if extra_meta.get("cnn"):
        md += "\n## Crop network (bridges and crossings)\n\n| decision | AUC tabular | AUC crop network | used |\n| --- | --- | --- | --- |\n"
        md += "".join(f"| {sl.TASK_TEXT[t]} | {r['auc_tabular']:.3f} | {r['auc_cnn']:.3f} | {'yes' if r['used'] else 'no'} |\n"
                      for t, r in extra_meta["cnn"].items())
    if extra_meta.get("geometry"):
        g = extra_meta["geometry"]
        md += (f"\n## Learned displacement of the lines\n\n{g['scored']} sample points scored on unseen blocks: mean offset to BD TOPO "
               f"{g['mean_offset_m']:.1f} m -> {g['mean_after_m']:.1f} m, within 10 m {g['within_10m_before']:.0%} -> "
               f"{g['within_10m_after']:.0%}; network within 25 m of BD TOPO {g['network_25m_before']:.1%} -> "
               f"{g['network_25m_after']:.1%} (R2 {g['r2']:.2f}). Lines moved: {'yes' if g['enabled'] else 'no'}.\n")
    (out / "corrector_report.md").write_text(md, encoding="utf-8")
    (out / "corrector_scores.json").write_text(json.dumps(sl.jsonable(cv), indent=1, default=float), encoding="utf-8")
    print(md)
    if not args.no_check:
        from .scancheck import validate

        G_ref, _, _ = load_graph_cached(paths.bdtopo, cache)
        res = validate(Gc, paths.bdtopo, paths.osm, G_ref=G_ref, out=out / "corrected_network")
        print(f"corrected network: correctness vs BD TOPO (25 m) {res['correctness_vs_bdtopo']['25m']:.1%}; "
              f"checks in {out / 'corrected_network' / 'validation.md'}")
    print(f"corrector in {out} ({time.perf_counter() - t0:.0f} s): use --corrector {out} with exercise, plan --scan or scan-roads")


def cmd_scan_unet(args) -> None:
    from . import scanunet
    from .scenario import ScenarioPaths

    paths = ScenarioPaths.from_dir(args.data)
    tif = args.tif or (paths.scan100 if args.scale == "100k" else paths.scan250)
    if not (tif and paths.bdtopo):
        sys.exit("scan-unet needs the scan and the BD TOPO shapefile (labels)")
    t0 = time.perf_counter()
    sc = scanunet.learn(tif, paths.bdtopo, Path(args.out), grid=args.grid, steps=args.steps, final=not args.no_final)
    print(json.dumps(sc, indent=1))
    print(f"U-Net in {args.out} ({time.perf_counter() - t0:.0f} s): use scan-learn --unet {args.out}")


def cmd_pdf_exercises(args) -> None:
    """Solve every route exercise of a PDF exercise file on the image network (+ corrector); BD TOPO / OSM check only."""
    import geopandas as gpd
    import numpy as np
    import shapely
    from scipy.spatial import cKDTree

    from . import pdfexercises as px
    from .exercise import edge_lines
    from .scenario import ScenarioPaths, annotate_areas, file_key, load_graph_cached

    paths = ScenarioPaths.from_dir(args.data)
    tif = paths.scan100
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache) if args.cache else out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    meta, exs = px.read_pdf(args.pdf, figures_dir=out)
    if args.only:
        exs = [e for e in exs if e.number in set(args.only)]
    (out / "pdf_metadata.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "exercises_read.json").write_text(json.dumps([{k: v for k, v in e.__dict__.items()} for e in exs], indent=1,
                                                        ensure_ascii=False), encoding="utf-8")
    print(f"{args.pdf}: {meta.get('pages')} pages, {len(exs)} exercises read ({time.perf_counter() - t0:.0f} s)", flush=True)

    # road network: the map reader (+ corrector), or the segmentation network's own graph (prc-alone)
    if args.network == "prc-alone":
        from . import scanunet

        if not args.unet:
            sys.exit("--network prc-alone needs --unet DIR (a folder with unet_oof.tif, e.g. from scripts/prc_unet.py merge)")
        P, tr = scanunet.load_raster(Path(args.unet) / "unet_oof.tif")
        G = scanunet.unet_network(Path(args.unet), args.tracing)
        net = {"source": (f"road network traced from the fine-tuned PRC U-Net probabilities `{args.unet}` ({G.number_of_edges() // 2:,} "
                          "sections; road where p >= 0.5, thinned to centre lines; no map reader; the U-Net was trained on BD TOPO "
                          "labels only, out of fold)"), "corrector": "none (the U-Net network has no map symbols to correct)"}
        args.corrector = None
    else:
        G, _, _ = load_graph_cached(tif, cache, kind="scan")
        net = {"source": f"road network read from the scan {Path(tif).name} ({G.number_of_edges() // 2:,} sections)"}
    if args.corrector:
        from .scanlearn import correct_graph

        ch = correct_graph(G, tif, args.corrector)
        meta_c = json.loads((Path(args.corrector) / "corrector.json").read_text(encoding="utf-8"))
        net["corrector"] = (f"`{args.corrector}` ({ch.get('mode', '')}); corrections switched on: {', '.join(meta_c.get('enabled', [])) or 'none'}; "
                            f"{ch.get('links added', 0)} missing links added, {ch.get('white roads to rank 4', 0)} white roads moved to rank 4, "
                            f"{ch.get('roads priced as doubtful (p < 0.7)', 0)} roads priced as doubtful, {ch.get('label gaps priced', 0)} label gaps priced"
                            + (f", lines moved by the learned displacement" if "move_lines" in meta_c.get("enabled", []) else ""))
    else:
        net["corrector"] = "none"
    # built-up tint: palette indices chosen by colour (each IGN sheet has its own palette order)
    import rasterio

    rgb = np.array([int(v) for v in args.builtup_rgb.split(",")])
    with rasterio.open(tif) as src:
        cm = src.colormap(1)
    classes = tuple(sorted(k for k, v in cm.items() if np.abs(np.array(v[:3]) - rgb).max() <= args.builtup_tol))
    if not classes:
        sys.exit(f"no palette colour within {args.builtup_tol} of {args.builtup_rgb}: give --builtup-rgb")
    bu_path = cache / f"builtup_{file_key(tif)}_{'-'.join(map(str, classes))}.geojson"
    if bu_path.exists():
        builtup = gpd.read_file(bu_path)
    else:
        from .scanmap import builtup_polygons

        builtup = builtup_polygons(tif, classes=classes)
        builtup.to_file(bu_path, driver="GeoJSON")
    net["builtup"] = (f"{len(builtup):,} built-up polygons ({builtup.to_crs(px.L93).area.sum() / 1e6:.0f} km2) read from the scan tint "
                      f"RGB {args.builtup_rgb} (palette indices {', '.join(map(str, classes))})")
    builtup.to_file(out / "builtup_areas.geojson", driver="GeoJSON")
    N = px.Network(G, "PRC U-Net network" if args.network == "prc-alone" else
                   "image network + corrector" if args.corrector else "image network", builtup=builtup)
    if args.prob_threshold is not None:
        if args.network != "prc-alone":
            sys.exit("--prob-threshold needs --network prc-alone (the probabilities come from the U-Net raster)")
        pr = N.price_by_probability(P, tr, args.prob_threshold, args.prob_penalty)
        net["probability_penalty"] = (f"sections whose PRC road probability is below {pr['threshold']:g} (operator's threshold) cost "
                                      f"1 + {pr['strength']:g} x the share below it: {pr['sections_priced']:,} sections priced, "
                                      f"{pr['km_below_threshold']:,} km of road below the threshold")
        net["corrector"] += "; " + net["probability_penalty"]
    print(f"image network ready: {len(N.edges):,} directed sections of rank <= 5 ({time.perf_counter() - t0:.0f} s)", flush=True)

    ref_net = bd_kd = osm = None
    if paths.bdtopo and not args.no_check:
        Gb, _, _ = load_graph_cached(paths.bdtopo, cache)
        Lb = edge_lines(Gb, list(Gb.edges))
        ref_net = px.Network(Gb, "BD TOPO", priced=False, builtup=builtup)
        bd_kd = cKDTree(px._sample(Lb, 10.0))
    if args.no_osm and paths.osm:
        print("OSM ignored (--no-osm)", flush=True)
    if paths.osm and not args.no_check and not args.no_osm:
        from .realdata import read_lines

        o, _ = read_lines(paths.osm, "utf-8", ["maxspeed"])
        o = o.to_crs(px.L93)
        osm = (shapely.STRtree(o.geometry.values), o["maxspeed"].fillna(0).astype(float).values)
    print(f"checks ready ({time.perf_counter() - t0:.0f} s)", flush=True)

    rows = []
    for ex in exs:
        t = time.perf_counter()
        d = out / f"exercise_{ex.number:02d}"
        d.mkdir(parents=True, exist_ok=True)
        try:
            res = px.solve(ex, N, bd_kd, osm)
        except RuntimeError as e:
            (d / "decision.md").write_text(f"# Exercise {ex.number}\n\nNo solution: {e}\n", encoding="utf-8")
            print(f"exercise {ex.number}: no solution ({e})", flush=True)
            rows.append((ex, None, None))
            continue
        ref = px.solve(ex, ref_net) if ref_net is not None else None
        px.write_geojson(d / "route.geojson", ex, res)
        if ref:
            px.write_geojson(d / "route_bdtopo_check.geojson", ex, ref)
        px.render_layout(d / "layout.png", tif, ex, res, ref)
        (d / "decision.md").write_text(px.decision_markdown(ex, res, ref, net), encoding="utf-8")
        (d / "metrics.json").write_text(json.dumps({"exercise": ex.number, "result": px.jsonable(res),
                                                    "bdtopo_check": px.jsonable(ref) if ref else None}, indent=1), encoding="utf-8")
        rows.append((ex, res, ref))
        print(f"exercise {ex.number}: {res['total']['length_km']:.1f} km, {res['total']['duration_min']:.0f} min"
              + (f" (BD TOPO {ref['total']['length_km']:.1f} km, {ref['total']['duration_min']:.0f} min)" if ref else "")
              + f", {time.perf_counter() - t:.0f} s", flush=True)

    L = [f"# Route exercises of `{Path(args.pdf).name}`", "",
         f"PDF: {meta.get('pages')} pages, author {meta.get('Author', '?')}, created {meta.get('CreationDate', '?')}, {meta.get('Producer', '')}.",
         f"Network: {net['source']}. Corrector: {net['corrector']}.",
         "OSM: " + ("used for the speed-limit check only" if osm is not None else "not used") + ".",
         f"Built-up areas: {net['builtup']}.",
         f"Speed: legal limit of each section (built-up {px.URBAN_KMH:.0f}, expressway {px.EXPRESSWAY_KMH:.0f}, other roads {px.ROAD_KMH:.0f} km/h), "
         "capped at the convoy speed given in the PDF. " + (
             "BD TOPO and OSM are used only as checks." if osm is not None and ref_net is not None else
             "BD TOPO is used only as a check (no OSM)." if ref_net is not None else
             "OSM is used only as a check (no BD TOPO)." if osm is not None else
             "No BD TOPO and no OSM: the routes come from the scan and the PDF alone, without checks."), "",
         "| exercise | objective | route | waypoints | length | duration | inside area | continuous | within 25 m of BD TOPO | BD TOPO check (length / duration) | notes |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for ex, res, ref in rows:
        if res is None:
            L.append(f"| {ex.number} | | no solution | | | | | | | | |")
            continue
        wp = {"fixed": "fixed order", "free": "free order", "none": "-"}[ex.waypoint_order]
        route = "; ".join(l["name"] for l in res["legs"])
        b25 = np.mean([l.get("share_within_25m_of_bdtopo", np.nan) for l in res["legs"]])
        L.append(f"| [{ex.number}](exercise_{ex.number:02d}/decision.md) | {'shortest' if ex.objective == 'length' else 'fastest'} | {route} | {wp} | "
                 f"{res['total']['length_km']:.1f} km | {res['total']['duration_min']:.0f} min | "
                 f"{'yes' if all(l['inside_area'] for l in res['legs']) else 'NO'} | {'yes' if all(l['continuous'] for l in res['legs']) else 'NO'} | "
                 + (f"{b25:.0%} | " if b25 == b25 else "- | ") + (f"{ref['total']['length_km']:.1f} km / {ref['total']['duration_min']:.0f} min" if ref else "-") +
                 f" | {'; '.join(ex.notes)} |")
    (out / "summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print(f"outputs in {out} ({time.perf_counter() - t0:.0f} s)")


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
    src.add_argument("--scan", help="scanned map (GeoTIFF, e.g. 'Scenario1 100K.tif'): the road network is read from the image")
    src.add_argument("--synthetic", type=int, default=0, help="synthetic theatre seed (default)")
    p.add_argument("--corrector", help="with --scan: learned corrector folder (coa-net scan-learn) applied to the image network")
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
    x.add_argument("--network", choices=("scan", "scan250", "bdtopo", "osm"), default="scan",
                   help="road graph source: scan = read from the 1:100 000 image (default; BD TOPO and OSM only check it), "
                        "scan250 = from the 1:250 000 image, bdtopo = the BD TOPO shapefile (the pipeline on main), osm = the OSM shapefile")
    x.add_argument("--corrector", help="learned corrector folder (coa-net scan-learn) applied to the image network")
    x.add_argument("--graphhopper", action="store_true", help="also solve NET.DISTINCT with a local GraphHopper server "
                   "on the same roads and costs, and compare (needs Java 17+ and graphhopper-web-*.jar)")
    x.add_argument("--gh-turns", action="store_true", help="with --graphhopper: also solve with GraphHopper pricing "
                   "junction turns like the turn-aware search (turn_penalty, edge-based)")
    x.add_argument("--gh-home", help="folder with graphhopper-web-*.jar and a jdk* folder (default: COA_GH_HOME or ../tools)")
    for k in ("bdtopo", "osm", "scan100", "scan250"):
        x.add_argument(f"--{k}", help=f"explicit path for {k}")
    x.set_defaults(fn=cmd_exercise)

    rbp = sub.add_parser("real-bench", help="the 100-seed benchmark on a real network (random missions between the towns of a "
                         "gazetteer): B0, B1 and COA-Net; on the image network the routes are also checked against BD TOPO")
    rbp.add_argument("--data", required=True, help="folder with the Scenario files")
    rbp.add_argument("--places", required=True, help='JSON {"name": [lon, lat]} gazetteer (towns the missions link)')
    rbp.add_argument("--network", choices=("scan", "bdtopo"), default="scan")
    rbp.add_argument("--corrector", help="with --network scan: learned corrector folder (coa-net scan-learn)")
    rbp.add_argument("--max-rank", type=int, help="corridor backbone (default 5 on the image network, 4 on BD TOPO)")
    rbp.add_argument("--seeds", type=int, default=100)
    rbp.add_argument("--reps", type=int, default=20, help="Monte Carlo replications per plan")
    rbp.add_argument("--requirements", help="requirement base JSON (default: seed base)")
    rbp.add_argument("--out", default="outputs/real_bench")
    rbp.set_defaults(fn=cmd_real_bench)

    sr = sub.add_parser("scan-roads", help="read the road network from a scanned map (image input) and check it against the "
                        "shapefiles: network.geojson, validation.md, overlay figures")
    sr.add_argument("--data", required=True, help="folder with the Scenario files (scans; shapefiles for the checks)")
    sr.add_argument("--scale", choices=("100k", "250k"), default="100k", help="which scan to read")
    sr.add_argument("--tif", help="explicit scan path (overrides --data/--scale)")
    sr.add_argument("--out", default="outputs/scan_roads")
    sr.add_argument("--cache", help="cache folder (default: OUT/cache)")
    sr.add_argument("--no-check", action="store_true", help="skip the sanity checks against BD TOPO and OSM")
    sr.add_argument("--pairs", type=int, default=300, help="junction pairs for the route comparison")
    sr.add_argument("--corrector", help="learned corrector folder (coa-net scan-learn) applied before the checks")
    sr.set_defaults(fn=cmd_scan_roads)

    sl = sub.add_parser("scan-learn", help="train the learned corrector of the image network on the shapefiles (labels), "
                        "scored by spatial cross-validation on blocks of the sheet it never saw")
    sl.add_argument("--data", required=True, help="folder with the Scenario files (scan; shapefiles as labels)")
    sl.add_argument("--scale", choices=("100k", "250k"), default="100k")
    sl.add_argument("--tif", help="explicit scan path (overrides --data/--scale)")
    sl.add_argument("--out", default="outputs/scan_learn", help="corrector folder: models, out-of-fold predictions, report")
    sl.add_argument("--cache", help="cache folder (default: OUT/cache)")
    sl.add_argument("--grid", type=int, default=4, help="the sheet is cut into GRID x GRID blocks for the cross-validation")
    sl.add_argument("--no-check", action="store_true", help="skip the sanity checks of the corrected network")
    sl.add_argument("--no-route-gate", action="store_true", help="keep every correction that passes the road-level gate "
                    "(default: also require that it brings routes closer to BD TOPO's, on pairs drawn in unseen blocks)")
    sl.add_argument("--route-pairs", type=int, default=60, help="route-gate pairs per fold")
    sl.add_argument("--no-cnn", action="store_true", help="skip the crop network for bridges and crossings (needs PyTorch)")
    sl.add_argument("--no-geometry", action="store_true", help="skip the learned displacement of the lines")
    sl.add_argument("--unet", help="folder of coa-net scan-unet: its out-of-fold road, importance and structure "
                    "probabilities become features of every task")
    sl.add_argument("--allow-removal", action="store_true", help="remove the roads and label gaps the corrector doubts "
                    "(default: price them, so that no village is cut off)")
    sl.set_defaults(fn=cmd_scan_learn)

    su = sub.add_parser("scan-unet", help="train a small U-Net on the scan with BD TOPO drawn onto it (road, importance, "
                        "structure), out of fold on the blocks of scan-learn; needs PyTorch")
    su.add_argument("--data", required=True, help="folder with the Scenario files (scan; BD TOPO as labels)")
    su.add_argument("--scale", choices=("100k", "250k"), default="100k")
    su.add_argument("--tif", help="explicit scan path (overrides --data/--scale)")
    su.add_argument("--out", default="outputs/scan_unet", help="folder for unet_oof.tif, unet_final.pt, unet_scores.json")
    su.add_argument("--grid", type=int, default=4)
    su.add_argument("--steps", type=int, default=2000, help="training steps per fold (batches of 8 tiles)")
    su.add_argument("--no-final", action="store_true", help="skip the network trained on every block (for another sheet)")
    su.set_defaults(fn=cmd_scan_unet)

    pe = sub.add_parser("pdf-exercises", help="solve every route exercise of a PDF exercise file (points, deployment area, "
                        "waypoints, shortest/fastest, distinct round trip) on the image network; layout, GeoJSON, decision log")
    pe.add_argument("--data", required=True, help="folder with the scan (*100K.tif) and, for the checks, *BDTopo*.shp / *OSM*.shp")
    pe.add_argument("--pdf", required=True, help="exercise file (PDF)")
    pe.add_argument("--corrector", help="learned corrector folder (coa-net scan-learn) applied to the image network")
    pe.add_argument("--out", default="outputs/pdf_exercises")
    pe.add_argument("--cache", help="cache folder (default: OUT/cache)")
    pe.add_argument("--only", type=int, nargs="*", help="exercise numbers to solve (default: all)")
    pe.add_argument("--no-check", action="store_true", help="skip the BD TOPO / OSM checks")
    pe.add_argument("--network", choices=("scan", "prc-alone"), default="scan",
                    help="scan: road network read from the map (+ --corrector); prc-alone: the network traced from a fine-tuned "
                         "PRC U-Net (--unet), the closest to BD TOPO and OSM on the exercise file")
    pe.add_argument("--unet", help="with --network prc-alone: folder with unet_oof.tif (scripts/prc_unet.py merge)")
    pe.add_argument("--tracing", help="with --network prc-alone: how the road network is traced from the probabilities, e.g. "
                    "'low=0.3,bridge=150' (hysteresis: continue a road down to p 0.3; join dead ends to the road they point at "
                    "within 150 m when the probability along the link stays reasonable). Default: the folder's network.pkl or "
                    "tracing.json, else p >= 0.5")
    pe.add_argument("--no-osm", action="store_true", help="do not use OSM at all, even if the data folder has it")
    pe.add_argument("--prob-threshold", type=float, help="with --network prc-alone: penalise road sections whose PRC probability is "
                    "below this threshold (operator's choice, e.g. 0.7): routes avoid the stretches the U-Net is unsure of")
    pe.add_argument("--prob-penalty", type=float, default=3.0, help="cost x (1 + PENALTY x share of the section below the threshold)")
    pe.add_argument("--builtup-rgb", default="204,206,184", help="colour of the built-up tint on the scan (IGN 1:100 000: grey-beige)")
    pe.add_argument("--builtup-tol", type=int, default=6, help="max difference per channel for a palette colour to count as the tint")
    pe.set_defaults(fn=cmd_pdf_exercises)

    a = sub.add_parser("audit", help="audit a Scenario delivery: files, CRS, encodings, OSM/BD TOPO agreement, scan alignment")
    a.add_argument("--data", required=True)
    a.add_argument("--out", default="outputs/audit.json")
    a.set_defaults(fn=cmd_audit)

    lc = sub.add_parser("llm-check", help="check the local model server (COA_LLM_*): where it is, which model, JSON answers, "
                        "images for the sketch reader, speed")
    lc.add_argument("--no-image", action="store_true", help="skip the image probe (text-only model: L9 then uses position words)")
    lc.add_argument("--out", help="write the check as JSON here")
    lc.set_defaults(fn=cmd_llm_check)

    t = sub.add_parser("templates", help="print the requirement template catalogue")
    t.set_defaults(fn=cmd_templates)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
