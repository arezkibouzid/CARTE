import type { SourceRole, SourceType } from './types';

/** Valeur affichée pour toute donnée absente (règle validée : jamais inventée). */
export const NON_RENSEIGNE = 'Non renseigné';
/** Libellé des traitements futurs : l'interface ne lance aucun calcul. */
export const NON_DISPONIBLE = 'Non disponible';

export const ROLE_LABELS: Record<SourceRole, string> = {
  fond_tiff: 'Fond cartographique (TIFF)',
  reseau_importe: 'Réseau routier importé',
  reseau_candidat: 'Réseau candidat extrait d’un TIFF',
  resultat: 'Résultat de calcul',
};

export const ROLE_COLORS: Record<SourceRole, string> = {
  fond_tiff: '#9aa0a6',
  reseau_importe: '#1f6feb',
  reseau_candidat: '#8250df',
  resultat: '#e8a020',
};

export const TYPE_LABELS: Record<SourceType, string> = {
  tiff: 'TIFF / GeoTIFF',
  shapefile: 'Shapefile',
  osm: 'OSM (shapefile)',
  geojson: 'GeoJSON',
};

/** Rôles autorisés selon le type de fichier importé. Un TIFF n'est jamais un réseau importé. */
export const ALLOWED_ROLES: Record<SourceType, SourceRole[]> = {
  tiff: ['fond_tiff', 'reseau_candidat'],
  shapefile: ['reseau_importe', 'resultat'],
  osm: ['reseau_importe', 'resultat'],
  geojson: ['reseau_importe', 'reseau_candidat', 'resultat', 'fond_tiff'],
};

export const SCENARIO_COLORS = {
  point: '#d1495b',
  depart: '#1a7f37',
  arrivee: '#bf3b2f',
  zoneDeployment: 'rgba(26, 127, 55, 0.25)',
  zoneDeploymentLine: '#1a7f37',
  zoneRestricted: 'rgba(191, 59, 47, 0.25)',
  zoneRestrictedLine: '#bf3b2f',
  raccordement: '#8250df',
};
