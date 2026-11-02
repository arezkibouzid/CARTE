"""Rendering: route network overlay, road movement graph, COA chart and HTML report.

Dependency-free SVG (any coordinate system), with these
layers: named routes (MSR solid, ASR dashed) coloured by group, control posts,
halt areas, critical elements with the irreducible single points of failure
ringed, start and release points, and incidents. ``render_folium`` draws the
same routes on a web map for lon/lat graphs (needs ``pip install folium``).
"""

from __future__ import annotations

import html
import math
import re
from pathlib import Path
from typing import Any

import networkx as nx

from .corridors import CorridorGraph
from .graph import is_geographic, node_xy, ukey
from .schedule import Schedule, hhmm, movement_lines

PALETTE = ["#1f6feb", "#d97706", "#7c3aed", "#059669", "#db2777", "#0891b2", "#65a30d"]
COA_COLORS = ["#2563eb", "#9333ea", "#0d9488", "#ea580c", "#64748b"]
FONT = "font-family=\"Helvetica, Arial, sans-serif\""


def _esc(s: Any) -> str:
    return html.escape(str(s))


def _projector(G: nx.DiGraph, width: int, height: int, pad: int = 30, top: int = 0):
    xs = [d["x"] for _, d in G.nodes(data=True)]
    ys = [d["y"] for _, d in G.nodes(data=True)]
    kx = math.cos(math.radians(sum(ys) / len(ys))) if is_geographic(G) else 1.0
    x0, x1, y0, y1 = min(xs) * kx, max(xs) * kx, min(ys), max(ys)
    s = min((width - 2 * pad) / max(x1 - x0, 1e-9), (height - 2 * pad) / max(y1 - y0, 1e-9))

    def proj_xy(x: float, y: float) -> tuple[float, float]:
        return pad + (x * kx - x0) * s, top + height - pad - (y - y0) * s

    def proj(n):
        d = G.nodes[n]
        return proj_xy(d["x"], d["y"])

    proj.xy = proj_xy  # type: ignore[attr-defined]
    proj.scale = s / 111_320.0 if is_geographic(G) else s  # type: ignore[attr-defined]  # px per metre (approx.)
    return proj


def group_colors(groups) -> dict[str, str]:
    return {g: PALETTE[i % len(PALETTE)] for i, g in enumerate(sorted(groups))}


# ---------------------------------------------------------------------------
# Route network overlay
# ---------------------------------------------------------------------------

MAP_KEY = ["blue = bridge", "black dashed = tunnel", "orange = built-up", "green = forest", "red halo = threat", "□ control post (■ with escort)",
           "△ halt area", "red ring = single point of failure (SPOF)", "navy ring = critical element with an alternate", "✕ = closed",
           "now = position at the incident"]


def _wrap(items: list[str], width_px: float, char_px: float = 5.9) -> list[str]:
    """Join items with " · " into lines of at most ``width_px`` (about 5.9 px per character at 11 px)."""
    lines, cur = [], ""
    for it in items:
        nxt = f"{cur} · {it}" if cur else it
        if cur and char_px * len(nxt) > width_px:
            lines.append(cur)
            nxt = it
        cur = nxt
    return lines + [cur]


