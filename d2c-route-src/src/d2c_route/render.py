"""Plan rendering: dependency-free SVG (any CRS) and optional Folium web map."""

from __future__ import annotations

import html
import math
from pathlib import Path

import networkx as nx

from .graph import is_geographic
from .schemas import Plan

PALETTE = ["#1f6feb", "#d97706", "#7c3aed", "#059669", "#db2777", "#0891b2", "#65a30d"]


def _projector(G: nx.DiGraph, width: int, height: int, pad: int = 30):
    xs = [d["x"] for _, d in G.nodes(data=True)]
    ys = [d["y"] for _, d in G.nodes(data=True)]
    kx = math.cos(math.radians(sum(ys) / len(ys))) if is_geographic(G) else 1.0
    x0, x1, y0, y1 = min(xs) * kx, max(xs) * kx, min(ys), max(ys)
    s = min((width - 2 * pad) / max(x1 - x0, 1e-9), (height - 2 * pad) / max(y1 - y0, 1e-9))

    def proj(n):
        d = G.nodes[n]
        return pad + (d["x"] * kx - x0) * s, height - pad - (d["y"] - y0) * s

    return proj


def render_svg(G: nx.DiGraph, plan: Plan | None = None, title: str = "", width: int = 900, height: int = 680) -> str:
    proj = _projector(G, width, height - 60)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="11">',
        f'<rect width="{width}" height="{height}" fill="#fafaf7"/>',
        f'<text x="12" y="20" font-size="14" font-weight="bold">{html.escape(title)}</text>',
    ]
    seen = set()
    for u, v, d in G.edges(data=True):
        k = (min(str(u), str(v)), max(str(u), str(v)))
        if k in seen:
            continue
        seen.add(k)
        (x1, y1), (x2, y2) = proj(u), proj(v)
        w = max(0.6, 3.2 - 0.45 * d["road_rank"])
        color, dash = "#9ca3af", ""
        if d["built_up"]:
            color = "#f59e0b"
        elif d["forest"]:
            color = "#86b88a"
        if d["road_rank"] >= 6:
            dash = ' stroke-dasharray="3,3"'
        if d["bridge"]:
            color, w = "#2563eb", w + 2
        if d["tunnel"]:
            color, dash = "#111827", ' stroke-dasharray="6,2"'
        if d["threat"] > 0:
            out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#ef4444" stroke-opacity="{0.15 + 0.5 * d["threat"]:.2f}" stroke-width="{w + 7:.1f}"/>')
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{w:.1f}"{dash}/>')
        if d["closed"]:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            out.append(f'<text x="{mx:.1f}" y="{my + 5:.1f}" text-anchor="middle" font-size="16" fill="#dc2626" font-weight="bold">✕</text>')
        if d["bridge"]:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            label = f"MLC {d['mlc']}" if d.get("mlc") is not None else "MLC ?"
            out.append(f'<text x="{mx + 6:.1f}" y="{my:.1f}" fill="#1e40af">{label}</text>')
    legend_y = height - 40
    if plan:
        for i, (cid, cp) in enumerate(sorted(plan.convoys.items())):
            col = PALETTE[i % len(PALETTE)]
            off = (i - len(plan.convoys) / 2) * 2.5
            for j, r in enumerate(cp.asrs):
                pts = " ".join(f"{x + off:.1f},{y + off:.1f}" for x, y in map(proj, r.nodes))
                out.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2" stroke-dasharray="7,4" stroke-opacity="0.7"/>')
            if cp.msr:
                pts = " ".join(f"{x + off:.1f},{y + off:.1f}" for x, y in map(proj, cp.msr.nodes))
                out.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="4" stroke-linejoin="round" stroke-opacity="0.9"/>')
            lx = 12 + i * 150
            out.append(f'<line x1="{lx}" y1="{legend_y}" x2="{lx + 26}" y2="{legend_y}" stroke="{col}" stroke-width="4"/>')
            out.append(f'<text x="{lx + 32}" y="{legend_y + 4}">{html.escape(cid)} MSR ({cp.status})</text>')
    out.append(
        f'<text x="12" y="{height - 14}" fill="#555">dashed colour = ASR · blue = bridge · black dashed = tunnel · orange = built-up · '
        "green = forest · red halo = threat · ✕ = closed</text>"
    )
    for n, d in G.nodes(data=True):
        if d.get("name"):
            x, y = proj(n)
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#111827"/>')
            out.append(f'<text x="{x + 6:.1f}" y="{y - 6:.1f}" font-weight="bold">{html.escape(d["name"])}</text>')
    out.append("</svg>")
    return "\n".join(out)


def write_html(path: str | Path, svgs: list[tuple[str, str]], body_md: str = "") -> Path:
    """One page with several SVG panels and the explanation as preformatted text."""
    panels = "\n".join(f"<section><h2>{html.escape(t)}</h2>{s}</section>" for t, s in svgs)
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>D2C-Route plan</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:sans-serif;max-width:960px;margin:auto;padding:16px;background:#fff;color:#111}}
svg{{width:100%;height:auto;border:1px solid #ddd}}pre{{white-space:pre-wrap;background:#f6f6f6;padding:12px}}</style>
</head><body><h1>D2C-Route</h1>{panels}<h2>Explanation</h2><pre>{html.escape(body_md)}</pre></body></html>"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page, encoding="utf-8")
    return p


def render_folium(G: nx.DiGraph, plan: Plan, path: str | Path) -> Path:
    """Interactive web map for lon/lat graphs (needs ``pip install folium``)."""
    import folium

    if not is_geographic(G):
        raise ValueError("folium rendering needs a lon/lat graph")
    ys = [d["y"] for _, d in G.nodes(data=True)]
    xs = [d["x"] for _, d in G.nodes(data=True)]
    m = folium.Map(location=[sum(ys) / len(ys), sum(xs) / len(xs)], zoom_start=11, tiles="OpenStreetMap")
    for i, (cid, cp) in enumerate(sorted(plan.convoys.items())):
        col = PALETTE[i % len(PALETTE)]
        for r in cp.asrs:
            folium.PolyLine([(G.nodes[n]["y"], G.nodes[n]["x"]) for n in r.nodes], color=col, weight=3, dash_array="8", tooltip=f"{cid} ASR").add_to(m)
        if cp.msr:
            folium.PolyLine([(G.nodes[n]["y"], G.nodes[n]["x"]) for n in cp.msr.nodes], color=col, weight=6, tooltip=f"{cid} MSR").add_to(m)
    for u, v, d in G.edges(data=True):
        if d["closed"]:
            folium.PolyLine([(G.nodes[u]["y"], G.nodes[u]["x"]), (G.nodes[v]["y"], G.nodes[v]["x"])], color="red", weight=8, tooltip="closed").add_to(m)
    p = Path(path)
    m.save(str(p))
    return p
