# Interface AMIAD

Interface web pour configurer le pipeline, lire la carte, voir l’extraction du réseau pas à pas et calculer des
itinéraires, sans connaissance préalable en vision par ordinateur ni en SIG.

Elle s’appuie sur un **back end Python** (`backend/amiad_api.py`, FastAPI) qui tourne sur le cluster, à côté des
données et des modèles entraînés : tuiles de carte, couches routières de la zone visible, itinéraires, extraction,
lecture des PDF d’exercices. Tout ce qui est lourd est préparé une fois au démarrage (environ 1 minute) puis gardé en
mémoire.

## Lancer (sur le cluster)

```bash
interface/run.sh          # construit l’interface si besoin et démarre le serveur (port 8008)
interface/run.sh stop     # arrête
```

Puis ouvrir **http://localhost:8008** : VS Code redirige le port vers votre poste (sinon onglet « Ports » → 8008).
Prérequis : `.venv` du dépôt (+ `fastapi`, `uvicorn`), Node.js dans `~/.local/node` pour la construction.

Développement de l’interface : `cd interface && npm run dev` (port 5173, `/api` renvoyé vers le port 8008).

## Utilisation

1. **Configurer** — données (TIFF seul / TIFF + OSM / TIFF sans OSM ; OSM en contrôle ou en complément ; BD TOPO en
   contrôle), modèle de lecture de la carte, itinéraire le plus court ou le plus rapide, vitesse du convoi, **seuil
   d’incertitude** et option « éviter les routes incertaines », **coût d’incertitude** probabiliste (intensité λ,
   0,5 par défaut ; actif seulement quand l’itinéraire repose sur le seul TIFF).
2. **Scénario** — un itinéraire, **plusieurs convois** (priorités, déconfliction : aucune / pénaliser / interdire les
   routes déjà prises ; « Exercices du PDF → convois ») ou des itinéraires distincts sous contraintes. Charger un
   exercice du PDF (ou importer un PDF), ou placer départ, étapes (ordre imposé ou libre) et arrivée ; aller-retour ;
   zone de déploiement et zones à éviter. **Scénarios enregistrés** : enregistrer / recharger (jamais écrasés).
3. **Résultats** — distance, durée, confiance du modèle, comparaison avec la BD TOPO et OSM, **« Pourquoi ce tracé ? »**
   (segments, verdicts, contournements rejetés, limites des données), **questions sur l’itinéraire** (réponses tirées de
   la trace du solveur, ou du modèle local avec les ordres choisis, sous budget de contexte), décisions du calcul,
   export GeoJSON.
4. **Ordres** — extraction des ordres d’opération, règles applicables, explications et chatbot.

Détails, mesures et expériences : `docs/technical_overview.md` et `experiments/INDEX.md`.

**Carte** : barre d’outils (naviguer, départ, arrivée, étape, rectangle, zone de déploiement, zone à éviter,
recentrer, tout effacer) ; les points se déplacent à la souris ; clic sur une route = ses informations. Un
**rectangle** ouvre un menu : voir l’extraction animée du réseau (carte → probabilité → masque → lignes centrales →
graphe), en faire une zone de déploiement ou une zone à éviter, zoomer. Couches : carte scannée, probabilité U-Net,
incertitude (rouge = route tracée incertaine, orange = route possible non tracée), réseau extrait (coloré selon le
seuil), BD TOPO, OSM, itinéraires de contrôle.

## Configuration

`backend/sheets.json` : feuilles de carte (TIFF, BD TOPO, OSM, PDF, zones bâties, étiquettes de calibration) et
modèles proposés. Les rasters de probabilité sont **hors pli** sur la feuille Hackathon (aucun bloc prédit par un
modèle qui a vu ses étiquettes). Les réseaux tracés, la calibration et les tuiles sont mis en cache dans
`outputs/interface_cache/`.

## Vérification

```bash
interface/run.sh
export LD_LIBRARY_PATH=$(find ~/.local/chrome-libs -type d -name x86_64-linux-gnu | tr '\n' ':')   # bibliothèques de Chromium
.venv/bin/python interface/scripts/ui_smoke.py   # parcours complet dans Chromium headless, captures dans outputs/interface_cache/ui_smoke
```
