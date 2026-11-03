"""Regenerate the figures of docs/COA-Net_methodology.md and README.md.

    python docs/figures/make_figures.py [--out docs/figures]

Writes one self-contained HTML page per figure (SVG inside) and, when the
``playwright`` Python package and its Chromium are installed, the PNG next to
it (``pip install playwright && playwright install chromium``). In the diagrams,
blue boxes are language-model steps, grey boxes symbolic steps, dashed boxes inputs.
"""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

GREY, BLUE, BLUE_FILL, INK, MUTED, RED = "#c8c6bc", "#2b6fd6", "#e8f1fc", "#1a1a1a", "#5f5f5f", "#c2410c"
FONT = "Helvetica Neue, Helvetica, Arial, sans-serif"


def _e(s) -> str:
    return html.escape(str(s))


class Canvas:
    def __init__(self, w: int, h: int, title: str, subtitle: str = ""):
        self.w, self.h = w, h
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" font-family="{FONT}">',
            '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse">'
            f'<path d="M0,1 L9,5 L0,9 z" fill="{GREY}"/></marker>'
            '<marker id="ahr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse">'
            f'<path d="M0,1 L9,5 L0,9 z" fill="{RED}"/></marker></defs>',
            f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
            f'<text x="40" y="52" font-size="27" font-weight="700" fill="{INK}">{_e(title)}</text>',
        ]
        if subtitle:
            self.parts.append(f'<text x="40" y="86" font-size="19" fill="{MUTED}">{_e(subtitle)}</text>')

    def box(self, x, y, w, h, title, sub="", kind="code", sub2="", sub3=""):
        if kind == "llm":
            style = f'fill="{BLUE_FILL}" stroke="{BLUE}" stroke-width="2.6"'
        elif kind == "input":
            style = f'fill="#ffffff" stroke="{GREY}" stroke-width="2" stroke-dasharray="7,5"'
        elif kind == "out":
            style = f'fill="#f7f6f2" stroke="{GREY}" stroke-width="2"'
        else:
            style = f'fill="#ffffff" stroke="{GREY}" stroke-width="2"'
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" {style}/>')
        cx = x + w / 2
        lines = [s for s in (sub, sub2, sub3) if s]
        ty = y + h / 2 + (7 if not lines else -10 - 11 * (len(lines) - 1))
        weight = "400" if kind == "input" else "700"
        self.parts.append(f'<text x="{cx}" y="{ty}" text-anchor="middle" font-size="21" font-weight="{weight}" fill="{INK}">{_e(title)}</text>')
        for i, s in enumerate(lines):
            self.parts.append(f'<text x="{cx}" y="{ty + 30 + 24 * i}" text-anchor="middle" font-size="17.5" fill="{MUTED}">{_e(s)}</text>')

    def arrow(self, pts, label="", at=None, anchor="middle", color=GREY, dashed=False):
        d = " ".join(f"{x},{y}" for x, y in pts)
        dash = ' stroke-dasharray="6,5"' if dashed else ""
        mk = "ahr" if color == RED else "ah"
        self.parts.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="2"{dash} marker-end="url(#{mk})"/>')
        if label:
            lx, ly = at or ((pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2 - 10)
            self.parts.append(f'<text x="{lx}" y="{ly}" text-anchor="{anchor}" font-size="17.5" fill="{MUTED}">{_e(label)}</text>')

    def text(self, x, y, s, size=17.5, color=MUTED, anchor="start", weight="400"):
        self.parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" font-weight="{weight}" fill="{color}">{_e(s)}</text>')

    def raw(self, s):
        self.parts.append(s)

    def legend(self, y, items=(("llm", "Language-model step (small open model, checked by code)"), ("code", "Symbolic step (graph, CP-SAT, simulator)"), ("input", "Input"))):
        x = 40
        for kind, label in items:
            if kind == "llm":
                st = f'fill="{BLUE_FILL}" stroke="{BLUE}" stroke-width="2.4"'
            elif kind == "input":
                st = f'fill="#fff" stroke="{GREY}" stroke-width="2" stroke-dasharray="5,4"'
            else:
                st = f'fill="#fff" stroke="{GREY}" stroke-width="2"'
            self.parts.append(f'<rect x="{x}" y="{y - 16}" width="30" height="22" rx="5" {st}/>')
            self.text(x + 42, y + 1, label, 18.5, "#3f3f3f")
            x += 52 + 9.6 * len(label) + 30

    def svg(self) -> str:
        return "\n".join(self.parts + ["</svg>"])


def architecture() -> str:
    c = Canvas(1400, 1265, "The models read and write; code designs the network",
               "Doctrine and orders arrive as documents. Every model role (L1-L9) is checked by code and has a deterministic fallback.")
    bw, bh, bh5 = 290, 96, 120
    A, B, C, D = 40, 340, 680, 1040
    r1, r2, r3, r4, r5, r6, r7 = 145, 285, 435, 585, 745, 915, 1065
    mid = lambda r, h=bh: r + h / 2  # noqa: E731
    rm = 1368  # right margin lane

    for y, label in ((125, "AT THE EVENT, ONCE"), (r3 - 14, "PER EXERCISE OR ORDER"), (r7 - 14, "PER INCIDENT")):
        c.text(A, y, label, 16, INK, "start", "700")
    for r, title, sub in ((r1, "BD TOPO / OSM", "scans, DEM"), (r2, "Doctrine documents", "STANAG, NATO texts"),
                          (r3, "Order document", "text + illustration"), (r7, "Incident reports", "text or document")):
        c.box(A, r + 6, 245, 84, title, sub, kind="input")
        c.arrow([(A + 245, mid(r)), (B - 4, mid(r))])

    # at the event, once
    c.box(B, r1, bw, bh, "S0 Theatre preparation", "road graph, corridors,", sub2="critical elements, halt areas")
    c.box(B, r2, bw, r3 + bh - r2, "Document intake", "pages, paragraphs, sections,", sub2="figures, captions; OCR")
    c.box(C, r2, bw, bh, "S1 Doctrine compiler", "L1 + patterns, 28 templates,", sub2="every instance cited", kind="llm")
    c.box(D, r2, bw, bh, "Requirement base", "reconciled, seed fills gaps;", sub2="provisional, then signed off")
    c.arrow([(B + bw, mid(r1)), (rm, mid(r1)), (rm, r5 + 40), (D + bw + 4, r5 + 40)], "road graph, critical elements",
            at=(C + bw / 2 + 40, mid(r1) - 12))
    c.arrow([(B + bw, mid(r2)), (C - 4, mid(r2))])
    c.arrow([(C + bw, mid(r2)), (D - 4, mid(r2))])
    c.arrow([(D + bw / 2, r2 + bh), (D + bw / 2, r3 - 4)], "requirements", at=(D + bw / 2 + 12, r2 + bh + 32), anchor="start")

    # per exercise or order
    c.box(C, r3, bw, bh, "S2 Order understanding", "block roles, task, outputs;", sub2="L2 parser, numbers checked", kind="llm")
    c.box(D, r3, bw, bh, "Readback and task", "what was understood or not;", sub2="the operator confirms")
    c.box(B, r4, bw, bh, "S2.3 Sketch reader", "marks found by code,", sub2="named by L9, georeferenced", kind="llm")
    c.arrow([(B + bw, mid(r3) - 15), (C - 4, mid(r3) - 15)])
    c.arrow([(B + bw / 2, r3 + bh), (B + bw / 2, r4 - 4)], "illustration", at=(B + bw / 2 + 12, r3 + bh + 32), anchor="start")
    c.arrow([(B + bw, mid(r4)), (C - 25, mid(r4)), (C - 25, mid(r3) + 22), (C - 4, mid(r3) + 22)], "named zones",
            at=(C - 13, mid(r4) + 6), anchor="start")
    c.arrow([(C + bw, mid(r3)), (D - 4, mid(r3))])
    c.box(C, r5, bw, bh5, "Exercise solver", "k itineraries (NET.DISTINCT),", sub2="towns, turn radii, checker,",
          sub3="on the S0 road graph")
    c.box(D, r5, bw, bh5, "S3-S9 Movement planning", "routes, criticality (L4),", sub2="CP-SAT network and credits,",
          sub3="checks, playbooks, COAs (L5/L8)")
    c.arrow([(D + 40, r3 + bh), (D + 40, r4 + 75), (C + bw / 2, r4 + 75), (C + bw / 2, r5 - 4)], "itineraries task",
            at=((C + bw / 2 + D + 40) / 2, r4 + 63))
    c.arrow([(D + bw / 2, r3 + bh), (D + bw / 2, r5 - 4)], "movement task", at=(D + bw / 2 + 12, r4 + 40), anchor="start")
    c.box(C, r6, bw, bh, "Requested outputs", "layers, GeoJSON,", sub2="decision-process text", kind="out")
    c.box(D, r6, bw, bh, "Plan and orders", "COAs, overlay, march tables,", sub2="playbooks, recce tasks", kind="out")
    c.arrow([(C + bw / 2, r5 + bh5), (C + bw / 2, r6 - 4)])
    c.arrow([(D + bw / 2, r5 + bh5), (D + bw / 2, r6 - 4)], "operator's choice", at=(D + bw / 2 + 12, r5 + bh5 + 30), anchor="start")

    # per incident
    c.box(B, r7, bw, bh, "S10 Report parser", "L6: typed, graded report", kind="llm")
    c.box(C, r7, bw, bh, "S10 Belief and repair", "playbook, or S5 from", sub2="the current positions")
    c.box(D, r7, bw, bh, "FRAGO", "L7 phrases the", sub2="structured difference", kind="llm")
    c.arrow([(B + bw, mid(r7)), (C - 4, mid(r7))])
    c.arrow([(C + bw, r7 + 70), (D - 4, r7 + 70)])
    c.arrow([(C + bw - 30, r7), (C + bw - 30, r7 - 27), (C + bw + 25, r7 - 27), (C + bw + 25, r5 + 70), (D - 4, r5 + 70)],
            "repair, re-time", at=(C + bw - 42, r7 - 35), anchor="end")
    c.arrow([(D, r5 + 100), (C + bw + 45, r5 + 100), (C + bw + 45, r7 + 30), (C + bw + 4, r7 + 30)], "playbooks",
            at=(C + bw + 55, r6 - 12), anchor="start", dashed=True)
    c.legend(1225)
    return c.svg()


def incident_loop(timings: dict[str, str] | None = None) -> str:
    t = timings or {}
    c = Canvas(1400, 1060, "One loop per incident report, playbook first",
               "S10: beliefs first, then a pre-verified playbook or a repair from the current positions; the FRAGO holds only what changed.")
    bw, bh = 300, 96
    c.box(40, 150, 250, 70, "Report (text or form)", kind="input")
    c.box(350, 137, bw, bh, "Parse and grade", "L6: type, place,", sub2="Admiralty grading", kind="llm")
    c.box(710, 137, bw, bh, "Update beliefs", "noisy-OR, half-life,", sub2="closed if P >= 0.5")
    c.arrow([(290, 185), (346, 185)])
    c.arrow([(650, 185), (706, 185)])
    cx, cy, hw, hh = 860, 340, 150, 66
    xr, wr = 1130, 240
    c.raw(f'<path d="M{cx},{cy - hh} L{cx + hw},{cy} L{cx},{cy + hh} L{cx - hw},{cy} Z" fill="#fff" stroke="{GREY}" stroke-width="2"/>')
    c.text(cx, cy + 7, "What changed?", 20, INK, "middle", "700")
    c.arrow([(cx, 233), (cx, cy - hh - 4)])
    c.box(xr, cy - bh / 2, wr, bh, "Apply playbook", "pre-verified,", sub2="briefed before departure")
    c.arrow([(cx + hw, cy), (xr - 4, cy)], "covered loss", at=((cx + hw + xr) / 2, cy - 12))
    c.box(330, cy - bh / 2, 300, bh, "Keep the routes", "breakdown or delay:", sub2="re-time only")
    c.arrow([(cx - hw, cy), (634, cy)], "delay", at=(cx - hw - 30, cy - 12))
    c.box(710, 470, bw, bh, "Repair the network", "S5 from the positions,", sub2="stability last in the order")
    c.arrow([(cx, cy + hh), (cx, 466)], "other loss, threat", at=(cx + 12, cy + hh + 32), anchor="start")
    c.box(710, 630, bw, bh, "Re-time the columns", "S7 CP-SAT, the past fixed")
    c.arrow([(cx, 566), (cx, 626)])
    c.arrow([(xr + wr / 2, cy + bh / 2), (xr + wr / 2, 678), (1014, 678)])
    c.arrow([(480, cy + bh / 2), (480, 678), (706, 678)])
    c.box(710, 790, bw, bh, "Verify", "rule checker at five levels")
    c.arrow([(cx, 726), (cx, 786)])
    c.box(xr, 790, wr, bh, "Restore redundancy", "new ASR, or options", sub2="for each new SPOF")
    c.arrow([(1010, 838), (xr - 4, 838)])
    c.box(xr, 940, wr, bh, "FRAGO", "L7 phrases a", sub2="structured difference", kind="llm")
    c.arrow([(xr + wr / 2, 886), (xr + wr / 2, 936)])
    c.box(710, 953, bw, 70, "Units, movement control", kind="out")
    c.arrow([(xr - 4, 988), (1014, 988)])
    c.text(40, 790, "Measured on the demo (order LOG-01):", 18.5, INK, "start", "700")
    for i, k in enumerate(("I-1", "I-2", "I-3")):
        if t.get(k):
            c.text(40, 822 + 28 * i, t[k], 17)
    return c.svg()


def page(svg: str, width: int) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>body{{margin:0;background:#fff}}#fig{{width:{width}px}}</style></head>'
            f'<body><div id="fig">{svg}</div></body></html>')


def captioned(title: str, svg: str) -> str:
    """A map snapshot (drawn without a title) with a heading in the map's own style."""
    return (f'<div style="background:#fafaf7;padding:12px 14px 0;font:bold 16px Helvetica,Arial,sans-serif;color:#111827">{_e(title)}</div>'
            + svg)


def _crossings(H, r) -> list[str]:
    return [H.elements[e].name for e in r.elements if H.elements[e].kind in ("bridge", "tunnel")]


def demo_figures() -> tuple[dict[str, tuple[str, int]], dict[str, str]]:
    """Maps, road movement graph and COA chart of the demo session, and the measured incident timings."""
    from coa_net.baselines import b1, make_design
    from coa_net.parsing import parse_time
    from coa_net.pipeline import COANet
    from coa_net.refine import refine
    from coa_net.render import render_coa_chart, render_map, render_movement
    from coa_net.scenarios import sample_incidents, sample_text, synthetic_theatre

    s = COANet.from_graph(synthetic_theatre(0).G, sim_reps=50)
    s.plan(sample_text("sample_order.txt"))
    figs: dict[str, tuple[str, int]] = {}
    # survivability for C1: fastest route with no designed alternate (B1) against COA-Net's design, same graph and costs
    H, crit, prob = s.H, s.crit, s.prob
    coa = s.coas[s.default].design
    gid = "C1"
    t_star = prob.cands[gid].best_time

    def one(d):
        return make_design(prob, d.name, {gid: d.msr[gid]}, {gid: d.asrs.get(gid, [])})

    def note(d) -> str:
        m, asrs = d.msr[gid], d.asrs.get(gid, [])
        shared = [n for n in _crossings(H, m) if all(n in _crossings(H, a) for a in asrs)]
        if not asrs:
            return f"{gid} has no alternate: one strike on {', '.join(_crossings(H, m))} stops it"
        a = min(asrs, key=lambda r: r.time_min)
        if shared:
            return f"{gid}'s ASR also crosses {', '.join(shared)}: one strike stops both routes"
        return f"{gid}'s ASR avoids {', '.join(_crossings(H, m))} ({a.time_min:.1f} min, {a.time_min / t_star:.2f} x the best time)"

    left, right = one(b1(prob)), one(coa)
    colors = {gid: "#1f6feb"}
    m_left = render_map(H, refine(H, s.ctx.groups, left, crit), left, crit, title="Fastest route, no designed alternate",
                        subtitle=note(left), width=760, height=640, colors=colors, labels=False)
    m_right = render_map(H, refine(H, s.ctx.groups, right, crit), right, crit, title="COA-Net rule: survive any single loss",
                         subtitle=note(right), width=760, height=640, colors=colors, labels=False)
    figs["survivability"] = (f'<div style="display:flex;gap:12px">{m_left}{m_right}</div>', 1532)
    s.choose()
    c0 = s.coas[s.default]
    figs["demo-network-before"] = (captioned(f"{s.snapshots[0][0]} (order LOG-01)", s.snapshots[0][1]), 960)
    longest = max(c0.design.msr.values(), key=lambda r: r.length_m)
    figs["road-movement-graph"] = (render_movement(H, c0.sched, longest, c0.ref,
                                                   title=f"Road movement graph along {c0.ref.name_of(longest)} (COA {c0.label}, before departure)",
                                                   width=960, height=440), 960)
    figs["coa-chart"] = (render_coa_chart(s.facts, title="COAs in the confirmed priority order (lower is better)", width=960), 960)
    timings: dict[str, str] = {}
    captions = []
    for i in sample_incidents():
        o = s.incident(i["text"], parse_time(i["time"]))
        what = f"{o.incident.id} {i['time']} {o.incident.type.value.replace('_', ' ')}: {o.mode}" + (f" {o.playbook}" if o.playbook else "")
        timings[o.incident.id] = f"{what}, {o.seconds:.2f} s with FRAGO"
        captions.append(f"After {what}")
    for k, name in ((1, "demo-network-after-I1"), (2, "demo-network-after-I2")):
        figs[name] = (captioned(captions[k - 1], s.snapshots[k][1]), 960)
    return figs, timings


BENCH_LABELS = {"B0": "B0 fastest route", "B1": "B1 vehicle-aware",
                "COA-Net (greedy)": "COA-Net, greedy design", "COA-Net": "COA-Net, CP-SAT design"}
BENCH_PANELS = (("valid (all levels)", "Valid plans", ("no hard violation at any level", "higher is better")),
                ("reducible SPOF groups", "Avoidable SPOFs", ("groups whose MSR bridge or tunnel", "every ASR also crosses; lower is better")),
                ("reached lost element (sim)", "Reached a lost element", ("columns arriving at a destroyed crossing", "before knowing (simulation); lower is better")))


def parse_bench(md: str) -> tuple[int, dict[str, dict[str, str]]]:
    """First table of ``coa-net bench`` output: {pipeline: {column: cell}}, and the number of seeds."""
    lines = md.splitlines()
    seeds = next((int(x.split()[0]) for x in lines if x.endswith("seeds; means over seeds.")), 0)
    head = next(i for i, x in enumerate(lines) if x.startswith("| pipeline |"))
    cols = [c.strip() for c in lines[head].strip("|").split("|")]
    rows = {}
    for x in lines[head + 2:]:
        if not x.startswith("|"):
            break
        cells = [c.strip() for c in x.strip("|").split("|")]
        rows[cells[0]] = dict(zip(cols, cells))
    return seeds, rows


def bench_chart(md: str) -> str:
    """Small multiples, one panel per metric, one bar per pipeline (COA-Net highlighted)."""
    seeds, rows = parse_bench(md)
    names = [n for n in BENCH_LABELS if n in rows]
    W, label_w, gap = 1400, 236, 84
    pw = (W - 40 - label_w - 2 * gap - 64) / 3
    top, bar_h, step = 230, 26, 46
    bottom = top + step * len(names) - 12
    c = Canvas(W, bottom + 60, "COA-Net against fastest-route baselines, same graph and costs",
               f"Synthetic benchmark, {seeds} random theatres and missions; bars are shares of seeds, groups or simulated replications.")
    for i, n in enumerate(names):
        c.text(40, top + step * i + bar_h / 2 + 6, BENCH_LABELS[n], 18, INK)
    for j, (col, title, sub) in enumerate(BENCH_PANELS):
        x0 = 40 + label_w + j * (pw + gap)
        c.text(x0, 140, title, 19, INK, weight="700")
        for k, line in enumerate(sub):
            c.text(x0, 168 + 21 * k, line, 15, MUTED)
        for f in (0, 0.5, 1):
            gx = x0 + f * pw
            c.raw(f'<line x1="{gx:.1f}" y1="{top - 10}" x2="{gx:.1f}" y2="{bottom}" stroke="#e1e0d9" stroke-width="1"/>')
            c.text(gx, bottom + 26, f"{f:.0%}", 15, "#898781", "middle")
        c.raw(f'<line x1="{x0}" y1="{top - 10}" x2="{x0}" y2="{bottom}" stroke="#c3c2b7" stroke-width="1.5"/>')
        for i, n in enumerate(names):
            v = float(rows[n][col].rstrip("%")) / 100
            y = top + step * i
            w = max(v * pw, 0.0)
            fill = "#2a78d6" if n.startswith("COA-Net") else "#b4b2a9"
            if w > 0:
                r = min(4, w / 2)
                c.raw(f'<path d="M{x0},{y} H{x0 + w - r:.1f} Q{x0 + w:.1f},{y} {x0 + w:.1f},{y + r} V{y + bar_h - r} '
                      f'Q{x0 + w:.1f},{y + bar_h} {x0 + w - r:.1f},{y + bar_h} H{x0} Z" fill="{fill}"/>')
            c.text(x0 + w + 8, y + bar_h / 2 + 6, rows[n][col], 16, "#52514e")
    return c.svg()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent))
    ap.add_argument("--bench", help="markdown written by `coa-net bench --out` (adds the benchmark chart)")
    ap.add_argument("--html-only", action="store_true")
    ap.add_argument("--only", help="comma-separated figure names to build, e.g. coa-net-architecture (default: all)")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None
    figs, timings = ({}, {}) if only is not None and only <= {"coa-net-architecture", "benchmark"} else demo_figures()
    figs["coa-net-architecture"] = (architecture(), 1400)
    if only is None or "incident-loop" in only:
        figs["incident-loop"] = (incident_loop(timings), 1400)
    if args.bench:
        figs["benchmark"] = (bench_chart(Path(args.bench).read_text(encoding="utf-8")), 1400)
    if only is not None:
        figs = {k: v for k, v in figs.items() if k in only}
    pages = {}
    for name, (svg, width) in figs.items():
        p = out / f"{name}.html"
        p.write_text(page(svg, width), encoding="utf-8")
        pages[name] = p
    if args.html_only:
        print("wrote", ", ".join(str(p) for p in pages.values()))
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed: HTML pages written, PNGs not rendered", file=sys.stderr)
        return
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(device_scale_factor=1.5)
        for name, p in pages.items():
            pg.goto(p.resolve().as_uri())
            pg.locator("#fig").screenshot(path=str(out / f"{name}.png"))
            p.unlink()
        b.close()
    print("wrote", ", ".join(f"{n}.png" for n in pages))


if __name__ == "__main__":
    main()
