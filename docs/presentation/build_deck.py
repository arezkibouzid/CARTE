"""Build the 10-minute presentation: charts from the saved experiment runs, slides.html, the PDF, previews, notes.

    LD_LIBRARY_PATH=$(find ~/.local/chrome-libs -type d -name x86_64-linux-gnu | tr '\\n' ':') \
        .venv/bin/python docs/presentation/build_deck.py

Every chart reads its numbers from experiments/runs/ or experiments/analysis/ (paths in RUNS below); the values actually
plotted are written to charts/data_used.json with their source. The editable source is slides.src.html; this script
only inlines charts and icons into slides.html, prints CARTE_presentation.pdf (13.33 x 7.5 in, one slide per page),
renders preview/slide-NN.png and writes speaker_notes.md from the <aside class="notes"> of each slide.
"""
from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNS = {
    "routing": REPO / "experiments/runs/20261113-204810_routing_ablation_6ff7618/metrics.json",
    "stress": REPO / "experiments/runs/20261113-202358_stress_test_6ff7618/metrics.json",
    "imagery": REPO / "experiments/runs/20261113-202429_imagery_ablation_6ff7618/metrics.json",
    "feasibility": REPO / "experiments/analysis/stress_test_feasibility_relative.json",
    "context": REPO / "experiments/runs/20261114-033644_context_stress_eeeda4c/metrics.json",
    "doctrine": REPO / "RAG/rules/doctrine_final_unique_rules.csv",
}
CHARTS = HERE / "charts"

INK, MUTED, GRID = "#14232A", "#5B6B70", "#DCE3E4"
TEAL, CONTEXT, BLUE, AQUA, ZERO = "#1F7A6D", "#B9C3C5", "#2A78D6", "#1BAF7A", "#9AA6A9"

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
plt.rcParams.update({
    "font.family": "IBM Plex Sans", "font.size": 11.5, "svg.fonttype": "none",
    "text.color": INK, "axes.labelcolor": MUTED, "axes.edgecolor": "#9AA6A9", "axes.linewidth": 0.8,
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.major.size": 0, "ytick.major.size": 0,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.titlesize": 12.5, "axes.titleweight": "semibold", "axes.titlelocation": "left", "axes.titlepad": 10,
    "figure.facecolor": "none", "axes.facecolor": "none",
})
used: dict[str, dict] = {}


def fig(w_px, h_px):
    return plt.subplots(figsize=(w_px / 96, h_px / 96))


def save(f, name, source, data):
    plt.rcParams["svg.hashsalt"] = name  # unique ids once several SVGs are inlined in one page
    buf = io.StringIO()
    f.savefig(buf, format="svg", transparent=True)
    plt.close(f)
    svg = buf.getvalue()
    svg = svg[svg.index("<svg"):]
    (CHARTS / f"{name}.svg").write_text(svg, encoding="utf-8")
    used[name] = {"source": str(source.relative_to(REPO)) if isinstance(source, Path) else source, "data": data}