def render_map(H: CorridorGraph, ref=None, design=None, crit=None, title: str = "", subtitle: str = "",
               width: int = 960, height: int = 720, incidents: list[dict] | None = None, colors: dict[str, str] | None = None,
               legend: bool = True, labels: bool = True, origins: dict[str, Any] | None = None) -> str:
    """Road network with the designated routes of a design.

    ``incidents``: dicts with ``kind`` ("closure" | "threat" | "breakdown"), and ``element`` or ``node`` or
    ``xy`` (+ ``radius_m``), and an optional ``label``. ``origins`` (group -> start node of the mission): a
    route that starts elsewhere (re-planned from a current position) is marked "now" instead of "SP"."""
    G = H.G
    top = 52 if title else 10
    key = _wrap(MAP_KEY, width - 28) if legend else []
    bottom = 49 + 15 * len(key) if legend else 10
    proj = _projector(G, width, height - top - bottom, top=top)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" {FONT} font-size="11">',
        f'<rect width="{width}" height="{height}" fill="#fafaf7"/>',
    ]
    if title:
        out.append(f'<text x="14" y="24" font-size="16" font-weight="bold" fill="#111827">{_esc(title)}</text>')
    if subtitle:
        out.append(f'<text x="14" y="42" font-size="12" fill="#6b7280">{_esc(subtitle)}</text>')
    # threat halos below everything
    seen = set()
    edges = []
    for u, v, d in G.edges(data=True):
        k = ukey(u, v)
        if k in seen:
            continue
        seen.add(k)
        edges.append((u, v, d))
    for u, v, d in edges:
        if d.get("threat", 0) > 0:
            (x1, y1), (x2, y2) = proj(u), proj(v)
            out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#ef4444" '
                       f'stroke-opacity="{0.12 + 0.45 * d["threat"]:.2f}" stroke-width="12" stroke-linecap="round"/>')
    for u, v, d in edges:
        (x1, y1), (x2, y2) = proj(u), proj(v)
        rank = d.get("road_rank", 5)
        w = max(0.6, 3.0 - 0.4 * rank)
        color, dash = "#b6bcc6", ""
        if d.get("built_up"):
            color = "#f2a93b"
        elif d.get("forest"):
            color = "#9cc79f"
        if rank >= 6:
            dash = ' stroke-dasharray="3,3"'
        if d.get("bridge"):
            color, w = "#2563eb", w + 1.6
        if d.get("tunnel"):
            color, dash = "#111827", ' stroke-dasharray="6,2"'
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{w:.1f}"{dash}/>')
    # designated routes
    colors = colors or (group_colors(design.msr) if design is not None else {})
    if ref is not None:
        order = sorted(ref.routes.items(), key=lambda kv: (kv[1].role != "ASR", kv[0]))  # ASRs first, MSRs on top
        gidx = {g: i for i, g in enumerate(sorted(colors))}
        for i, (nm, nr) in enumerate(order):
            g0 = nr.groups[0] if nr.groups else ""
            col = colors.get(g0, "#374151")
            off = (gidx.get(g0, 0) - (len(colors) - 1) / 2) * 3.0
            pts = [proj(n) for n in nr.route.nodes]
            ps = " ".join(f"{x + off:.1f},{y + off:.1f}" for x, y in pts)
            if nr.role == "MSR":
                out.append(f'<polyline points="{ps}" fill="none" stroke="#ffffff" stroke-width="8" stroke-linejoin="round" stroke-opacity="0.8"/>')
                out.append(f'<polyline points="{ps}" fill="none" stroke="{col}" stroke-width="5" stroke-linejoin="round" stroke-linecap="round"/>')
            else:
                out.append(f'<polyline points="{ps}" fill="none" stroke="{col}" stroke-width="3" stroke-dasharray="9,5" stroke-linejoin="round" stroke-opacity="0.85"/>')
    # critical elements, posts, halts
    if design is not None:
        used = set()
        for g in design.msr:
            for r in design.routes(g):
                used |= set(r.elements)
        irr = {e for g, s in design.irreducible.items() for e in s if g in design.msr and e in design.msr[g].elements}
        # single points of failure of this design: on a group's MSR and on every one of its ASRs
        spof = {e for g, m in design.msr.items() for e in m.elements if all(e in a.elements for a in design.asrs.get(g, []))}
        for e in sorted(used):
            el = H.elements[e]
            if el.kind == "articulation" and e not in irr:
                continue
            x, y = proj.xy(*el.xy)
            if e in irr or e in spof:
                big = el.kind != "articulation"
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{13 if big else 9}" fill="none" stroke="#dc2626" stroke-width="{2.5 if big else 1.8}"/>')
                if big:
                    lx, anchor = (x - 16, "end") if x > 60 else (x + 16, "start")
                    out.append(f'<text x="{lx:.1f}" y="{y + 4:.1f}" text-anchor="{anchor}" font-size="10" font-weight="bold" fill="#dc2626" '
                               f'stroke="#fafaf7" stroke-width="3" paint-order="stroke">SPOF</text>')
            else:
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="none" stroke="#1e3a8a" stroke-width="1.5"/>')
    if ref is not None:
        halts = {}
        for nm, nr in ref.routes.items():
            for n in nr.route.halts or []:
                halts.setdefault(n, []).append(nm)
        for n in halts:
            x, y = proj(n)
            out.append(f'<path d="M{x:.1f},{y - 8:.1f} L{x + 7:.1f},{y + 5:.1f} L{x - 7:.1f},{y + 5:.1f} Z" fill="#fde68a" stroke="#92400e" stroke-width="1.2"/>')
            out.append(f'<text x="{x:.1f}" y="{y + 3.5:.1f}" text-anchor="middle" font-size="8" font-weight="bold" fill="#92400e">H</text>')
        for p in ref.posts:
            x, y = proj(p.node)
            fill = "#111827" if p.escort else "#ffffff"
            out.append(f'<rect x="{x - 5:.1f}" y="{y - 5:.1f}" width="10" height="10" fill="{fill}" stroke="#111827" stroke-width="1.5"/>')
            if labels:
                out.append(f'<text x="{x + 7:.1f}" y="{y - 7:.1f}" font-size="9" fill="#111827">{_esc(p.id)}</text>')
        for gid, a in ref.assignment.items():
            r = ref.routes[a["MSR"]].route
            col = colors.get(gid, "#111827")
            start = "SP" if origins is None or origins.get(gid, r.nodes[0]) == r.nodes[0] else "now"
            for role, n, dy in ((start, r.nodes[0], 18), ("RP", r.nodes[-1], 18)):
                x, y = proj(n)
                tw = 24 if role != "now" else 30
                out.append(f'<rect x="{x - tw / 2:.1f}" y="{y + dy - 10:.1f}" width="{tw}" height="13" rx="3" fill="{col}"/>')
                out.append(f'<text x="{x:.1f}" y="{y + dy:.1f}" text-anchor="middle" font-size="9" font-weight="bold" fill="#fff">{role}</text>')
    # bridge and tunnel labels
    for eid, el in H.elements.items():
        if el.kind not in ("bridge", "tunnel"):
            continue
        x, y = proj.xy(*el.xy)
        txt = el.name
        if el.kind == "bridge":
            txt += f" MLC {el.attrs['mlc']}" if el.attrs.get("mlc") is not None else " MLC ?"
        elif el.attrs.get("max_height_m") is not None:
            txt += f" {el.attrs['max_height_m']:g} m"
        out.append(f'<text x="{x + 10:.1f}" y="{y - 9:.1f}" font-size="10" fill="#1e40af" stroke="#fafaf7" stroke-width="3" paint-order="stroke">{_esc(txt)}</text>')
    # route name tags
    if ref is not None and labels:
        # tags avoid control-post and place labels (centres), then each other
        placed: list[tuple[float, float]] = []
        for p in ref.posts:
            x, y = proj(p.node)
            placed.append((x + 7 + 2.8 * len(p.id), y - 10))
        for n, d in G.nodes(data=True):
            if d.get("name"):
                x, y = proj(n)
                placed.append((x + 6 + 3.4 * len(d["name"]), y - 10))
        for a in ref.assignment.values():  # SP / now / RP badges
            r = ref.routes[a["MSR"]].route
            placed += [(x, y + 14) for x, y in (proj(r.nodes[0]), proj(r.nodes[-1]))]
        for i, (nm, nr) in enumerate(sorted(ref.routes.items())):
            pts = [proj(n) for n in nr.route.nodes]
            if len(pts) < 2:
                continue
            col = colors.get(nr.groups[0] if nr.groups else "", "#374151")
            for frac in (0.5, 0.35, 0.65, 0.25, 0.75, 0.42, 0.58, 0.15, 0.85):
                x, y = pts[min(len(pts) - 1, int(frac * (len(pts) - 1)))]
                if all(abs(x - a) > 60 or abs(y - b) > 18 for a, b in placed):
                    break
            placed.append((x, y))
            tw = 7 * len(nm) + 10
            out.append(f'<rect x="{x - tw / 2:.1f}" y="{y - 9:.1f}" width="{tw:.0f}" height="16" rx="8" fill="#ffffff" stroke="{col}" stroke-width="1.5"/>')
            out.append(f'<text x="{x:.1f}" y="{y + 3:.1f}" text-anchor="middle" font-size="10" font-weight="bold" fill="{col}">{_esc(nm)}</text>')
    # incidents
    for inc in incidents or []:
        if inc.get("element"):
            x, y = proj.xy(*H.elements[inc["element"]].xy)
        elif inc.get("node") is not None:
            x, y = proj(inc["node"])
        elif inc.get("xy") is not None:
            x, y = proj.xy(*inc["xy"])
        else:
            continue
        if inc.get("kind") == "threat" and inc.get("radius_m"):
            r = inc["radius_m"] * proj.scale  # type: ignore[attr-defined]
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="#ef4444" fill-opacity="0.12" stroke="#dc2626" stroke-dasharray="4,3"/>')
        else:
            out.append(f'<text x="{x:.1f}" y="{y + 7:.1f}" text-anchor="middle" font-size="22" font-weight="bold" fill="#dc2626">✕</text>')
        if inc.get("label"):
            out.append(f'<text x="{x + 14:.1f}" y="{y + 22:.1f}" font-size="11" font-weight="bold" fill="#b91c1c" stroke="#fafaf7" stroke-width="3" paint-order="stroke">{_esc(inc["label"])}</text>')
    for u, v, d in edges:
        if d.get("closed"):
            (x1, y1), (x2, y2) = proj(u), proj(v)
            out.append(f'<text x="{(x1 + x2) / 2:.1f}" y="{(y1 + y2) / 2 + 6:.1f}" text-anchor="middle" font-size="18" font-weight="bold" fill="#dc2626">✕</text>')
    # places
    for n, d in G.nodes(data=True):
        if d.get("name"):
            x, y = proj(n)
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#111827"/>')
            out.append(f'<text x="{x + 6:.1f}" y="{y - 6:.1f}" font-weight="bold" font-size="12" stroke="#fafaf7" stroke-width="3" paint-order="stroke">{_esc(d["name"])}</text>')
    if legend:
        ly = height - bottom + 22
        x = 14
        for g, col in sorted(colors.items()):
            out.append(f'<line x1="{x}" y1="{ly}" x2="{x + 24}" y2="{ly}" stroke="{col}" stroke-width="5"/>')
            out.append(f'<line x1="{x + 30}" y1="{ly}" x2="{x + 54}" y2="{ly}" stroke="{col}" stroke-width="3" stroke-dasharray="7,4"/>')
            out.append(f'<text x="{x + 60}" y="{ly + 4}">{_esc(g)} MSR / ASR</text>')
            x += 150
        for i, line in enumerate(key):
            out.append(f'<text x="14" y="{ly + 24 + 15 * i}" fill="#4b5563">{_esc(line)}</text>')
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Road movement graph (time-distance)
# ---------------------------------------------------------------------------


