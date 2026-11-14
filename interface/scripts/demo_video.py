"""Records a demo video of the interface, for a live presentation: a tour of the interface, one simple route, one complex mission
covering every kind of constraint, and the vision part. Captions, a visible pointer, and pause points listed with their times.

    LD_LIBRARY_PATH=$(find ~/.local/chrome-libs -type d -name x86_64-linux-gnu | tr '\\n' ':') \\
        .venv/bin/python interface/scripts/demo_video.py [--url http://127.0.0.1:8008] [--out docs/presentation/demo]

Writes ``CARTE_demo.webm`` (1600 x 900) and ``demo_chapters.md`` (the time of each chapter). Needs the server running
(interface/run.sh), the language model for the chat chapters (deploy/serve_llm.sbatch; without it the answers come from
the rules) and the synthetic linked orders (scripts/make_test_orders.py) for the last chapter.
"""
from __future__ import annotations

import argparse
import io
import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

OVERLAY = """
(() => {
  if (document.getElementById('demo-cap')) return;
  const st = document.createElement('style');
  st.textContent = `
  #demo-cap { position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%); z-index: 99999; max-width: 980px;
    background: rgba(20,35,42,.92); color: #fff; font: 500 21px/1.35 system-ui, sans-serif; padding: 14px 22px;
    border-radius: 12px; box-shadow: 0 6px 24px rgba(0,0,0,.35); pointer-events: none; transition: opacity .3s; }
  #demo-cap b { color: #ffcf6e; font-weight: 700; }
  #demo-step { position: fixed; right: 18px; top: 58px; z-index: 99999; background: #c8382f; color: #fff;
    font: 700 15px system-ui, sans-serif; padding: 6px 12px; border-radius: 999px; pointer-events: none; }
  #demo-cursor { position: fixed; z-index: 100000; width: 22px; height: 22px; border-radius: 50%; left: 800px; top: 450px;
    background: rgba(245,159,0,.85); border: 3px solid #fff; box-shadow: 0 0 0 2px rgba(0,0,0,.35);
    transform: translate(-50%,-50%); pointer-events: none; transition: left .55s ease, top .55s ease, transform .15s; }
  #demo-cursor.down { transform: translate(-50%,-50%) scale(.7); }
  .demo-hi { outline: 3px solid #f59f00 !important; outline-offset: 2px; }
  #demo-card { position: fixed; inset: 0; z-index: 100001; background: #14232a; color: #f2f5f4; display: flex;
    flex-direction: column; align-items: center; justify-content: center; font-family: system-ui, sans-serif; text-align: center; }
  #demo-card h1 { font-size: 54px; margin: 0 0 18px; } #demo-card p { font-size: 24px; color: #a9babe; max-width: 1000px; line-height: 1.4; }`;
  document.head.appendChild(st);
  for (const id of ['demo-cap', 'demo-step', 'demo-cursor']) { const d = document.createElement('div'); d.id = id; document.body.appendChild(d); }
  document.getElementById('demo-cap').style.opacity = 0;
  document.getElementById('demo-step').style.display = 'none';
})();
"""


