import type { FeatureCollection } from 'geojson';

export type LonLat = [number, number];
export type BBox = [number, number, number, number]; // ouest, sud, est, nord

export interface ModelInfo {
  id: string;
  label: string;
  description?: string;
}

export interface SheetInfo {
  id: string;
  label: string;
  bounds: BBox | null;
  has_bdtopo: boolean;
  has_osm: boolean;
  has_pdf: boolean;
  models: ModelInfo[];
  default_model: string;
}

export interface BackendStatus {
  ready: boolean;
  steps: Record<string, string>;
}

/** Outils de la barre d'outils de la carte. */
export type ToolId = 'pan' | 'start' | 'end' | 'waypoint' | 'rect' | 'area' | 'avoid';

export interface Scenario {
  label?: string; // ex. « Exercice 7 » quand chargé depuis le PDF
  start?: LonLat;
  end?: LonLat;
  waypoints: LonLat[];
  waypointOrder: 'fixed' | 'free';
  roundTrip: boolean;
  area?: LonLat[]; // zone de déploiement (DA)
  avoid: LonLat[][]; // zones à éviter (RA)
  requirements?: string[];
}

export type DataPreset = 'tiff' | 'tiff_osm' | 'tiff_no_osm' | 'custom';

export interface Options {
  model: string;
  objective: 'length' | 'time';
  speedCap: number;
  useBdtopo: boolean;
  useOsm: boolean;
  osmMode: 'reference' | 'fill';
  tau: number; // seuil d'incertitude (probabilité calibrée)
  penalize: boolean;
  strength: number;
  traceThreshold: number;
}

export interface LayerState {
  scan: boolean;
  prob: boolean;
  uncert: boolean;
  network: boolean;
  bdtopo: boolean;
  osm: boolean;
  refs: boolean;
}

export interface Leg {
  name: string;
  length_km: number;
  duration_min: number;
  mean_speed_kmh: number;
  km_built_up: number;
  km_below_prob_threshold?: number;
  mean_prob?: number;
  inside_area: boolean;
  continuous: boolean;
  km_by_speed_limit: Record<string, number>;
  doubtful_sections?: { count: number; km: number };
}

export interface RouteResult {
  route_id: string;
  route: FeatureCollection;
  total: { length_km: number; duration_min: number };
  legs: Leg[];
  log: string[];
  points: Record<string, { snap_m: number; inside_area: boolean }>;
  visit_order?: string[];
  objective: 'length' | 'time';
  network: string;
  probability_penalty: { threshold: number; strength: number; sections_priced: number; km_below_threshold: number } | null;
  compare: ('bdtopo' | 'osm')[];
  timing_s: Record<string, number>;
}

export interface CompareResult {
  which: 'bdtopo' | 'osm';
  error?: string;
  total?: { length_km: number; duration_min: number };
  route?: FeatureCollection;
  length_gap?: number;
  time_gap?: number;
  overlap?: number;
  timing_s?: number;
}

export interface ExtractResult {
  bounds: BBox;
  images: Record<'scan' | 'probability' | 'mask' | 'skeleton', string>;
  graph: FeatureCollection;
  nodes: FeatureCollection;
  stats: {
    pixels: number;
    road_pixels: number;
    sections: number;
    km: number;
    junctions: number;
    dead_ends: number;
    pieces: number;
    largest_piece_share: number;
    mean_probability_on_roads: number | null;
    bdtopo?: { bdtopo_km: number; completeness?: number; correctness?: number };
  };
  timing_s: Record<string, number>;
}

export interface PdfExercise {
  number: number;
  points: Record<string, LonLat>;
  area: string[];
  start: string;
  end: string;
  waypoints: string[];
  waypoint_order: 'fixed' | 'free' | 'none';
  objective: 'length' | 'time';
  round_trip: boolean;
  speed_cap_kmh: number;
  requirements: string[];
  description: string;
  notes: string[];
}

export interface PdfResult {
  metadata: Record<string, string | number>;
  exercises: PdfExercise[];
}
