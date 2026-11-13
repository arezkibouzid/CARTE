# Interface AMIAD

Interface web de visualisation/édition de scénarios, en **lecture seule** sur le
dépôt parent : aucun calcul, aucun entraînement, aucune écriture hors de
`interface/` (les projets utilisateur vivent dans le localStorage du navigateur).

## Lancer

```bash
cd interface
npm install        # si node_modules absent
npm run dev        # http://localhost:5173/
```

Le serveur Vite sert la racine du dépôt sous `/data/` en **GET/HEAD uniquement**
(middleware `repoDataPlugin` dans `vite.config.ts`) : traversées `..` → 403,
fichier inconnu → 404, méthodes en écriture non servies.

## Build / vérifications

```bash
npm run typecheck                 # tsc --noEmit
npm run build                     # tsc + vite build → dist/
node scripts/check-demo-paths.mjs # sonde les chemins /data/ des projets démo (serveur dev requis)
node scripts/smoke-format.mjs     # tests des fonctions pures (formatage, rôles)
node scripts/smoke-data.mjs       # lit les vrais TIFF/shapefiles du dépôt avec geotiff/shpjs (emprise, entités)
```

## Règles d'intégrité (validées avec l'utilisateur)

- un TIFF importé est un **fond cartographique**, jamais un réseau routier ;
- extraction TIFF→candidat, vectorisation, entraînement, calcul d'itinéraires,
  contrôles automatiques, OSM en ligne, géocodage/OCR = boutons **« Non disponible »** ;
- toute valeur absente s'affiche **« Non renseigné »** ;
- les verdicts affichés sont lus dans des fichiers existants du dépôt, jamais recalculés ;
- l'espace Scénario est en saisie libre (texte + points ordonnés + zones + images) ;
  l'application n'interprète rien.

## Structure

- 6 espaces : Projets, Données, Scénario, Carte+couches, Inspecteur, Résultats
- 4 rôles de couches : fond TIFF / réseau importé / réseau candidat extrait / résultat
- `src/data/demoProjects.ts` : 2 projets démo câblés sur `hackathon/` +
  `output/hackathon_*` et `amiad-files/` + `output/route_statement_v4/`
- Voir `DEVELOPPEMENT.md` pour le journal de développement.