class Demo:
    def __init__(self, page, total: int):
        self.p, self.total, self.t0, self.chapters, self.n = page, total, time.perf_counter(), [], 0

    def overlay(self):
        self.p.evaluate(OVERLAY)

    def now(self) -> str:
        s = int(time.perf_counter() - self.t0)
        return f"{s // 60}:{s % 60:02d}"

    def chapter(self, title: str):
        self.n += 1
        self.chapters.append((self.now(), self.n, title))
        self.overlay()
        self.p.evaluate("([t]) => { const s = document.getElementById('demo-step'); s.textContent = t; s.style.display = 'block'; }",
                        [f"{self.n}/{self.total} · {title}"])

    def numbers(self):
        """Route length and km of network below the threshold, as the results panel shows them."""
        import re

        t = self.p.locator(".side").inner_text()
        km = re.search(r"Trajet\s*:\s*([\d.,]+)\s*km", t)
        below = re.search(r"([\d.,]+)\s*km du réseau sous le seuil", t)
        return (km.group(1) if km else "?"), (below.group(1) if below else "?")

    def legend(self, show: bool):
        """The layers panel covers the top right of the map: hidden while a result is on screen."""
        self.p.evaluate("([v]) => { const l = document.querySelector('.layers'); if (l) l.style.visibility = v ? 'visible' : 'hidden'; }", [show])
        self.p.wait_for_timeout(200)

    def cap_side(self, side: str):
        """Caption at the bottom centre (default) or over the side panel, leaving the whole map visible."""
        self.overlay()
        css = ("left: 50%; transform: translateX(-50%); max-width: 980px; bottom: 26px;" if side == "center"
               else "left: 14px; transform: none; max-width: 430px; bottom: 80px;")
        self.p.evaluate("([c]) => { const e = document.getElementById('demo-cap'); e.style.cssText += c; }", [css])

    def pause(self, what: str):
        """A good moment for the presenter to pause the video and explain (listed in demo_chapters.md)."""
        self.chapters.append((self.now(), -1, what))

    def part(self, title: str):
        self.card(title.split(" · ")[0], title.split(" · ")[1], 2600)

    def cap(self, html: str, hold: int = 2600):
        self.overlay()
        self.p.evaluate("([h]) => { const c = document.getElementById('demo-cap'); c.innerHTML = h; c.style.opacity = 1; }", [html])
        self.p.wait_for_timeout(hold)

    def card(self, title: str, text: str, hold: int = 3500):
        self.overlay()
        self.p.evaluate("([t, x]) => { const d = document.createElement('div'); d.id = 'demo-card'; d.innerHTML = `<h1>${t}</h1><p>${x}</p>`; document.body.appendChild(d); }",
                        [title, text])
        self.p.wait_for_timeout(hold)
        self.p.evaluate("() => document.getElementById('demo-card')?.remove()")

    def move(self, x: float, y: float, wait: int = 650):
        self.p.evaluate("([x, y]) => { const c = document.getElementById('demo-cursor'); c.style.left = x + 'px'; c.style.top = y + 'px'; }", [x, y])
        self.p.wait_for_timeout(wait)

    def point(self, loc, wait: int = 650):
        loc.scroll_into_view_if_needed()
        b = loc.bounding_box()
        self.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, wait)
        return b

    def click(self, sel_or_loc, wait_after: int = 700):
        loc = self.p.locator(sel_or_loc).first if isinstance(sel_or_loc, str) else sel_or_loc
        self.point(loc)
        loc.evaluate("e => e.classList.add('demo-hi')")
        self.p.evaluate("() => document.getElementById('demo-cursor').classList.add('down')")
        self.p.wait_for_timeout(180)
        try:
            loc.evaluate("e => e.classList.remove('demo-hi')")
        except Exception:  # noqa: BLE001
            pass
        loc.click()
        self.p.evaluate("() => document.getElementById('demo-cursor').classList.remove('down')")
        self.p.wait_for_timeout(wait_after)

    def map_click(self, x: float, y: float, dbl: bool = False):
        self.move(x, y, 450)
        self.p.mouse.dblclick(x, y) if dbl else self.p.mouse.click(x, y)
        self.p.wait_for_timeout(250)

    def type(self, sel: str, text: str):
        loc = self.p.locator(sel).first
        self.point(loc)
        loc.click()
        loc.fill("")
        loc.press_sequentially(text, delay=22)
        self.p.wait_for_timeout(400)

    def route_pixel(self):
        """A point of the computed route on screen (its red line), near the map centre, for drawing on it."""
        import numpy as np
        from PIL import Image

        box = self.p.locator(".map-wrap").bounding_box()
        img = np.asarray(Image.open(io.BytesIO(self.p.screenshot(clip=box))).convert("RGB")).astype(int)
        m = (abs(img[..., 0] - 209) < 30) & (abs(img[..., 1] - 73) < 35) & (abs(img[..., 2] - 91) < 35)
        ys, xs = np.nonzero(m)
        if not len(xs):
            return box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5
        cx, cy = img.shape[1] / 2, img.shape[0] / 2
        i = int(np.argmin((xs - cx) ** 2 + (ys - cy) ** 2))
        return float(box["x"] + xs[i]), float(box["y"] + ys[i])

    def compute(self, timeout: int = 180_000):
        self.click("button.big:has-text('Calculer'), button.big:has-text('Planifier')", wait_after=300)
        self.p.wait_for_function("!document.querySelector('.busy')", timeout=timeout)
        self.p.wait_for_timeout(1400)


