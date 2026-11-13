import type { Feature, FeatureCollection, GeoJsonProperties, Geometry } from 'geojson';

export type SourceType = 'tiff' | 'shapefile' | 'osm' | 'geojson';

export type SourceRole =
  | 'fond_tiff'          // TIFF servant de fond cartographique
  | 'reseau_importe'     // réseau routier importé (BD TOPO, OSM, …)
  | 'reseau_candidat'    // réseau candidat extrait d'un TIFF (extraction non disponible)
  | 'resultat';          // résultats de calcul lus dans des fichiers existants

export interface TiffImage {
  url: string; // dataURL rendu côté client
  /** [ouest, nord], [est, nord], [est, sud], [ouest, sud] — coordonnées image de MapLibre */
  coordinates: [number, number][];
  width: number;
  height: number;
  crsNote: string;
}

export interface DataSource {
  id: string;
  name: string;
  type: SourceType;
  role: SourceRole;
  /** Chemin servi en lecture seule sous /data/… (fichiers du dépôt), ou objectURL pour un import. */
  path?: string;
  /** Shapefile : chemin du .dbf accompagnon. */
  dbfPath?: string;
  /** Shapefile importé en .zip (objectURL). */
  zipPath?: string;
  /** objectURL du fichier importé (session navigateur). */
  url?: string;
  origin: 'depot' | 'import';
  visible: boolean;
  loaded: boolean;
  loading: boolean;
  error?: string;
  featureCount?: number;
  /** 1 = tout afficher ; N = 1 entité affichée sur N (affichage seul, données intactes). */
  sample: number;
  color: string;
  crsNote?: string;
  /** Données chargées en mémoire (non persistées). */
  geojson?: FeatureCollection;
  image?: TiffImage;
}

export interface ScenarioPoint {
  id: string;
  name: string;      // ex. PD5, WP1, PA5 — saisi librement par l'utilisateur
  lon?: number;
  lat?: number;
}

export interface ScenarioZone {
  id: string;
  name: string;
  kind: 'deployment' | 'restricted'; // DA = aire de déploiement, RA = zone à éviter
  coords: [number, number][];
}

export interface ScenarioImage {
  id: string;
  name: string;
  /** /data/… (dépôt) ou objectURL (upload, session uniquement). */
  url: string;
  origin: 'depot' | 'upload';
}

export interface Scenario {
  /** Consigne / ordre écrit librement par l'utilisateur, affiché tel quel, jamais interprété. */
  text: string;
  points: ScenarioPoint[]; // ordre choisi par l'utilisateur
  zones: ScenarioZone[];
  images: ScenarioImage[];
}

export interface Project {
  id: string;
  name: string;
  description: string;
  createdAt: string;
  sources: DataSource[];
  scenario: Scenario;
  /** Dossiers de résultats du dépôt (sous /data/…) : audit.json, comparaison.csv, etc. */
  resultPaths: string[];
}

export interface InspectorSelection {
  sourceId: string;
  sourceName: string;
  role: SourceRole;
  geometryType: string;
  properties: GeoJsonProperties;
}

export type DrawMode = 'point' | 'zone-deployment' | 'zone-restricted' | null;

export type { Feature, FeatureCollection, Geometry };