def render_movement(H: CorridorGraph, sched: Schedule, along, ref=None, title: str = "", width: int = 900, height: int = 420,
                    colors: dict[str, str] | None = None, t0: float | None = None, t1: float | None = None) -> str:
    """Time-distance graph of every march unit on the legs it shares with the route ``along``:
    one band per unit between its head and its tail, halts and waits as flat steps."""
    from .refine import chainage

    colors = colors or group_colors({c.group for c in sched.credits.values()})
    pos = chainage(H, along)
    total_km = max(pos.values()) / 1000.0 if pos else 1.0
    bands = []
    for c in sched.credits.values():
        pts = movement_lines(H, c, along)
        if pts:
            bands.append((c, pts))
    ts = [t for _, pts in bands for t, _, p in pts] + [t + p for _, pts in bands for t, _, p in pts]
    if not ts:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} 60" {FONT}><text x="10" y="30">no unit on this route</text></svg>'
    lo = t0 if t0 is not None else math.floor(min(ts) / 10.0) * 10.0
    hi = t1 if t1 is not None else math.ceil(max(ts) / 10.0) * 10.0
    left, right, top, bottom = 70, 20, 50 if title else 16, 46
    pw, ph = width - left - right, height - top - bottom

    def X(t: float) -> float:
        return left + (t - lo) / max(hi - lo, 1e-9) * pw

    def Y(km: float) -> float:
        return top + ph - km / max(total_km, 1e-9) * ph

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" {FONT} font-size="11">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>']
    if title:
        out.append(f'<text x="14" y="24" font-size="15" font-weight="bold" fill="#111827">{_esc(title)}</text>')
    step = 10 if hi - lo <= 120 else 30
    t = math.ceil(lo / step) * step
    while t <= hi + 1e-9:
        out.append(f'<line x1="{X(t):.1f}" y1="{top}" x2="{X(t):.1f}" y2="{top + ph}" stroke="#eef0f3"/>')
        out.append(f'<text x="{X(t):.1f}" y="{top + ph + 16}" text-anchor="middle" fill="#4b5563">{hhmm(t)}</text>')
        t += step
    kstep = 5 if total_km > 12 else 2
    k = 0.0
    while k <= total_km + 1e-9:
        out.append(f'<line x1="{left}" y1="{Y(k):.1f}" x2="{left + pw}" y2="{Y(k):.1f}" stroke="#eef0f3"/>')
        out.append(f'<text x="{left - 8}" y="{Y(k) + 4:.1f}" text-anchor="end" fill="#4b5563">{k:g} km</text>')
        k += kstep
    out.append(f'<line x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}" stroke="#9ca3af"/>')
    out.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + ph}" stroke="#9ca3af"/>')
    out.append(f'<text x="{left + pw / 2:.1f}" y="{height - 8}" text-anchor="middle" fill="#374151">time (head and tail of each column)</text>')
    # control posts along the route
    if ref is not None:
        for p, dist in ref.posts_along(H, along):
            y = Y(dist / 1000.0)
            out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + pw}" y2="{y:.1f}" stroke="#9ca3af" stroke-dasharray="2,4"/>')
            out.append(f'<text x="{left + pw - 4}" y="{y - 3:.1f}" text-anchor="end" font-size="9" fill="#6b7280">{_esc(p.id)} {_esc(H.label(p.node))}</text>')
    for c, pts in bands:
        col = colors.get(c.group, "#374151")
        head = [(X(t), Y(s / 1000.0)) for t, s, _ in pts]
        tail = [(X(t + p), Y(s / 1000.0)) for t, s, p in pts]
        poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in head + tail[::-1])
        out.append(f'<polygon points="{poly}" fill="{col}" fill-opacity="0.28" stroke="none"/>')
        out.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in head)}" fill="none" stroke="{col}" stroke-width="2"/>')
        x, y = head[-1]
        out.append(f'<text x="{x + 4:.1f}" y="{y - 4:.1f}" font-weight="bold" fill="{col}">{_esc(c.unit)}</text>')
        if c.deadline is not None and lo <= c.deadline <= hi:
            out.append(f'<line x1="{X(c.deadline):.1f}" y1="{top}" x2="{X(c.deadline):.1f}" y2="{top + ph}" stroke="{col}" stroke-dasharray="5,4"/>')
            out.append(f'<text x="{X(c.deadline) + 3:.1f}" y="{top + 10}" font-size="9" fill="{col}">{_esc(c.unit)} deadline</text>')
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# COA chart
# ---------------------------------------------------------------------------