def prewarm(url: str, ex: dict):
    """Builds the distinct-itineraries cache for the vehicle of chapter 11, so the video does not wait a minute."""
    P = ex["points"]
    body = {"sheet": "hackathon", "model": "nodice", "use_osm": False, "start": P[ex["start"]], "end": P[ex["end"]],
            "zone_radius_km": 8, "k": 3, "min_sep_km": 5, "no_intersect": True, "vehicle": "tank_transporter", "turn_aware": True,
            "area": [P[k] for k in ex["area"]] if len(ex["area"]) >= 3 else None, "avoid": []}
    r = urllib.request.Request(f"{url}/api/itineraries", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(r, timeout=400).read()
    except Exception as e:  # noqa: BLE001
        print("prewarm:", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8008")
    ap.add_argument("--out", default="docs/presentation/demo")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    exs = {e["number"]: e for e in json.load(urllib.request.urlopen(f"{a.url}/api/pdf?sheet=hackathon"))["exercises"]}
    t = time.perf_counter()
    prewarm(a.url, exs[1])
    print(f"prewarm {time.perf_counter() - t:.0f} s")
    failed = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--use-gl=swiftshader", "--enable-webgl", "--ignore-gpu-blocklist"])
        ctx = b.new_context(viewport={"width": 1600, "height": 900}, record_video_dir=str(out / "raw"),
                            record_video_size={"width": 1600, "height": 900})
        page = ctx.new_page()
        page.goto(a.url)
        page.wait_for_selector(".status.ok", timeout=180_000)
        page.wait_for_timeout(1500)
        D = Demo(page, 10)
        ex_sel = ".side select:has(option:has-text('Charger un exercice'))"

        def scene(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                failed.append(f"{fn.__name__}: {e}")
                print("FAILED", fn.__name__, e)
                page.keyboard.press("Escape")

        D.card("CARTE", "Constraint-Aware Routing with Trust Estimation — convoy routes from a scanned 1:100,000 map — every constraint, the doubt priced in, every choice explained.", 4500)

        # ---------------------------------------------------------------------------------------- part 1: the interface
        def interface():
            D.part("Part 1 · The interface")
            D.chapter("Interface tour")
            D.cap("Four tabs, left to right: <b>Configurer</b> → <b>Scénario</b> → <b>Résultats</b> → <b>Ordres</b>.", 900)
            for t in ("1 · Configurer", "2 · Scénario", "3 · Résultats", "4 · Ordres"):
                D.point(page.locator(".tabs button", has_text=t).first, 650)
            D.cap("Toolbar: departure <b>D</b>, arrival <b>A</b>, waypoints, rectangle, deployment area, <b>zone to avoid</b>, fit, clear.", 600)
            for i in range(page.locator(".toolbar .tool").count()):
                D.point(page.locator(".toolbar .tool").nth(i), 380)
            D.cap("Layers: the scan, the U-Net road probability, its uncertainty, the extracted network, BD TOPO and OSM for checking.", 600)
            D.point(page.locator(".layers").first, 2600)
            D.cap("<b>Configurer</b>: the data the route may use — the scan alone, with OpenStreetMap, or checked against BD TOPO.", 600)
            for lab in ("Image TIFF seule", "TIFF + OSM", "TIFF sans OSM"):
                D.point(page.locator("button.choice", has_text=lab).first, 500)
            D.click(page.locator("button.choice", has_text="TIFF sans OSM").first)
            D.cap("How the roads are read: four trained U-Net variants; and the <b>uncertainty cost</b> (λ = 0.5).", 600)
            D.point(page.locator("button.choice", has_text="Couverture maximale").first, 700)
            D.point(page.locator("label.check", has_text="Coût d’incertitude").first, 1800)
            D.pause("the interface: tabs, toolbar, layers, data and model")
        scene(interface)

        # ---------------------------------------------------------------------------------------- part 2: simple
        def simple():
            D.part("Part 2 · A simple route")
            D.chapter("Simple: from A to B")
            D.click("text=2 · Scénario")
            D.cap("<b>Exercise 1</b> of the PDF: from PD1 to PA1, shortest, inside the deployment area.", 600)
            D.point(page.locator(ex_sel))
            page.locator(ex_sel).select_option(value="1")
            page.wait_for_timeout(1500)
            D.compute()
            D.cap("The route on the network read from the scan; length, time, and the check against BD TOPO (dashed).", 3500)
            D.pause("result: length, time, BD TOPO check")
            D.point(page.locator("table.segs").first, 400)
            D.cap("<b>Pourquoi ce tracé ?</b> each segment, its confidence, and what the best alternative would cost.", 3200)
            D.click("button:has-text('Pourquoi cet itinéraire ?')")
            page.wait_for_selector(".route-chat .msg.assistant .meta", timeout=60_000)
            page.locator(".route-chat .msg.assistant").last.scroll_into_view_if_needed()
            D.cap("Answered from the solver's own trace: instant and exact.", 3200)
            D.pause("the explanation and the chat")
        scene(simple)

        # ---------------------------------------------------------------------------------------- part 3: complex
        def complex_constraints():
            D.part("Part 3 · A complex mission")
            D.chapter("Complex: many constraints")
            D.click("text=2 · Scénario")
            D.cap("<b>Exercise 7</b>: deployment area and <b>4 waypoints in any order</b> (24 orders tried).", 600)
            page.locator(ex_sel).select_option(value="7")
            page.wait_for_timeout(1500)
            D.compute()
            D.cap("Now a <b>zone to avoid</b>, drawn on the route with the toolbar.", 600)
            D.click("button.tool[title='Voir toute la carte']", 1500)
            x, y = D.route_pixel()
            D.click("button.tool.t-avoid", 400)
            r = 34
            for dx, dy in ((-r, -r), (r, -r), (r, r)):
                D.map_click(x + dx, y + dy)
            D.map_click(x - r, y + r, dbl=True)
            page.wait_for_timeout(500)
            D.click("text=2 · Scénario")
            D.cap("And a <b>written order</b>: fastest, convoy limited to 60 km/h.", 400)
            D.type(".text-order-box textarea", "Le plus rapide, vitesse maximale 60 km/h")
            D.click(".text-order-box button:has-text('Lire l’ordre')", 300)
            page.wait_for_selector(".text-order-box .text-order button:has-text('Appliquer et calculer')", timeout=60_000)
            D.cap("The order is read and shown before anything changes.", 2200)
            D.click(".text-order-box .text-order button:has-text('Appliquer et calculer')", 300)
            page.wait_for_function("!document.querySelector('.busy')", timeout=120_000)
            page.wait_for_timeout(1200)
            D.cap("One route through all of it: area, free-order waypoints, zone avoided, fastest at 60 km/h, the doubt priced.", 4000)
            D.pause("all constraints in one route")
        scene(complex_constraints)

        def complex_orders():
            D.chapter("Orders and NATO doctrine")
            D.click(".route-chat button:has-text('Ordres pris en compte')", 400)
            fr = page.locator(".order-pick label", has_text="FRAGO 2")
            if fr.count():
                D.click(fr.locator("input").first, 400)
                page.wait_for_selector(".order-pick :text('ordres liés ajoutés')", timeout=30_000)
                D.cap("Picking <b>FRAGO 2</b> brings in the orders it amends (synthetic test orders): the values in force are computed by code.", 3000)
            n = page.locator(".route-chat .msg.assistant .meta").count()
            D.type(".route-chat .chat-input", "Quelle vitesse maximale les ordres imposent-ils au convoi ?")
            page.keyboard.press("Enter")
            page.wait_for_function(f"document.querySelectorAll('.route-chat .msg.assistant .meta').length > {n}", timeout=120_000)
            page.wait_for_timeout(800)
            page.locator(".route-chat .msg.assistant").last.scroll_into_view_if_needed()
            D.cap("The local model answers with the value in force and the <b>NATO rules</b> that apply, checked before display.", 4500)
            D.pause("orders, doctrine, linked orders")
        scene(complex_orders)

        def complex_leg():
            D.chapter("Next leg, no reuse")
            D.cap("<b>Next leg from the arrival</b>: this route goes into the mission log.", 400)
            D.click("button:has-text('Étape suivante depuis l’arrivée')", 1000)
            D.type(".text-order-box textarea", "Puis va à PA1 sans repasser par le trajet précédent")
            D.click(".text-order-box button:has-text('Lire l’ordre')", 300)
            page.wait_for_selector(".text-order-box .text-order button:has-text('Appliquer et calculer')", timeout=60_000)
            D.click(".text-order-box .text-order button:has-text('Appliquer et calculer')", 300)
            page.wait_for_selector(".history-note", timeout=120_000)
            page.wait_for_timeout(800)
            D.point(page.locator(".history-note"), 300)
            D.cap("The earlier path (grey dashed) is <b>forbidden</b>; reopened only around the shared point if needed, and said.", 4000)
            D.pause("mission legs")
        scene(complex_leg)

        def complex_convoys():
            D.chapter("Several convoys")
            D.click("text=2 · Scénario")
            for _ in range(3):
                if page.locator(".card.mission").count():
                    page.click("button:has-text('Vider le journal')")
            D.click("button:has-text('Plusieurs convois')")
            D.click("button:has-text('Exercices du PDF → convois')", 600)
            n = page.locator(".pts li:has(.it-sw)").count()
            for _ in range(max(0, n - 8)):
                page.locator(".pts li:has(.it-sw) button.danger").last.click()
            D.cap("<b>8 convoys</b> at once, by priority; roads already taken by an earlier convoy are penalised.", 600)
            D.compute(timeout=240_000)
            D.cap("Each convoy in its colour; the km shared between convoys are reported.", 4000)
            D.pause("convoys")
        scene(complex_convoys)

        def complex_vehicle():
            D.chapter("Vehicle and separation")
            D.click("text=2 · Scénario")
            D.click("button:has-text('Itinéraires distincts')")
            page.locator(ex_sel).select_option(value="1")
            page.wait_for_timeout(800)
            veh = page.locator("label.field", has_text="Véhicule du convoi").locator("select")
            D.point(veh)
            veh.select_option(value="tank_transporter")
            D.cap("<b>3 itineraries at least 5 km apart</b>, never crossing, every road checked for a <b>tank transporter</b>.", 600)
            D.compute(timeout=300_000)
            D.cap("Every constraint is checked and listed; difficult turns and areas are shown.", 4000)
            D.pause("vehicle constraints")
        scene(complex_vehicle)

        # ---------------------------------------------------------------------------------------- part 4: vision
        def vision():
            D.part("Part 4 · The vision part")
            D.chapter("Reading the scan")
            D.click("button.tool.danger", 600)  # clear the map
            D.click("text=2 · Scénario")
            D.click("button:has-text('Un itinéraire')")
            D.cap("A route <b>through the towns</b>: Châlons-en-Champagne → Sainte-Menehould → Verdun → Bar-le-Duc, written with their coordinates.", 400)
            D.type(".text-order-box textarea", "De 48.957, 4.364 à 48.773, 5.160 par 49.090, 4.897 et 49.160, 5.384 dans cet ordre, le plus court, 80 km/h")
            D.click(".text-order-box button:has-text('Lire l’ordre')", 300)
            page.wait_for_selector(".text-order-box .text-order button:text-is('Appliquer')", timeout=60_000)
            page.wait_for_timeout(1200)
            D.click(page.locator(".text-order-box .text-order button:text-is('Appliquer')").first, 1200)
            D.cap_side("left")
            D.click(page.locator(".layers label", has_text="Probabilité de route (U-Net)").locator("input[type=checkbox]").first, 600)
            D.legend(False)
            D.cap("The U-Net's <b>road probability</b> over the scan: dense streets in towns, where the model is least sure.", 4000)
            D.legend(True)
            page.locator(".layers label", has_text="Probabilité de route (U-Net)").locator("input[type=checkbox]").first.uncheck()
            D.pause("road probability")

            D.chapter("Uncertainty threshold → route")
            D.click("text=1 · Configurer")
            unc = page.locator("label.check", has_text="Coût d’incertitude").locator("input").first
            if unc.is_checked():
                D.click(unc, 300)
            av = page.locator("label.check", has_text="Éviter les routes incertaines").locator("input").first
            if not av.is_checked():
                D.click(av, 300)
            tau = page.locator("label.slider", has_text="Seuil d’incertitude").locator("input").first
            D.point(tau)
            tau.fill("0.3")
            D.cap("<b>Avoid uncertain roads</b>: a road counts as uncertain below a probability threshold τ, and costs ×4. Start with τ = 30 %.", 1500)
            D.compute()
            D.click(page.locator(".layers label", has_text="Incertitude").locator("input[type=checkbox]").first, 600)
            D.legend(False)
            km1, below1 = D.numbers()
            D.cap(f"τ = 30 %: <b>{below1} km</b> of the network count as uncertain (red); the route, <b>{km1} km</b>, barely changes.", 4000)
            D.pause("threshold 30 %")
            D.legend(True)
            lay = page.locator(".layers input[type=range]").first
            D.point(lay, 500)
            D.cap("Raise the threshold: more and more roads count as uncertain.", 400)
            for v in ("0.4", "0.5", "0.6", "0.7"):
                lay.fill(v)
                page.wait_for_timeout(1300)
            D.cap("τ = 70 %: many more roads are now uncertain. Recompute…", 1200)
            D.compute()
            D.legend(False)
            km2, below2 = D.numbers()
            D.cap(f"τ = 70 % ({below2} km uncertain): the route now <b>avoids the roads the model is unsure of</b>, around and between the towns: "
                  f"{km1} → {km2} km. The operator chooses how much doubt to accept.", 5000)
            D.pause("threshold 70 %: the route moves")
            D.legend(True)

            D.chapter("How the network is read")
            D.click("button.tool.t-rect", 300)
            box = page.locator(".map-wrap").bounding_box()
            x0, y0 = box["x"] + box["width"] * 0.45, box["y"] + box["height"] * 0.40
            D.move(x0, y0, 400)
            page.mouse.move(x0, y0)
            page.mouse.down()
            page.mouse.move(x0 + 170, y0 + 115, steps=12)
            D.move(x0 + 170, y0 + 115, 200)
            page.mouse.up()
            page.wait_for_selector(".rect-menu")
            D.click("text=Voir l’extraction du réseau", 300)
            page.wait_for_selector(".player", timeout=120_000)
            D.cap("Step by step: scan → road probability → mask → centre lines → <b>road network</b>, each section with its confidence.", 10000)
            D.pause("network extraction")
        scene(vision)

        D.card("CARTE", "One cost function for every constraint · doubt priced in · every decision explained · runs on a CPU.", 4000)
        D.chapters.append((D.now(), 0, "end"))
        video = page.video
        ctx.close()
        b.close()
        src = Path(video.path())
    dst = out / "CARTE_demo.webm"
    src.replace(dst)
    L = ["# CARTE demo video — chapters", "", f"File: `{dst.name}` (1600 × 900, WebM). Recorded {time.strftime('%Y-%m-%d %H:%M')} from {a.url}.", "",
         "Parts: 1 interface · 2 a simple route · 3 a complex mission · 4 the vision part. ⏸ = a good moment to pause and explain.", "",
         "| time | chapter |", "|---|---|"] + [f"| {t} | {n}. {title} |" if n > 0 else (f"| {t} | ⏸ {title} |" if n < 0 else f"| {t} | end |")
                                            for t, n, title in D.chapters]
    if failed:
        L += ["", "Chapters that failed during recording: " + "; ".join(failed)]
    (out / "demo_chapters.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
