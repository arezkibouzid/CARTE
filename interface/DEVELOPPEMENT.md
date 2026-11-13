# Journal de développement — interface AMIAD

But : permettre de reprendre le travail sur l'interface sans toucher aux
fichiers scientifiques existants. **Aucune donnée, script, poids ou résultat
 des dossiers `amiad-files/`, `tiff_lab/`, `output/`, `hackathon/`,
`exercices/`, `tools/`, `documentation/` n'est modifié.** Tout le travail vit
dans `interface/` (nouveau dossier). L'application lit ces dossiers en
**lecture seule** (servie par un middleware Vite en GET uniquement) et ne
lance **aucun calcul, entraînement ou téléchargement de données**.

Règles d'intégrité de l'interface (validées avec l'utilisateur) :

- un TIFF importé est un **fond cartographique**, jamais un réseau routier ;
- les traitements futurs (extraction TIFF→candidat, vectorisation,
  entraînement, calcul d'itinéraires, contrôles automatiques, OSM en ligne,
  géocodage/OCR de la consigne) sont des boutons **« Non disponible »** ;
- toute valeur absente s'affiche **« Non renseigné »** ;
- aucun résultat ni contrôle affiché s'il n'est pas lu dans un fichier
  existant (les verdicts affichés sont copiés des fichiers, jamais recalculés) ;
- l'espace Scénario laisse l'utilisateur **écrire librement l'ordre**
  (consigne texte + liste ordonnée de points) et **joindre des images**
  (comme dans les exercices fournis) ; l'application n'interprète rien.

## Entrées

- 2026-11-13 — Correction de l’écran blanc au démarrage : `MapSpace`
  accédait à `map.getStyle().layers` avant le chargement du style MapLibre.
  La synchronisation des couches attend désormais l’événement `load`, avec
  nettoyage de l’écouteur au démontage (compatible React StrictMode).
  Erreur reproduite dans Chrome headless avant correction ; après correction,
  les six onglets, les deux projets démo et le canevas de carte sont présents,
  sans exception JavaScript non interceptée. Compilation de production vérifiée.
- 2026-11-12 — Lancement de la construction de l'interface (React + TypeScript
  + Vite + MapLibre, dossier `interface/`). Proposition validée par
  l'utilisateur : 6 espaces (Projets, Données, Scénario, Carte+couches,
  Inspecteur, Résultats), 4 rôles de couches (fond TIFF / réseau importé /
  réseau candidat extrait / résultats), projets démo centrés sur
  `hackathon/` + `output/hackathon_*`, second démo `amiad-files/` +
  `output/route_statement_v4/`. Scénario en saisie libre (texte + ordre +
  images) selon la dernière consigne.
- 2026-11-12 (soir) — Interface développée et compilée (`npm run build` OK) :
  `main.tsx`, `styles.css`, `App.tsx` (6 onglets + état global, projets
  persistés en localStorage), composants `ProjectsSpace`, `DataSpace`
  (charger/décharger, visibilité, échantillon 1/N d'affichage, rôle selon
  `ALLOWED_ROLES`, import de fichiers en session), `ScenarioSpace` (texte
  libre, points ordonnés placés au clic ou en coordonnées, zones DA/RA
  dessinées, images jointes), `MapSpace` (MapLibre : fonds TIFF en raster
  géoréférencé, couches vectorielles, scénario, clic → inspecteur),
  `InspectorSpace` (attributs copiés du fichier, « Non renseigné »),
  `ResultsSpace` (fichiers de résultats du dépôt lus tels quels en HEAD/GET,
  traitements futurs en « Non disponible »). `loadTiffImage` lit par bandes
  de 512 lignes + palette ColorMap (gros TIFF palettés supportés).
  Serveur dev vérifié : page 200, `/data/…` servi en GET/HEAD uniquement,
  traversée `..` → 403, POST → non traité, inexistant → 404.
  URL locale : http://localhost:5173/