def ygrid(ax):
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def xgrid(ax):
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def charts():
    CHARTS.mkdir(exist_ok=True)
    R = json.loads(RUNS["routing"].read_text())["summary"]["per_config"]
    S = json.loads(RUNS["stress"].read_text())
    I = json.loads(RUNS["imagery"].read_text())["summary"]
    F = json.loads(RUNS["feasibility"].read_text())

    # --- risk as a function of confidence: a plot of the formula, not a measurement
    import numpy as np
    p_ref = 0.78
    p = np.linspace(0.01, 0.95, 400)
    c = np.clip(p / p_ref, 0.05, 1)
    f, ax = fig(500, 300)
    ax.plot(p, -np.log(c), color="#C8382F", linewidth=2.2)
    for pp, lab, dx, dy in [(0.78, "sure road: 0", -0.02, 0.35), (0.39, "half as sure: 0.69", 0.03, 0.3),
                            (0.039, "floor: 3.0", 0.04, -0.05)]:
        r = -np.log(np.clip(pp / p_ref, 0.05, 1))
        ax.plot([pp], [r], "o", color="#C8382F", markersize=7, markeredgecolor="white", markeredgewidth=1.5)
        ax.annotate(lab, (pp, r), (pp + dx, r + dy), color=INK, fontsize=11.5)
    ax.set_xlabel("calibrated U-Net road probability p")
    ax.set_ylabel("risk = −ln(confidence)")
    ax.set_ylim(-0.15, 3.4)
    ax.set_xlim(0, 0.95)
    ygrid(ax)
    f.tight_layout()
    save(f, "risk_curve", "formula: risk = -ln(clip(p / 0.78, 0.05, 1))", {"p_ref": p_ref})

    # --- lambda: km off real roads (bars) and overlap with the BD TOPO route (line): two charts, one axis each
    lams = ["0", "0.1", "0.25", "0.5", "1", "2", "4", "8"]
    keys = ["cost_none"] + [f"cost_lambda_{x}" for x in lams[1:]]
    km = [R[k]["total_km_off_bdtopo_roads"] for k in keys]
    ov = [R[k]["mean_overlap_bdtopo"] for k in keys]
    f, ax = fig(350, 300)
    ax.plot(lams, km, color=INK, linewidth=2, marker="o", markersize=6)
    i = lams.index("0.5")
    ax.plot([lams[i]], [km[i]], "o", color=TEAL, markersize=10, markeredgecolor="white", markeredgewidth=1.5)
    for x, v in zip(lams, km):
        if x in ("0", "0.5", "2", "8"):
            ax.annotate(f"{v:.0f}", (x, v), (0, 9) if x != "0.5" else (0, -18), textcoords="offset points",
                        ha="center", fontsize=10.5, color=INK, fontweight="semibold" if x == "0.5" else "normal")
    ax.set_ylim(95, 130)
    ax.set_title("km off real roads (10 routes)")
    ax.set_xlabel("λ")
    ygrid(ax)
    f.tight_layout()
    save(f, "lambda_km", RUNS["routing"], dict(zip(lams, km)))

    f, ax = fig(350, 300)
    ax.plot(lams, ov, color=INK, linewidth=2, marker="o", markersize=6, markerfacecolor=INK)
    i = lams.index("0.5")
    ax.plot([lams[i]], [ov[i]], "o", color=TEAL, markersize=10, markeredgecolor="white", markeredgewidth=1.5)
    for x, v in zip(lams, ov):
        if x in ("0", "0.5", "1", "8"):
            ax.annotate(f"{v:.2f}", (x, v), (6, 6), textcoords="offset points", fontsize=10.5, color=INK)
    ax.set_ylim(0, 1)
    ax.set_title("overlap with the reference route")
    ax.set_xlabel("λ")
    ygrid(ax)
    f.tight_layout()
    save(f, "lambda_overlap", RUNS["routing"], dict(zip(lams, ov)))

    # --- network ablation: km off real roads by configuration
    cfg = [("U-Net coverage (default)", "unet_nodice", TEAL), ("U-Net compromise (TTA)", "unet_nodice_tta", TEAL),
           ("U-Net precision", "unet_mix12k_tta", TEAL), ("U-Net reference (Dice)", "unet_mix12k", TEAL),
           ("U-Net + OSM complement", "unet_nodice+osm_fill", BLUE), ("no model: OSM", "no_model_osm", CONTEXT)]
    f, ax = fig(600, 330)
    labels = [c[0] for c in cfg][::-1]
    vals = [R[c[1]]["total_km_off_bdtopo_roads"] for c in cfg][::-1]
    gaps = [R[c[1]]["mean_abs_gap_bdtopo"] for c in cfg][::-1]
    ax.barh(labels, vals, color=[c[2] for c in cfg][::-1], height=0.6)
    for y, (v, g) in enumerate(zip(vals, gaps)):
        ax.text(v + 2, y, f"{v:.0f} km  ·  length gap {g * 100:.1f} %", va="center", fontsize=10.5, color=INK)
    ax.set_xlim(0, 215)
    ax.set_title("km off real roads, 10 exercises (lower is better)")
    ax.spines["left"].set_visible(False)
    xgrid(ax)
    f.tight_layout()
    save(f, "network_ablation", RUNS["routing"], {c[1]: {"km_off": R[c[1]]["total_km_off_bdtopo_roads"],
                                                       "abs_gap": R[c[1]]["mean_abs_gap_bdtopo"]} for c in cfg})

    rec = S["records"]
    # --- deconfliction: shared km
    dec = [r for r in rec if r["test"] == "deconflict"]
    order = ["none", "penalize", "forbid"]
    names = {"none": "none", "penalize": "penalise", "forbid": "forbid"}
    dec.sort(key=lambda r: order.index(r["deconflict"]))
    f, ax = fig(520, 220)
    base = dec[0]["total_km"]
    ys = [names[r["deconflict"]] for r in dec][::-1]
    vs = [r["shared_km"] for r in dec][::-1]
    ax.barh(ys, vs, color=[CONTEXT if y == "none" else TEAL for y in ys], height=0.55)
    for y, r in enumerate(dec[::-1]):
        extra = (r["total_km"] / base - 1) * 100
        ax.text(r["shared_km"] + 3, y, f"{r['shared_km']:.1f} km shared · total {r['total_km']:,.0f} km"
                + (f" (+{extra:.0f} %)" if extra > 0.5 else ""), va="center", fontsize=10.5, color=INK)
    ax.set_xlim(0, 330)
    ax.set_title("16 convoys: km of road shared between convoys")
    ax.spines["left"].set_visible(False)
    xgrid(ax)
    f.tight_layout()
    save(f, "deconflict", RUNS["stress"], {r["deconflict"]: {k: r[k] for k in ("shared_km", "total_km")} for r in dec})

    # --- scalability: distance, convoys, cores
    d = [r for r in rec if r["test"] == "distance"]
    f, ax = fig(275, 250)
    ax.scatter([r["straight_km"] for r in d], [r["solve_s"] for r in d], s=34, color=TEAL, edgecolor="white",
               linewidth=1)
    ax.set_ylim(0, 0.5)
    ax.set_xlabel("distance between points (km)")
    ax.set_title("solve time (s)")
    ygrid(ax)
    f.tight_layout()
    save(f, "scale_distance", RUNS["stress"], {"points": [(r["straight_km"], r["solve_s"]) for r in d]})

    cv = sorted([r for r in rec if r["test"] == "convoys"], key=lambda r: r["convoys"])
    f, ax = fig(275, 250)
    xs, ys2 = [r["convoys"] for r in cv], [r["wall_s"] for r in cv]
    ax.plot(xs, ys2, color=TEAL, linewidth=2, marker="o", markersize=6)
    for x, y in zip(xs, ys2):
        if x in (16, 32, 64):
            ax.annotate(f"{y:.0f} s", (x, y), (-32, 2), textcoords="offset points", fontsize=10.5, color=INK)
    ax.set_xlabel("number of convoys")
    ax.set_title("planning time (s)")
    ax.set_ylim(0, 38)
    ygrid(ax)
    f.tight_layout()
    save(f, "scale_convoys", RUNS["stress"], {r["convoys"]: r["wall_s"] for r in cv})

    th = sorted([r for r in rec if r["test"] == "threads"], key=lambda r: r["cores"])
    f, ax = fig(370, 270)
    lab = [str(r["cores"]) for r in th]
    bars = ax.bar(lab, [r["workload_s"] for r in th], color=CONTEXT, width=0.6)
    for b, r in zip(bars, th):
        ax.text(b.get_x() + b.get_width() / 2, r["workload_s"] + 0.1, f"{r['workload_s']:.1f}", ha="center",
                va="bottom", fontsize=10.5, color=INK)
    ax.set_ylim(0, 6.5)
    ax.set_xlabel("CPU cores")
    ax.set_title("same workload (s) vs cores")
    ygrid(ax)
    f.tight_layout()
    save(f, "scale_cores", RUNS["stress"], {r["cores"]: r["workload_s"] for r in th})

    # --- robustness: feasibility with missing data (relative criterion, derived analysis)
    inc = F["incomplete"]
    for name, kind, shares, title in [("robust_random", "random sections", ["0.01", "0.02", "0.05", "0.1"],
                                       "random gaps"),
                                      ("robust_patches", "unreadable patches", ["0.05", "0.1", "0.2", "0.3"],
                                       "map patches")]:
        f, ax = fig(275, 250)
        v = [inc[f"{kind} {s}"]["feasible"] * 100 for s in shares]
        lab = [f"{float(s) * 100:.0f} %" for s in shares]
        bars = ax.bar(lab, v, color=[TEAL if x >= 90 else ("#E39B00" if x >= 50 else "#C8382F") for x in v],
                      width=0.6)
        for b, x in zip(bars, v):
            ax.text(b.get_x() + b.get_width() / 2, x + 2, f"{x:.0f} %", ha="center", va="bottom", fontsize=10.5,
                    color=INK)
        ax.set_ylim(0, 115)
        ax.set_yticks([0, 50, 100])
        ax.set_title(f"feasible: {title}")
        ax.set_xlabel("share of sections removed" if kind.startswith("random") else "share of the sheet covered")
        ygrid(ax)
        f.tight_layout()
        save(f, name, RUNS["feasibility"], dict(zip(shares, v)))

    # --- imagery / fine-tuning
    conds = [("original", "original"), ("jpeg_q20", "JPEG q20"), ("noise_12", "noise σ 12"),
             ("grayscale", "grayscale"), ("other_palette", "other palette"), ("half_resolution", "½ resolution"),
             ("blur", "blur"), ("quarter_resolution", "¼ resolution"), ("low_contrast", "low contrast")]
    models = [("zeroshot", "zero-shot (Swiss maps)", ZERO), ("finetuned_hack", "fine-tuned, 1 sheet", BLUE),
              ("mix12k_nodice", "fine-tuned, 2 sheets", AQUA)]
    f, ax = fig(600, 440)
    n = len(conds)
    h = 0.26
    for j, (m, ml, col) in enumerate(models):
        vals = [I[f"{m} / {c}"]["f1"] for c, _ in conds]
        ys3 = [n - 1 - i + (1 - j) * h for i in range(n)]
        ax.barh(ys3, vals, height=h * 0.9, color=col, label=ml)
        if m != "zeroshot":
            for y, v in zip(ys3, vals):
                ax.text(v + 0.008, y, f"{v:.2f}", va="center", fontsize=9.5, color=INK)
        else:
            ax.text(vals[0] + 0.008, ys3[0], f"{vals[0]:.2f}", va="center", fontsize=9.5, color=INK)
    ax.set_yticks(range(n))
    ax.set_yticklabels([c[1] for c in conds][::-1])
    ax.set_xlim(0, 0.7)
    ax.set_title("road F1 against BD TOPO, out of fold (208 Mpx)")
    ax.spines["left"].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.45, -0.07), ncol=3, frameon=False, fontsize=10.5)
    xgrid(ax)
    f.tight_layout()
    save(f, "imagery_f1", RUNS["imagery"], {f"{m} / {c}": I[f"{m} / {c}"]["f1"] for m, _, _ in models for c, _ in conds})

    # --- context window stress test
    C = json.loads(RUNS["context"].read_text())["records"]
    o = [r for r in C if r["test"] == "orders"]
    f, ax = fig(330, 250)
    xs = [str(r["orders"]) for r in o]
    ax.plot(xs, [r["used_tokens"] for r in o], color=TEAL, linewidth=2, marker="o", markersize=6)
    ax.axhline(6000, color="#9AA6A9", linestyle="--", linewidth=1.2)
    ax.text(0, 6150, "budget 6,000", fontsize=10, color=MUTED)
    offs = [(-62, -30), (-44, -62)]
    for r, x in zip([r for r in o if r["order_blocks_omitted"]], [x for r, x in zip(o, xs) if r["order_blocks_omitted"]]):
        ax.annotate(f"{r['order_blocks_omitted']} left out", (x, r["used_tokens"]), offs.pop(0) if offs else (-40, -40), textcoords="offset points",
                    fontsize=9.5, color=INK, arrowprops=dict(arrowstyle="-", color="#9AA6A9", lw=0.8))
    ax.set_ylim(0, 7000)
    ax.set_xlabel("orders selected")
    ax.set_title("tokens used")
    ygrid(ax)
    f.tight_layout()
    save(f, "ctx_orders", RUNS["context"], {r["orders"]: [r["used_tokens"], r["order_blocks_omitted"]] for r in o})

    ch = [r for r in C if r["test"] == "chain"]
    f, ax = fig(330, 250)
    xs = [str(r["depth"]) for r in ch]
    ax.bar(xs, [r["used_tokens"] - (r["links_block_tokens"] or 0) for r in ch], color=CONTEXT, width=0.6, label="rest of context")
    ax.bar(xs, [r["links_block_tokens"] or 0 for r in ch], bottom=[r["used_tokens"] - (r["links_block_tokens"] or 0) for r in ch],
           color=TEAL, width=0.6, label="links block")
    for x, r in zip(xs, ch):
        ax.text(x, r["used_tokens"] + 120, "✓" if r["answer_has_value"] and not r["answer_stale"] else "✗", ha="center",
                fontsize=12, color=TEAL if r["answer_has_value"] else "#C8382F")
    ax.set_ylim(0, 8600)
    ax.set_xlabel("FRAGOs in the chain")
    ax.set_title("tokens · ✓ = value in force given")
    ax.legend(frameon=False, fontsize=9.5, loc="upper left")
    ygrid(ax)
    f.tight_layout()
    save(f, "ctx_chain", RUNS["context"], {r["depth"]: [r["used_tokens"], r["links_block_tokens"], r["answer_has_value"], r["answer_stale"]] for r in ch})

    bu = [r for r in C if r["test"] == "budget"]
    f, ax = fig(330, 250)
    xs = [f"{r['budget'] // 1000 if r['budget'] >= 1000 and r['budget'] % 1000 == 0 else r['budget'] / 1000:g}k" for r in bu]
    bars = ax.bar(xs, [r["omitted_blocks"] for r in bu], color=CONTEXT, width=0.6)
    for b_, r in zip(bars, bu):
        ax.text(b_.get_x() + b_.get_width() / 2, r["omitted_blocks"] + 0.6, f"{r['omitted_blocks']}", ha="center", fontsize=10.5, color=INK)
    ax.set_ylim(0, 36)
    ax.set_xlabel("token budget (30 orders in play)")
    ax.set_title("blocks left out · links kept 4/4")
    ygrid(ax)
    f.tight_layout()
    save(f, "ctx_budget", RUNS["context"], {r["budget"]: [r["omitted_blocks"], r["links_block_form"], r["answer_has_value"]] for r in bu})

    # --- doctrine rule base (the CSV itself)
    import csv
    import collections
    rows = list(csv.DictReader(open(RUNS["doctrine"], encoding="utf-8-sig")))
    grp = collections.Counter()
    for r_ in rows:
        d = r_["document"]
        k = "FM 55-30 / STANAG 2174" if "55-30" in d else "FM 5-36 / STANAG 2015" if "5-36" in d else d
        grp[k] += 1
    f, ax = fig(380, 210)
    labs = [k for k, _ in grp.most_common()][::-1]
    vals = [grp[k] for k in labs]
    ax.barh(labs, vals, color=BLUE, height=0.6)
    for y, v in enumerate(vals):
        ax.text(v + 1.5, y, str(v), va="center", fontsize=10.5, color=INK)
    ax.set_xlim(0, 90)
    ax.set_title(f"{len(rows)} rules by source")
    ax.spines["left"].set_visible(False)
    xgrid(ax)
    f.tight_layout()
    save(f, "doctrine_docs", RUNS["doctrine"], dict(grp))

    (CHARTS / "data_used.json").write_text(json.dumps(used, indent=1, ensure_ascii=False), encoding="utf-8")


