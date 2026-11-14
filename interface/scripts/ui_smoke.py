"""End-to-end check of the interface in headless Chromium (the back end must run: interface/run.sh).

Loads exercise 7 of the PDF, computes the route, shows the uncertainty, draws a rectangle and plays the extraction;
takes a screenshot at each step and fails on any JavaScript error.

    .venv/bin/python interface/scripts/ui_smoke.py [--url http://127.0.0.1:8008] [--out DIR]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8008")
    ap.add_argument("--out", default="outputs/interface_cache/ui_smoke")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    timings: dict[str, float] = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--use-gl=swiftshader", "--enable-webgl", "--ignore-gpu-blocklist"])
        page = b.new_page(viewport={"width": 1600, "height": 950})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
        t = time.perf_counter()
        page.goto(a.url)
        page.wait_for_selector(".status.ok", timeout=180_000)
        page.wait_for_timeout(2500)
        timings["page ready"] = time.perf_counter() - t
        page.screenshot(path=str(out / "1_config.png"))

        page.click("text=2 · Scénario")
        page.wait_for_selector(".side select:has(option:has-text('Charger un exercice'))")
        sel = page.locator(".side select:has(option:has-text('Charger un exercice'))")
        sel.select_option(value="7")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "2_exercise7.png"))
        t = time.perf_counter()
        page.click("button.big:has-text('Calculer')")
        page.wait_for_selector(".kpi", timeout=120_000)
        timings["route (exercise 7)"] = time.perf_counter() - t
        page.wait_for_function("document.querySelectorAll('.card.ref .small.muted').length === 0", timeout=120_000)
        timings["route + comparisons"] = time.perf_counter() - t
        page.wait_for_timeout(1800)
        page.screenshot(path=str(out / "3_route.png"))

        for label in ("Incertitude", "Réseau extrait"):
            page.locator(".layers label", has_text=label).locator("input").check()
        page.wait_for_timeout(2500)
        page.screenshot(path=str(out / "4_uncertainty.png"))

        # why this route: segment table and the route chat (trace answer, no model)
        page.wait_for_selector("table.segs tbody tr")
        page.locator("table.segs tbody tr").nth(1).click()
        t = time.perf_counter()
        page.click("button:has-text('Pourquoi cet itinéraire ?')")
        page.wait_for_selector(".route-chat .msg.assistant .meta", timeout=60_000)
        timings["route chat (trace answer)"] = time.perf_counter() - t
        page.locator(".route-chat .chat-input").fill("Explique le segment 2")
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelectorAll('.route-chat .msg.assistant .meta').length === 2", timeout=60_000)
        page.wait_for_timeout(400)
        page.screenshot(path=str(out / "4a_why_and_chat.png"), full_page=True)

        # an order written in the chat: previewed (not answered), then applied and computed
        t = time.perf_counter()
        page.locator(".route-chat .chat-input").fill("De PD4 à PA4 par WP1 et WP3 dans n'importe quel ordre, le plus rapide, 60 km/h, "
                                                     "éviter 2 km autour de WP2")
        page.keyboard.press("Enter")
        page.wait_for_selector(".route-chat .text-order button:has-text('Appliquer et calculer')", timeout=60_000)
        timings["text order read (chat)"] = time.perf_counter() - t
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "4d_chat_order.png"), full_page=True)
        t = time.perf_counter()
        page.click(".route-chat .text-order button:has-text('Appliquer et calculer')")
        page.wait_for_timeout(500)
        page.wait_for_selector("table.segs tbody tr", timeout=120_000)
        timings["text order applied + route"] = time.perf_counter() - t
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "4e_order_route.png"))
        # the same from the Scenario tab
        page.click("text=2 · Scénario")
        page.locator(".text-order-box textarea").fill("Exercice 7 mais le plus rapide à 60 km/h")
        page.click(".text-order-box button:has-text('Lire l’ordre')")
        page.wait_for_selector(".text-order-box .text-order button:has-text('Appliquer')", timeout=60_000)
        page.screenshot(path=str(out / "4f_text_box.png"))
        page.click(".text-order-box .text-order button:text-is('Appliquer')")
        page.wait_for_selector(".text-order-box .ok-text", timeout=10_000)
        # what is on the map: a click in hand mode marks P (« ici »); the waypoints are « l'étape n »
        page.wait_for_timeout(800)
        box = page.locator(".map-wrap canvas").first.bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.42, box["y"] + box["height"] * 0.55)
        page.wait_for_selector(".pin-p", timeout=5_000)
        page.keyboard.press("Escape")
        page.locator(".text-order-box textarea").fill("Va de PD4 à ici par l'étape 2, évite 2 km autour de l'étape 1")
        page.click(".text-order-box button:has-text('Lire l’ordre')")
        page.wait_for_selector(".text-order-box .text-order:has-text('point P') button:has-text('Appliquer et calculer')", timeout=60_000)
        page.screenshot(path=str(out / "4g_map_refs.png"))
        t = time.perf_counter()
        page.click(".text-order-box .text-order button:has-text('Appliquer et calculer')")
        page.wait_for_timeout(500)
        page.wait_for_selector("table.segs tbody tr", timeout=120_000)
        timings["map-reference order + route"] = time.perf_counter() - t
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "4h_map_refs_route.png"))

        # mission log: the next leg starts at this arrival and may not reuse this path
        page.click("button:has-text('Étape suivante depuis l’arrivée')")
        page.wait_for_selector(".card.mission", timeout=10_000)
        page.locator(".text-order-box textarea").fill("Puis va à WP1 sans repasser par le trajet précédent")
        page.click(".text-order-box button:has-text('Lire l’ordre')")
        page.wait_for_selector(".text-order-box .text-order:has-text('interdits') button:has-text('Appliquer et calculer')", timeout=60_000)
        t = time.perf_counter()
        page.click(".text-order-box .text-order button:has-text('Appliquer et calculer')")
        page.wait_for_selector(".history-note", timeout=120_000)
        timings["next leg, previous path forbidden"] = time.perf_counter() - t
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "4k_mission_next_leg.png"))

        # linked orders (synthetic test orders, scripts/make_test_orders.py): FRAGO 2 brings its chain into the chat
        if page.locator(".route-chat").count():
            page.click(".route-chat button:has-text('Ordres pris en compte')")
            frago = page.locator(".order-pick label", has_text="FRAGO 2")
            if frago.count():
                frago.locator("input").check()
                page.wait_for_selector(".order-pick :text('ordres liés ajoutés')", timeout=30_000)
                n = page.locator(".route-chat .msg.assistant .meta").count()
                t = time.perf_counter()
                page.locator(".route-chat .chat-input").fill("Quelle vitesse maximale les ordres imposent-ils au convoi ?")
                page.keyboard.press("Enter")
                page.wait_for_function(f"document.querySelectorAll('.route-chat .msg.assistant .meta').length > {n}", timeout=120_000)
                timings["chat with linked orders"] = time.perf_counter() - t
                page.wait_for_timeout(400)
                page.screenshot(path=str(out / "4i_linked_orders_chat.png"), full_page=True)
        page.click("text=4 · Ordres")
        sel = page.locator(".panel.orders select").first
        page.wait_for_selector(".panel.orders select option:has-text('Exercice')", state="attached", timeout=30_000)
        opt = sel.locator("option", has_text="FRAGO 2")
        if opt.count():
            sel.select_option(value=opt.first.get_attribute("value"))
            page.wait_for_selector(".links-card", timeout=120_000)
            page.wait_for_timeout(600)
            page.screenshot(path=str(out / "4j_order_links.png"))

        # TIFF + OSM, OSM filling the gaps, uncertain roads avoided: both comparisons must come back
        page.click("text=1 · Configurer")
        page.click("button.choice:has-text('TIFF + OSM')")
        page.click("text=Compléter le réseau")
        page.locator("label.check", has_text="Éviter les routes incertaines").locator("input").check()
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "4b_config_osm.png"))
        t = time.perf_counter()
        page.click("button.big:has-text('Calculer')")
        page.wait_for_function("document.querySelectorAll('.card.ref').length === 2 && document.querySelectorAll('.card.ref .small.muted').length === 0", timeout=180_000)
        timings["route, OSM fill + uncertain avoided, 2 comparisons"] = time.perf_counter() - t
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "4c_route_osm_fill.png"))

        page.click("button.tool.t-rect")
        box = page.locator(".map-wrap").bounding_box()
        x0, y0 = box["x"] + box["width"] * 0.45, box["y"] + box["height"] * 0.40
        page.mouse.move(x0, y0)
        page.mouse.down()
        page.mouse.move(x0 + 160, y0 + 110, steps=8)
        page.mouse.up()
        page.wait_for_selector(".rect-menu")
        t = time.perf_counter()
        page.click("text=Voir l’extraction du réseau")
        page.wait_for_selector(".player", timeout=120_000)
        timings["extraction"] = time.perf_counter() - t
        for i, wait in enumerate((600, 1900, 1900, 1900, 3200), start=1):
            page.wait_for_timeout(wait)
            page.screenshot(path=str(out / f"5_extraction_stage{i}.png"))
        page.click("text=3 · Résultats")
        page.wait_for_timeout(500)
        page.screenshot(path=str(out / "6_results.png"))

        # several convoys: the PDF exercises as convoys, deconflicted
        page.click("text=1 · Configurer")
        page.click("button.choice:has-text('TIFF sans OSM')")
        page.click("text=2 · Scénario")
        page.click("button:has-text('Plusieurs convois')")
        page.click("button:has-text('Exercices du PDF → convois')")
        n = page.locator(".pts li:has(.it-sw)").count()
        for _ in range(max(0, n - 8)):  # 8 convoys
            page.locator(".pts li:has(.it-sw) button.danger").last.click()
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "7_convoys_setup.png"))
        t = time.perf_counter()
        page.click("button.big:has-text('Planifier')")
        page.wait_for_selector(".kpi:has-text('Planifiés')", timeout=180_000)
        timings["8 convoys, deconflicted"] = time.perf_counter() - t
        page.wait_for_timeout(1200)
        page.screenshot(path=str(out / "8_convoys.png"))
        # save the scenario (a new file every time)
        page.click("text=2 · Scénario")
        page.click("summary:has-text('Scénarios enregistrés')")
        page.locator("details.saved input").fill("smoke convoys")
        page.click("details.saved button:has-text('Enregistrer')")
        page.wait_for_selector("text=Enregistré", timeout=10_000)
        b.close()
    print("timings (s):", {k: round(v, 2) for k, v in timings.items()})
    print(f"screenshots in {out}")
    if errors:
        print("ERRORS:\n" + "\n".join(errors))
        sys.exit(1)
    print("no JavaScript error")


if __name__ == "__main__":
    main()
