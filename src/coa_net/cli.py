"""Command line: ``coa-net demo | plan | exercise | scan-roads | scan-learn | read-order | compile-doctrine | signoff | llm-check | audit | bench | fetch-osm |
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

        places = {k: tuple(v) for k, v in json.loads(Path(args.places).read_text()).items()} if args.places else None
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
                       compare=not args.no_compare, osm_check=not args.no_osm, llm=_llm(args), requirements=reqs,
                       network=args.network, corrector=args.corrector)
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
    ds = sl.build_dataset(G, tif, paths.bdtopo, paths.osm, grid=args.grid)
    cv = sl.cross_validate(ds)
    models = sl.train_final(ds)
    P = sl.predictions_from_oof(ds, cv)
    sl.save(out, models, P, cv, ds, tif, sl.scan_graph_key(tif))
    Gc = copy.deepcopy(G)
    changes = sl.correct_graph(Gc, tif, out)
    md = sl.markdown(cv, sl.feature_importance(models), changes, ds)
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
    x.add_argument("--network", choices=("scan", "scan250", "bdtopo"), default="scan",
                   help="road graph source: scan = read from the 1:100 000 image (default; BD TOPO and OSM only check it), "
                        "scan250 = from the 1:250 000 image, bdtopo = the BD TOPO shapefile (the pipeline on main)")
    x.add_argument("--corrector", help="learned corrector folder (coa-net scan-learn) applied to the image network")
    for k in ("bdtopo", "osm", "scan100", "scan250"):
        x.add_argument(f"--{k}", help=f"explicit path for {k}")
    x.set_defaults(fn=cmd_exercise)

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
    sl.set_defaults(fn=cmd_scan_learn)

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