def assemble() -> Path:
    src = (HERE / "slides.src.html").read_text(encoding="utf-8")

    def chart(m):
        return f'<div class="chart">{(CHARTS / (m.group(1) + ".svg")).read_text(encoding="utf-8")}</div>'

    def icon(m):
        svg = (HERE / "icons" / f"{m.group(1)}.svg").read_text(encoding="utf-8")
        return svg.replace("<svg ", '<svg class="ic" ', 1)

    out = re.sub(r"<!--chart:([\w]+)-->", chart, src)
    out = re.sub(r"<!--icon:([\w]+)-->", icon, out)
    p = HERE / "slides.html"
    p.write_text(out, encoding="utf-8")
    return p


def notes(html: str):
    L = ["# Speaker notes — CARTE, 10-minute talk", "",
         "Generated from `slides.src.html` by `build_deck.py`. Times add up to about 10 minutes.", ""]
    total = 0
    for i, sec in enumerate(re.findall(r'<section class="slide[^"]*"(.*?)</section>', html, re.S), 1):
        t = re.search(r"<h1[^>]*>(.*?)</h1>", sec, re.S)
        n = re.search(r'<aside class="notes" data-time="(\d+)">(.*?)</aside>', sec, re.S)
        title = re.sub(r"<[^>]+>", "", t.group(1)).strip() if t else f"Slide {i}"
        title = re.sub(r"\s+", " ", title)
        secs = int(n.group(1)) if n else 0
        total += secs
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", n.group(2))).strip() if n else ""
        L += [f"## {i}. {title}  ({secs // 60}:{secs % 60:02d})", "", body, ""]
    L += [f"**Total: {total // 60}:{total % 60:02d}**", ""]
    (HERE / "speaker_notes.md").write_text("\n".join(L), encoding="utf-8")
    print(f"notes: {total} s")


