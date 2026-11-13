import type { Project } from '../types';

/**
 * Projets de démonstration, câblés en lecture seule sur les fichiers du dépôt.
 * Aucun fichier n'est modifié ; les chemins sont servis sous /data/.
 */
export function buildDemoProjects(): Project[] {
  const now = new Date().toISOString();
  return [
    {
      id: 'demo-hackathon',
      name: 'Hackathon AMIAD — données fournies',
      description:
        'Démo en lecture seule : fonds TIFF, réseaux BD TOPO et OSM fournis, résultats des exercices 1–15 (output/hackathon_*).',
      createdAt: now,
      resultPaths: [
        '/data/output/hackathon_01_10_v4',
        '/data/output/hackathon_11_15_donnees_fournies_v1',
      ],
      sources: [
        {
          id: 'demo-hk-tiff250k', name: 'Hackathon_250K.tif', type: 'tiff', role: 'fond_tiff',
          path: '/data/hackathon/Hackathon_250K.tif', origin: 'depot',
          visible: true, loaded: false, loading: false, sample: 1, color: '#9aa0a6',
        },
        {
          id: 'demo-hk-tiff100k', name: 'Hackathon_100K.tif', type: 'tiff', role: 'fond_tiff',
          path: '/data/hackathon/Hackathon_100K.tif', origin: 'depot',
          visible: false, loaded: false, loading: false, sample: 1, color: '#9aa0a6',
        },
        {
          id: 'demo-hk-bdtopo', name: 'Hackathon_BDTopo.shp', type: 'shapefile', role: 'reseau_importe',
          path: '/data/hackathon/Hackathon_BDTopo.shp', dbfPath: '/data/hackathon/Hackathon_BDTopo.dbf', origin: 'depot',
          visible: true, loaded: false, loading: false, sample: 20, color: '#1f6feb',
          crsNote: 'CRS non déclaré (.prj absent) ; interprété EPSG:4326 comme le TIFF fourni (cf. inventaire_sources.json).',
        },
        {
          id: 'demo-hk-osm', name: 'Hackathon_OSM.shp', type: 'osm', role: 'reseau_importe',
          path: '/data/hackathon/Hackathon_OSM.shp', dbfPath: '/data/hackathon/Hackathon_OSM.dbf', origin: 'depot',
          visible: true, loaded: false, loading: false, sample: 5, color: '#2da44e',
          crsNote: 'CRS non déclaré (.prj absent) ; interprété EPSG:4326 comme le TIFF fourni (cf. inventaire_sources.json).',
        },
        {
          id: 'demo-hk-ex01', name: 'exercice_01.geojson (résultat ex. 1)', type: 'geojson', role: 'resultat',
          path: '/data/output/hackathon_01_10_v4/exercice_01.geojson', origin: 'depot',
          visible: true, loaded: false, loading: false, sample: 1, color: '#e8a020',
        },
        {
          id: 'demo-hk-cand1115', name: 'routes_candidates.geojson (résultats ex. 11–15)', type: 'geojson', role: 'resultat',
          path: '/data/output/hackathon_11_15_donnees_fournies_v1/routes_candidates.geojson', origin: 'depot',
          visible: true, loaded: false, loading: false, sample: 1, color: '#e8a020',
        },
        {
          id: 'demo-hk-ouvrages', name: 'ouvrages_a_verifier.geojson', type: 'geojson', role: 'resultat',
          path: '/data/output/hackathon_11_15_donnees_fournies_v1/ouvrages_a_verifier.geojson', origin: 'depot',
          visible: false, loaded: false, loading: false, sample: 1, color: '#e8a020',
        },
      ],
      scenario: {
        text: 'Exercice 11 (consigne fournie) : PD5 → WP1 → WP2 → WP3 → WP4 → PA5, puis retour sans WP ; éviter RA ; plus court. (Texte saisi librement, non interprété par l’interface.)',
        points: [
          { id: 'p-pd5', name: 'PD5', lon: 5.0382, lat: 49.2343 },
          { id: 'p-wp1', name: 'WP1', lon: 5.42517, lat: 49.30678 },
          { id: 'p-wp2', name: 'WP2' },
          { id: 'p-wp3', name: 'WP3' },
          { id: 'p-wp4', name: 'WP4' },
          { id: 'p-pa5', name: 'PA5' },
        ],
        zones: [],
        images: [
          { id: 'img-ex11', name: 'exercice_11.png', url: '/data/output/hackathon_11_15_donnees_fournies_v1/exercice_11.png', origin: 'depot' },
          { id: 'img-ex1', name: 'exercice_01.png', url: '/data/output/hackathon_01_10_v4/exercice_01.png', origin: 'depot' },
        ],
      },
    },
    {
      id: 'demo-scenario1',
      name: 'Scénario 1 — Ploërmel (comparaison V4)',
      description:
        'Second démo en lecture seule : TIFF Scenario1, réseaux BD TOPO/OSM, zones Sud/Nord et comparaison route_statement_v4.',
      createdAt: now,
      resultPaths: ['/data/output/route_statement_v4'],
      sources: [
        {
          id: 'demo-s1-tiff250k', name: 'Scenario1 250K.tif', type: 'tiff', role: 'fond_tiff',
          path: '/data/amiad-files/Scenario1 250K.tif', origin: 'depot',
          visible: true, loaded: false, loading: false, sample: 1, color: '#9aa0a6',
        },
        {
          id: 'demo-s1-bdtopo', name: 'Scenario1_BDTopo.shp', type: 'shapefile', role: 'reseau_importe',
          path: '/data/amiad-files/Scenario1_BDTopo.shp', dbfPath: '/data/amiad-files/Scenario1_BDTopo.dbf', origin: 'depot',
          visible: true, loaded: false, loading: false, sample: 20, color: '#1f6feb',
          crsNote: 'CRS non déclaré (.prj absent) ; emprise cohérente EPSG:4326 (cf. raster.json).',
        },
        {
          id: 'demo-s1-osm', name: 'Scenario1_OSM.shp', type: 'osm', role: 'reseau_importe',
          path: '/data/amiad-files/Scenario1_OSM.shp', dbfPath: '/data/amiad-files/Scenario1_OSM.dbf', origin: 'depot',
          visible: false, loaded: false, loading: false, sample: 5, color: '#2da44e',
          crsNote: 'CRS non déclaré (.prj absent) ; emprise cohérente EPSG:4326 (cf. raster.json).',
        },
        {
          id: 'demo-s1-zones', name: 'zones.geojson (zones Sud/Nord)', type: 'geojson', role: 'resultat',
          path: '/data/output/route_comparison_v1/reference_full/zones.geojson', origin: 'depot',
          visible: true, loaded: false, loading: false, sample: 1, color: '#e8a020',
        },
        {
          id: 'demo-s1-itineraires', name: 'itineraires_comparaison.geojson (V4)', type: 'geojson', role: 'resultat',
          path: '/data/output/route_statement_v4/itineraires_comparaison.geojson', origin: 'depot',
          visible: true, loaded: false, loading: false, sample: 1, color: '#e8a020',
        },
        {
          id: 'demo-s1-courbures', name: 'courbures_informatives.geojson', type: 'geojson', role: 'resultat',
          path: '/data/output/route_statement_v4/courbures_informatives.geojson', origin: 'depot',
          visible: false, loaded: false, loading: false, sample: 10, color: '#e8a020',
        },
      ],
      scenario: {
        text: 'Comparaison commune V4 : trois chemins Sud–Nord, villes > 10 000 hab. évitées, pas d’intersection entre chemins, séparation ≥ 5 km. (Contraintes de l’énoncé, affichées telles quelles.)',
        points: [],
        zones: [],
        images: [
          { id: 'img-v4', name: 'comparaison_controlee.png', url: '/data/output/route_statement_v4/comparaison_controlee.png', origin: 'depot' },
        ],
      },
    },
  ];
}

export const DEMO_IMAGE_SUGGESTIONS = [
  '/data/output/hackathon_01_10_v4/exercice_01.png',
  '/data/output/hackathon_01_10_v4/exercice_05.png',
  '/data/output/hackathon_01_10_v4/exercice_10.png',
  '/data/output/hackathon_11_15_donnees_fournies_v1/exercice_11.png',
  '/data/output/hackathon_11_15_donnees_fournies_v1/exercice_12.png',
  '/data/output/hackathon_11_15_donnees_fournies_v1/exercice_13.png',
  '/data/output/hackathon_11_15_donnees_fournies_v1/exercice_14.png',
  '/data/output/hackathon_11_15_donnees_fournies_v1/exercice_15.png',
  '/data/output/route_statement_v4/comparaison_controlee.png',
  '/data/output/route_statement_v4/revue_routiere.png',
];
