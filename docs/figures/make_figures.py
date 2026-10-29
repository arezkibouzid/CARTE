"""Regenerate the figures of docs/COA-Net_methodology.md and README.md.

    python docs/figures/make_figures.py [--out docs/figures]

Writes one self-contained HTML page per figure (SVG inside) and, when the
``playwright`` Python package and its Chromium are installed, the PNG next to
it (``pip install playwright && playwright install chromium``). The diagrams
follow the style of D2C-Route's architecture figure: blue boxes are
language-model steps, grey boxes symbolic steps, dashed boxes inputs.
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

    def box(self, x, y, w, h, title, sub="", kind="code", sub2=""):
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
        lines = [s for s in (sub, sub2) if s]
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
    c = Canvas(1400, 1120, "The models read and write; code designs the network",
               "COA-Net stages S0-S10. Every model role (L1-L8) is checked by code and has a deterministic fallback.")
    bw, bh = 290, 96
    X0, X1, X2, X3 = 40, 340, 680, 1060
    w3, c3 = 310, 1060 + 155
    rows = {"map": 140, "doc": 300, "order": 500, "inc": 940}
    mid = lambda r: r + bh / 2  # noqa: E731
    for key, label in (("map", "BD TOPO / OSM + DEM"), ("doc", "Doctrine corpus"), ("order", "Logistic order, intent"), ("inc", "Incident reports")):
        c.box(X0, rows[key] + 13, 245, 70, label, kind="input")
        c.arrow([(X0 + 245, mid(rows[key])), (X1 - 4, mid(rows[key]))])
    c.box(X1, rows["map"], bw, bh, "S0 Theatre preparation", "corridor graph,", sub2="critical elements, halt areas")
    c.box(X1, rows["doc"], bw, bh, "S1 Requirement compiler", "L1: 26 templates, 4 tiers", kind="llm")
    c.box(X1, rows["order"], bw, bh, "S2 Order and intent", "L2 order, L3 priority order", kind="llm")
    c.box(X1, rows["inc"], bw, bh, "S10 Report parser", "L6: typed, graded report", kind="llm")
    c.box(X2, rows["order"], bw, bh, "S3-S4 Routes, criticality", "candidates, SPOFs,", sub2="L4 red team (likelihood)")
    c.box(X2, rows["inc"], bw, bh, "S10 Belief and repair", "playbook, or S5 from", sub2="the current positions")
    c.box(X3, rows["order"], w3, bh, "S5 Network design", "CP-SAT: survivability,", sub2="priorities, detour guard")
    c.box(X3, 650, w3, bh, "S6-S8 Credits and checks", "schedule, checker, playbooks,", sub2="recce value, Monte Carlo")
    c.box(X3, 795, w3, bh, "S9 COAs and briefing", "facts table, L5 comparison,", sub2="L8 explanations")
    c.box(X3, rows["inc"], w3, bh, "Plan and orders", "overlay, march tables,", sub2="road movement graph, annex", kind="out")
    c.arrow([(X1 + bw, mid(rows["map"])), (c3, mid(rows["map"])), (c3, rows["order"] - 4)], "corridor graph, critical elements",
            at=(X2 + 150, mid(rows["map"]) - 12))
    c.arrow([(X1 + bw, mid(rows["doc"])), (X2 + bw / 2, mid(rows["doc"])), (X2 + bw / 2, rows["order"] - 4)], "approved requirements",
            at=(X1 + bw + 175, mid(rows["doc"]) - 12))
    c.arrow([(X1 + bw, mid(rows["order"])), (X2 - 4, mid(rows["order"]))])
    c.arrow([(X2 + bw, mid(rows["order"])), (X3 - 4, mid(rows["order"]))])
    c.arrow([(c3, rows["order"] + bh), (c3, 650 - 4)])
    c.arrow([(c3, 650 + bh), (c3, 795 - 4)])
    c.arrow([(c3, 795 + bh), (c3, rows["inc"] - 4)], "operator's choice", at=(c3 + 12, 795 + bh + 30), anchor="start")
    c.arrow([(X1 + bw, mid(rows["inc"])), (X2 - 4, mid(rows["inc"]))])
    c.arrow([(X2 + bw, mid(rows["inc"])), (X3 - 4, mid(rows["inc"]))])
    c.text((X2 + bw + X3) / 2, mid(rows["inc"]) - 34, "FRAGO", anchor="middle")
    c.text((X2 + bw + X3) / 2, mid(rows["inc"]) - 12, "(L7)", anchor="middle")
    c.arrow([(X2 + 95, rows["inc"]), (X2 + 95, 698), (X3 - 4, 698)], "repair, re-time", at=(X2 + 107, 686), anchor="start")
    c.arrow([(X3 - 4, 843), (X2 + 200, 843), (X2 + 200, rows["inc"] - 4)], "playbooks", at=(X2 + 212, 875), anchor="start", dashed=True)
    c.legend(1088)
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
    from coa_net.baselines import b2, make_design
    from coa_net.parsing import parse_time
    from coa_net.pipeline import COANet
    from coa_net.refine import refine
    from coa_net.render import render_coa_chart, render_map, render_movement
    from coa_net.scenarios import sample_incidents, sample_text, synthetic_theatre

    s = COANet.from_graph(synthetic_theatre(0).G, sim_reps=50)
    s.plan(sample_text("sample_order.txt"))
    figs: dict[str, tuple[str, int]] = {}
    # survivability for C1: D2C-Route's alternate rule (B2) against COA-Net's design, same graph and costs
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
            return f"{gid} has no ASR"
        a = min(asrs, key=lambda r: r.time_min)
        if shared:
            return f"{gid}'s ASR also crosses {', '.join(shared)}: one strike stops both routes"
        return f"{gid}'s ASR avoids {', '.join(_crossings(H, m))} ({a.time_min:.1f} min, {a.time_min / t_star:.2f} x the best time)"

    left, right = one(b2(prob)), one(coa)
    colors = {gid: "#1f6feb"}
    m_left = render_map(H, refine(H, s.ctx.groups, left, crit), left, crit, title="D2C-Route rule: alternate with < 50 % overlap",
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


BENCH_LABELS = {"B0": "B0 fastest route", "B1": "B1 vehicle-aware", "B2": "B2 D2C-Route style", "B3": "B3 D2C + disjoint fix",
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
    c = Canvas(W, bottom + 60, "Same graph, same costs: only the design rule changes",
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
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    figs, timings = demo_figures()
    figs["coa-net-architecture"] = (architecture(), 1400)
    figs["incident-loop"] = (incident_loop(timings), 1400)
    if args.bench:
        figs["benchmark"] = (bench_chart(Path(args.bench).read_text(encoding="utf-8")), 1400)
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