def render(page_html: Path):
    from playwright.sync_api import sync_playwright
    prev = HERE / "preview"
    prev.mkdir(exist_ok=True)
    for old in prev.glob("slide-*.png"):
        old.unlink()
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 720})
        pg.goto(page_html.as_uri())
        pg.wait_for_load_state("networkidle")
        pg.evaluate("document.fonts.ready")
        pg.pdf(path=str(HERE / "CARTE_presentation.pdf"), width="1280px", height="720px", print_background=True,
               margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        n = pg.locator("section.slide").count()
        for i in range(n):
            pg.locator("section.slide").nth(i).screenshot(path=str(prev / f"slide-{i + 1:02d}.png"))
        # overflow check: any element sticking out of its slide
        bad = pg.evaluate("""() => [...document.querySelectorAll('section.slide')].flatMap((s, i) => {
            const r = s.getBoundingClientRect();
            return [...s.querySelectorAll('*')].filter(e => !e.closest('aside')).filter(e => {
                const q = e.getBoundingClientRect();
                return q.width && (q.right > r.right + 1 || q.bottom > r.bottom + 1 || q.left < r.left - 1);
            }).slice(0, 3).map(e => `slide ${i + 1}: <${e.tagName.toLowerCase()} class=${e.className.baseVal ?? e.className}>`);
        })""")
        b.close()
    print(f"{n} slides; PDF and previews written")
    for x in bad:
        print("OVERFLOW", x)


if __name__ == "__main__":
    charts()
    p = assemble()
    notes(p.read_text(encoding="utf-8"))
    render(p)