- 2026-11-12 (nuit, session auto-approve) — Vérifications et finitions :
  `scripts/check-demo-paths.mjs` sonde en HEAD les 27 chemins fichiers des
  projets démo → 27/27 OK (les `resultPaths` sont des dossiers, exclus du check
  car le middleware ne sert que des fichiers). `scripts/smoke-format.mjs`
  (esbuild) teste les fonctions pures : 10/10 OK (« Non renseigné »,
  « Non disponible », rôles TIFF = fond/candidat uniquement, formatage km).
  `README.md` (lancement, règles, commandes). MapSpace : ajout
  NavigationControl + ScaleControl. `npm run build` OK. Serveur dev toujours
  sur http://localhost:5173/ ; middleware vérifié (403 traversée, 404 inconnu,
  POST non servi).
- 2026-11-12 (nuit, suite) — `scripts/smoke-data.mjs` valide les vrais fichiers
  du dépôt avec les mêmes libs que le client : 4/4 TIFF ont une emprise
  EPSG:4326 lisible (ex. Scenario1 100K : origine -2.8477,48.6660,
  1.0253x1.1900 deg, conforme à raster.json), OSM 106 386 entités et BD TOPO
  188 401 entités avec attributs. **Deux bugs corrigés grâce à ce test** :
  (1) geotiff.js renvoie ModelPixelScale/ModelTiepoint/GeoKeyDirectory en
  objet indexé {"0":…} → `numArray()` dans loaders.ts, sinon l’emprise
  apparaissait « absente » côté client ; (2) shpjs v6 n’a plus `shp.combine`,
  l’entrée est `shp({shp, dbf})` → loadShapefile/loadShapefileGeometryOnly
  corrigés (fallback géométries seules si .dbf illisible). typecheck + build OK.
- 2026-11-13 — **Version 2 : interface de pilotage du pipeline** (demande : configuration, carte interactive,
  extraction visible, outils de dessin, latence minimale). Changement de règle validé : l’interface n’est plus en
  lecture seule, elle **appelle un back end Python** (`backend/amiad_api.py`, FastAPI) qui exécute le pipeline sur le
  cluster (choix : back end ajouté, exécution sur le cluster, OSM en contrôle *ou* en complément au choix de
  l’utilisateur, chemins du cluster `examples/` + `outputs/hackathon100k/`).
  - Back end : tuiles XYZ de la carte, de la probabilité et de l’incertitude (7-40 ms, 3-15 ms en cache, au lieu du
    décodage du TIFF de 83 Mo dans le navigateur) ; couches BD TOPO / OSM / réseau extrait limitées à la zone
    visible (0,01-0,16 s) ; itinéraires avec points, étapes ordonnées ou libres, aller-retour, zone de déploiement,
    zones à éviter, OSM en complément, pénalité d’incertitude (0,2-0,5 s ; contrôles BD TOPO et OSM en parallèle
    ensuite) ; extraction par étapes dans un rectangle (1 s) ; lecture des PDF d’exercices ; préparation au démarrage
    (réseaux, calibration, références).
  - Interface : 3 étapes (Configurer, Scénario, Résultats), barre d’outils de dessin, points déplaçables, menu du
    rectangle, lecteur d’extraction en 5 étapes, tracé progressif des itinéraires, couches et légende, seuil
    d’incertitude en direct. Supprimés : espaces Projets/Données/Inspecteur/Résultats v1, projets démo câblés sur des
    dossiers locaux, lecture TIFF/shapefile dans le navigateur (geotiff, shpjs).
  - Vérifié dans Chromium headless (`scripts/ui_smoke.py`) : exercice 7 (4 étapes, ordre libre) calculé en 0,45 s,
    +0,6 % de distance et 82 % de tracé commun avec la BD TOPO ; mode OSM en complément + routes incertaines évitées
    avec les deux contrôles en 5,8 s ; extraction 1,3 s ; aucune erreur JavaScript.