def render_coa_chart(facts, title: str = "", width: int = 900) -> str:
    """One row per objective (in the default COA's order), one bar per COA, scaled to the row's largest value."""
    from .design import OBJECTIVE_INFO

    if not facts:
        return ""
    order = ["lateness"] + [k for k in facts[0].order if k not in ("lateness", "stability")]
    n = len(facts)
    row_h = 10 * n + 18
    top = 46 if title else 12
    left, right = 190, 120
    height = top + row_h * len(order) + 40
    pw = width - left - right
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" {FONT} font-size="11">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>']
    if title:
        out.append(f'<text x="14" y="24" font-size="15" font-weight="bold" fill="#111827">{_esc(title)}</text>')
    for i, k in enumerate(order):
        y0 = top + i * row_h
        vals = [f.objectives.get(k, 0.0) for f in facts]
        mx = max(abs(v) for v in vals) or 1.0
        out.append(f'<text x="{left - 10}" y="{y0 + row_h / 2 + 2:.1f}" text-anchor="end" fill="#111827">{i}. {_esc(k)}</text>')
        out.append(f'<text x="{left - 10}" y="{y0 + row_h / 2 + 14:.1f}" text-anchor="end" font-size="9" fill="#6b7280">{_esc(OBJECTIVE_INFO[k][0])}, lower is better</text>')
        for j, (f, v) in enumerate(zip(facts, vals)):
            col = COA_COLORS[j % len(COA_COLORS)]
            w = max(1.0, abs(v) / mx * pw) if v else 1.0
            y = y0 + 6 + j * 10
            out.append(f'<rect x="{left}" y="{y:.1f}" width="{w:.1f}" height="8" fill="{col}" rx="2"/>')
            out.append(f'<text x="{left + w + 5:.1f}" y="{y + 7.5:.1f}" font-size="9" fill="#374151">{v:.1f}</text>')
        out.append(f'<line x1="{left}" y1="{y0 + row_h:.1f}" x2="{left + pw}" y2="{y0 + row_h:.1f}" stroke="#f1f2f4"/>')
    ly = height - 16
    x = left
    for j, f in enumerate(facts):
        col = COA_COLORS[j % len(COA_COLORS)]
        out.append(f'<rect x="{x}" y="{ly - 9}" width="12" height="10" fill="{col}" rx="2"/>')
        out.append(f'<text x="{x + 17}" y="{ly}">COA {_esc(f.label)}</text>')
        x += 40 + 8 * len(f.label) + 30
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------


def _inline(s: str) -> str:
    s = _esc(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    return s


def md_to_html(md: str) -> str:
    """Minimal Markdown: headings, pipe tables, bullet lists, fenced code and paragraphs."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre>" + _esc("\n".join(lines[i + 1:j])) + "</pre>")
            i = j + 1
            continue
        m = re.match(r"(#{1,4})\s+(.*)", ln)
        if m:
            lvl = len(m.group(1)) + 1
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if ln.strip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            body = [r for r in rows if not all(re.fullmatch(r":?-{3,}:?", c) for c in r)]
            if body:
                head, rest = body[0], body[1:]
                out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                           + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rest) + "</tbody></table>")
            continue
        if re.match(r"\s*[-*]\s+", ln):
            items = []
            while i < len(lines) and re.match(r"\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            continue
        if not ln.strip():
            i += 1
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"(#{1,4}\s|\||```|\s*[-*]\s)", lines[i]):
            para.append(lines[i])
            i += 1
        out.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(out)


def write_html(path: str | Path, title: str, panels: list[tuple[str, str]], sections: list[tuple[str, str]] | None = None) -> Path:
    """One page: SVG panels, then Markdown sections (facts table, explanations, march table, annexes, FRAGOs)."""
    figs = "\n".join(f"<section><h2>{_esc(t)}</h2>{s}</section>" for t, s in panels)
    body = "\n".join(f"<section><h2>{_esc(t)}</h2>{md_to_html(md)}</section>" for t, md in (sections or []))
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:Helvetica,Arial,sans-serif;max-width:1000px;margin:auto;padding:16px;background:#fff;color:#111827;line-height:1.45}}
h1{{font-size:24px}} h2{{font-size:18px;margin-top:28px;border-bottom:1px solid #e5e7eb;padding-bottom:4px}} h3{{font-size:15px}}
svg{{width:100%;height:auto;border:1px solid #e5e7eb;border-radius:6px}}
table{{border-collapse:collapse;font-size:12px;margin:8px 0;display:block;overflow-x:auto}}
th,td{{border:1px solid #e5e7eb;padding:4px 6px;text-align:left;vertical-align:top}} th{{background:#f9fafb}}
pre{{white-space:pre-wrap;background:#f6f7f9;padding:10px;border-radius:6px;font-size:12px}} code{{background:#f3f4f6;padding:0 3px}}
</style></head><body><h1>{_esc(title)}</h1>{figs}{body}</body></html>"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page, encoding="utf-8")
    return p


def render_folium(H: CorridorGraph, ref, path: str | Path, colors: dict[str, str] | None = None) -> Path:
    """Interactive web map of the named routes and control posts for lon/lat graphs (needs ``pip install folium``)."""
    import folium

    G = H.G
    if not is_geographic(G):
        raise ValueError("folium rendering needs a lon/lat graph")
    ys = [d["y"] for _, d in G.nodes(data=True)]
    xs = [d["x"] for _, d in G.nodes(data=True)]
    m = folium.Map(location=[sum(ys) / len(ys), sum(xs) / len(xs)], zoom_start=11, tiles="OpenStreetMap")
    colors = colors or group_colors({g for nr in ref.routes.values() for g in nr.groups})
    for nm, nr in ref.routes.items():
        col = colors.get(nr.groups[0] if nr.groups else "", "#374151")
        folium.PolyLine([(G.nodes[n]["y"], G.nodes[n]["x"]) for n in nr.route.nodes], color=col,
                        weight=6 if nr.role == "MSR" else 3, dash_array=None if nr.role == "MSR" else "8",
                        tooltip=f"{nm} ({nr.role}, {', '.join(nr.groups)}, {nr.status})").add_to(m)
    for p in ref.posts:
        x, y = node_xy(G, p.node)
        folium.CircleMarker((y, x), radius=5, color="#111827", fill=True, tooltip=f"{p.id}: {'; '.join(p.reasons)}").add_to(m)
    out = Path(path)
    m.save(str(out))
    return out


__all__ = ["PALETTE", "group_colors", "md_to_html", "render_coa_chart", "render_folium", "render_map", "render_movement", "write_html"]
